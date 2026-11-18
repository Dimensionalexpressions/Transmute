"""Qt widget smoke tests (P2) - run with QT_QPA_PLATFORM=offscreen, no X11
needed. Uses only PySide6's own QtTest (pytest-qt is NOT in the approved
dependency list, so we drive QApplication by hand).

Widget tests assert only WIRING (picker mirrors controller, disabled rows
stay visible with reason tooltips, Run passes UI state into the queue).
All decision logic is already proven in test_gui_controller.py.
"""
from __future__ import annotations

import os
import time

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
        assert any("hevc" in l.lower() for l in labels)   # NOT hidden (F3)
        idx = next(i for i in range(window.tgt_box.count())
                   if "hevc" in _model_item(window.tgt_box, i).text().lower())
        it = _model_item(window.tgt_box, idx)
        assert not (it.flags() & Qt.ItemFlag.ItemIsEnabled)
        # the WHY is now in the LABEL itself (real-user feedback, SPEC F.2);
        # the machine's full words stay in the tooltip
        assert "not available here" in it.text().lower()
        assert "trial encode failed" in it.toolTip()

    def test_first_available_selected_by_default(self, window):
        window.cat_box.setCurrentText("video")
        cur = _model_item(window.tgt_box, window.tgt_box.currentIndex())
        assert cur.isEnabled()
        assert cur.data(Qt.ItemDataRole.UserRole) == "av1"

    def test_encoder_ladder_populated_fastest_first(self, window):
        window.cat_box.setCurrentText("video")
        # av1 is row 0 and enabled. Raw ids ride as item DATA (engine
        # contract); the visible text is human words (SPEC F.1).
        assert [window.enc_box.itemData(i)
                for i in range(window.enc_box.count())] == \
            ["", "libsvtav1", "libaom-av1"]
        assert window.enc_box.itemText(0) == "auto (fastest verified)"
        assert window.enc_box.itemText(1) == "SVT-AV1 - fast AV1"
        assert "libsvtav1" not in window.enc_box.itemText(1)

    def test_encoder_note_is_meaning_not_raw_id(self, window):
        """Live-test finding: the note line showed 'ffmpeg/libaom-av1' -
        meaningless AND it implied auto uses the static row, not the verified
        ladder. Text = what the choice MEANS; raw ids only in the tooltip."""
        window.cat_box.setCurrentText("video")
        assert window.enc_box.currentIndex() == 0  # auto
        assert "fastest verified encoder" in window.lbl_note.text()
        assert "SVT-AV1" in window.lbl_note.text()  # ladder winner, human
        assert "libsvtav1" not in window.lbl_note.text()
        assert "libaom-av1" not in window.lbl_note.text()
        window.enc_box.setCurrentIndex(2)  # explicit override
        assert window.lbl_note.text().startswith("Using AOM reference")
        assert "libaom-av1" not in window.lbl_note.text()
        assert "ffmpeg ->" in window.lbl_note.toolTip()  # raw chain, tooltip

    def test_no_ladder_for_image_targets(self, window):
        window.cat_box.setCurrentText("image")
        assert window.enc_box.count() == 0
        assert not window.enc_box.isEnabled()

    def test_badges_render_in_labels(self, window):
        window.cat_box.setCurrentText("video")
        texts = [_model_item(window.tgt_box, i).text().lower()
                 for i in range(window.tgt_box.count())]
        assert any("h.264" in t and "(!)" in t for t in texts)  # F14
        # (!) replaces the emoji badge: it rendered as a broken box on some
        # desktop fonts; full patent note stays in the item tooltip.
        assert any("av1" in t and "✓" in t for t in texts)

    def test_estimate_line_shown_for_added_file(self, window, monkeypatch,
                                                tmp_path):
        """User directive: with files chosen, the panel must state measured
        time and output size - numbers from this PC, never table guesses."""
        from PySide6.QtTest import QTest
        monkeypatch.setattr(window, "_current_target", lambda: "h264")
        monkeypatch.setattr(window, "_target_info", lambda c, t: None)
        monkeypatch.setattr(
            window.ctrl, "conversion_estimate",
            lambda *a, **k: {"ok": True, "est_s": 62.0, "bytes": 5e6,
                             "size_is_target": False, "sec": 2.0,
                             "note": ""})
        window.cat_box.setCurrentText("video")
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        window._add_one(clip)
        window._est_run()  # bypass the debounce timer
        t0 = time.time()
        while time.time() - t0 < 10:
            QTest.qWait(50)
            if (window.lbl_est.isVisible()
                    and "Estimate for this file" in window.lbl_est.text()):
                break
        assert "output about 5 MB" in window.lbl_est.text()
        # the measured size also shows inline by the quality slider
        assert "about 5 MB" in window.lbl_size.text()

    def test_advanced_size_label_re_measures_on_slider_move(
            self, window, monkeypatch, tmp_path):
        """User directive: EVERY movement of the quality selector shows the
        estimated size, parent-product style - and here the number is
        re-MEASURED (a fresh call at the new setting), never recomputed."""
        from PySide6.QtTest import QTest
        seen = []

        def fake(cat, target, src, **kw):
            seen.append(kw)
            return {"ok": True, "est_s": 10.0, "bytes": kw["crf"] * 1e6,
                    "size_is_target": False, "sec": 2.0, "note": ""}

        monkeypatch.setattr(window, "_current_target", lambda: "h264")
        monkeypatch.setattr(window, "_target_info", lambda c, t: None)
        monkeypatch.setattr(window.ctrl, "conversion_estimate", fake)
        window.cat_box.setCurrentText("video")
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        window._add_one(clip)
        window._est_run()
        t0 = time.time()
        while time.time() - t0 < 10 and "about 23 MB" not in window.lbl_size.text():
            QTest.qWait(50)
        assert "about 23 MB" in window.lbl_size.text()
        # drag the slider to coarser quality: measuring… at once, then a
        # smaller measured size from a call made at the NEW crf
        window.qslider.setValue(34)
        assert window.lbl_size.text() == "measuring…"
        window._est_run()
        t0 = time.time()
        while time.time() - t0 < 10 and "about 34 MB" not in window.lbl_size.text():
            QTest.qWait(50)
        assert "about 34 MB" in window.lbl_size.text()
        assert seen[-1]["crf"] == 34
        # fine-tuning knobs ride along so the measurement matches the plan
        assert seen[-1]["preset"] == "medium"
        assert seen[-1]["audio_mode"] == "copy"

    def test_slider_row_shows_which_way_is_better(self, window):
        """User feedback: an unlabeled quality slider is a guess - the ends
        must say worse / better."""
        assert window.lbl_crf_worse.text() == "worse"
        assert window.lbl_crf_better.text() == "better"

    def test_new_measurement_supersedes_the_previous(self, window,
                                                     monkeypatch, tmp_path):
        """Workstation must not lock up: every new measurement cancels the
        one before it (stop Event fires), so superseded samples stop burning
        CPU instead of stacking."""
        seen = []

        def fake(cat, target, src, **kw):
            seen.append(kw)
            return {"ok": True, "est_s": 1.0, "bytes": 1e6,
                    "size_is_target": False, "sec": 1.0, "note": ""}

        monkeypatch.setattr(window, "_current_target", lambda: "h264")
        monkeypatch.setattr(window, "_target_info", lambda c, t: None)
        monkeypatch.setattr(window.ctrl, "conversion_estimate", fake)
        window.cat_box.setCurrentText("video")
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        window._add_one(clip)
        window._est_run()
        window._est_run()  # second movement replaces the first measurement
        assert len(seen) >= 1
        first_stop = seen[0].get("stop")
        assert first_stop is not None, "estimate must receive a stop Event"
        assert first_stop.is_set(), "superseded measurement was never cancelled"
        if len(seen) > 1:
            assert seen[1]["stop"] is not first_stop
            assert not seen[1]["stop"].is_set()

    def test_gpu_switch_persists_and_remeasures(self, window, monkeypatch):
        """User directive: the GPU switch lives in More… and off means the
        report must stop claiming GPU support - so toggling re-measures."""
        calls = []
        # stub the TRIGGER (the real one spawns a measuring thread - no
        # racing a worker inside a unit test)
        monkeypatch.setattr(window, "_start_reprobe", lambda: calls.append(1))
        monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
        # drive from a known state: outer pytest env may have started the
        # switch off already, and toggling to the SAME state fires no signal
        window.chk_gpu.setChecked(True)
        calls.clear()
        window.chk_gpu.setChecked(False)
        assert window.ctrl.prefs().get("use_gpu") is False
        assert os.environ.get("TRANSMUTE_DISABLE_GPU") == "1"
        assert calls == [1]
        window.chk_gpu.setChecked(True)
        assert window.ctrl.prefs().get("use_gpu") is True
        assert "TRANSMUTE_DISABLE_GPU" not in os.environ
        assert calls == [1, 1]


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
        assert not window.qslider.isEnabled()
        assert not window.preset_box.isEnabled()
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


