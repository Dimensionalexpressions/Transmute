r"""P6: Windows Job Object enforcement - logic proven on Linux via a fake
kernel32 api; the real ctypes calls are exercised by tests/test_governor_windows.py
on the Windows CI runner.

What is locked here (all pure decision logic, OS-independent):
  - creation order: job -> KILL_ON_JOB_CLOSE -> CPU hard cap,
  - CPURate units (percent x 100),
  - attach = OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE) -> assign ->
    CloseHandle,
  - measured degradation: an API failure records the OS reason, turns the
    job inactive and STAYS degraded (no per-sample log spam), while the
    governor falls back to psutil suspend/resume,
  - governor feedback: hot total load squeezes the kernel cap, cool load
    restores it; suspend/resume path only when the job is not active.
"""
from __future__ import annotations

import sys

import psutil  # noqa: F401  MUST load with the REAL sys.platform: it picks
# its implementation at import time; loading it later while a test has
# patched sys.platform='win32' would raise (no _winapi on Linux) and the
# governor would honestly report "psutil missing".
import pytest

from transmute.governor import ResourceGovernor
from transmute.governor import winjob as W

pytestmark = pytest.mark.unit


class FakeApi:
    """Records every call; any named op can be made to raise WinJobError."""

    def __init__(self, fail=()):
        self.calls: list[str] = []
        self.fail = set(fail)
        self._h = 100

    def _op(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise W.WinJobError(f"{name}: denied by policy")

    def create_job(self):
        self._op("create_job")
        self._h += 1
        return self._h

    def set_kill_on_close(self, job):
        self._op(f"kill_on_close:{job}")

    def set_cpu_hard_cap(self, job, pct):
        self._op(f"cap:{job}:{pct}")

    def open_process(self, pid):
        self._op(f"open:{pid}")
        return 9000 + pid

    def assign(self, job, process_handle):
        self._op(f"assign:{job}:{process_handle}")

    def set_io_priority_low(self, process_handle):
        self._op(f"io_low:{process_handle}")

    def close(self, handle):
        self._op(f"close:{handle}")


class RefusingIoApi(FakeApi):
    """Fake ntdll refusal with the REAL error text shape (measured NTSTATUS;
    0xC0000004 = STATUS_ENTRYPOINT_NOT_FOUND, pre-8.1 behaviour)."""

    def set_io_priority_low(self, process_handle):
        self.calls.append(f"io_low:{process_handle}")
        raise W.WinJobError(
            "NtSetInformationProcess(ProcessIoPriority): NTSTATUS 0xC0000004")


# ----------------------------------------------------------------- WinJob

def test_ensure_configures_kill_on_close_then_hard_cap():
    api = FakeApi()
    job = W.WinJob(80.0, api=api)
    assert job.ensure() is True
    assert job.active
    assert api.calls == ["create_job",
                         f"kill_on_close:{job.job}",
                         f"cap:{job.job}:80.0"]


def test_ensure_is_idempotent():
    api = FakeApi()
    job = W.WinJob(80.0, api=api)
    job.ensure()
    n = len(api.calls)
    assert job.ensure() and len(api.calls) == n


def test_cpu_rate_units_percent_x100_clamped():
    assert W.cpu_rate_for(80) == 8000
    assert W.cpu_rate_for(100) == 10000
    assert W.cpu_rate_for(0.01) == 100        # floor: 1% - never zero
    assert W.cpu_rate_for(150) == 10000


def test_attach_open_process_assigns_and_closes_handle():
    api = FakeApi()
    job = W.WinJob(80.0, api=api)
    assert job.attach_pid(4321) is True
    assert job.assigned_pids == {4321}
    jobh = job.job
    assert "open:4321" in api.calls
    assert f"assign:{jobh}:13321" in api.calls   # handle 9000+pid from fake
    assert "io_low:13321" in api.calls           # IO_PRIORITY_LOW on same handle
    assert api.calls.index("io_low:13321") < api.calls.index("close:13321")
    assert "close:13321" in api.calls            # our open handle closed
    assert f"close:{jobh}" not in api.calls      # job handle stays open
    assert job.io_low_pids == {4321} and job.io_error is None


def test_io_priority_refusal_is_measured_not_fatal():
    # An old Windows SKU or a policy can refuse ProcessIoPriority. The child
    # is still under the job (CPU cap lives on); the refusal is recorded with
    # the OS status and surfaces as a caveat, never swallowed.
    api = RefusingIoApi()
    job = W.WinJob(80.0, api=api)
    assert job.attach_pid(4321) is True
    assert job.assigned_pids == {4321}
    assert job.io_low_pids == set()
    assert "ProcessIoPriority" in (job.io_error or "")
    assert "not applied" in job.io_state()


def test_governor_records_io_refusal_as_caveat(win_gov):
    api = RefusingIoApi()
    g = _win_governor(api)
    g.register_child(502)
    assert g.status()["windows_job_object"].startswith("active")
    assert any("IO priority not applied" in c and "ProcessIoPriority" in c
               for c in g.caveats)
    g.stop()


def test_api_failure_degrades_once_with_reason_and_stays_degraded():
    api = FakeApi(fail={"create_job"})
    job = W.WinJob(80.0, api=api)
    assert job.ensure() is False
    assert job.active is False
    assert "denied by policy" in (job.error or "")
    n = len(api.calls)
    assert job.ensure() is False
    assert len(api.calls) == n                   # no retry spam per sample


def test_attach_failure_does_not_teardown_enforcement():
    api = FakeApi(fail={"open:1"})
    job = W.WinJob(80.0, api=api)
    job.ensure()
    assert job.attach_pid(1) is False            # pid 1 open fails
    assert "attach pid 1" in (job.error or "")
    assert job.active is True                    # enforcement lives on
    assert job.attach_pid(2) is True             # other children unaffected


def test_set_cap_squeezes_and_restores_with_deadband():
    api = FakeApi()
    job = W.WinJob(80.0, api=api)
    job.ensure()
    n = len(api.calls)
    assert job.set_cap(80.0) is True
    assert len(api.calls) == n                   # deadband: no kernel churn
    assert job.set_cap(70.0) is True
    assert job.cap_pct == 70.0
    assert job.set_cap(150) is True              # clamped to 100
    assert job.cap_pct == 100.0
    assert job.set_cap(0.0) is True
    assert job.cap_pct == 1.0                    # floor 1%, never a freeze


def test_close_closes_job_handle():
    api = FakeApi()
    job = W.WinJob(80.0, api=api)
    job.ensure()
    jobh = job.job
    job.close()
    assert f"close:{jobh}" in api.calls
    assert not job.active


# ------------------------------------------------- governor on "Windows"

@pytest.fixture()
def win_gov(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    return None


def _win_governor(api):
    # sys.platform patched by fixture BEFORE construction (flag read in init)
    return ResourceGovernor(win_job_api=api)


def test_windows_governor_uses_job_object_and_is_testable_here(win_gov):
    api = FakeApi()
    g = _win_governor(api)
    assert g.win is True and g.posix is False and g.enforceable
    assert any("Storage QoS" in c for c in g.caveats)   # honest IO caveat
    tok = g.register_child(501)
    assert tok is not None
    assert 501 in g._job.assigned_pids
    # hot machine: kernel cap squeezed below target
    g._react(95.0)
    assert g._job.cap_pct <= g.cpu_cap_pct - g.resume_margin_pct
    # cool machine: cap restored
    g._react(30.0)
    assert g._job.cap_pct == g.cpu_cap_pct
    st = g.status()
    assert "kernel hard cap" in st["windows_job_object"]
    assert "IO priority: LOW on 1 child(ren)" in st["windows_job_object"]
    jobh = g._job.job
    g.stop()
    assert f"close:{jobh}" in api.calls   # KILL_ON_JOB_CLOSE safety net on exit


def test_windows_governor_attach_refusal_is_reported_while_active(win_gov):
    # Measured on a runner whose session tree already lives in a Job that
    # forbids nesting: AssignProcessToJobObject -> "Access is denied".
    # The job stays active for whoever CAN attach, but the refusal must be
    # loud: status() names it, a caveat explains the uncapped fallback.
    api = FakeApi(fail={"open:1"})
    g = _win_governor(api)
    assert g.register_child(1) is not None       # tracked for suspend/resume
    wjo = g.status()["windows_job_object"]
    assert wjo.startswith("active")
    assert "ATTACH REFUSED for 1 child(ren)" in wjo
    assert "denied by policy" in wjo
    assert any("attach refused" in c and "uncapped" in c for c in g.caveats)
    assert g._job.status()["attach_refused"] == 1
    # a later child that CAN attach is capped normally; refusal count stands
    assert g.register_child(2) is not None
    assert 2 in g._job.assigned_pids
    assert "ATTACH REFUSED for 1 child(ren)" in g.status()["windows_job_object"]
    g.stop()


def test_windows_governor_falls_back_to_suspend_resume(win_gov, monkeypatch):
    api = FakeApi(fail={"create_job"})
    g = _win_governor(api)
    susp: list[str] = []

    class FakeProc:
        def __init__(self, pid): self.pid = pid
        def suspend(self): susp.append(f"s{self.pid}")
        def resume(self): susp.append(f"r{self.pid}")

    monkeypatch.setattr(g._psutil, "Process", FakeProc)
    g.register_child(777)
    g._react(95.0)          # job inactive -> SIGSTOP-free fallback suspends
    assert "s777" in susp
    g._react(30.0)
    assert "r777" in susp
    assert "suspend/resume feedback" in g.status()["windows_job_object"]
    assert any("Job Object cap unavailable" in c for c in g.caveats)
    g.stop()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="asserts the Linux host (posix enforcement path); "
                           "the Linux CI job owns this guarantee")
def test_linux_governor_unchanged_by_p6():
    g = ResourceGovernor()
    assert g.win is False and g.posix is True and g.enforceable
    assert g._job is None
    assert g.status()["windows_job_object"] == "n/a"
    assert not any("Job Object" in c for c in g.caveats)


def test_struct_layout_matches_winnt_h():
    """SetInformationJobObject validates nLength against the REAL struct:
    a 4-byte Affinity (ULONG_PTR is pointer-sized) made sizeof 136 != 144
    and the kernel rejected every call - silently, because ensure() only
    records the error and stays degraded. This test pins the ABI and runs
    on any machine, Windows not required."""
    import ctypes
    from transmute.governor.winjob import (
        JOBOBJECT_BASIC_LIMIT_INFORMATION as B,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION as E,
    )
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        import pytest
        pytest.skip("64-bit layout asserted only")
    assert ctypes.sizeof(B) == 64
    assert B.Affinity.offset == 48
    assert ctypes.sizeof(E) == 144
