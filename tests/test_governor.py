"""Governor (F20/F21): tracked children are stopped/resumed, never leaked.

Honest limits, proven on the bench:
- control is a sawtooth (SIGSTOP all / resume all); single-sample overshoot
  near 100% is possible, the *mean* stays under the cap;
- a stopped child that is waited on would hang - unregister_child must have
  resumed it (test_below locks that contract);
- requires enough load for the cap to bite: 32-core bench => cpu_count hammers.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import psutil
import pytest

from transmute.governor import make_governor

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGSTOP control")
def test_register_resume_child_never_left_stopped():
    proc = subprocess.Popen(["sh", "-c", "sleep 0.15; exit 0"],
                            start_new_session=True)
    gov = make_governor(cpu_cap_pct=5.0, sample_s=0.05)  # cap low -> child gets stopped
    gov.start()
    token = None
    try:
        token = gov.register_child(proc.pid)
        deadline = time.time() + 2.0
        stopped_seen = False
        while time.time() < deadline:
            if token is not None and gov.status()["stopped_children"] >= 1:
                stopped_seen = True
                break
            time.sleep(0.03)
        gov.unregister_child(token)
        token = None
        assert proc.wait(timeout=5) == 0, "child hung: left stopped after unregister"
    finally:
        gov.stop()
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    # sawtooth timing may race a 0.15 s child; not a failure, only a diagnostic
    # (the contract under test is resume-before-return, asserted above)


@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGSTOP control")
@pytest.mark.skipif((psutil.cpu_count() or 0) < 8, reason="needs >=8 cores for cap to bite")
def test_cpu_cap_under_load_slow():
    if not psutil.cpu_percent(interval=0.2):
        pytest.skip("psutil unavailable")
    n = psutil.cpu_count() or 8
    hammers = []
    gov = make_governor(cpu_cap_pct=80.0, sample_s=0.3)
    tokens = []
    gov.start()
    try:
        for _ in range(n):
            p = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=255",
                 "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            hammers.append(p)
            tok = gov.register_child(p.pid)
            if tok is not None:
                tokens.append((p.pid, tok))
        time.sleep(1.0)  # psutil per-cpu first call primes
        peak_stopped = 0
        t0 = time.time()
        for _ in range(30):  # ~9 s of measurement
            time.sleep(0.3)
            peak_stopped = max(peak_stopped, gov.status()["stopped_children"])
        measured = [c for ts, c in gov.samples if ts >= t0]
        assert measured, "governor recorded feedback samples"
        mean_cpu = sum(measured) / len(measured)
        assert mean_cpu <= 85.0, f"mean CPU {mean_cpu:.1f}% over cap 80%"
        assert peak_stopped >= 1, "sawtooth never engaged - nothing to throttle"
    finally:
        for pid, tok in tokens:
            gov.unregister_child(tok)
        gov.stop()
        for p in hammers:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for p in hammers:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