class TestQueueControlsP5:
    """Pause/Resume/Cancel-pending buttons on a REAL JobQueue: buttons are
    pure wiring; the doctrine (recoverable vs terminal, in-flight untouched)
    is proven in test_pause_cancel.py at the queue level."""

    @pytest.fixture
    def qwin(self, qapp, tmp_path, fixtures):
        from transmute.gui.app import MainWindow
        from transmute.jsonl import JsonlWriter
        from transmute.runner import JobQueue, Journal
        from test_gui_controller import _report
        jq = JobQueue(journal=Journal(tmp_path / "journal.jsonl"),
                      events=JsonlWriter(tmp_path / "events.jsonl"),
                      logs_root=tmp_path / "captures",
                      engine_get=lambda cat: None)
        ctrl = ConverterController(report_provider=_report, queue=jq)
        ctrl._report = _report()
        win = MainWindow(controller=ctrl)
        for n in ("a", "b"):
            jq.add(fixtures["jpg"], tmp_path / "out" / f"{n}.webp",
                   category="image")
        yield win, jq
        win.close()

    def test_pause_button_parks_jobs_and_flips_controls(self, qwin):
        win, jq = qwin
        win.btn_pause.click()
        assert all(r.state == "paused" for r in jq.journal.records.values())
        assert win.btn_resume.isEnabled() and not win.btn_pause.isEnabled()
        assert "Paused 2" in win.lbl_status.text()
        assert not jq.journal.pending()          # nothing auto-resumes itself

    def test_resume_button_restores_and_rearms(self, qwin):
        win, jq = qwin
        win.btn_pause.click()
        win.btn_resume.click()
        assert all(r.state == "queued" for r in jq.journal.records.values())
        assert win.btn_pause.isEnabled() and not win.btn_resume.isEnabled()
        assert "Resumed 2" in win.lbl_status.text()

    def test_cancel_button_abandons_with_visible_reason(self, qwin):
        win, jq = qwin
        win.btn_cancel.click()
        rows = [(r.state, r.reason) for r in jq.journal.records.values()]
        assert all(s == "abandoned" and "cancelled by user" in rs
                   for s, rs in rows)
        # the table actually shows it — cancel must never be silent
        assert win.table.rowCount() == 2
        assert all(win.table.item(i, 2).text() == "abandoned" for i in range(2))
        assert "Cancelled 2" in win.lbl_status.text()


