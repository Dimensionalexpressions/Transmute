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
import threading
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
                gpu: bool = False,
                cancel: "threading.Event | None" = None) -> CommandResult:
    """Run one engine invocation. Never raises for engine misbehaviour.

    gpu=True while a governor is present takes a GPU concurrent-session slot.
    cancel: set the Event and this invocation's process tree is killed
    promptly - callers that can be superseded (live estimates!) must pass one
    so superseded work stops burning the user's machine.
    """
    if cancel is not None and cancel.is_set():
        return CommandResult([str(a) for a in argv], -1, "", "cancelled",
                             log_path, 0.0, timed_out=False)
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
            # Windows: start_new_session is a NO-OP there - without an own
            # process group the child sits in OUR console group and
            # _terminate_tree's CTRL_BREAK lands on the whole console
            # (live-fire: GitHub Actions runs 44810470963/44812379371/
            # 44814376186 - pytest and its spawner were terminated mid-run,
            # no faulthandler output, only pwsh survived). NEW_PROCESS_GROUP
            # makes CTRL_BREAK address the child's tree alone.
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if sys.platform == "win32" else 0),
            preexec_fn=_nice_preexec if (not sys.platform == "win32") else None,
        )
    except OSError as exc:
        if slot is not None:
            slot.__exit__(None, None, None)
        return CommandResult(full, -1, "", f"launch failed: {exc}", log_path,
                             time.monotonic() - t0, timed_out=False)
    if reg:
        child_token = reg(proc.pid)  # start_new_session=True -> pgid == pid

    if cancel is not None:
        # supersede-killer: the moment the caller's event flips, this
        # child's whole tree dies (a replaced estimate must never keep
        # the workstation busy)
        def _kill_when_cancelled():
            if cancel.wait(timeout_s):
                _terminate_tree(proc)
        threading.Thread(target=_kill_when_cancelled, daemon=True,
                         name="transmute-cancel").start()
    try:
        stdout, stderr = communicate_bounded(proc, timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(proc)
        stdout, stderr = drain_bounded(proc, 10)
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
    """SIGINT the whole group (let ffmpeg clean its partial file), then SIGKILL.

    Two traps this avoids (both live-fire on Windows and POSIX benches):
    - Never look the group id up at kill time: start_new_session=True pins
      pgid == proc.pid, while os.getpgid() on an exited child can return a
      REUSED pid's group - you signal a stranger (it found and SIGINTed the
      pytest process itself).
    - The direct child exiting does NOT mean the group is dead: POSIX shells
      start background children with SIGINT ignored (sh's `sleep &` survives
      INT), and on Windows GUI launchers' helpers (soffice.exe -> soffice.bin)
      outlive proc.kill() of the direct child. Always escalate: SIGKILL the
      group / taskkill /T /F the tree; a vanished group is just ESRCH.
    """
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # console children, best effort
        except (OSError, ValueError):
            pass
        time.sleep(1.5)
        _win_taskkill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    time.sleep(1.5)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _win_taskkill_tree(pid: int) -> None:
    """Kill pid and every descendant. DEVNULL pipes: taskkill spawns nothing,
    so its own timeout drain can never block."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _pump_to(stream, box: list) -> None:
    """Read a pipe to EOF into box[0]; a concurrent close() is a normal end."""
    try:
        box[0] = stream.read() or ""
    except (OSError, ValueError):  # stream closed on deadline - partial stands
        pass


def _drain_threads(proc: subprocess.Popen) -> tuple[list[threading.Thread], list, list]:
    threads: list[threading.Thread] = []
    boxes: list[list] = []
    streams: list = []
    for stream in (proc.stdout, proc.stderr):
        box = [""]
        boxes.append(box)
        if stream is not None:
            streams.append(stream)
            t = threading.Thread(target=_pump_to, args=(stream, box),
                                 daemon=True, name="transmute-drain")
            t.start()
            threads.append(t)
    return threads, boxes, streams


def _quiet_close(stream) -> None:
    try:
        stream.close()
    except Exception:  # noqa: BLE001 - closing a dead/abandoned pipe is noise
        pass


def _close_streams(streams) -> None:
    """Release our pipe ends WITHOUT ever blocking the caller (both OSes,
    both live-fire: POSIX 120s waits, Windows an eternal hang).

    CPython serialises close() against in-flight operations: a stream with an
    abandoned pump thread parked in read() makes close() wait for that read -
    and a pipe kept open by a survivor grandchild never finishes it. So every
    close runs in an abandoned daemon thread. If it stalls we hold one fd pair
    until the writer dies (the close then completes by itself) or the process
    exits; a hung app is never the trade.
    """
    for stream in streams:
        if stream is None:
            continue
        threading.Thread(target=_quiet_close, args=(stream,),
                         daemon=True, name="transmute-closer").start()


def communicate_bounded(proc: subprocess.Popen, timeout_s: float) -> tuple[str, str]:
    """Popen.communicate(timeout=) whose deadline CPython actually honors.

    CPython's own timeout path is broken on Windows when the pipes outlive the
    direct child (GUI launchers: soffice.exe exits, soffice.bin converts and
    holds the write ends): its reader-thread joins carry no deadline, so the
    TimeoutExpired every caller depends on never fires and the app hangs
    forever (live-fire: the suite itself stalled there). Here the deadline
    covers everything, including the wait for EOF - which stays the true
    completion signal for engine output; overrun raises TimeoutExpired after
    closing our ends so the daemon readers die instead of piling up.
    """
    threads, boxes, streams = _drain_threads(proc)
    endtime = time.monotonic() + timeout_s
    for t in threads:
        t.join(timeout=max(0.0, endtime - time.monotonic()))
    if any(t.is_alive() for t in threads):
        _close_streams(streams)
        raise subprocess.TimeoutExpired(proc.args, timeout_s)
    out = boxes[0][0] if boxes else ""
    err = boxes[1][0] if len(boxes) > 1 else ""
    # all writers are gone; reap the process itself (bounded - process-object
    # wait only, no pipes involved)
    try:
        proc.wait(timeout=max(0.5, endtime - time.monotonic()) + 5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return out, err


def drain_bounded(proc: subprocess.Popen, grace: float) -> tuple[str, str]:
    """Collect remaining output after termination, but NEVER block forever.

    A grandchild that survived the kill can keep the pipes open indefinitely
    (exactly the CPython communicate(timeout=) hole above), so this uses the
    same own-thread bounded drain; on overrun we close our ends and return
    whatever the partials hold - a timed-out result is worth more than a hung
    app.
    """
    threads, boxes, streams = _drain_threads(proc)
    endtime = time.monotonic() + grace
    for t in threads:
        t.join(timeout=max(0.0, endtime - time.monotonic()))
    _close_streams(streams)
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        # direct child only: every LEGITIMATE writer is already tree-killed by
        # the caller; what survives is an orphan grandchild (a parent whose
        # tree link is gone) - a targeted kill here just fires blanks, while
        # this one kills a stubborn direct child still parked behind its own
        # helper and stops it outliving the drain.
        proc.kill()
    except OSError:
        pass
    out = boxes[0][0] if boxes else ""
    err = boxes[1][0] if len(boxes) > 1 else ""
    return out, err
