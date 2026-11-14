"""Controller unit tests (P2): pure logic, NO Qt, NO display, NO engines
beyond the fake report. The GUI must never invent capability - every picker
row must trace back to a matrix row (F3), ladders must come from verified
probe results (F10), and enqueued options must match engine signatures."""
from __future__ import annotations

import time

import pytest

from transmute.gui.controller import ConverterController
from transmute.probe.capability import (
    CapabilityReport, EncoderCapability, MatrixRow,
)

pytestmark = pytest.mark.unit


def _report() -> CapabilityReport:
    matrix = [
        MatrixRow(category="video", target="av1", path="ffmpeg -> libsvtav1",
                  available=True, verified=True, recommended=True,
                  note="measured 500 fps"),
        MatrixRow(category="video", target="h264", path="ffmpeg -> libx264",
                  available=True, verified=True, compat_flag=True),
        MatrixRow(category="video", target="hevc", path="nvenc (probe failed)",
                  available=False, hw=True, note="trial encode failed"),
        MatrixRow(category="audio", target="opus", path="ffmpeg -> libopus",
                  available=True, verified=True, recommended=True),
        MatrixRow(category="image", target="webp", path="pillow",
                  available=True, verified=True, recommended=True),
        MatrixRow(category="image", target="avif", path="pillow",
                  available=False, note="AVIF plugin missing"),
        MatrixRow(category="pdf", target="pdf (lossless smaller)",
                  path="pikepdf -> qpdf -> ghostscript", available=True,
                  recommended=True),
        MatrixRow(category="pdf", target="pdf (verify renders)",
                  path="pdftoppm", available=True),   # diagnostic: never a target
        MatrixRow(category="document", target="pdf", path="libreoffice",
                  available=True, recommended=True),
    ]
    enc = [
        EncoderCapability(name="libsvtav1", codec="av1", backend="cpu",
                          hw=False, verified=True, fps=500.0),
        EncoderCapability(name="libaom-av1", codec="av1", backend="cpu",
                          hw=False, verified=True, fps=140.0),
        EncoderCapability(name="libx264", codec="h264", backend="cpu",
                          hw=False, verified=True, fps=760.0),
    ]
    return CapabilityReport(schema=1, probe_key="k", generated_at=time.time(),
                            app_version="t", machine={}, engines={},
                            encoders=enc, filters={}, gpu_nodes={},
                            image_backends={}, pdf_backends={}, caveats=[],
                            matrix=matrix)


class _FakeQueue:
    """Captures what the controller asks of the real queue."""

    def __init__(self):
        self.added = []
        self.journal = type("J", (), {"records": {}})()

    def add(self, src, dst, *, category=None, options=None):
        self.added.append((src, dst, category, options))
        return type("R", (), {"job_id": "j1", "src": str(src), "dst": str(dst),
                              "state": "queued", "reason": None,
                              "encoder": None, "attempts": 0})()


@pytest.fixture
def ctrl():
    return ConverterController(report_provider=_report, queue=_FakeQueue())


class TestPickers:
    def test_targets_trace_matrix_exactly(self, ctrl):
        got = [(t.target, t.available) for t in ctrl.targets("video")]
        assert got == [("av1", True), ("h264", True), ("hevc", False)]

    def test_diagnostic_rows_are_not_targets(self, ctrl):
        names = [t.target for t in ctrl.targets("pdf")]
        assert names == ["pdf (lossless smaller)"]  # verify-render row excluded

    def test_unavailable_carries_machine_reason(self, ctrl):
        hevc = next(t for t in ctrl.targets("video") if t.target == "hevc")
        assert not hevc.available and "trial encode failed" in hevc.reason
        avif = next(t for t in ctrl.targets("image") if t.target == "avif")
        assert "AVIF plugin missing" in avif.reason

    def test_badges(self, ctrl):
        av1 = next(t for t in ctrl.targets("video") if t.target == "av1")
        h264 = next(t for t in ctrl.targets("video") if t.target == "h264")
        assert av1.label.endswith("✓") and not av1.compat_flag
        assert h264.label.endswith("ℹ️") and h264.compat_flag

    def test_default_target_is_first_available(self, ctrl):
        assert ctrl.default_target("video").target == "av1"
        assert ctrl.default_target("image").target == "webp"

    def test_no_engines_no_default(self):
        empty = CapabilityReport(schema=1, probe_key="k", generated_at=0.0,
                                 app_version="t", machine={}, engines={},
                                 encoders=[], filters={}, gpu_nodes={},
                                 image_backends={}, pdf_backends={},
                                 caveats=[], matrix=[])
        c = ConverterController(report_provider=lambda: empty)
        assert c.default_target("video") is None
        assert c.targets("video") == []


