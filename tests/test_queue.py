"""Queue + journal state machine (no engines): F23/F24 semantics, F19, retry ladder.

Every branch of _process is driven with fake engines - the queue must never
lose or clobber bytes: staging cleaned on every failure, verify strictly
before os.replace, honest terminal vs retryable classification.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from transmute.engines.base import EngineResult
from transmute.jsonl import JsonlWriter
from transmute.runner import JobQueue, Journal



def _queue(tmp_path, engine=None, backoff=(0.0, 0.0, 0.0)):
    eg = (lambda cat: engine) if engine is not None else (lambda cat: None)
    q = JobQueue(journal=Journal(tmp_path / "journal.jsonl"),
                 events=JsonlWriter(tmp_path / "events.jsonl"),
                 logs_root=tmp_path / "captures",
                 backoff_s=backoff,
                 engine_get=eg)
    return q


class FakeEngine:
    def __init__(self, convert=None, verify_ok=True, available=True):
        self._convert = convert
        self._verify_ok = verify_ok
        self._available = available

    def availability(self):
        return SimpleNamespace(ok=self._available,
                               reason="" if self._available else "ffmpeg missing",
                               guidance=None)

    def convert(self, src, dst, **kw):
        return self._convert(src, dst)

    def verify(self, src, out):
        return SimpleNamespace(ok=self._verify_ok,
                               reason="" if self._verify_ok else "pixels differ")


def _ok_convert(payload=b"NEWBYTES"):
    def f(src, dst):
        dst.write_bytes(payload)
        return EngineResult(outcome="converted", encoder="fake")
    return f


def test_replaced_and_staging_cleaned(tmp_path, fixtures):
    eng = FakeEngine(convert=_ok_convert())
    q = _queue(tmp_path, eng)
    jid = q.add(fixtures["jpg"], tmp_path / "out" / "x.flac", category="audio").job_id
    s = q.run()
    assert s.counts.get("replaced") == 1
    assert (tmp_path / "out" / "x.flac").read_bytes() == b"NEWBYTES"
    staged = tmp_path / "out" / ".transmute-tmp"
    assert not (staged.exists() and list(staged.glob("*")))
    assert q.journal.records[jid].state == "replaced"


def test_kept_does_not_touch_dst(tmp_path, fixtures):
    def conv(src, dst):
        return EngineResult(outcome="kept", reason="already optimal")
    eng = FakeEngine(convert=conv)
    q = _queue(tmp_path, eng)
    jid = q.add(fixtures["jpg"], tmp_path / "k.flac", category="audio").job_id
    s = q.run()
    assert s.counts.get("kept") == 1 and not (tmp_path / "k.flac").exists()
    assert q.journal.records[jid].state == "kept"


def test_retry_ladder_then_abandoned(tmp_path, fixtures):
    def conv(src, dst):
        return EngineResult(outcome="failed", reason="corrupt input")
    eng = FakeEngine(convert=conv)
    q = _queue(tmp_path, eng)
    jid = q.add(fixtures["jpg"], tmp_path / "b.flac", category="audio").job_id
    states = []
    for _ in range(6):  # ladder exhausted well under the loop bound
        q.run()
        states.append(q.journal.records[jid].state)
        if q.journal.records[jid].terminal:
            break
        time.sleep(0.02)
    assert states[-1] == "abandoned"
    assert states.count("failed") == 3  # backoff_s has 3 rungs
    assert q.journal.records[jid].attempts == 4


def test_engine_unavailable_result_is_terminal(tmp_path, fixtures):
    def conv(src, dst):
        return EngineResult(outcome="failed", reason="ffmpeg died at spawn",
                            data={"engine_unavailable": True})
    q = _queue(tmp_path, FakeEngine(convert=conv))
    jid = q.add(fixtures["jpg"], tmp_path / "u.flac", category="audio").job_id
    q.run()
    assert q.journal.records[jid].state == "abandoned"


def test_engine_unavailable_availability_is_terminal(tmp_path, fixtures):
    q = _queue(tmp_path, FakeEngine(convert=_ok_convert(), available=False))
    jid = q.add(fixtures["jpg"], tmp_path / "a.flac", category="audio").job_id
    q.run()
    rec = q.journal.records[jid]
    assert rec.state == "abandoned" and "engine unavailable" in (rec.reason or "")


def test_verify_failure_preserves_original(tmp_path, fixtures):
    dst = tmp_path / "o.flac"
    dst.write_bytes(b"SENTINEL")  # original bytes must survive
    q = _queue(tmp_path, FakeEngine(convert=_ok_convert(), verify_ok=False))
    jid = q.add(fixtures["jpg"], dst, category="audio").job_id
    q.run()
    assert dst.read_bytes() == b"SENTINEL"
    assert q.journal.records[jid].state == "failed"  # retryable
    staged = tmp_path / ".transmute-tmp"
    assert not (staged.exists() and list(staged.glob("*")))


def test_verified_by_engine_skips_separate_verify(tmp_path, fixtures):
    # Engine self-verified during convert (tone-map path): queue must NOT run
    # the standard verify (which would legitimately flag the intended step),
    # yet the replace still happens atomically.
    def conv(src, dst):
        dst.write_bytes(b"TONEMAPPED")
        return EngineResult(outcome="converted", encoder="fake",
                            data={"verified_by_engine": True})
    # verify_ok=False proves the queue never called verify().
    q = _queue(tmp_path, FakeEngine(convert=conv, verify_ok=False))
    jid = q.add(fixtures["jpg"], tmp_path / "t.mp4", category="video").job_id
    s = q.run()
    assert s.counts.get("replaced") == 1
    assert (tmp_path / "t.mp4").read_bytes() == b"TONEMAPPED"
    assert q.journal.records[jid].state == "replaced"


def test_crashing_engine_does_not_break_queue(tmp_path, fixtures):
    def conv(src, dst):
        raise RuntimeError("engine bug")
    q = _queue(tmp_path, FakeEngine(convert=conv))
    jid = q.add(fixtures["jpg"], tmp_path / "c.flac", category="audio").job_id
    s = q.run()  # must not raise
    assert s.counts.get("failed") == 1
    assert "engine crashed" in (q.journal.records[jid].reason or "")


def test_missing_engine_for_category_is_terminal(tmp_path, fixtures):
    q = _queue(tmp_path, engine=None)
    jid = q.add(fixtures["jpg"], tmp_path / "z.flac", category="audio").job_id
    q.run()
    assert q.journal.records[jid].state == "abandoned"


def test_ghost_running_replays_as_interrupted_then_discard(tmp_path, fixtures):
    # REAL lifecycle: a crashed job has a queued line BEFORE its running
    # line - the normal case a live process leaves behind on kill -9.
    # The old recovery only handled an orphan running line (prev=None), so
    # every real crashed job replayed stuck in "running" forever.
    jp = tmp_path / "journal.jsonl"
    base = {"job_id": "ghost-1", "src": str(fixtures["jpg"]),
            "dst": str(tmp_path / "g.flac"), "category": "audio",
            "attempts": 0}
    jp.write_text(
        json.dumps({**base, "state": "queued", "ts": time.time() - 2}) + "\n"
        + json.dumps({**base, "state": "running", "ts": time.time() - 1}) + "\n")
    j = Journal(jp)
    assert j.records["ghost-1"].state == "interrupted"
    # the orphan variant (running with no queued line) must recover too
    jp2 = tmp_path / "journal2.jsonl"
    jp2.write_text(json.dumps({**base, "job_id": "ghost-2", "state": "verifying",
                               "ts": time.time()}) + "\n")
    j2 = Journal(jp2)
    assert j2.records["ghost-2"].state == "interrupted"
    q = JobQueue(journal=j, events=JsonlWriter(tmp_path / "e.jsonl"),
                 logs_root=tmp_path / "cap", engine_get=lambda c: None)
    assert q.discard() == 1
    assert j.records["ghost-1"].state == "abandoned"


def test_discard_refuses_ids_the_worker_owns(tmp_path, fixtures):
    # a GUI row can be stale by the time a click lands; the journal is the
    # second lock: an in-flight record is never abandoned by id, whatever
    # the caller thought its state was.
    q = _queue(tmp_path, engine=None)
    jid = q.add(fixtures["jpg"], tmp_path / "y.flac", category="image").job_id
    q.journal.transition(q.journal.records[jid], "running")
    assert q.discard([jid]) == 0
    assert q.journal.records[jid].state == "running"


def test_torn_journal_line_tolerated(tmp_path, fixtures):
    jp = tmp_path / "journal.jsonl"
    jp.write_text(json.dumps({
        "job_id": "ok-1", "src": str(fixtures["jpg"]), "dst": str(tmp_path / "t.flac"),
        "category": "audio", "state": "queued", "attempts": 0, "ts": time.time()})
        + "\n" + '{"job_id": "cut' + "\n")  # crash-torn line
    j = Journal(jp)
    assert "ok-1" in j.records and len(j.records) == 1


def test_add_idempotent_for_done_jobs(tmp_path, fixtures):
    q = _queue(tmp_path, FakeEngine(convert=_ok_convert()))
    jid = q.add(fixtures["jpg"], tmp_path / "i.flac", category="audio").job_id
    q.run()
    again = q.add(fixtures["jpg"], tmp_path / "i.flac", category="audio")
    assert again.job_id == jid and again.state == "replaced"


def test_readd_during_flight_honest_and_fingerprint_at_encode_start(
        tmp_path, fixtures):
    """Two bugs, one test: (1) re-adding a file while its worker is mid-
    convert must NOT fork a second record or stamp state the worker does not
    own; (2) a 'replaced' record's src_fp is the fingerprint of the bytes
    the encode STARTED from - a source replaced mid-encode must re-queue
    afterwards, never be believed stale-matched."""
    import threading
    started, release = threading.Event(), threading.Event()

    def conv(src, dst):
        started.set()
        assert release.wait(10)
        src.write_bytes(b"MUTATED MID-ENCODE")   # source swapped in flight
        dst.write_bytes(b"OUTDATA")
        return EngineResult(outcome="converted", encoder="fake")

    src = tmp_path / "in.jpg"
    src.write_bytes(b"ORIGINAL")
    from transmute.runner.queue import _fingerprint
    fp_orig = _fingerprint(src)
    eng = FakeEngine(convert=conv)
    q = _queue(tmp_path, eng)
    rec = q.add(src, tmp_path / "out" / "x.flac", category="audio")
    th = threading.Thread(target=lambda: q.run(max_workers=1))
    th.start()
    assert started.wait(10)

    again = q.add(src, tmp_path / "out" / "x.flac", category="audio")
    assert again.job_id == rec.job_id          # no forked record
    assert again.state == "running"            # worker still owns it

    release.set()
    th.join(15)
    assert not th.is_alive()
    r = q.journal.records[rec.job_id]
    assert r.state == "replaced"
    # the event's size is the OUTPUT size, not a stat of the source
    from transmute.jsonl import read_jsonl
    ev = [e for e in read_jsonl(tmp_path / "events.jsonl")
          if e["event"] == "job_replaced"]
    assert ev and ev[-1]["size"] == len(b"OUTDATA")
    # the record claims the bytes the encode began from - not the mutated
    # ones the encode left behind
    assert r.src_fp == fp_orig
    # the mutated file no longer matches it: re-add honestly re-queues
    re2 = q.add(src, tmp_path / "out" / "x.flac", category="audio")
    assert re2.state == "queued"


def test_unwritable_output_folder_fails_the_job_not_the_run(tmp_path, fixtures):
    """An unreadable/uncreatable output volume must surface as THIS job's
    failure with the machine's own error - never kill the runner loop or
    escape run() as an exception the caller never sees."""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"i am a file, not a folder")
    q = _queue(tmp_path, FakeEngine(convert=_ok_convert()))
    rec = q.add(fixtures["jpg"], blocker / "sub" / "x.flac", category="audio")
    summary = q.run(max_workers=1)             # must NOT raise
    r = q.journal.records[rec.job_id]
    assert r.state == "failed"
    assert "cannot prepare output folder" in (r.reason or "")
