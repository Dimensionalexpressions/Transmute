"""Image -> PDF (Phase A, last item): one picture becomes one page.

Everything asserted here is behaviour MEASURED on this machine's Pillow
build, not believed from docs: raw RGBA embeds as lossy JPEG-2000 and its
alpha lands on a SECOND page (so alpha flattens onto white), a PDF may
honestly outgrow the picture (container change - the savings gate must not
"keep" the original), and Pillow cannot open PDFs as pictures so the verify
path checks %PDF magic + pikepdf pages like the PDF writer does.
Availability of the target still comes only from the probe (F3).
"""
from __future__ import annotations

import time

import pytest

from transmute.engines.image import ImageEngine
from transmute.gui.controller import ConverterController
from transmute.probe.capability import CapabilityReport, MatrixRow, build_matrix

pytestmark = pytest.mark.unit

PIL = pytest.importorskip("PIL")
pikepdf = pytest.importorskip("pikepdf")


@pytest.fixture
def eng():
    return ImageEngine()


def _pic(tmp_path, name="in.png", mode="RGB", color=(200, 30, 30)):
    from PIL import Image
    p = tmp_path / name
    Image.new(mode, (64, 48), color).save(p)
    return p


def _pages(path):
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def test_picture_becomes_one_pdf_page(eng, tmp_path):
    src = _pic(tmp_path, name="in.jpg")
    dst = tmp_path / "out.pdf"
    res = eng.convert(src, dst)
    assert res.outcome == "converted", res.reason
    assert dst.read_bytes()[:5] == b"%PDF-"
    assert _pages(dst) == 1


def test_transparency_flattens_to_white_single_page(eng, tmp_path):
    """Measured regression: writing RGBA straight produced a second page."""
    src = _pic(tmp_path, mode="RGBA", color=(200, 30, 30, 120))
    dst = tmp_path / "out.pdf"
    res = eng.convert(src, dst)
    assert res.outcome == "converted", res.reason
    assert _pages(dst) == 1


def test_pdf_may_honestly_grow_still_converts(eng, tmp_path):
    """8x8 png is bytes; its PDF page is bigger. A container change is not
    compression - the 'kept original' savings gate must not fire here."""
    from PIL import Image
    src = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(src)
    dst = tmp_path / "out.pdf"
    res = eng.convert(src, dst)
    assert res.outcome == "converted", res.reason
    assert dst.stat().st_size > src.stat().st_size  # grew - and that is fine


def test_verify_accepts_written_pdf_and_refuses_fakes(eng, tmp_path):
    src = _pic(tmp_path)
    dst = tmp_path / "out.pdf"
    assert eng.convert(src, dst).outcome == "converted"
    good = eng.verify(src, dst)
    assert good.outcome == "converted" and good.data.get("pages") == 1
    bogus = tmp_path / "fake.pdf"
    bogus.write_bytes(b"%PDF-1.7 but not really a file")
    bad = eng.verify(src, bogus)
    assert bad.outcome == "failed" and "unreadable" in bad.reason
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not even close")
    bad2 = eng.verify(src, junk)
    assert bad2.outcome == "failed" and "not a PDF" in bad2.reason


def test_animated_input_single_first_frame_page(eng, tmp_path):
    """Documented behaviour: the picker says one page per picture - an
    animated file is that page, its first frame (verified pages == 1)."""
    from PIL import Image
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (32, 32), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100)
    dst = tmp_path / "out.pdf"
    res = eng.convert(src, dst)
    assert res.outcome == "converted", res.reason
    assert _pages(dst) == 1


def test_pdf_write_capability_is_measured_not_assumed():
    from transmute.probe.capability import _probe_pillow
    feats = _probe_pillow()["features"]
    assert feats.get("pdf") is True  # this build just wrote one, in memory


def _matrix_for(pdf_ok: bool):
    rep = CapabilityReport(schema=1, probe_key="k", generated_at=time.time(),
                           app_version="t", machine={}, engines={}, encoders=[],
                           filters={}, gpu_nodes={},
                           image_backends={"features": {"png": True, "pdf": pdf_ok}},
                           pdf_backends={}, caveats=[], matrix=[])
    return build_matrix(rep)


def test_matrix_row_follows_the_probe_feature():
    rows = _matrix_for(True)
    row = next(r for r in rows if r.category == "image" and r.target == "pdf (page)")
    assert row.available and "transparency" in row.note
    row2 = next(r for r in _matrix_for(False)
                if r.category == "image" and r.target == "pdf (page)")
    assert not row2.available and "cannot write PDF" in row2.note


def test_picker_words_and_suffix_without_colliding_with_document_pdf():
    def report():
        return CapabilityReport(
            schema=1, probe_key="k", generated_at=time.time(), app_version="t",
            machine={}, engines={}, encoders=[], filters={}, gpu_nodes={},
            image_backends={}, pdf_backends={}, caveats=[],
            matrix=[
                MatrixRow(category="image", target="pdf (page)", path="Pillow/PDF",
                          available=True),
                MatrixRow(category="document", target="pdf", path="libreoffice",
                          available=True),
            ])
    c = ConverterController(report_provider=report)
    img = next(t for t in c.targets("image") if t.target == "pdf (page)")
    assert img.available and img.label.startswith("PDF page")
    # the document category's own "pdf" target keeps its own words
    doc = next(t for t in c.targets("document") if t.target == "pdf")
    assert "PDF page" not in doc.label
    assert c.suffix_for("image", "pdf (page)") == ".pdf"
    assert c.dst_for("/x/a.png", "image", "pdf (page)", "/out").name == "a.pdf"


def test_kept_reason_says_bigger_when_output_grows(tmp_path, fixtures):
    # The savings gate must state the DIRECTION honestly: an encode that is
    # larger than the original used to print "only -94.7% smaller"
    # (WinBoat VM battery, 2026-11-27 - noise fixture webp > png).
    from transmute.engines.image import ImageEngine
    dst = tmp_path / "grown.png"
    r = ImageEngine().convert(fixtures["jpg"], dst)
    assert r.outcome == "kept"
    assert not dst.exists()
    assert "bigger" in r.reason and "-" not in r.reason
    pct = float(r.reason.split()[3].rstrip("%"))
    assert pct > 0