class TestLadder:
    def test_ladder_fastest_first_verified_only(self, ctrl):
        assert ctrl.encoders("video", "av1") == ["libsvtav1", "libaom-av1"]

    def test_non_codec_targets_have_no_ladder(self, ctrl):
        assert ctrl.encoders("image", "webp") == []
        assert ctrl.encoders("pdf", "pdf (lossless smaller)") == []


class TestEnqueue:
    def test_containers_and_suffixes(self, ctrl):
        assert ctrl.dst_for("/x/a.mkv", "video", "av1", "/out").name == "a.mkv"
        assert ctrl.dst_for("/x/a.mkv", "video", "vp9", "/out").name == "a.webm"
        assert ctrl.dst_for("/x/a.mp3", "audio", "opus", "/out").name == "a.opus"
        assert ctrl.dst_for("/x/a.jpg", "image", "webp", "/out").name == "a.webp"
        assert ctrl.dst_for("/x/a.webp", "image", "webp(animated)",
                            "/out").name == "a.webp"
        assert ctrl.dst_for("/x/a.txt", "document", "pdf", "/out").name == "a.pdf"
        assert ctrl.dst_for("/x/a.pdf", "pdf", "pdf (lossless smaller)",
                            "/out").name == "a.pdf"

    def test_options_match_engine_signatures(self, ctrl):
        # video/audio take codec+encoder, image quality, pdf deep flag ONLY
        assert ctrl.options_for("video", "av1", encoder="libsvtav1") == \
            {"codec": "av1", "encoder": "libsvtav1"}
        assert ctrl.options_for("audio", "opus") == {"codec": "opus"}
        assert ctrl.options_for("image", "webp", quality=72) == {"quality": 72}
        assert ctrl.options_for("pdf", "pdf (lossless smaller)") == {}  # lossless default
        assert ctrl.options_for("pdf", "pdf (lossless smaller)",
                                deep_pdf=True) == {"deep": True}

    def test_enqueue_passes_everything_down(self, ctrl):
        info = ctrl.enqueue("/x/a.jpg", "/out", "image", "webp", quality=63)
        src, dst, cat, opts = ctrl._queue.added[0]
        assert (str(src), str(dst), cat, opts) == \
            ("/x/a.jpg", "/out/a.webp", "image", {"quality": 63})
        assert info.state == "queued"


class TestCategoryRouting:
    def test_category_of(self, ctrl):
        assert ctrl.category_of("a.mp4") == "video"
        assert ctrl.category_of("a.flac") == "audio"
        assert ctrl.category_of("a.jpg") == "image"
        assert ctrl.category_of("a.pdf") == "pdf"
        assert ctrl.category_of("a.odt") == "document"
        assert ctrl.category_of("a.xyz123") is None

    def test_hdr_unknown_without_ffprobe(self, tmp_path, monkeypatch):
        # no exception may leak, ever; absent ffprobe -> None not False
        c = ConverterController(report_provider=_report)
        assert c.hdr_source(tmp_path / "ghost.mp4") is None


class TestRealImageEndToEnd:
    """Offscreen, zero external engines: controller -> real JobQueue ->
    Pillow image engine -> verify -> replaced output on disk (F1/F5 core)."""

    def test_jpg_to_webp_through_controller(self, tmp_path, fixtures):
        from transmute.paths import AppPaths
        from transmute.runner.queue import JobQueue
        root = tmp_path / "root"
        paths = AppPaths(root=root)
        q = JobQueue(paths=paths)
        c = ConverterController(report_provider=_report, queue=q)
        out = tmp_path / "out"
        c.enqueue(fixtures["jpg"], out, "image", "webp", quality=70)
        summary = c.process()
        assert summary.counts.get("replaced", 0) == 1
        assert (out / "noise.webp").exists()
        snap = c.snapshot()
        assert len(snap) == 1 and snap[0].state == "replaced"
        assert snap[0].encoder  # engine identity recorded for audit
