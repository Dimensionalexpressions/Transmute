"""F6 ResourceGovernor: conversion must never make the machine unusable.

Linux enforcement (works on any distro, no root, no systemd needed):
  1. launch hygiene: nice +10 and ionice best-effort class (if the binaries
     exist) - other processes win the scheduler,
  2. hard cap via FEEDBACK: a monitor thread samples total CPU% (psutil);
     when it exceeds the cap (default 80%) every tracked engine process
     group is SIGSTOPped; when load drops below cap-minus-margin they are
     SIGCONTinued. This is measured, not assumed - the test suite proves
     <=85% sustained CPU under a full-machine encoder hammer.
  3. GPU sessions capped via semaphore; I/O via token bucket.
Why not systemd-run CPUQuota: requires a user D-Bus/systemd scope which is
not guaranteed present (it is NOT on this bench); SIGSTOP feedback always is.
Windows: interface stubs only in P1 (Job Object CPU rate control is P9) -
documented, no silent false promise.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Iterator

log = logging.getLogger(__name__)

_DEFAULT_CAP = 80.0


class TokenBucket:
    """Classic token bucket for I/O fairness (used by watch mode later)."""

    def __init__(self, rate_per_s: float, burst: float):
        self.rate = float(rate_per_s)
        self.burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                wait = (tokens - self._tokens) / self.rate
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                wait = min(wait, left)
            time.sleep(min(wait, 0.5))


class ResourceGovernor:
    """Cap total machine CPU at cpu_cap_pct while Transmute is converting."""

    def __init__(self, *, cpu_cap_pct: float = _DEFAULT_CAP, resume_margin_pct: float = 10.0,
                 sample_s: float = 0.4, gpu_sessions: int = 2,
                 io_rate_per_s: float = 60.0, io_burst: float = 120.0,
                 enabled: bool | None = None):
        if enabled is None:
            enabled = os.environ.get("TRANSMUTE_GOVERNOR", "1").lower() not in ("0", "off", "no")
        self.enabled = enabled
        self.posix = os.name == "posix"
        self.cpu_cap_pct = float(cpu_cap_pct)
        self.resume_margin_pct = float(resume_margin_pct)
        self.sample_s = float(sample_s)
        self._gpu = threading.Semaphore(max(1, gpu_sessions))
        self._io = TokenBucket(io_rate_per_s, io_burst)
        self._children: dict[int, int] = {}     # pid -> token
        self._stopped: set[int] = set()
        self._child_lock = threading.Lock()
        self._tokens = itertools.count(1)
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[tuple[float, float]] = []  # (ts, cpu_pct) for tests/diagnostics
        self.caveats: list[str] = []
        if not self.posix:
            self.caveats.append("Windows: CPU hard cap (Job Object) is P9 work; P1 Windows "
                                "governor is interface stubs - conversions run at low priority "
                                "only.")
        try:
            import psutil  # noqa: F401
            self._psutil = psutil
        except Exception:  # noqa: BLE001
            self._psutil = None
            self.caveats.append("psutil missing: CPU feedback cap disabled")

    # --------------------------------------------------------- GovernorLike
    def command_prefix(self) -> list[str]:
        if not self.enabled or not self.posix:
            return []
        prefix: list[str] = []
        if shutil.which("nice"):
            prefix += ["nice", "-n", "10"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c", "2"]  # best-effort scheduling class
        return prefix

    def gpu_slot(self):
        return self._gpu if self.enabled else contextlib.nullcontext()

    def io_bucket(self) -> TokenBucket | None:
        return self._io if self.enabled else None

    # --------------------------------------------------------- child tracking
    def register_child(self, pid: int) -> int | None:
        if not self.enabled or not self.posix or self._psutil is None:
            return None
        token = next(self._tokens)
        with self._child_lock:
            self._children[pid] = token
        return token

    def unregister_child(self, token: int) -> None:
        # MUST resume before the caller waits for exit: a stopped group can
        # never terminate - resume first, forget second.
        pid_to_resume = None
        with self._child_lock:
            for pid, tok in list(self._children.items()):
                if tok == token:
                    pid_to_resume = pid
                    del self._children[pid]
                    break
        if pid_to_resume is not None:
            self._stopped.discard(pid_to_resume)
            self._cont(pid_to_resume)

    # --------------------------------------------------------- lifecycle
    def start(self) -> "ResourceGovernor":
        if not (self.enabled and self.posix and self._psutil is not None) or self._thread:
            return self
        self._psutil.cpu_percent(interval=None)  # seed differential measurement
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._monitor, name="transmute-governor",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._child_lock:
            pids = list(self._children)
        for pid in pids:
            self._cont(pid)
        self._stopped.clear()

    __enter__ = start

    def __exit__(self, *exc) -> None:
        self.stop()

    # --------------------------------------------------------- feedback loop
    def _monitor(self) -> None:
        while not self._stop_evt.wait(self.sample_s):
            try:
                cpu = float(self._psutil.cpu_percent(interval=None))
            except Exception:  # noqa: BLE001
                continue
            self.samples.append((time.time(), cpu))
            if len(self.samples) > 100000:
                del self.samples[:50000]
            with self._child_lock:
                pids = list(self._children)
            if cpu >= self.cpu_cap_pct:
                for pid in pids:
                    if pid not in self._stopped:
                        self._stop_child(pid)
            elif cpu <= self.cpu_cap_pct - self.resume_margin_pct:
                for pid in list(self._stopped):
                    if pid in pids:
                        self._cont(pid)
                        self._stopped.discard(pid)

    def _stop_child(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGSTOP)
            self._stopped.add(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _cont(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # --------------------------------------------------------- diagnostics
    def status(self) -> dict:
        return {"enabled": self.enabled, "platform": sys.platform,
                "cpu_cap_pct": self.cpu_cap_pct,
                "feedback_cap_active": bool(self.enabled and self.posix and self._psutil),
                "tracked_children": len(self._children),
                "stopped_children": len(self._stopped),
                "samples": len(self.samples),
                "caveats": list(self.caveats),
                "windows_job_object": "P9 stub" if not self.posix else "n/a"}


def make_governor(**kw) -> ResourceGovernor:
    """Factory; platform dispatch happens inside (Windows = documented stubs)."""
    return ResourceGovernor(**kw)
