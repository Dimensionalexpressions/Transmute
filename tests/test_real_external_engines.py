"""P5 real coverage for qpdf and pandoc (e2e tier).

These run ONLY on machines that actually have the binaries — which is what
the CI matrix provides (apt installs qpdf/pandoc/texlive). On a bench without
them they skip, and the skip says exactly what ran where. Doctrine: real
binaries, real outputs, engine verify() as the judge — no mocking here
(mocked ladders live in the unit tier).
"""
from __future__ import annotations

import shutil

import pytest

pytestmark = [pytest.mark.e2e]

qpdf = pytest.mark.skipif(shutil.which("qpdf") is None,
                          reason="qpdf not on this machine (CI installs it)")


@qpdf
def test_qpdf_rung_real_binary_compresses_and_pixel_verifies(tmp_path, fixtures):
    from transmute.engines.pdf import PdfEngine
    eng = PdfEngine()
    src = fixtures["pdf"]
    dst = tmp_path / "out.pdf"
    err = eng._run("qpdf", src, dst, timeout_s=120, governor=None,
                   log_path=tmp_path / "qpdf.log")
    assert err is None, f"qpdf rung failed: {err}"
    assert dst.stat().st_size > 0
    import pikepdf
    with pikepdf.open(src) as a, pikepdf.open(dst) as b:
        assert len(a.pages) == len(b.pages) == 4
    # the engine's own proof: structure + page-pixel identity (F24)
    v = eng.verify(src, dst)
    assert v.ok, f"verify: {v.reason}"


@qpdf
def test_qpdf_engine_reports_rung_available(engine_info):
    from transmute.engines.pdf import PdfEngine
    rung = {name: (ok, why) for name, ok, why in PdfEngine().ladder()}
    assert rung["qpdf"][0] is True, rung["qpdf"][1]


pandoc = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="pandoc not on this machine (CI installs it)")
latex = pytest.mark.skipif(
    all(shutil.which(w) is None for w in ("pdflatex", "xelatex", "lualatex")),
    reason="no LaTeX writer for pandoc's PDF output (CI installs texlive)")


@pandoc
@latex
def test_pandoc_markdown_to_pdf_real(tmp_path):
    from transmute.engines.document import DocumentEngine
    eng = DocumentEngine()
    writer = eng._latex_writer()
    assert writer is not None
    src = tmp_path / "note.md"
    src.write_text("# Transmute\n\nHello **pandoc** world.\n", encoding="utf-8")
    dst = tmp_path / "note.pdf"
    res = eng._via_pandoc(src, dst, writer, timeout_s=180, governor=None,
                          log_path=None, t0=0.0)
    assert res.outcome == "converted", res.reason
    assert dst.stat().st_size > 1000  # a real PDF, not an empty stub
    import pikepdf
    with pikepdf.open(dst) as pdf:
        assert len(pdf.pages) >= 1
    assert writer in res.encoder  # encoder string reports what really ran
