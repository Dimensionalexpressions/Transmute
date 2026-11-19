"""Performance wish (user-approved Option 2): Auto / Quality-first /
Speed-first. The wish FILTERS the verified encoder ladder - it never
invents capability, and a wish this PC cannot honour falls back to the
fastest verified path with the machine's own reason shown (perf_note).

Also pins the two accessors that were once silently dead:
- gpu_decode_status() must read the report PROPERTY, not call it;
- the decode line must be shown at window construction, not only after a
  device re-pick.
"""
from __future__ import annotations

import time

import pytest

from transmute.gui.controller import ConverterController
from transmute.paths import AppPaths
from transmute.probe.capability import (
    CapabilityReport, EncoderCapability, MatrixRow,
)

pytestmark = pytest.mark.unit


def _enc(name, codec, backend, hw, fps):
    return EncoderCapability(name=name, codec=codec, backend=backend,
                             hw=hw, verified=True, fps=fps)


def _rep(h264, av1, gpu_decode=None) -> CapabilityReport:
    enc, matrix = [], []
    for t, rows in (("h264", h264), ("av1", av1)):
        enc += rows
        matrix.append(MatrixRow(category="video", target=t, path="x",
                                available=True, verified=True))
    rep = CapabilityReport(schema=10, probe_key="k",
                           generated_at=time.time(), app_version="t",
                           machine={}, engines={}, encoders=enc,
                           filters={}, gpu_nodes={}, image_backends={},
                           pdf_backends={}, caveats=[], matrix=matrix)
    if gpu_decode is not None:
        rep.gpu_decode = gpu_decode
    return rep


CPU_GPU = [_enc("h264_nvenc", "h264", "nvidia-nvenc", True, 900.0),
           _enc("libx264", "h264", "cpu", False, 60.0)]
CPU_ONLY = [_enc("libx264", "h264", "cpu", False, 60.0)]
GPU_ONLY = [_enc("h264_nvenc", "h264", "nvidia-nvenc", True, 900.0)]
NO_AV1 = []


@pytest.fixture
def ctrl(tmp_path):
    """Controller with an injected report and an isolated preferences file -
    the same seams the GUI uses, so these tests exercise shipped code paths."""
    paths = AppPaths(root=tmp_path / "root").ensure()

    def make(rep):
        return ConverterController(report_provider=lambda: rep, paths=paths)
    return make


class TestWishFiltersTheLadder:
    def test_auto_keeps_measured_order_gpu_first(self, ctrl):
        c = ctrl(_rep(CPU_GPU, NO_AV1))
        assert c.perf_mode() == "auto"
        assert c.encoders("video", "h264") == ["h264_nvenc", "libx264"]

    def test_quality_keeps_cpu_rows_only(self, ctrl):
        c = ctrl(_rep(CPU_GPU, NO_AV1))
        c.set_perf_mode("quality")
        assert c.encoders("video", "h264") == ["libx264"]
        # auto now MEANS the wish: estimates, queue jobs and the details
        # block all resolve the encoder through these two methods
        assert c.auto_encoder("video", "h264") == "libx264"
        assert c.perf_note("video", "h264") == ""

    def test_speed_keeps_gpu_rows_only(self, ctrl):
        c = ctrl(_rep(CPU_GPU, NO_AV1))
        c.set_perf_mode("speed")
        assert c.encoders("video", "h264") == ["h264_nvenc"]
        assert c.auto_encoder("video", "h264") == "h264_nvenc"
        assert c.perf_note("video", "h264") == ""

    def test_wish_without_capability_falls_back_visibly(self, ctrl):
        # machine that can only do one of the two - the wish must not hide
        # the working path, and must say what happened
        c = ctrl(_rep(CPU_ONLY, NO_AV1))
        c.set_perf_mode("speed")
        assert c.encoders("video", "h264") == ["libx264"]
        assert "no graphics-card encoder" in c.perf_note("video", "h264")

        c = ctrl(_rep(GPU_ONLY, NO_AV1))
        c.set_perf_mode("quality")
        assert c.encoders("video", "h264") == ["h264_nvenc"]
        assert "no working CPU encoder" in c.perf_note("video", "h264")

        c.set_perf_mode("auto")
        assert c.perf_note("video", "h264") == ""

    def test_wish_survives_restart_and_junk_falls_back(self, tmp_path):
        paths = AppPaths(root=tmp_path / "root").ensure()
        rep = _rep(CPU_GPU, NO_AV1)
        c = ConverterController(report_provider=lambda: rep, paths=paths)
        c.set_perf_mode("quality")
        c2 = ConverterController(report_provider=lambda: rep, paths=paths)
        assert c2.perf_mode() == "quality"  # persisted, machine-independent
        c2.set_pref("perf_mode", "ludicrous")  # a future/older build value
        assert c2.perf_mode() == "auto"
        c2.set_perf_mode("ludicrous")  # the setter sanitises too
        assert c2.prefs()["perf_mode"] == "auto"