class TestAdaptivePanel:
    """Live-user rule: the panel adapts to the selection - audio must not
    show PDF/video rows. TARGETS keep grey-with-reason (F3); controls that
    mean nothing for the current category/target are hidden (F3 covers
    unsupported targets, not irrelevant controls)."""

    @staticmethod
    def _pick(win, cat, target=None):
        win.cat_box.setCurrentIndex(win.cat_box.findText(cat))
        if target:
            j = win.tgt_box.findData(target)
            assert j >= 0, f"fake report lacks target {target!r}"
            win.tgt_box.setCurrentIndex(j)

    @staticmethod
    def _hidden(win, *names):
        for n in names:
            assert not getattr(win, n).isVisibleTo(win), f"{n} shown"

    def test_audio_shows_no_video_or_pdf_controls(self, window):
        self._pick(window, "audio")
        self._hidden(window, "chk_remux", "deep_pdf", "quality", "rad_crf",
                     "chk_tonemap", "btn_adv", "adv_frame")

    def test_video_shows_only_video_controls(self, window):
        self._pick(window, "video")
        assert window.chk_remux.isVisibleTo(window)
        assert window.rad_crf.isVisibleTo(window)
        assert window.size_box.isVisibleTo(window)
        assert window.chk_tonemap.isVisibleTo(window)
        self._hidden(window, "deep_pdf", "quality")

    def test_remux_reveals_container_picker_and_nothing_else(self, window):
        self._pick(window, "video")
        assert not window.remux_box.isVisibleTo(window)
        window.chk_remux.setChecked(True)
        assert window.remux_box.isVisibleTo(window)
        window.chk_remux.setChecked(False)
        assert not window.remux_box.isVisibleTo(window)

    def test_image_shows_quality_and_nothing_video(self, window):
        self._pick(window, "image")
        assert window.quality.isVisibleTo(window)
        self._hidden(window, "chk_remux", "chk_tonemap")

    def test_pdf_deep_pass_only_for_the_lossless_target(self, window):
        self._pick(window, "pdf", "pdf (lossless smaller)")
        assert window.deep_pdf.isVisibleTo(window)
        self._pick(window, "pdf", "png (per page)")
        assert not window.deep_pdf.isVisibleTo(window)

    def test_document_shows_format_menu_only(self, window):
        self._pick(window, "document", "docx")
        self._hidden(window, "chk_remux", "quality", "deep_pdf")
