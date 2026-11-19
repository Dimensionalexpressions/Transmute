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
        # this test speaks the x264 scale (its numbers 23/34 are CRF values):
        # pin it, like the real app does when the encoder actually is x264
        monkeypatch.setattr(window.ctrl, "quality_range",
                            lambda c, t: (0, 51, 23))
        window._sync_options_enabled()
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

    def test_device_pick_persists_and_remeasures(self, window, monkeypatch):
        """User directive: the named CPU/GPU picker replaces the old GPU
        switch; pinning the CPU means the report must stop claiming GPU
        support - so any pick re-measures this machine."""
        calls = []
        # stub the TRIGGER (the real one spawns a measuring thread - no
        # racing a worker inside a unit test)
        monkeypatch.setattr(window, "_start_reprobe", lambda: calls.append(1))
        monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
        # drive from a known state: outer pytest env may have started with
        # the CPU pin already active from an earlier test
        window.device_box.setCurrentIndex(window.device_box.findData("auto"))
        calls.clear()
        window.device_box.setCurrentIndex(window.device_box.findData("cpu"))
        assert window.ctrl.device() == "cpu"
        assert os.environ.get("TRANSMUTE_DISABLE_GPU") == "1"
        assert calls == [1]
        window.device_box.setCurrentIndex(window.device_box.findData("auto"))
        assert window.ctrl.device() == "auto"
        assert "TRANSMUTE_DISABLE_GPU" not in os.environ
        assert calls == [1, 1]

    def test_device_picker_lists_this_machine_not_placeholders(self, window):
        """Zero hardware assumptions: every entry is a device read from
        THIS machine (Auto + the CPU under its real model name)."""
        datas = [window.device_box.itemData(i)
                 for i in range(window.device_box.count())]
        assert datas[0] == "auto"
        assert "cpu" in datas
        i = datas.index("cpu")
        assert window.device_box.itemText(i).startswith("CPU - ")
        # real model name, not "your processor" filler or an empty label
        assert len(window.device_box.itemText(i)) > len("CPU - ") + 3

    def test_device_pin_scopes_encoders_and_estimates(self, window,
                                                      monkeypatch):
        """Pinning the CPU must narrow the encoder list to CPU-verified
        paths and resolve 'auto' TO a concrete encoder, so queue jobs and
        estimates provably belong to the chosen device."""
        monkeypatch.setattr(window, "_start_reprobe", lambda: None)
        window.cat_box.setCurrentText("video")  # av1: ladder is all-cpu rows
        window.device_box.setCurrentIndex(window.device_box.findData("cpu"))
        assert [window.enc_box.itemData(i)
                for i in range(window.enc_box.count())] == \
            ["", "libsvtav1", "libaom-av1"]
        assert window._effective_encoder() == "libsvtav1"  # auto -> pinned
        window.device_box.setCurrentIndex(window.device_box.findData("auto"))
        assert window._effective_encoder() == ""  # engine auto again

    def test_switching_devices_drops_other_vendors_encoder_now(
            self, qapp, monkeypatch):
        """The user's screenshot (2026-11): Encoder held "GPU - NVIDIA av1"
        while CPU/GPU switched from the RTX 5090 to the CPU - and it STAYED
        held for the whole re-benchmark, so a job would have encoded on the
        GPU they had just deselected ("i see a logic issue, nvidia and
        being able to select cpu"). A pick must drop other vendors'
        encoders from the effective plan the same instant, not after
        measuring; the measurement then refines the ladder."""
        from transmute.gui.app import MainWindow
        from transmute.probe.capability import EncoderCapability
        from test_gui_controller import _report
        rep = _report()
        rep.encoders = list(rep.encoders) + [
            EncoderCapability(name="av1_nvenc", codec="av1",
                              backend="nvidia-nvenc", hw=True,
                              verified=True, fps=59.0),
        ]
        ctrl = ConverterController(report_provider=lambda: rep,
                                   queue=_RecordingQueue())
        ctrl._report = rep
        # fixed inventory: one CPU, one NVIDIA GPU - the contradiction must
        # not depend on what box the suite runs on
        monkeypatch.setattr(ctrl, "devices", lambda: [
            dict(id="cpu", kind="cpu", name="Test CPU", backends=["cpu"],
                 works=True, why="", disabled=False, disabled_kind=""),
            dict(id="gpu:nvidia:0x2b85", kind="gpu", name="Test GPU",
                 backends=["nvidia-nvenc"], works=True, why="",
                 disabled=False, disabled_kind=""),
            dict(id="gpu:amdgpu:0x13c0", kind="gpu", name="Other GPU",
                 backends=["va-api", "amd-amf"], works=True, why="",
                 disabled=False, disabled_kind=""),
        ])
        monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
        win = MainWindow(controller=ctrl)
        monkeypatch.setattr(win, "_start_reprobe", lambda: None)
        try:
            win.cat_box.setCurrentText("video")
            win.device_box.setCurrentIndex(
                win.device_box.findData("gpu:nvidia:0x2b85"))
            win._load_encoders(0)
            assert [win.enc_box.itemData(i)
                    for i in range(win.enc_box.count())] == ["", "av1_nvenc"]
            win.enc_box.setCurrentIndex(1)
            assert win._effective_encoder() == "av1_nvenc"
            # the pick that used to keep contradicting: CPU, this instant
            win.device_box.setCurrentIndex(
                win.device_box.findData("cpu"))
            datas = [win.enc_box.itemData(i)
                     for i in range(win.enc_box.count())]
            assert "av1_nvenc" not in datas      # no re-benchmark needed
            assert win._effective_encoder() == "libsvtav1"
            assert "NVIDIA" not in win.lbl_note.text()
            # belt AND braces: even a hand-stale selection cannot leak
            win.enc_box.clear()
            win.enc_box.addItem("stale", "av1_nvenc")
            assert win._effective_encoder() == "libsvtav1"
            # a device that WORKS but not for THIS target must offer a way
            # out, not empty parentheses ("...for this here ().") - the
            # Radeon + AV1 case on the user's box
            win.device_box.setCurrentIndex(
                win.device_box.findData("gpu:amdgpu:0x13c0"))
            assert "()" not in win.lbl_note.text()
            assert "works on this PC" in win.lbl_note.text()
            # leave the persisted pick as Auto: set_device WRITES prefs, and
            # a fake device id left behind would poison every later window
            # in the session (device() != "auto" resolves auto_encoder)
            win.device_box.setCurrentIndex(
                win.device_box.findData("auto"))
            assert ctrl.device() == "auto"
        finally:
            win.close()


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
        # the selector's CURRENT value rides along - whatever scale the
        # current encoder carries (per-encoder quality ranges)
        assert opts.get("crf") == window.crf_box.value() \
            and "size_mb" not in opts

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
        # cancel is never silent: the status says it, and the abandoned rows
        # fold behind "Show finished" - one click away, never deleted
        assert win.table.rowCount() == 0
        assert "Cancelled 2" in win.lbl_status.text()
        assert win.btn_finished.text() == "Show finished (2)"
        assert win.btn_finished.isVisibleTo(win)
        win.btn_finished.click()                     # reveal the history
        assert win.table.rowCount() == 2
        assert all(win.table.item(i, 2).text() == "abandoned" for i in range(2))

    def test_discard_selected_abandons_only_the_selection(self, qwin):
        win, jq = qwin
        ids = list(jq.journal.records)
        win.refresh_queue()                          # fixture added jobs after build
        win.table.selectRow(0)
        win.btn_discard.click()
        recs = jq.journal.records
        assert recs[ids[0]].state == "abandoned"
        assert recs[ids[0]].reason == "discarded by user"
        assert recs[ids[1]].state == "queued"       # sibling untouched
        # the discarded row leaves the view at once (auto-hide), the live
        # sibling stays, the source file is NOT deleted...
        assert win.table.rowCount() == 1
        assert win.table.item(0, 2).text() == "queued"
        assert "Discarded 1 job(s) — source files untouched." in \
            win.lbl_status.text()
        # ...and the history is one click away, never deleted
        win.btn_finished.click()
        assert win.table.rowCount() == 2
        assert sorted(win.table.item(i, 2).text() for i in range(2)) == \
            ["abandoned", "queued"]

    def test_discard_autohides_even_while_finished_are_shown(self, qwin):
        # "A + auto-hide": even with history on display, the row a Discard
        # click abandons leaves the table NOW; older finished rows stay.
        win, jq = qwin
        ids = list(jq.journal.records)
        first = jq.journal.records[ids[0]]
        jq.journal.transition(first, "abandoned", reason="old history")
        win.refresh_queue()                          # folds the old row away
        assert win.btn_finished.text() == "Show finished (1)"
        win.btn_finished.click()                     # show finished
        assert win.table.rowCount() == 2
        rows_by_id = {win.table.item(i, 0).data(Qt.ItemDataRole.UserRole): i
                      for i in range(2)}
        win.table.selectRow(rows_by_id[ids[1]])
        win.btn_discard.click()
        assert jq.journal.records[ids[1]].state == "abandoned"
        assert win.table.rowCount() == 1             # just-discarded vanished…
        assert win.table.item(0, 2).text() == "abandoned"
        assert win.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == ids[0]
        # old history stays visible
        # an explicit view choice outranks the auto-hide: hide, then show
        # again, and every finished row — including the just-discarded one —
        # is back (the journal never lost it)
        win.btn_finished.click()
        assert win.table.rowCount() == 0
        assert win.btn_finished.text() == "Show finished (2)"
        win.btn_finished.click()
        assert win.table.rowCount() == 2

    def test_discard_leaves_in_flight_rows_alone(self, qwin):
        win, jq = qwin
        rec = next(iter(jq.journal.records.values()))
        jq.journal.transition(rec, "running")       # worker owns this one
        win.refresh_queue()                          # table re-syncs to truth
        win.table.selectAll()
        win.btn_discard.click()
        assert rec.state == "running"               # GUI refused to touch it
        other = [r for r in jq.journal.records.values() if r is not rec]
        assert all(r.state == "abandoned" for r in other)
        assert "left as they were" in win.lbl_status.text()

    def test_discard_without_selection_says_so(self, qwin):
        win, jq = qwin
        win.btn_discard.click()
        assert all(r.state == "queued" for r in jq.journal.records.values())
        assert "Select row(s) to discard" in win.lbl_status.text()

    def test_finished_rows_stay_folded_away_across_restarts(self, qapp,
                                                             tmp_path, fixtures):
        # the reported scene: yesterday's abandoned row must NOT greet the
        # user as live clutter at next boot - journal keeps it, view folds
        # it behind "Show finished" until asked
        from transmute.gui.app import MainWindow
        from transmute.jsonl import JsonlWriter
        from transmute.runner import JobQueue, Journal
        from test_gui_controller import _report
        jq = JobQueue(journal=Journal(tmp_path / "journal.jsonl"),
                      events=JsonlWriter(tmp_path / "events.jsonl"),
                      logs_root=tmp_path / "captures",
                      engine_get=lambda cat: None)
        jq.add(fixtures["jpg"], tmp_path / "drop.webp", category="image")
        jq.cancel_all()                              # abandoned + persisted
        ctrl = ConverterController(report_provider=_report, queue=jq)
        ctrl._report = _report()
        win = MainWindow(controller=ctrl)            # boots like a new session
        try:
            assert win.table.rowCount() == 0         # no zombie clutter
            assert win.btn_finished.text() == "Show finished (1)"
            assert "finished row(s) hidden" in win.lbl_status.text()
            win.btn_finished.click()
            assert win.table.rowCount() == 1
            assert win.table.item(0, 2).text() == "abandoned"
            assert "cancelled by user" in win.table.item(0, 3).text()
        finally:
            win.close()


