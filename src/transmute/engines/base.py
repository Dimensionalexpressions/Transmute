"""Engine base: one disciplined way to run external engine processes.

Contract every engine obeys:
- Never leak an exception across the convert()/verify() boundary - failures
  come back as EngineResult(outcome="failed", reason=...).
- Full engine stderr goes to a per-invocation capture file (F13).
- Timeout kills the ENTIRE process group (ffmpeg spawns threads/helpers);
  SIGINT first so ffmpeg discards its partial file cleanly, SIGKILL after.
- When a ResourceGovernor is supplied, its command prefix (cgroup/systemd-run
  scope, Windows job wrapper later) wraps the launch, and GPU slot / I/O
  bucket accounting goes through it (F6).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .. import winpaths
from ..errors import EngineUnavailable  # noqa: F401  (re-export for engines)
from ..paths import paths  # noqa: F401  (kept for engine default log roots)


@dataclass
class Availability:
    """Result of an engine's availability() - 'unavailable' is a STATE, not an error."""
    ok: bool
    reason: str = ""
    guidance: dict[str, str] | None = None   # per-OS install guidance (F26)
    info: dict | None = None                 # EngineInfo snapshots for diagnostics

    def raise_if_missing(self, engine: str = "engine") -> None:
        if not self.ok:
            raise EngineUnavailable(engine=engine, reason=self.reason,
                                    guidance=self.guidance or {})


class GovernorLike(Protocol):
    def command_prefix(self) -> list[str]: ...
    def gpu_slot(self): ...          # context manager
    def io_bucket(self): ...         # TokenBucket | None


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr_text: str
    log_path: Path | None
    seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class EngineResult:
    """Uniform engine outcome. outcome: converted|kept|skipped|failed."""
    outcome: str
    reason: str | None = None
    output: Path | None = None
    encoder: str | None = None
    seconds: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    log_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("converted", "kept", "skipped")


def _nice_preexec():  # pragma: no cover - child-side
    # NOTE: start_new_session=True already calls setsid() in the child;
    # calling it again here is a classic EPERM (we ARE the group leader).
    try:
        os.nice(10)
    except OSError:
        pass


def run_command(argv: Sequence[str | os.PathLike], *, timeout_s: float,
                log_path: Path | None = None, cwd: Path | None = None,
                env: Mapping[str, str] | None = None,
                governor: GovernorLike | None = None,
                gpu: bool = False) -> CommandResult:
    """Run one engine invocation. Never raises for engine misbehaviour.

    gpu=True while a governor is present takes a GPU concurrent-session slot.
    """
    argv_s = [str(a) for a in argv]
    # P6: on Windows, path arguments over MAX_PATH are rewritten to the
    # \\?\ / \\?\UNC\ extended form (pure string logic; identical no-op on
    # Linux - protected by tests in test_winpaths.py).
    argv_s = winpaths.protect_argv(argv_s)
    full = list(governor.command_prefix()) + argv_s if governor else argv_s
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b"")

    slot = governor.gpu_slot() if (governor and gpu) else None
    if slot is not None:
        slot.__enter__()
    # A governor with child tracking participates in the CPU feedback loop
    # (SIGSTOP/SIGCONT these process groups when the cap is exceeded).
    reg = getattr(governor, "register_child", None)
    unreg = getattr(governor, "unregister_child", None)
    child_token = None
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(
            full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None, env=dict(env) if env else None,
            start_new_session=True,
            preexec_fn=_nice_preexec if (not sys.platform == "win32") else None,
        )
    except OSError as exc:
        if slot is not None:
            slot.__exit__(None, None, None)
        return CommandResult(full, -1, "", f"launch failed: {exc}", log_path,
                             time.monotonic() - t0, timed_out=False)
    if reg:
        child_token = reg(proc.pid)  # start_new_session=True -> pgid == pid

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - belt and braces
            os.killpg(proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
            stdout, stderr = proc.communicate()
    finally:
        if slot is not None:
            slot.__exit__(None, None, None)
        if unreg and child_token is not None:
            unreg(child_token)

    stderr_text = stderr or ""
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(argv_s)}\n[exit {proc.returncode}] "
                         f"[timeout={timed_out}] [{time.monotonic() - t0:.2f}s]\n")
                if stderr_text:
                    fh.write(stderr_text)
        except OSError:
            pass
    return CommandResult(full, proc.returncode, stdout or "", stderr_text,
                         log_path, time.monotonic() - t0, timed_out)


def _terminate_tree(proc: subprocess.Popen) -> None:
    """SIGINT the whole group (let ffmpeg clean its partial file), then SIGKILL."""
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        time.sleep(1.5)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if proc.poll() is None:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
