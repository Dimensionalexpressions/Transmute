"""Qt widget smoke tests (P2) - run with QT_QPA_PLATFORM=offscreen, no X11
needed. Uses only PySide6's own QtTest (pytest-qt is NOT in the approved
dependency list, so we drive QApplication by hand).

Widget tests assert only WIRING (picker mirrors controller, disabled rows
stay visible with reason tooltips, Run passes UI state into the queue).
All decision logic is already proven in test_gui_controller.py.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# exc_type=ImportError: skip not only when PySide6 is absent
# (headless-only install) but also when it is installed yet broken
# (missing platform plugin on a bare box) - pytest>=8.2; default
# (pytest>=9.1) would turn the broken case into a collection error.
pytest.importorskip("PySide6", exc_type=ImportError)

from PySide6.QtCore import Qt  # noqa: E402

from transmute.gui.controller import ConverterController  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _RecordingQueue:
    def __init__(self):
        self.added = []
        import types
        self.journal = types.SimpleNamespace(records={})

    def add(self, src, dst, *, category=None, options=None):
        self.added.append((src, dst, category, options))
        import types
        return types.SimpleNamespace(job_id="j", src=str(src), dst=str(dst),
                                     state="queued", reason=None,
                                     encoder=None, attempts=0)

    def run(self, **_kw):  # ctrl.process() calls this on the worker thread
        import types
        return types.SimpleNamespace(processed=0, counts={}, results=[])


@pytest.fixture
def window(qapp):
    import types as _t
    from transmute.gui.app import MainWindow
    from test_gui_controller import _report  # noqa: F401 (fake matrix)
    ctrl = ConverterController(report_provider=_report,
                               queue=_RecordingQueue())
    ctrl._report = _report()
    win = MainWindow(controller=ctrl)
    yield win
    win.close()


def _model_item(box, idx):
    return box.model().item(idx)


class TestPickersMirrorTheReport:
    def test_unavailable_target_visible_but_disabled_with_reason(self, window):
        window.cat_box.setCurrentText("video")
        labels = [_model_item(window.tgt_box, i).text()
                  for i in range(window.tgt_box.count())]
        assert any("hevc" in l for l in labels)      # NOT hidden (F3)
        idx = next(i for i in range(window.tgt_box.count())
                   if "hevc" in _model_item(window.tgt_box, i).text())
        it = _model_item(window.tgt_box, idx)
        assert not (it.flags() & Qt.ItemFlag.ItemIsEnabled)
        assert "trial encode failed" in it.toolTip()

    def test_first_available_selected_by_default(self, window):
        window.cat_box.setCurrentText("video")
        cur = _model_item(window.tgt_box, window.tgt_box.currentIndex())
        assert cur.isEnabled()
        assert cur.data(Qt.ItemDataRole.UserRole) == "av1"

    def test_encoder_ladder_populated_fastest_first(self, window):
        window.cat_box.setCurrentText("video")
        # av1 is row 0 and enabled
        encs = [window.enc_box.itemText(i)
                for i in range(window.enc_box.count())]
        assert encs == ["auto (fastest verified)", "libsvtav1", "libaom-av1"]

    def test_no_ladder_for_image_targets(self, window):
        window.cat_box.setCurrentText("image")
        assert window.enc_box.count() == 0
        assert not window.enc_box.isEnabled()

    def test_badges_render_in_labels(self, window):
        window.cat_box.setCurrentText("video")
        texts = [_model_item(window.tgt_box, i).text()
                 for i in range(window.tgt_box.count())]
        assert any("h264" in t and "ℹ️" in t for t in texts)  # F14
        assert any("av1" in t and "✓" in t for t in texts)


class TestFileList:
    def test_unsupported_type_disabled(self, window, tmp_path):
        ghost = tmp_path / "a.xyz123"
        ghost.write_bytes(b"x")
        window._add_one(ghost)
        it = window.files.item(0)
        assert "unsupported" in it.text()
        assert not (it.flags() & Qt.ItemFlag.ItemIsEnabled)

    def test_duplicate_not_added_twice(self, window, fixtures):
        window._add_one(fixtures["jpg"])
        window._add_one(fixtures["jpg"])
        assert window.files.count() == 1


class TestRunWiring:
    def test_run_enqueues_ui_state(self, window, fixtures):
        window.cat_box.setCurrentText("image")
        window.quality.setValue(61)
        window.dst_edit.setText("/tmp/whatever-out")
        window._add_one(fixtures["jpg"])
        window._run()
        assert len(window.ctrl._queue.added) == 1
        _, dst, cat, opts = window.ctrl._queue.added[0]
        assert cat == "image" and opts == {"quality": 61}
        assert str(dst).endswith(".webp")

    def test_run_button_reenabled_via_queued_signal(self, window, fixtures):
        # worker completion must cross threads the safe way (Signal, queued);
        # qWait spins the event loop so the queued emit is delivered.
        # Wait on the CONDITION with a deadline, never a fixed sleep: under
        # parallel suite load the thread object can retire after the queued
        # signal is delivered (observed as a flake at 400 ms).
        import time
        from PySide6.QtTest import QTest
        window.cat_box.setCurrentText("image")
        window.dst_edit.setText("/tmp/whatever-out")
        window._add_one(fixtures["jpg"])
        window._run()
        assert not window.btn_run.isEnabled()
        deadline = time.monotonic() + 10.0
        while (not window.btn_run.isEnabled() or window._worker.is_alive()) \
                and time.monotonic() < deadline:
            QTest.qWait(50)
        assert window.btn_run.isEnabled()
        assert window._worker.is_alive() is False

    def test_hdr_unknown_shows_notice_not_error(self, window, monkeypatch,
                                                fixtures, tmp_path):
        mp4 = tmp_path / "x.mp4"
        mp4.write_bytes(b"x")
        monkeypatch.setattr(window.ctrl, "hdr_source", lambda p: None)
        window._add_one(mp4)
        assert "ffprobe missing" in window.lbl_hdr.text() or \
            window.lbl_hdr.text() == ""   # either is honest; no exception


class TestP3VideoControls:
    """Wiring of remux / tone-map / rate-control / preview (decisions are
    proven in test_gui_p3.py + test_video_p3.py; here: widget behaviour)."""

    def test_remux_toggle_disables_codec_choice(self, window):
        window.cat_box.setCurrentText("video")
        assert window.enc_box.isEnabled()      # av1 ladder from fake report
        window.chk_remux.setChecked(True)
        assert not window.enc_box.isEnabled()  # stream copy: no encoder (F2)
        assert not window.tgt_box.isEnabled()
        assert not window.crf_box.isEnabled()
        assert not window.size_box.isEnabled()
        window.chk_remux.setChecked(False)
        assert window.tgt_box.isEnabled()

    def test_run_remux_sends_only_the_remux_option(self, window, fixtures,
                                                   monkeypatch):
        monkeypatch.setattr(window.ctrl, "remux_block_reason",
                            lambda src, ext: "")
        window.cat_box.setCurrentText("video")
        window.chk_remux.setChecked(True)
        window.remux_box.setCurrentText(".mp4")
        window.dst_edit.setText("/tmp/remux-out")
        window._add_one(fixtures["mp4"])
        window._run()
        assert len(window.ctrl._queue.added) == 1
        _, dst, cat, opts = window.ctrl._queue.added[0]
        assert cat == "video"
        assert opts == {"remux": True}          # nothing else rides along
        assert str(dst).endswith(".mp4")        # container from remux picker
        assert "✓" in window.lbl_remux.text()   # machine said copy is safe

    def test_remux_blocked_reason_shown_from_machine(self, window, fixtures,
                                                     monkeypatch):
        monkeypatch.setattr(
            window.ctrl, "remux_block_reason",
            lambda src, ext: "'vp9' cannot be copied into '.mp4' losslessly")
        window.cat_box.setCurrentText("video")
        window.chk_remux.setChecked(True)
        window._add_one(fixtures["mp4"])
        assert "cannot be copied" in window.lbl_remux.text()  # F3 visible

    def test_tonemap_greyed_when_probe_says_no(self, window, monkeypatch):
        # fake report has no probed tonemap_chain -> greyed WITH the reason
        window.cat_box.setCurrentText("video")
        assert not window.chk_tonemap.isEnabled()
        assert "probed" in window.chk_tonemap.toolTip()
        monkeypatch.setattr(type(window.ctrl), "tonemap_available",
                            property(lambda self: True))
        window._sync_options_enabled()
        assert window.chk_tonemap.isEnabled()
        assert window.chk_tonemap.toolTip() == ""

    def test_hdr_notice_advertises_tonemap_only_when_real(self, window,
                                                          monkeypatch,
                                                          fixtures):
        monkeypatch.setattr(window.ctrl, "hdr_source", lambda p: True)
        window.cat_box.setCurrentText("video")
        monkeypatch.setattr(type(window.ctrl), "tonemap_available",
                            property(lambda self: False))
        window._add_one(fixtures["mp4"])
        assert "lacks the tone-map chain" in window.lbl_hdr.text()
        monkeypatch.setattr(type(window.ctrl), "tonemap_available",
                            property(lambda self: True))
        window._update_hdr(fixtures["mp4"])
        assert "Tone-map HDR to SDR" in window.lbl_hdr.text()

    def test_size_target_radio_and_enqueue(self, window, fixtures):
        window.cat_box.setCurrentText("video")
        window.rad_size.setChecked(True)
        assert window.size_box.isEnabled() and not window.crf_box.isEnabled()
        window.size_box.setValue(350.0)
        window.dst_edit.setText("/tmp/size-out")
        window._add_one(fixtures["mp4"])
        window._run()
        _, _, _, opts = window.ctrl._queue.added[0]
        assert opts.get("size_mb") == 350.0 and "crf" not in opts

    def test_crf_path_wins_when_radio_back(self, window, fixtures):
        window.cat_box.setCurrentText("video")
        window.rad_size.setChecked(True)
        window.rad_crf.setChecked(True)
        assert window.crf_box.isEnabled() and not window.size_box.isEnabled()
        window.dst_edit.setText("/tmp/crf-out")
        window._add_one(fixtures["mp4"])
        window._run()
        _, _, _, opts = window.ctrl._queue.added[0]
        assert opts.get("crf") == 23 and "size_mb" not in opts

    def test_files_of_other_category_are_not_enqueued(self, window, fixtures):
        window.cat_box.setCurrentText("image")
        window.dst_edit.setText("/tmp/cat-out")
        window._add_one(fixtures["mp4"])        # video file, image category
        window._run()
        assert window.ctrl._queue.added == []   # nothing silently mismuxed

    def test_preview_button_shows_picture_or_honest_note(self, window,
                                                         fixtures,
                                                         monkeypatch, tmp_path):
        import base64
        png = tmp_path / "f.png"
        png.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        window._add_one(fixtures["mp4"])
        window.files.setCurrentRow(0)
        assert window.btn_prev.isEnabled()      # current item is a video
        monkeypatch.setattr(window.ctrl, "first_frame", lambda src: png)
        window._preview()
        pm = window.lbl_prev.pixmap()
        assert pm is not None and not pm.isNull()
        monkeypatch.setattr(window.ctrl, "first_frame", lambda src: None)
        window._preview()
        assert "preview unavailable" in window.lbl_prev.text()