class TestProbeCaveatsVisible:
    """Machine-wide probe caveats (probe/capability.py) must reach the GUI
    VERBATIM — the doctrine that no machine-level limitation is silent.
    The chip appears only while the machine actually reports something."""

    @staticmethod
    def _window_with_caveats(caveats):
        from transmute.gui.app import MainWindow
        from test_gui_controller import _report
        rep = _report()
        rep.caveats = list(caveats)
        ctrl = ConverterController(report_provider=lambda: rep)
        ctrl._report = rep
        return MainWindow(controller=ctrl), rep

    def test_caveats_render_verbatim(self, qapp):
        cav = ["ffmpeg not found: no codec capability can be probed; "
               "every conversion row explains itself.",
               "GPU probing disabled by TRANSMUTE_DISABLE_GPU."]
        win, _ = self._window_with_caveats(cav)
        try:
            assert win.lbl_caveats.text().splitlines() == cav  # word-for-word
            assert win.lbl_caveats.isVisibleTo(win)
        finally:
            win.close()

    def test_empty_caveats_hides_the_chip(self, qapp):
        win, rep = self._window_with_caveats([])
        try:
            assert not win.lbl_caveats.isVisibleTo(win)
            assert win.lbl_caveats.text() == ""
            # and it appears the moment a later probe reports something
            rep.caveats = ["ffmpeg -encoders failed; capability report is "
                           "partial."]
            win._refresh_caveats()
            assert win.lbl_caveats.isVisibleTo(win)
            assert "partial" in win.lbl_caveats.text()
        finally:
            win.close()


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
                     "chk_tonemap", "adv_header", "adv_frame")

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


