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
        # qWait spins the event loop so the queued emit is delivered
        from PySide6.QtTest import QTest
        window.cat_box.setCurrentText("image")
        window.dst_edit.setText("/tmp/whatever-out")
        window._add_one(fixtures["jpg"])
        window._run()
        assert not window.btn_run.isEnabled()
        QTest.qWait(400)
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
