"""F26: a box with ZERO external engines must start and report honestly.

Image (Pillow) and PDF (pikepdf) engines run pure-Python in-process, so they
keep working with no ffmpeg/gs/qpdf/LO/pandoc at all. Every ffmpeg-dependent
path must come back as a clean "unavailable", never an exception - and the
queue must classify such jobs terminal instead of retrying forever.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_engine_manager_reports_missing_with_hints(no_engines):
    from transmute.probe.engines import EngineManager, STATUS_MISSING
    infos = EngineManager().detect()
    assert infos, "engine table must always be populated"
    for name, info in infos.items():
        assert info.status == STATUS_MISSING, f"{name} should be missing"
        assert info.reason
        assert isinstance(info.install_hint, str) and info.install_hint


def test_media_engines_unavailable_not_raising(no_engines, tmp_path, fixtures):
    from transmute.engines import engine_for
    for cat in ("video", "audio", "document"):
        eng = engine_for(cat)
        av = eng.availability()
        assert av.ok is False, cat
        assert av.reason, f"{cat} must say why"
        # converting anyway must fail cleanly, flagged engine_unavailable
        res = eng.convert(fixtures["jpg"], tmp_path / "out.bin")
        assert res.outcome == "failed"
        assert (res.data or {}).get("engine_unavailable") is True


def test_image_engine_works_without_any_engine(no_engines, tmp_path, fixtures):
    """Pillow is in-process: image conversion is the zero-dependency path."""
    from transmute.engines import engine_for
    eng = engine_for("image")
    assert eng.availability().ok is True
    res = eng.convert(fixtures["jpg"], tmp_path / "out.webp", quality=70)
    assert res.outcome == "converted", res.reason
    assert (tmp_path / "out.webp").exists()
    v = eng.verify(fixtures["jpg"], tmp_path / "out.webp")
    assert v.ok


def test_pdf_engine_in_process_without_qpdf(no_engines, tmp_path, fixtures):
    """pikepdf is in-process: the lossless PDF rung works with zero engines."""
    from transmute.engines import engine_for
    eng = engine_for("pdf")
    av = eng.availability()
    if not av.ok:
        pytest.skip("pikepdf missing from this venv - external engines required")
    res = eng.convert(fixtures["pdf"], tmp_path / "out.pdf")
    assert res.ok, res.reason  # converted or kept - both legitimate
    if res.outcome == "converted":
        assert eng.verify(fixtures["pdf"], tmp_path / "out.pdf").ok


def test_queue_terminal_on_missing_engine(no_engines, tmp_path, fixtures):
    from transmute.jsonl import JsonlWriter
    from transmute.runner import JobQueue, Journal
    q = JobQueue(journal=Journal(tmp_path / "j.jsonl"),
                 events=JsonlWriter(tmp_path / "e.jsonl"),
                 logs_root=tmp_path / "logs")
    jid = q.add(fixtures["jpg"], tmp_path / "v.mp4", category="video").job_id
    s = q.run()
    rec = q.journal.records[jid]
    assert rec.state == "abandoned", rec.reason
    assert "engine unavailable" in (rec.reason or "")