class TestQualityScaleFollowsEncoder:
    """Live bug: the slider always offered x264's 0..51. On AV1/SVT-AV1
    crf 0 is a measured COLLAPSE (smaller file than crf 10), so dragging to
    "better" shrank the estimate 900 MB -> 300 MB - backwards. The scale
    now follows the encoder that will actually run."""

    def test_slider_reranges_when_the_encoder_scale_changes(
            self, window, monkeypatch):
        monkeypatch.setattr(window.ctrl, "conversion_estimate",
                            lambda *a, **k: {"ok": False})
        monkeypatch.setattr(
            window.ctrl, "quality_range",
            lambda c, t: (0, 51, 23) if t == "h264" else (2, 63, 30))
        window.cat_box.setCurrentText("video")
        monkeypatch.setattr(window, "_current_target", lambda: "av1")
        window._sync_options_enabled()
        assert (window.crf_box.minimum(), window.crf_box.maximum()) == (2, 63)
        assert (window.qslider.minimum(), window.qslider.maximum()) == (2, 63)
        # the old number belonged to a DIFFERENT scale: never re-mapped
        # silently - restart at what this encoder itself measured
        assert window.crf_box.value() == 30 == window.qslider.value()
        assert "CRF 30" in window.lbl_crf_word.text()
        monkeypatch.setattr(window, "_current_target", lambda: "h264")
        window._sync_options_enabled()
        assert (window.crf_box.minimum(), window.crf_box.maximum()) == (0, 51)
        assert window.crf_box.value() == 23 == window.qslider.value()

    def test_crf_word_judges_position_not_absolute_number(
            self, window, monkeypatch):
        monkeypatch.setattr(window.ctrl, "conversion_estimate",
                            lambda *a, **k: {"ok": False})
        monkeypatch.setattr(window, "_current_target", lambda: "av1")
        monkeypatch.setattr(window.ctrl, "quality_range",
                            lambda c, t: (2, 63, 30))
        window._sync_quality_scale()
        window.crf_box.setValue(2)    # this encoder's floor = best
        assert "near-lossless" in window.lbl_crf_word.text()
        window.crf_box.setValue(30)   # its measured default = balanced
        assert "balanced" in window.lbl_crf_word.text()
        window.crf_box.setValue(55)
        assert "small file" in window.lbl_crf_word.text()

    def test_rav1e_scale_says_qp_not_crf(self, window, monkeypatch):
        # librav1e has no -crf; showing "CRF 100" on a -qp knob is a lie
        monkeypatch.setattr(window.ctrl, "conversion_estimate",
                            lambda *a, **k: {"ok": False})
        monkeypatch.setattr(window, "_current_target", lambda: "av1")
        monkeypatch.setattr(window.ctrl, "quality_range",
                            lambda c, t: (20, 255, 100))
        window._sync_quality_scale()
        assert (window.crf_box.minimum(), window.crf_box.maximum()) == (20, 255)
        assert window.lbl_crf_word.text().startswith("QP 100")


def test_throttle_checkbox_controls_queue_and_persists(window):
    """The governor exists but only ever runs when the user says so:
    the checkbox is the single switch, it detaches the governor from the
    queue immediately, and the choice survives restarts."""
    assert window.chk_throttle.isChecked()          # considerate default
    assert window.ctrl.queue.governor is window._governor
    window.chk_throttle.setChecked(False)
    assert window.ctrl.queue.governor is None
    assert window._governor.enabled is False
    assert window.ctrl.prefs().get("throttle") is False
    window.chk_throttle.setChecked(True)
    assert window.ctrl.queue.governor is window._governor
    assert window._governor.enabled is True
    assert window.ctrl.prefs().get("throttle") is True


def test_throttle_choice_survives_restart(qapp):
    from transmute.gui.app import MainWindow
    from test_gui_controller import _report
    probe = ConverterController(report_provider=_report)
    probe.set_pref("throttle", False)
    win = MainWindow()
    try:
        assert win.chk_throttle.isChecked() is False
        assert win.ctrl.queue.governor is None
    finally:
        win.close()
        # leave the default for any later test that assumes it
        ConverterController(report_provider=_report).set_pref("throttle", True)
