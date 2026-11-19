"""Bounded process handling (Windows live-fire regression).

A GUI launcher whose helper child survives its parent (soffice.exe spawns
soffice.bin and holds the stdout pipes) turned every timeout path into an
infinite hang on Windows - including CPython's own subprocess.run(timeout=).
These tests pin the contract: kill the tree, drain with a deadline, return.
A timed-out answer is always worth more than a hung app.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from transmute.engines.base import drain_bounded, run_command
from transmute.probe.engines import _run_version

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX fixtures; Windows exercised on bench")


def test_drain_bounded_never_blocks_on_live_process():
    """Even with the child ALIVE (pipes held), drain_bounded returns fast."""
    proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    try:
        out, err = drain_bounded(proc, 1.0)
        assert (out, err) == ("", "")
        assert time.monotonic() - t0 < 6.0
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_run_command_timeout_kills_tree_and_returns(tmp_path):
    """Group members that inherit the pipes die with the group; bounded."""
    script = tmp_path / "spawns.sh"
    script.write_text("#!/bin/sh\nsleep 120 &\nsleep 120\n")
    script.chmod(0o755)
    t0 = time.monotonic()
    res = run_command([str(script)], timeout_s=1.5,
                      log_path=tmp_path / "eng.log")
    elapsed = time.monotonic() - t0
    assert res.timed_out is True
    assert elapsed < 25.0, f"timeout path took {elapsed:.1f}s - unbounded drain?"
    left = subprocess.run(["pgrep", "-f", str(script)],
                          capture_output=True, text=True)
    assert left.stdout.strip() == "", "group survivor still running"


def test_probe_version_timeout_is_bounded_and_raises(tmp_path):
    """_run_version must raise TimeoutExpired quickly even though the child
    outlives a plain kill (the subprocess.run(timeout=) hang shape)."""
    fake = tmp_path / "slowengine"
    fake.write_text("#!/bin/sh\nsleep 120 &\nsleep 120\n")
    fake.chmod(0o755)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_version(fake, ("--version",), timeout_s=1.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 25.0, f"_run_version took {elapsed:.1f}s - unbounded drain?"
    left = subprocess.run(["pgrep", "-f", str(fake)],
                          capture_output=True, text=True)
    assert left.stdout.strip() == "", "tree survivor still running after probe timeout"
