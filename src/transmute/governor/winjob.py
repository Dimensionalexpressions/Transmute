r"""P6: Windows Job Object enforcement - kernel-enforced CPU hard cap.

This replaces the P1 "Windows governor is stubs" state. A Job Object gives
us what SIGSTOP feedback gives on Linux, but better: the kernel enforces a
CPU cap for every process in the job (JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP),
no signals, no races, and KILL_ON_JOB_CLOSE means a crashed app cannot leak
engine children running forever.

Doctrine in force: EVERY call is measured (BOOL + GetLastError). If the
API rejects anything (old SKU without CPU rate control, group policy, a
parent job forbidding breakaway), WinJob.active turns False with the OS's
own reason recorded, and ResourceGovernor degrades to suspend/resume
feedback (psutil works on Windows) - a degradation with a reason, never a
silent false promise.

Import-safe on any OS: the ctypes structures are pure data; kernel32 is
only touched by CtypesWinJobApi, which raises cleanly off win32. All
call/decision logic lives against an injectable api object and is unit
tested on Linux (tests/test_governor_winjob.py).
"""
from __future__ import annotations

import ctypes
import sys

# --- Win32 constants (winnt.h) -------------------------------------------
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
PROCESS_SET_QUOTA = 0x00000100
PROCESS_TERMINATE = 0x00000020
PROCESS_SET_INFORMATION = 0x00000200
# IO priority without admin (same mechanism Process Lasso uses):
# ntdll!NtSetInformationProcess(ProcessIoPriority=36, IO_PRIORITY_LOW=2).
# Win8.1+; anything older returns an error status which we RECORD, never
# ignore. Storage-QoS per-volume *rates* still need admin - those stay a
# caveat, this is the honest non-admin slice of "IO priority".
PROCESS_IO_PRIORITY_CLASS = 36
IO_PRIORITY_LOW = 2


class WinJobError(RuntimeError):
    """A Win32 call failed; the message carries the OS's own reason."""


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        # Affinity is ULONG_PTR in winjob.h - pointer-sized. c_uint32 made
        # every following field 4 bytes early and sizeof 136 instead of
        # 144, so SetInformationJobObject rejected the struct outright
        # (ERROR_BAD_LENGTH) on 64-bit Windows; c_size_t mirrors ULONG_PTR
        # exactly on both 32- and 64-bit ABIs.
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    # winnt.h IO_COUNTERS: exactly 6 ULONGLONGs - the kernel validates the
    # total sizeof(EXTENDED_LIMIT_INFORMATION), so this layout must match.
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    # union { DWORD CPURate; DWORD Weight; } collapses to one DWORD here;
    # the HARD_CAP semantics we use interpret it as percent * 100.
    _fields_ = [("ControlFlags", ctypes.c_uint32), ("CPURate", ctypes.c_uint32)]


def cpu_rate_for(pct: float) -> int:
    """HARD_CAP CPURate units = percent x 100 (0..10000); clamp, no float dust."""
    return max(100, min(10000, int(round(float(pct) * 100))))


class CtypesWinJobApi:
    """Real kernel32 calls. Only constructible on win32; every failure
    raises WinJobError with GetLastError translated."""

    def __init__(self) -> None:
        if sys.platform != "win32":            # import-safe off Windows
            raise WinJobError(f"Job Objects need win32, running on {sys.platform}")
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._nt = ctypes.WinDLL("ntdll", use_last_error=True)

        k = self._k32
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k.CreateJobObjectW.restype = ctypes.c_void_p
        k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_void_p, ctypes.c_uint32]
        k.SetInformationJobObject.restype = ctypes.c_bool
        k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.AssignProcessToJobObject.restype = ctypes.c_bool
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        k.OpenProcess.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_bool
        n = self._nt
        n.NtSetInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_uint32]
        n.NtSetInformationProcess.restype = ctypes.c_long  # NTSTATUS

    @staticmethod
    def _reason() -> str:
        return ctypes.WinError(ctypes.get_last_error()).strerror or "Win32 error"

    def create_job(self) -> int:
        h = self._k32.CreateJobObjectW(None, None)
        if not h:
            raise WinJobError(f"CreateJobObjectW: {self._reason()}")
        return int(h)

    def set_kill_on_close(self, job: int) -> None:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            ctypes.c_void_p(job), JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise WinJobError(f"SetInformationJobObject(ExtendedLimits): {self._reason()}")

    def set_cpu_hard_cap(self, job: int, pct: float) -> None:
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        info.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        info.CPURate = cpu_rate_for(pct)
        ok = self._k32.SetInformationJobObject(
            ctypes.c_void_p(job), JobObjectCpuRateControlInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise WinJobError(f"SetInformationJobObject(CpuRateControl): {self._reason()}")

    def open_process(self, pid: int) -> int:
        h = self._k32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SET_INFORMATION,
            False, pid)
        if not h:
            raise WinJobError(f"OpenProcess({pid}): {self._reason()}")
        return int(h)

    def assign(self, job: int, process_handle: int) -> None:
        if not self._k32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                                  ctypes.c_void_p(process_handle)):
            raise WinJobError(f"AssignProcessToJobObject: {self._reason()}")

    def set_io_priority_low(self, process_handle: int) -> None:
        prio = ctypes.c_uint32(IO_PRIORITY_LOW)
        st = self._nt.NtSetInformationProcess(
            ctypes.c_void_p(process_handle), PROCESS_IO_PRIORITY_CLASS,
            ctypes.byref(prio), 4)
        if st != 0:  # measured NTSTATUS; 0 == STATUS_SUCCESS
            raise WinJobError(
                f"NtSetInformationProcess(ProcessIoPriority): NTSTATUS "
                f"0x{st & 0xFFFFFFFF:08X}")

    def close(self, handle: int) -> None:
        self._k32.CloseHandle(ctypes.c_void_p(handle))


