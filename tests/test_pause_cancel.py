"""P5 queue UX primitives (unit tier, fake engines, no Qt): pause_all /
resume_all are the RECOVERABLE pair — nothing in flight is interrupted
(verify-before-replace doctrine), nothing is destroyed. cancel_all is the
terminal one: visible 'abandoned' with a reason, in-flight jobs finish.
Disk preflight measures the destination volume and fails TRANSIENTLY
(retry ladder) with the machine's own numbers.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from transmute.engines.base import EngineResult
from transmute.jsonl import JsonlWriter
from transmute.runner import JobQueue, Journal
from transmute.runner import preflight


def _queue(tmp_path, engine=None, backoff=(0.0, 0.0, 0.0)):
    eg = (lambda cat: engine) if engine is not None else (lambda cat: None)
    return JobQueue(journal=Journal(tmp_path / "journal.jsonl"),
                    events=JsonlWriter(tmp_path / "events.jsonl"),
                    logs_root=tmp_path / "captures",
                    backoff_s=backoff, engine_get=eg)


class FakeEngine:
    def __init__(self, convert=None):
        self._convert = convert or self._ok

    @staticmethod
    def _ok(src, dst):
        dst.write_bytes(b"NEWBYTES")
        return EngineResult(outcome="converted", encoder="fake")

    def availability(self):
        return SimpleNamespace(ok=True, reason="", guidance=None)

    def convert(self, src, dst, **kw):
        return self._convert(src, dst)

    def verify(self, src, out):
        return SimpleNamespace(ok=True, reason="")


def _wait_until(pred, what, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    pytest.fail(f"timeout waiting: {what}")


# --------------------------------------------------------- pause / resume
def test_pause_all_is_recoverable_and_excluded_from_pending(tmp_path, fixtures):
    q = _queue(tmp_path, FakeEngine())
    q.add(fixtures["jpg"], tmp_path / "o" / "a.flac", category="audio")
    q.add(fixtures["jpg"], tmp_path / "o" / "b.flac", category="audio")
    assert q.pause_all() == 2
    assert q.is_paused
    assert not q.journal.pending()  # paused must never auto-qualify
    # survives a restart (journal persistence of paused + origin)
    re = Journal(tmp_path / "journal.jsonl")
    assert all(r.state == "paused" and r.paused_from == "queued"
               for r in re.records.values())
    assert q.resume_all() == 2
    assert not q.is_paused
    s = q.run()
    assert s.counts.get("replaced") == 2


def test_pause_preserves_failed_retry_schedule(tmp_path, fixtures):
    def boom(src, dst):
        raise RuntimeError("encoder exploded")
    q = _queue(tmp_path, FakeEngine(convert=boom), backoff=(3600.0, 0.0, 0.0))
    jid = q.add(fixtures["jpg"], tmp_path / "f.flac", category="audio").job_id
    q.run()
    assert q.journal.records[jid].state == "failed"
    due = q.journal.records[jid].next_attempt_at
    assert q.pause_all() == 1
    rec = q.journal.records[jid]
    assert rec.state == "paused" and rec.paused_from == "failed"
    assert rec.next_attempt_at == due  # schedule remembered, not reset
    q.resume_all()
    rec = q.journal.records[jid]
    assert rec.state == "failed" and rec.next_attempt_at == due
    assert not rec.eligible()  # still respects the retry window
    assert rec.eligible(now=due + 1)


def test_pause_mid_run_inflight_finishes_rest_never_starts(tmp_path, fixtures):
    gate = threading.Event()

    def slow(src, dst):
        gate.wait(10)
        dst.write_bytes(b"NEWBYTES")
        return EngineResult(outcome="converted", encoder="fake")

    q = _queue(tmp_path, FakeEngine(convert=slow))
    j1 = q.add(fixtures["jpg"], tmp_path / "p.flac", category="audio").job_id
    j2 = q.add(fixtures["jpg"], tmp_path / "q.flac", category="audio").job_id
    t = threading.Thread(target=lambda: q.run(max_workers=1))
    t.start()
    _wait_until(lambda: q.journal.records[j1].state == "running",
                what="job1 running")
    q.pause_all()          # job2 is queued (its future waits for the worker)
    gate.set()
    t.join(15)
    assert not t.is_alive()
    assert q.journal.records[j1].state == "replaced"      # in flight: finished
    assert q.journal.records[j2].state == "paused"        # never started
    assert q.resume_all() == 1
    assert q.run().counts.get("replaced") == 1            # resumes cleanly


# ---------------------------------------------------------------- cancel
def test_cancel_all_abandons_pending_but_not_inflight(tmp_path, fixtures):
    q = _queue(tmp_path, FakeEngine())
    j_run = q.add(fixtures["jpg"], tmp_path / "r.flac", category="audio").job_id
    j_pend = q.add(fixtures["jpg"], tmp_path / "p.flac", category="audio").job_id
    q.journal.transition(q.journal.records[j_run], "running")  # simulate in-flight
    assert q.cancel_all() == 1  # only the pending one
    assert q.journal.records[j_run].state == "running"
    rec = q.journal.records[j_pend]
    assert rec.state == "abandoned" and "cancelled by user" in rec.reason


# ------------------------------------------------------------- preflight
def test_preflight_blocks_full_volume_transiently_then_proceeds(tmp_path,
                                                                fixtures,
                                                                monkeypatch):
    import transmute.runner.preflight as pf
    tiny = SimpleNamespace(total=10 ** 10, used=10 ** 10 - 1024, free=1024)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: tiny)
    q = _queue(tmp_path, FakeEngine())
    jid = q.add(fixtures["jpg"], tmp_path / "o" / "z.flac",
                category="audio").job_id
    s = q.run()
    rec = q.journal.records[jid]
    assert rec.state == "failed" and not rec.terminal  # transient: ladder
    assert "insufficient space" in rec.reason and "MiB" in rec.reason
    assert rec.next_attempt_at is not None
    assert not (tmp_path / "o" / ".transmute-tmp").exists()  # nothing written
    big = SimpleNamespace(total=10 ** 12, used=0, free=10 ** 12)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: big)
    assert q.run().counts.get("replaced") == 1  # space freed -> resumes alone


def test_preflight_unmeasurable_never_blocks(tmp_path, fixtures, monkeypatch):
    import transmute.runner.preflight as pf

    def broken(p):
        raise OSError("volume vanished")
    monkeypatch.setattr(pf.shutil, "disk_usage", broken)
    q = _queue(tmp_path, FakeEngine())
    jid = q.add(fixtures["jpg"], tmp_path / "z.flac", category="audio").job_id
    assert q.run().counts.get("replaced") == 1


def test_required_bytes_honest_estimate():
    floor = preflight.MIN_FREE_BYTES
    assert preflight.required_bytes(1_000) == floor          # small src: floor
    assert preflight.required_bytes(2 * floor) == 2 * floor  # big src: its size
    n500 = preflight.required_bytes(1_000, {"size_mb": 500})
    assert n500 == int(500e6 * preflight.SIZE_TARGET_SLACK)  # budget dominates
