"""A1: PDF export TRANSFORMS (pages -> PNG/JPEG, text -> TXT).

Transforms are NOT the lossless ladder: outputs are judged by well-formedness
(magic bytes / non-blank text), and the queue accepts them only because the
engine self-verifies inside convert() (data["verified_by_engine"]).
Every skip here is a machine without poppler - the row stays VISIBLE in the
UI with that machine's reason (F3), it is not hidden.
"""
from __future__ import annotations

import pytest

from transmute.engines.pdf import PdfEngine
from transmute.probe.engines import EngineManager

pytestmark = pytest.mark.unit

_engines = EngineManager()
HAS_PPM = _engines.is_available("pdftoppm")
HAS_TXT = _engines.is_available("pdftotext")
needs_ppm = pytest.mark.skipif(not HAS_PPM, reason="pdftoppm absent on this machine")
needs_txt = pytest.mark.skipif(not HAS_TXT, reason="pdftotext absent on this machine")


@pytest.fixture
def engine():
    return PdfEngine()


class TestPageCount:
    def test_counted_from_file(self, engine, text_pdf):
        assert engine.page_count(text_pdf) == 2

    def test_garbage_raises_machine_reason(self, engine, tmp_path):
        p = tmp_path / "x.pdf"
        p.write_bytes(b"not a pdf at all")
        with pytest.raises(ValueError):
            engine.page_count(p)


class TestTextExport:
    @needs_txt
    def test_extracts_text_both_pages(self, engine, text_pdf, tmp_path):
        dst = tmp_path / "out.txt"
        res = engine.convert(text_pdf, dst, target="txt")
        assert res.outcome == "converted", res.reason
        body = dst.read_text()
        assert "Hello Transmute Page One" in body
        assert "Second page says goodbye" in body
        assert res.data["verified_by_engine"]

    @needs_txt
    def test_scanned_pdf_fails_with_actionable_reason(self, engine, fixtures, tmp_path):
        # noise.pdf is image-only: pdftotext yields nothing extractable.
        res = engine.convert(fixtures["pdf"], tmp_path / "o.txt", target="txt")
        assert res.outcome == "failed"
        assert "no extractable text" in res.reason
        assert not (tmp_path / "o.txt").exists()      # nothing half-written


class TestPageRender:
    @needs_ppm
    @pytest.mark.parametrize("target,magic", [("png", b"\x89PNG"), ("jpg", b"\xff\xd8")])
    def test_renders_requested_page_only(self, engine, text_pdf, tmp_path, target, magic):
        dst = tmp_path / f"p.{target}"
        res = engine.convert(text_pdf, dst, target=target, page=2)
        assert res.outcome == "converted", res.reason
        assert dst.read_bytes().startswith(magic)
        assert res.data["verified_by_engine"] and res.data["page"] == 2

    @needs_ppm
    def test_bad_page_number_fails_not_crashes(self, engine, text_pdf, tmp_path):
        res = engine.convert(text_pdf, tmp_path / "p.png", target="png", page=99)
        assert res.outcome == "failed"
        assert not (tmp_path / "p.png").exists()


class TestLadderUntouched:
    def test_no_target_still_runs_lossless_ladder(self, engine, fixtures, tmp_path):
        res = engine.convert(fixtures["pdf"], tmp_path / "out.pdf")
        # noise.pdf is already-optimal noise -> kept, never silently rewritten
        assert res.outcome in ("converted", "kept")
        assert res.encoder in ("pikepdf", "qpdf", "ghostscript-lossless")

    def test_unknown_export_target_rejected(self, engine, text_pdf, tmp_path):
        res = engine.convert(text_pdf, tmp_path / "x", target="docx")
        assert res.outcome == "failed" and "docx" in res.reason


class TestQueueIntegration:
    """Full pipeline: fan-out enqueue -> JobQueue -> self-verified outputs
    (verify-before-replace bypass) -> files on disk, source untouched."""

    @needs_ppm
    @needs_txt
    def test_per_page_and_txt_through_real_queue(self, tmp_path, text_pdf, monkeypatch):
        from transmute.paths import AppPaths
        from transmute.runner.queue import JobQueue
        monkeypatch.setenv("TRANSMUTE_ROOT", str(tmp_path / "root"))
        paths = AppPaths(root=tmp_path / "root")
        q = JobQueue(paths=paths)
        from transmute.gui.controller import ConverterController
        from test_gui_controller import _report
        c = ConverterController(report_provider=_report, queue=q)
        out = tmp_path / "out"
        out.mkdir()
        c.enqueue(text_pdf, out, "pdf", "png (per page)")
        c.enqueue(text_pdf, out, "pdf", "txt (text)")
        summary = c.process()
        assert summary.counts.get("replaced", 0) == 3
        assert (out / "letter-01.png").is_file()
        assert (out / "letter-02.png").is_file()
        assert (out / "letter.txt").is_file()
        assert "Hello Transmute Page One" in (out / "letter.txt").read_text()
        # source never modified (non-destructive default)
        assert text_pdf.read_bytes()[:8] == b"%PDF-1.4"