class WinJob:
    """One shared job for ALL engine children of this process: the hard cap
    then bounds Transmute's TOTAL cpu share (matching the Linux governor's
    semantics - nice/ionice prefix is launch hygiene, the cap is the rule).

    Lifecycle is fully measured: ensure() creates + configures lazily and
    records the exact failure; after any failure every further call is a
    cheap no-op returning False so the governor degrades to suspend/resume
    exactly once, with one caveat line, not a log spam per sample.
    """

    def __init__(self, cpu_cap_pct: float, api=None):
        self.api = api  # None -> real ctypes api on first ensure()
        self._api_init_error: str | None = None
        self.job: int | None = None
        self.active = False
        self.cap_pct = float(cpu_cap_pct)
        self.error: str | None = None
        self.assigned_pids: set[int] = set()
        self.io_low_pids: set[int] = set()
        self.io_error: str | None = None

    def _get_api(self):
        if self.api is not None:
            return self.api
        try:
            self.api = CtypesWinJobApi()
        except (WinJobError, AttributeError, OSError) as exc:
            raise WinJobError(f"kernel32 unavailable: {exc}") from exc
        return self.api

    def ensure(self) -> bool:
        """Create + configure the job. True = kernel enforcement live."""
        if self.active:
            return True
        if self.error is not None:
            return False                     # failed once; stay degraded
        try:
            api = self._get_api()
            self.job = api.create_job()
            api.set_kill_on_close(self.job)
            api.set_cpu_hard_cap(self.job, self.cap_pct)
            self.active = True
        except WinJobError as exc:
            self.error = str(exc)
            if self.job:
                try:
                    api.close(self.job)
                except Exception:  # noqa: BLE001
                    pass
                self.job = None
        return self.active

    def attach_pid(self, pid: int) -> bool:
        """Put a freshly spawned child under the job. False (recorded) ->
        the caller's child keeps running uncapped; the governor's suspend
        fallback still covers it."""
        if not self.ensure():
            return False
        try:
            api = self.api
            ph = api.open_process(pid)
            try:
                api.assign(self.job, ph)
                try:
                    api.set_io_priority_low(ph)  # non-fatal if the OS refuses
                    self.io_low_pids.add(pid)
                except WinJobError as exc:
                    self.io_error = str(exc)
            finally:
                api.close(ph)                # job holds its own reference
            self.assigned_pids.add(pid)
            return True
        except WinJobError as exc:
            self._record(f"attach pid {pid}: {exc}")
            return False

    def set_cap(self, pct: float) -> bool:
        """Feedback adjustment while running (total load high -> squeeze)."""
        pct = max(1.0, min(100.0, float(pct)))
        if not self.active:
            return False
        if abs(pct - self.cap_pct) < 0.5:
            return True                       # avoid pointless kernel calls
        try:
            self.api.set_cpu_hard_cap(self.job, pct)
            self.cap_pct = pct
            return True
        except WinJobError as exc:
            self._record(f"set_cap: {exc}")
            return False

    def _record(self, msg: str) -> None:
        # A per-operation failure does NOT tear enforcement down (other ops
        # may work); it is recorded for diagnostics and reported in status.
        self.error = msg

    def status(self) -> dict:
        return {"active": self.active, "cap_pct": self.cap_pct,
                "assigned": sorted(self.assigned_pids), "error": self.error,
                "io_low": sorted(self.io_low_pids), "io_error": self.io_error}

    def io_state(self) -> str:
        """Human-readable IO-priority state for status()/caveats."""
        if self.io_low_pids:
            base = f"LOW on {len(self.io_low_pids)} child(ren)"
            return base + (f"; refused for later children ({self.io_error})"
                           if self.io_error else "")
        return f"not applied ({self.io_error})" if self.io_error else "no children yet"

    def close(self) -> None:
        if self.job is not None:
            try:
                self.api.close(self.job)      # KILL_ON_JOB_CLOSE reaps strays
            except Exception:  # noqa: BLE001
                pass
            self.job = None
        self.active = False
