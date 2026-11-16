"""P6 gate: Windows governor LOAD test - runs ONLY on a real win32 runner
(the windows-latest CI job executes this file explicitly, `pytest -q
tests/test_governor_windows.py`). Nothing here is mocked: real Job Object,
real busy-loop children, real measured CPU.

On the Linux bench these tests collect as one skip (platform), which is the
honest state until CI runs; the decision logic is unit-proven in
tests/test_governor_winjob.py against a fake kernel32/ntdll.
"""
from __future__ import annotations

import sys
import subprocess
import time

import psutil
import pytest

from transmute.governor import make_governor

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32",
                       reason="Windows Job Object enforcement; runs on the windows CI runner"),
]

HAMMER = "while True:\n    pass\n"


def _spawn_hammers(n: int) -> list[subprocess.Popen]:
    return [subprocess.Popen([sys.executable, "-c", HAMMER]) for _ in range(n)]


def test_job_object_cap_holds_under_load():
    n = max(4, psutil.cpu_count() or 4)
    gov = make_governor(cpu_cap_pct=50.0, sample_s=0.3)
    gov.start()
    procs: list[subprocess.Popen] = []
    try:
        procs = _spawn_hammers(n)
        for p in procs:
            gov.register_child(p.pid)
        st = gov.status()
        assert st["windows_job_object"].startswith("active"), st["windows_job_object"]
        # attach + IO priority are reported as measured facts, not promises
        assert f"{n} child(ren) assigned" in st["windows_job_object"]
        assert "IO priority:" in st["windows_job_object"]
        time.sleep(2.0)  # let the kernel cap settle
        samples = [psutil.cpu_percent(interval=0.5) for _ in range(8)]
        mean = sum(samples) / len(samples)
        # children are ~the only load on a clean runner; the kernel cap MUST
        # keep total below 50% + scheduler slack. 75 = cap + 25 margin, the
        # same sawtooth tolerance the Linux e2e locks (mean, not peak).
        assert mean <= 75.0, f"job cap not enforced: mean {mean:.1f}% of total CPU"
    finally:
        gov.stop()   # closes the job handle
        for p in procs:
            try:
                # KILL_ON_JOB_CLOSE is the contract: closing the job reaps
                # every assigned child - no orphan encoder survives us.
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                pytest.fail("child outlived job close: KILL_ON_JOB_CLOSE broken")
        for p in procs:
            assert p.poll() is not None, "engine child leaked past job close"


def test_io_priority_state_reported_not_silently_ignored():
    gov = make_governor(cpu_cap_pct=50.0)
    gov.start()
    try:
        p = _spawn_hammers(1)[0]
        gov.register_child(p.pid)
        wjo = gov.status()["windows_job_object"]
        # Either applied (LOW) or refused WITH the OS reason - never absent.
        assert ("IO priority: LOW on 1 child(ren)" in wjo
                or "IO priority: not applied" in wjo), wjo
    finally:
        gov.stop()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