class TestDecodeStatusReadsTheReport:
    """gpu_decode_status once CALLED the report property - the exception was
    swallowed and the GUI line silently never appeared. Pinned for good."""

    def test_status_returns_the_probe_answer(self, ctrl):
        gd = {"attempted": True, "available": True, "api": "cuda",
              "reason": "", "note": "nv12 seen"}
        c = ctrl(_rep(CPU_ONLY, NO_AV1, gpu_decode=gd))
        assert c.gpu_decode_status() == gd

    def test_status_empty_when_probe_absent(self, ctrl):
        c = ctrl(_rep(CPU_ONLY, NO_AV1))  # old-style report: no field
        assert c.gpu_decode_status() == {}


class TestPerformanceControlInTheWindow:
    @pytest.fixture
    def qapp(self):
        # same pattern as test_gui_widgets: one QApplication per process
        from PySide6.QtWidgets import QApplication
        return QApplication.instance() or QApplication([])

    @pytest.fixture
    def win(self, qapp, tmp_path, monkeypatch):
        from transmute.gui.app import MainWindow
        gd = {"attempted": True, "available": True, "api": "cuda",
              "reason": ""}
        paths = AppPaths(root=tmp_path / "root").ensure()
        c = ConverterController(report_provider=lambda: _rep(
            CPU_GPU, NO_AV1, gpu_decode=gd), paths=paths)
        w = MainWindow(controller=c)
        yield w
        w.close()

    def test_control_offers_the_three_modes(self, win):
        assert [win.perf_box.itemData(i) for i in range(win.perf_box.count())] \
            == ["auto", "quality", "speed"]
        assert win.perf_box.currentData() == "auto"

    def test_decode_line_visible_at_startup(self, win):
        # construction must not leave the proven-GPU line hidden
        # (isHidden: the window itself is never shown in offscreen tests)
        assert not win.lbl_dec.isHidden()
        assert "NVDEC" in win.lbl_dec.text()

    def test_picking_quality_rebuilds_ladder_and_persists(self, win):
        from PySide6.QtCore import Qt
        win.cat_box.setCurrentText("video")
        for k in range(win.tgt_box.count()):
            if win.tgt_box.model().item(k).data(
                    Qt.ItemDataRole.UserRole) == "h264":
                if win.tgt_box.currentIndex() != k:
                    win.tgt_box.setCurrentIndex(k)
                break
        assert win._current_target() == "h264"

        def items():
            return [win.enc_box.itemData(k)
                    for k in range(win.enc_box.count())]

        assert "libx264" in items()  # both paths offered under Auto
        win.perf_box.setCurrentIndex(win.perf_box.findData("quality"))
        assert win.ctrl.perf_mode() == "quality"
        assert items() == ["", "libx264"]  # "" is "auto (fastest verified)"
        # and speed offers the GPU row; the decode line still tells the truth
        win.perf_box.setCurrentIndex(win.perf_box.findData("speed"))
        assert items() == ["", "h264_nvenc"]
        assert not win.lbl_dec.isHidden()
