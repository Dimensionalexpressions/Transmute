"""GUI watch-panel wiring (P7 live-test fix: watch/auto-convert was CLI-only).

Offscreen Qt; the real FolderWatcher is replaced by a fake - its semantics
are proven in test_watch.py. What is proven HERE is the GUI's role as the
safe interface: dialogs rescan mounts (SPEC F.3), settled files reach the
queue on the GUI thread only, honest refusals stay visible, and window close
stops the watcher threads.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", exc_type=ImportError)

from pathlib import Path  # noqa: E402

from PySide6.QtWidgets import QFileDialog, QApplication  # noqa: E402

from transmute.gui.controller import ConverterController  # noqa: E402
from transmute.watch import WatchFolder  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def qapp():
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

    def run(self, **_kw):
        import types
        return types.SimpleNamespace(processed=0, counts={}, results=[])


class _FakeWatcher:
    """FolderWatcher API surface the GUI uses. Mirrors its real refusal:
    dst == watched root would re-ingest outputs (ValueError)."""
    instances: list["_FakeWatcher"] = []

    def __init__(self, *, on_settled, quiet_s=2.0, poll_s=0.25):
        self.on_settled = on_settled
        self.folders: list[tuple[Path, Path]] = []
        self.started = 0
        self.stopped = 0
        _FakeWatcher.instances.append(self)

    def add_folder(self, src, dst):
        src, dst = Path(src), Path(dst)
        if src == dst:
            raise ValueError(
                f"destination is the watched folder ({src}) — outputs would "
                "be re-ingested; pick a separate destination tree")
        self.folders.append((src, dst))

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def fake_watchers(monkeypatch):
    _FakeWatcher.instances = []
    monkeypatch.setattr("transmute.watch.FolderWatcher", _FakeWatcher)
    return _FakeWatcher


@pytest.fixture
def window(qapp, tmp_path):
    from transmute.gui.app import MainWindow
    from transmute.paths import AppPaths
    from test_gui_controller import _report
    # isolated prefs: the panel PERSISTS watch rules, so an unpersistent
    # controller would leak test paths into the developer's real file
    root = AppPaths(tmp_path / "root")
    root.ensure()
    ctrl = ConverterController(report_provider=_report,
                               queue=_RecordingQueue(), paths=root)
    ctrl._report = _report()
    win = MainWindow(controller=ctrl)
    yield win
    win.close()


NONNATIVE = QFileDialog.Option.DontUseNativeDialog


class _ExecRecorder:
    """Patch QFileDialog.exec: record every dialog the app opens, then answer
    with the queued codes (Rejected unless given). The dialog objects stay
    inspectable afterwards - options, mode, sidebar - exactly what the bug
    needs pinned: a volume mounted AFTER start must be pickable."""

    def __init__(self, monkeypatch, answers=()):
        self.opened: list[QFileDialog] = []
        self._answers = list(answers)
        from PySide6.QtWidgets import QDialog
        self._Accepted = QDialog.DialogCode.Accepted
        self._Rejected = QDialog.DialogCode.Rejected
        # a deterministic "selection" for accepted dialogs (the offscreen
        # file-view model would only make this test flakier, not stricter)
        monkeypatch.setattr(QFileDialog, "selectedFiles",
                            lambda self: ["/tmp"])

        def fake_exec(dlg):
            self.opened.append(dlg)
            return self._answers.pop(0) if self._answers else self._Rejected
        monkeypatch.setattr(QFileDialog, "exec", fake_exec)


class TestDialogsRescanMounts:
    """User-reported stale drive in Add files: every picker is the Qt dialog
    (rebuilt per open) whose sidebar is re-seeded from /proc/mounts each time -
    Qt's own mount/volume caches go stale for late mounts (VeraCrypt reports
    no D-Bus change at all)."""

    def test_add_files_dialog_is_nonnative_existing_files(self, window,
                                                          monkeypatch):
        rec = _ExecRecorder(monkeypatch)
        window._add_dialog()
        assert len(rec.opened) == 1
        dlg = rec.opened[0]
        assert dlg.options() & NONNATIVE
        assert dlg.fileMode() == QFileDialog.FileMode.ExistingFiles

    def test_destination_dialog_is_nonnative_directory_mode(self, window,
                                                            monkeypatch):
        from PySide6.QtWidgets import QDialog
        rec = _ExecRecorder(monkeypatch, answers=[QDialog.DialogCode.Accepted])
        window.dst_edit.setText("")
        window._choose_dst()
        assert len(rec.opened) == 1
        dlg = rec.opened[0]
        assert dlg.options() & NONNATIVE
        assert dlg.fileMode() == QFileDialog.FileMode.Directory
        assert window.dst_edit.text() == "/tmp"

    def test_watch_dialog_pair_nonnative_second_cancel_means_no_rule(
            self, window, monkeypatch):
        from PySide6.QtWidgets import QDialog
        rec = _ExecRecorder(
            monkeypatch,
            answers=[QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected])
        added = []
        monkeypatch.setattr(window, "_watch_add",
                            lambda s, d: added.append((s, d)))
        window._watch_add_dialog()
        assert len(rec.opened) == 2
        assert all(d.options() & NONNATIVE for d in rec.opened)
        assert all(d.fileMode() == QFileDialog.FileMode.Directory
                   for d in rec.opened)
        assert added == []  # second dialog cancelled -> no rule (F3: no guess)

    def test_sidebar_includes_volume_mounted_after_start(self, window,
                                                         monkeypatch,
                                                         tmp_path):
        # stand-in for /run/media/veracrypt11 appearing mid-session
        vol = tmp_path / "veracrypt11"
        vol.mkdir()
        monkeypatch.setattr("transmute.gui.app._live_volume_roots",
                            lambda: [str(vol)])
        rec = _ExecRecorder(monkeypatch)
        window._add_dialog()
        assert len(rec.opened) == 1
        from PySide6.QtCore import QUrl
        assert QUrl.fromLocalFile(str(vol)) in rec.opened[0].sidebarUrls()

    def test_mount_table_parsing_edges(self):
        from transmute.gui.app import _mount_roots_from_table
        table = "\n".join([
            "/dev/sda1 /run/media/veracrypt11 ext4 rw 0 0",
            "/dev/sdb1 /run/media/kieros/My\\040Disk ext4 rw 0 0",
            "/dev/sdc1 /tmp/.veracrypt_aux_mnt1 fuse rw 0 0",
            "overlay /run/media/u/vol/sub ext4 rw 0 0",
            "/dev/sdd1 /mnt/data xfs rw 0 0",
            "/dev/sde1 /mnt ext4 rw 0 0",
            "tmpfs /media/kieros/Fedora\\011USB tmpfs rw 0 0",
            "proc /proc proc rw 0 0",
        ])
        assert _mount_roots_from_table(table) == [
            "/run/media/veracrypt11",  # the reported case
            "/run/media/kieros/My Disk",  # \040 = space
            "/mnt/data",
            "/media/kieros/Fedora\tUSB",  # \011 = tab
        ]



class TestWatchRules:
    def test_add_starts_one_watcher_and_lists_rule(self, window, fake_watchers,
                                                   tmp_path):
        src, dst = tmp_path / "in", tmp_path / "out"
        window._watch_add(src, dst)
        assert len(fake_watchers.instances) == 1
        w = fake_watchers.instances[0]
        assert w.started == 1 and w.folders == [(src, dst)]
        assert window.watch_list.count() == 1
        assert str(src) in window.watch_list.item(0).text()
        assert "watching 1" in window.lbl_watch.text()

    def test_bad_rule_refused_visibly(self, window, fake_watchers, tmp_path):
        same = tmp_path / "in"
        window._watch_add(same, same)
        # refusal lands on the status label (F3), no watcher left half-alive
        assert window.lbl_watch.text().startswith("⚠")
        assert window._watcher is None
        assert window.watch_list.count() == 0

    def test_second_rule_joins_running_watcher(self, window, fake_watchers,
                                               tmp_path):
        window._watch_add(tmp_path / "a", tmp_path / "oa")
        window._watch_add(tmp_path / "b", tmp_path / "ob")
        assert len(fake_watchers.instances) == 1  # one watcher, two roots
        assert fake_watchers.instances[0].started == 1
        assert window.watch_list.count() == 2
        assert "watching 2" in window.lbl_watch.text()

    def test_remove_rebuilds_without_it(self, window, fake_watchers, tmp_path):
        window._watch_add(tmp_path / "a", tmp_path / "oa")
        window._watch_add(tmp_path / "b", tmp_path / "ob")
        window.watch_list.setCurrentRow(0)
        window._watch_remove_selected()
        old = fake_watchers.instances[0]
        assert old.stopped == 1  # old observer threads joined (no leak)
        assert len(fake_watchers.instances) == 2
        assert fake_watchers.instances[1].folders == [
            (tmp_path / "b", tmp_path / "ob")]
        assert window.watch_list.count() == 1

    def test_close_stops_watcher(self, qapp, fake_watchers, tmp_path):
        from transmute.gui.app import MainWindow
        from test_gui_controller import _report
        ctrl = ConverterController(report_provider=_report,
                                   queue=_RecordingQueue())
        ctrl._report = _report()
        win = MainWindow(controller=ctrl)
        win._watch_add(tmp_path / "in", tmp_path / "out")
        win.close()
        assert fake_watchers.instances[0].stopped == 1


class TestSettledFeedIsThreadSafe:
    """The watcher thread may only ring the bell (SimpleQueue); the GUI
    thread drains, displays, and pumps the worker."""

    def test_note_lines_land_in_log_and_pump_once(self, window, monkeypatch):
        started = []
        monkeypatch.setattr(window, "_start_worker", lambda: started.append(1))
        window._watch_note("queued clip.mp4 → clip.av1 (video/av1)")
        window._watch_note("skipped raw.xyz: unsupported file type")
        assert window.watch_log.count() == 0  # not before the drain
        window._drain_watch()
        texts = [window.watch_log.item(i).text()
                 for i in range(window.watch_log.count())]
        assert texts[0].startswith("queued clip.mp4")
        assert texts[1].startswith("skipped raw.xyz")
        assert started == [1]  # queued jobs -> exactly one worker pass
        assert not window._watch_more  # flag consumed, no double pump

    def test_skip_lines_never_start_worker(self, window, monkeypatch):
        started = []
        monkeypatch.setattr(window, "_start_worker", lambda: started.append(1))
        window._watch_note("skipped a.xyz: no encoder verified")
        window._drain_watch()
        assert started == []
        assert not window._watch_more

    def test_settled_file_real_callback_end_to_end(self, window,
                                                   fake_watchers, tmp_path):
        """Full loop with the REAL make_queue_callback: a settled file that
        the watcher hands over lands in the queue AND the note feed."""
        src, dst = tmp_path / "in", tmp_path / "out"
        window._watch_add(src, dst)
        cb = fake_watchers.instances[0].on_settled
        clip = src / "sub" / "clip.mp4"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"x")
        cb(WatchFolder(src, dst), clip)
        # note() ran on 'this' thread; events wait in the SimpleQueue...
        assert window.watch_log.count() == 0
        window._drain_watch()
        line = window.watch_log.item(0).text()
        assert line.startswith("queued clip.mp4")
        assert window._watch_more or window._worker  # pump fired or ran
        w = window.ctrl.queue
        assert w.added, "settled file never reached the queue"
        assert w.added[0][0] == clip  # src preserved, not renamed


class _FakeWatcherStrict(_FakeWatcher):
    """Also mirrors the real 'watch root is not a directory' refusal, so the
    persistence tests see the same failure an unplugged drive causes."""

    def add_folder(self, src, dst):
        src = Path(src)
        if not src.is_dir():
            raise ValueError(f"watch root is not a directory: {src}")
        super().add_folder(src, dst)


class TestWatchFoldersSurviveRestart:
    """A watch folder is a standing arrangement: rules persist, resume on
    relaunch, and a missing (unplugged) folder is stated - never dropped."""

    def _ctrl(self, tmp_path):
        from transmute.paths import AppPaths
        from test_gui_controller import _report
        root = AppPaths(tmp_path / "root")
        root.ensure()
        return ConverterController(report_provider=_report,
                                   queue=_RecordingQueue(), paths=root)

    def _window(self, qapp, monkeypatch, ctrl):
        monkeypatch.setattr("transmute.watch.FolderWatcher",
                            _FakeWatcherStrict)
        _FakeWatcher.instances = []
        from transmute.gui.app import MainWindow
        return MainWindow(controller=ctrl)

    def test_add_persists_remove_unpersists(self, qapp, monkeypatch, tmp_path):
        ctrl = self._ctrl(tmp_path)
        win = self._window(qapp, monkeypatch, ctrl)
        try:
            a, b = tmp_path / "inbox", tmp_path / "out"
            a.mkdir(); b.mkdir()
            win._watch_add(a, b)
            assert ctrl.prefs()["watch_folders"] == [[str(a), str(b)]]
            win.watch_list.setCurrentRow(0)
            win._watch_remove_selected()
            assert ctrl.prefs()["watch_folders"] == []
            assert win.watch_list.count() == 0
        finally:
            win.close()

    def test_saved_rules_resume_at_startup(self, qapp, monkeypatch, tmp_path):
        ctrl = self._ctrl(tmp_path)
        a, b = tmp_path / "inbox", tmp_path / "out"
        c, d = tmp_path / "cam", tmp_path / "camout"
        for p in (a, b, c, d):
            p.mkdir()
        ctrl.set_pref("watch_folders", [[str(a), str(b)], [str(c), str(d)]])
        win = self._window(qapp, monkeypatch, ctrl)
        try:
            watching = {tuple(map(str, f)) for w in _FakeWatcher.instances
                        for f in w.folders}
            assert watching == {(str(a), str(b)), (str(c), str(d))}
            assert any(w.started for w in _FakeWatcher.instances)
            texts = [win.watch_list.item(i).text()
                     for i in range(win.watch_list.count())]
            assert not any("missing" in t for t in texts)
            assert "resumed" in win.lbl_watch.text()
            # rules themselves unchanged: resuming must not rewrite prefs
            assert ctrl.prefs()["watch_folders"] == [[str(a), str(b)],
                                                     [str(c), str(d)]]
        finally:
            win.close()

    def test_missing_folder_listed_not_dropped_then_resumable(
            self, qapp, monkeypatch, tmp_path):
        ctrl = self._ctrl(tmp_path)
        gone = tmp_path / "usb"      # NOT created: the drive is out
        out = tmp_path / "out"
        out.mkdir()
        ctrl.set_pref("watch_folders", [[str(gone), str(out)]])
        win = self._window(qapp, monkeypatch, ctrl)
        try:
            assert win.watch_list.count() == 1
            assert "(folder missing)" in win.watch_list.item(0).text()
            assert "not here" in win.lbl_watch.text()
            assert ctrl.prefs()["watch_folders"] == [[str(gone), str(out)]]
            # drive comes back: picking the SAME folder re-arms that rule
            gone.mkdir()
            win._watch_add(gone, out)
            assert "resumed" in win.lbl_watch.text()
            assert "(folder missing)" not in win.watch_list.item(0).text()
            assert any(tuple(map(str, f)) == (str(gone), str(out))
                       for w in _FakeWatcher.instances for f in w.folders)
        finally:
            win.close()

    def test_duplicate_add_says_already_watching(self, qapp, monkeypatch,
                                                 tmp_path):
        ctrl = self._ctrl(tmp_path)
        a, b = tmp_path / "inbox", tmp_path / "out"
        a.mkdir(); b.mkdir()
        win = self._window(qapp, monkeypatch, ctrl)
        try:
            win._watch_add(a, b)
            win._watch_add(a, b)
            assert "already watching" in win.lbl_watch.text()
            assert ctrl.prefs()["watch_folders"] == [[str(a), str(b)]]
        finally:
            win.close()

    def test_junk_rules_skipped_not_fatal(self, qapp, monkeypatch, tmp_path):
        ctrl = self._ctrl(tmp_path)
        a, b = tmp_path / "inbox", tmp_path / "out"
        a.mkdir(); b.mkdir()
        ctrl.set_pref("watch_folders", ["nope", [str(a)], None,
                                        [str(a), str(b)]])
        win = self._window(qapp, monkeypatch, ctrl)
        try:
            assert win.watch_list.count() == 1  # only the valid pair survives
            assert any(w.folders for w in _FakeWatcher.instances)
        finally:
            win.close()
