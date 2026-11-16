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
def window(qapp):
    from transmute.gui.app import MainWindow
    from test_gui_controller import _report
    ctrl = ConverterController(report_provider=_report,
                               queue=_RecordingQueue())
    ctrl._report = _report()
    win = MainWindow(controller=ctrl)
    yield win
    win.close()


NONNATIVE = QFileDialog.Option.DontUseNativeDialog


class TestDialogsRescanMounts:
    """User-reported stale drive in Add files: every dialog must be the Qt
    one - the native (portal/KDE) dialog serves cached volume lists."""

    def test_add_files_dialog_is_nonnative(self, window, monkeypatch):
        seen = []

        def fake(parent, caption, directory, *a, **kw):
            seen.append(kw.get("options", 0))
            return [], ""
        monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(fake))
        window._add_dialog()
        assert seen and all(o & NONNATIVE for o in seen)

    def test_destination_dialog_is_nonnative(self, window, monkeypatch):
        seen = []

        def fake(parent, caption, directory, *a, **kw):
            seen.append(kw.get("options", 0))
            return "/tmp/somewhere"
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(fake))
        window._choose_dst()
        assert seen and all(o & NONNATIVE for o in seen)

    def test_watch_dialog_pair_is_nonnative(self, window, monkeypatch):
        seen, calls = [], []

        def fake(parent, caption, directory, *a, **kw):
            seen.append(kw.get("options", 0))
            calls.append(caption)
            return "/tmp/watch-src" if len(calls) == 1 else ""
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(fake))
        added = []
        monkeypatch.setattr(window, "_watch_add",
                            lambda s, d: added.append((s, d)))
        window._watch_add_dialog()
        assert len(seen) == 2 and all(o & NONNATIVE for o in seen)
        assert added == []  # second dialog cancelled -> no rule (F3: no guess)


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
