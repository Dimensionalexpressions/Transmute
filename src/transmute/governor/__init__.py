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
Windows (P6): a Job Object with a kernel-enforced CPU hard cap
(JOB_OBJECT_CPU_RATE_CONTROL HARD_CAP) hosts every engine child, plus
KILL_ON_JOB_CLOSE as the child-leak safety net, plus IO_PRIORITY_LOW per
child (ntdll!NtSetInformationProcess, measured NTSTATUS). The API is tried
and MEASURED; where it fails (old SKU, group policy) the governor degrades
to suspend/resume feedback (psutil works on Windows) with the OS's own
reason in status(). Per-volume IO *rate* limits need admin (Storage QoS) -
not promised, carried as a caveat; the process-level LOW class is the
honest non-admin slice.
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

from .winjob import WinJob

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
                 enabled: bool | None = None, win_job_api=None):
        if enabled is None:
            enabled = os.environ.get("TRANSMUTE_GOVERNOR", "1").lower() not in ("0", "off", "no")
        self.enabled = enabled
        self.win = sys.platform == "win32"
        self.posix = os.name == "posix" and not self.win
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
        # P6: on Windows every engine child lives in one shared Job Object
        # (kernel-enforced CPU hard cap + kill-on-close). win_job_api exists
        # for the Linux unit tests; production passes None -> real ctypes.
        self._job = WinJob(self.cpu_cap_pct, api=win_job_api) if self.win else None
        # Enforcement needs a mechanism: SIGSTOP groups (posix) or Job
        # Object/suspend-resume (win). Tests inject win=True + a fake job api
        # to exercise the whole Windows path on Linux.
        self.enforceable = self.posix or self.win
        if self.win:
            self.caveats.append(
                "Windows: per-volume IO rate limits need admin (Storage QoS) - not "
                "applied; engine children run at LOW IO priority (non-admin, measured).")
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
    # NOTE: Windows IO priority (IO_PRIORITY_LOW via ntdll, measured) is
    # applied inside WinJob.attach_pid at register_child time - the single
    # integration point where a child handle is already open. On Linux the
    # equivalent is the `ionice -c 2` launch prefix above.

    # --------------------------------------------------------- child tracking
    def register_child(self, pid: int) -> int | None:
        if not self.enabled or not self.enforceable or self._psutil is None:
            return None
        token = next(self._tokens)
        with self._child_lock:
            self._children[pid] = token
        # Win: put the child under the Job Object NOW. A failed attach does
        # NOT unregister it - suspend/resume feedback still covers it.
        if self._job is not None:
            if not self._job.attach_pid(pid):
                self._note_degraded()
            elif self._job.io_error:
                self._note_io_refused()
        return token

    def _note_degraded(self) -> None:
        if self._job is None:
            return
        if not self._job.active:
            note = (f"Windows Job Object cap unavailable ({self._job.error}); "
                    "using suspend/resume feedback instead")
        elif self._job.attach_refused:
            # Job exists but the OS refused to move children into it (e.g.
            # a container/session job that forbids nesting - measured
            # "Access is denied"). The cap covers 0 of them; say so.
            note = (f"Windows Job Object attach refused "
                    f"({self._job.attach_error}); those children run "
                    "uncapped - suspend/resume feedback still covers them")
        else:
            return
        if note not in self.caveats:
            self.caveats.append(note)

    def _note_io_refused(self) -> None:
        note = (f"Windows IO priority not applied ({self._job.io_error}); "
                "engine children keep the default IO class")
        if note not in self.caveats:
            self.caveats.append(note)

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
        if not (self.enabled and self.enforceable and self._psutil is not None) \
                or self._thread:
            return self
        if self._job is not None and self._job.ensure():
            self._job.error = None  # transient pre-fix; a live job supersedes it
        self._note_degraded()       # records the ONE caveat if the job is dead
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
        if self._job is not None:
            self._job.close()   # KILL_ON_JOB_CLOSE: no engine may outlive us

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
            self._react(cpu)

    def _react(self, cpu: float) -> None:
        """One measured sample -> one enforcement decision. Hysteresis
        (cap / cap-margin) prevents oscillation on either platform."""
        with self._child_lock:
            pids = list(self._children)
        if cpu >= self.cpu_cap_pct:
            if self._job is not None and self._job.active:
                # Kernel cap squeezed below target until total load cools;
                # children keep running, just bounded - strictly better
                # than stop/continue and race-free.
                self._job.set_cap(max(1.0, self.cpu_cap_pct - self.resume_margin_pct))
            else:
                for pid in pids:
                    if pid not in self._stopped:
                        self._stop_child(pid)
        elif cpu <= self.cpu_cap_pct - self.resume_margin_pct:
            if self._job is not None and self._job.active:
                self._job.set_cap(self.cpu_cap_pct)
            for pid in list(self._stopped):
                if pid in pids:
                    self._cont(pid)
                    self._stopped.discard(pid)

    def _stop_child(self, pid: int) -> None:
        if self.win:
            try:  # psutil suspend == TerminateJobObject-free freeze; Windows-safe
                self._psutil.Process(pid).suspend()
                self._stopped.add(pid)
            except Exception:  # noqa: BLE001  child gone/denied: retry next sample
                pass
            return
        try:
            os.killpg(pid, signal.SIGSTOP)
            self._stopped.add(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _cont(self, pid: int) -> None:
        if self.win:
            try:
                self._psutil.Process(pid).resume()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            os.killpg(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # --------------------------------------------------------- diagnostics
    def status(self) -> dict:
        if not self.win:
            wjo = "n/a"
        elif self._job is None:
            wjo = "not initialised"
        elif self._job.active:
            wjo = (f"active: kernel hard cap {self._job.cap_pct:.0f}% of total CPU, "
                   f"{len(self._job.assigned_pids)} child(ren) assigned, kill-on-close; "
                   f"IO priority: {self._job.io_state()}")
            if self._job.attach_refused:
                # "active" + honest coverage: refused children are NOT capped.
                wjo += (f"; ATTACH REFUSED for {self._job.attach_refused} "
                        f"child(ren) ({self._job.attach_error}) - those "
                        "children run uncapped")
        else:
            wjo = f"unavailable ({self._job.error}); suspend/resume feedback in use"
        return {"enabled": self.enabled, "platform": sys.platform,
                "cpu_cap_pct": self.cpu_cap_pct,
                "feedback_cap_active": bool(self.enabled and self.enforceable and self._psutil),
                "tracked_children": len(self._children),
                "stopped_children": len(self._stopped),
                "samples": len(self.samples),
                "caveats": list(self.caveats),
                "windows_job_object": wjo}


def make_governor(**kw) -> ResourceGovernor:
    """Factory; platform dispatch happens inside (Windows: Job Object cap,
    measured, with suspend/resume fallback - see governor/winjob.py)."""
    return ResourceGovernor(**kw)
