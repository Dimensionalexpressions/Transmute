"""A2: document EXPORT transforms (docx/odt/rtf/html/markdown/txt).

Routing doctrine: markup sources -> pandoc; office sources -> LibreOffice;
markdown target -> pandoc only. Outputs are judged by format magic BEFORE
acceptance (same rule as the PDF A1 exports) and self-verify, so the queue
skips its PDF-shaped verify(). Skips here are machines without the tool -
the matrix row stays VISIBLE with that machine's reason (F3).
"""
from __future__ import annotations

import pytest

from transmute.engines.document import DocumentEngine
from transmute.probe.engines import EngineManager

pytestmark = pytest.mark.unit

_engines = EngineManager()
HAS_LO = _engines.is_available("libreoffice")
HAS_PANDOC = _engines.is_available("pandoc")
needs_lo = pytest.mark.skipif(not HAS_LO, reason="LibreOffice absent on this machine")
needs_pandoc = pytest.mark.skipif(not HAS_PANDOC, reason="pandoc absent on this machine")

MD = "# Title\n\nHello *world*, this is a test document.\n"


@pytest.fixture
def engine():
    return DocumentEngine()


@pytest.fixture
def md_source(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(MD)
    return p


class TestPandocExports:
    @needs_pandoc
    def test_all_targets_produce_wellformed_files(self, engine, md_source, tmp_path):
        for tgt, magic in [("docx", b"PK\x03\x04"), ("odt", b"PK\x03\x04"),
                           ("rtf", b"{\\rtf"), ("html", b"<")]:
            res = engine.convert(md_source, tmp_path / f"o.{tgt}", target=tgt)
            assert res.outcome == "converted", f"{tgt}: {res.reason}"
            assert tmp_path.joinpath(f"o.{tgt}").read_bytes().startswith(magic), tgt
            assert res.data["verified_by_engine"]
        # text targets: non-blank, content preserved
        res = engine.convert(md_source, tmp_path / "o.txt", target="txt")
        body = (tmp_path / "o.txt").read_text()
        assert res.outcome == "converted" and "Hello world" in body
        assert "\n" in body.strip()
        # markdown output proven from a DIFFERENT source: md->markdown is a skip
        html = tmp_path / "s.html"
        html.write_text("<h1>Title</h1><p>Hello <em>world</em></p>")
        res = engine.convert(html, tmp_path / "o2.md", target="markdown")
        assert res.outcome == "converted" and "*world*" in (tmp_path / "o2.md").read_text()

    @needs_pandoc
    def test_rtf_and_html_get_full_document_wrapper(self, engine, md_source, tmp_path):
        # pandoc's bare writers emit FRAGMENTS; a saved file must open in a
        # real viewer, so the engine demands -s (asserted via the wrappers).
        engine.convert(md_source, tmp_path / "r.rtf", target="rtf")
        assert (tmp_path / "r.rtf").read_text().startswith("{\\rtf1")
        engine.convert(md_source, tmp_path / "h.html", target="html")
        html = (tmp_path / "h.html").read_text().lower()
        assert "<html" in html and "<head" in html

    @needs_pandoc
    def test_word_source_to_markdown_native_reader(self, engine, md_source, tmp_path):
        docx = tmp_path / "s.docx"
        assert engine.convert(md_source, docx, target="docx").outcome == "converted"
        res = engine.convert(docx, tmp_path / "back.md", target="markdown")
        assert res.outcome == "converted", res.reason
        assert "Hello" in (tmp_path / "back.md").read_text()

    def test_same_format_skips_honestly(self, engine, md_source, tmp_path):
        res = engine.convert(md_source, tmp_path / "again.md", target="markdown")
        assert res.outcome == "skipped" and "already" in res.reason

    def test_unknown_target_rejected(self, engine, md_source, tmp_path):
        res = engine.convert(md_source, tmp_path / "x", target="pages")
        assert res.outcome == "failed" and "pages" in res.reason

    def test_markdown_without_pandoc_is_terminal(self, engine, tmp_path,
                                                 monkeypatch):
        html = tmp_path / "s.html"
        html.write_text("<p>hi</p>")
        monkeypatch.setattr(engine.engines, "is_available",
                            lambda name: name == "libreoffice")
        res = engine.convert(html, tmp_path / "x.md", target="markdown")
        assert res.outcome == "failed" and res.data.get("engine_unavailable")


class TestMatrixRows:
    def _rep(self, lo_ok: bool, pan_ok: bool):
        from transmute.probe.capability import CapabilityReport, STATUS_OK
        eng = {"libreoffice": {"status": STATUS_OK if lo_ok else "missing"},
               "pandoc": {"status": STATUS_OK if pan_ok else "missing"}}
        return CapabilityReport(schema=5, probe_key="k", generated_at=0.0,
                                app_version="t", machine={}, engines=eng,
                                encoders=[], filters={}, gpu_nodes={},
                                image_backends={}, pdf_backends={},
                                caveats=[], matrix=[])

    def test_rows_visible_even_with_nothing_installed(self):
        from transmute.probe.capability import build_matrix
        rows = {r.target: r for r in build_matrix(self._rep(False, False))
                if r.category == "document"}
        assert {"pdf", "docx", "odt", "rtf", "html", "markdown", "txt"} <= set(rows)
        assert not rows["docx"].available
        assert "LibreOffice" in rows["docx"].note        # the machine's reason

    def test_pandoc_only_enables_markdown(self):
        from transmute.probe.capability import build_matrix
        rows = {r.target: r for r in build_matrix(self._rep(False, True))
                if r.category == "document"}
        assert rows["markdown"].available and rows["docx"].available

    def test_libreoffice_only_marks_markdown_absent(self):
        from transmute.probe.capability import build_matrix
        rows = {r.target: r for r in build_matrix(self._rep(True, False))
                if r.category == "document"}
        assert rows["docx"].available and not rows["markdown"].available
        assert "pandoc" in rows["markdown"].note


class TestLibreOfficeRoute:
    @needs_lo
    @needs_pandoc
    def test_office_source_really_uses_libreoffice(self, engine, md_source, tmp_path):
        # build a Word file (via pandoc), then convert it with LO present:
        # office sources must route to LibreOffice, not pandoc-guesswork.
        docx = tmp_path / "s.docx"
        assert engine.convert(md_source, docx, target="docx").outcome == "converted"
        res = engine.convert(docx, tmp_path / "o.odt", target="odt")
        assert res.outcome == "converted", res.reason
        assert res.encoder == "libreoffice"
        assert (tmp_path / "o.odt").read_bytes().startswith(b"PK\x03\x04")

    @needs_lo
    def test_garbage_container_fails_before_engines(self, engine, tmp_path):
        fake = tmp_path / "notword.docx"
        fake.write_bytes(b"pretending to be docx")
        res = engine.convert(fake, tmp_path / "o.odt", target="odt")
        assert res.outcome == "failed"
        assert "valid document container" in res.reason
        assert not (tmp_path / "o.odt").exists()

    @needs_lo
    def test_real_libreoffice_failure_shows_tools_own_story(self, engine, tmp_path):
        # a VALID zip that is not a word-processor document: LibreOffice must
        # be the one reporting defeat (no silent text-import junk).
        import zipfile
        bogus = tmp_path / "bogus.odt"
        with zipfile.ZipFile(bogus, "w") as zf:
            zf.writestr("junk.txt", "not a document")
        res = engine.convert(bogus, tmp_path / "o.docx", target="docx")
        assert res.outcome == "failed"
        assert "LibreOffice" in res.reason
        assert not (tmp_path / "o.docx").exists()


class TestQueueIntegration:
    @needs_pandoc
    def test_md_to_txt_through_real_queue(self, tmp_path, md_source, monkeypatch):
        from transmute.paths import AppPaths
        from transmute.runner.queue import JobQueue
        monkeypatch.setenv("TRANSMUTE_ROOT", str(tmp_path / "root"))
        q = JobQueue(paths=AppPaths(root=tmp_path / "root"))
        from transmute.gui.controller import ConverterController
        from test_gui_controller import _report
        c = ConverterController(report_provider=_report, queue=q)
        out = tmp_path / "out"
        out.mkdir()
        c.enqueue(md_source, out, "document", "txt")
        summary = c.process()
        assert summary.counts.get("replaced", 0) == 1
        assert c.snapshot()[0].state == "replaced"
        assert "Hello world" in (out / "note.txt").read_text()
        assert md_source.read_text() == MD               # non-destructive


class TestExitStatusIsAuthority:
    """Two half-truths the engine used to believe: pandoc exiting nonzero
    while leaving a file that sniffs valid, and LibreOffice killed
    mid-write leaving a half file with a valid header. The process status
    and the timeout - not the sniff - decide."""

    @needs_pandoc
    def test_pandoc_nonzero_exit_beats_look_valid_output(self, engine, md_source,
                                                         tmp_path, monkeypatch):
        from pathlib import Path
        from types import SimpleNamespace
        import transmute.engines.document as D

        def fake_run(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"PK\x03\x04plausible bytes")
            return SimpleNamespace(ok=False, timed_out=False, returncode=2,
                                   stderr_text="real pandoc error: bad writer",
                                   stdout_text="")

        monkeypatch.setattr(D, "run_command", fake_run)
        res = engine.convert(md_source, tmp_path / "o.docx", target="docx")
        assert res.outcome == "failed"
        assert "bad writer" in res.reason
        assert not (tmp_path / "o.docx").exists()   # garbage never stays

    @needs_lo
    @needs_pandoc
    def test_lo_timeout_mid_write_is_not_a_conversion(self, engine, md_source,
                                                      tmp_path, monkeypatch):
        from pathlib import Path
        from types import SimpleNamespace
        import transmute.engines.document as D

        docx = tmp_path / "s.docx"
        assert engine.convert(md_source, docx, target="docx").outcome == "converted"

        def fake_run(cmd, **kw):
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            # valid magic, half-written body - exactly the trap
            (outdir / "s.odt").write_bytes(b"PK\x03\x04half written")
            return SimpleNamespace(ok=False, timed_out=True, returncode=-9,
                                   stderr_text="", stdout_text="")

        monkeypatch.setattr(D, "run_command", fake_run)
        res = engine.convert(docx, tmp_path / "o.odt", target="odt")
        assert res.outcome == "failed"
        assert "timed out mid-write" in res.reason
        assert not (tmp_path / "o.odt").exists()

    def test_truncated_zip_is_not_a_valid_office_file(self, engine, tmp_path):
        # hermetic: no external tool decides this one
        half = tmp_path / "half.docx"
        half.write_bytes(b"PK\x03\x04garbage beyond the header")
        msg = engine._invalid_output(half, "docx")
        assert msg and "not a valid DOCX" in msg

        import zipfile
        good = tmp_path / "good.docx"
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("[Content_Types].xml", "<?xml version='1.0'?>")
        assert engine._invalid_output(good, "docx") is None
