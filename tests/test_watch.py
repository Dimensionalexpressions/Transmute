"""F24 watch-folder tests — REAL kernel events (inotify via watchdog), real
threads. ROADMAP P4 gate: partial-file behaviour and rename storms included.
Doctrine in tests: never sleep-and-assert; poll conditions with a deadline."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from transmute.watch import FolderWatcher
from transmute.watch import WatchFolder
from transmute.watch import main as watch_main

pytestmark = pytest.mark.unit  # hermetic (kernel FS events, no external engines)

QUIET = 0.2   # measured stability window under test
POLL = 0.04   # sample interval under test
DEADLINE = 15.0


def _wait_until(fn, *, what: str, timeout: float = DEADLINE):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.03)
    pytest.fail(f"condition not met in {timeout}s: {what}")


class Harness:
    """Start/stop a watcher around test bodies; record callbacks safely.
    Callbacks run on the poll thread; list.append is atomic, that is enough."""

    def __init__(self, on_settled=None, quiet_s=QUIET):
        self.got: list[Path] = []
        cb = on_settled or (lambda folder, path: self.got.append(path))
        self.w = FolderWatcher(on_settled=cb, quiet_s=quiet_s, poll_s=POLL)

    def watch(self, src, dst):
        return self.w.add_folder(src, dst)

    def start(self):
        self.w.start()

    def stop(self):
        self.w.stop()

    def settle_n(self):
        """Wall time for one full quiet window plus scheduler slack."""
        time.sleep(3 * QUIET + 10 * POLL)


# ---------------------------------------------------------------- happy paths
def test_drop_file_gets_dispatched_once(tmp_path):
    src = tmp_path / "watch"; src.mkdir()
    h = Harness(); h.watch(src, tmp_path / "out"); h.start()
    try:
        f = src / "holiday.mp4"
        f.write_bytes(b"A" * 100)
        _wait_until(lambda: len(h.got) == 1, what="settled file dispatches")
        assert h.got[0] == f
        h.settle_n()
        assert h.got == [f]  # exactly once — no re-dispatch while idle
    finally:
        h.stop()


def test_writer_in_two_chunks_only_final_content_matters(tmp_path):
    """Partial-write protection is MEASURED: mid-write files stay pending.
    quiet 1.5s >> 0.3s chunk gap, so a mid-write dispatch is impossible."""
    src = tmp_path / "watch"; src.mkdir()
    seen_bytes: list[int] = []
    h = Harness(on_settled=lambda folder, p: (seen_bytes.append(len(p.read_bytes())),
                                              h.got.append(p)), quiet_s=1.5)
    h.watch(src, tmp_path / "out"); h.start()
    try:
        f = src / "clip.mp4"
        f.write_bytes(b"x" * 1000)
        time.sleep(0.3)                               # writer pauses briefly
        f.write_bytes(b"x" * 2500)                    # ... and resumes
        _wait_until(lambda: len(h.got) == 1, what="final dispatch after growth")
        assert h.got[0] == f
        assert 1000 not in seen_bytes, f"dispatched a partial file: {seen_bytes}"
        assert seen_bytes == [2500]
    finally:
        h.stop()


def test_zero_byte_file_waits_then_dispatches(tmp_path):
    src = tmp_path / "watch"; src.mkdir()
    h = Harness(); h.watch(src, tmp_path / "out"); h.start()
    try:
        f = src / "fresh.png"
        f.write_bytes(b"")
        h.settle_n()
        assert h.got == [], "empty file must not dispatch — still being written"
        f.write_bytes(b"PNG" * 10)
        _wait_until(lambda: len(h.got) == 1, what="non-empty settles")
        assert h.got[0] == f
    finally:
        h.stop()


# --------------------------------------------------------- partial-name rules
def test_part_suffix_ignored_until_atomic_rename(tmp_path):
    """Downloader pattern: big.mp4.part written, then renamed to final."""
    src = tmp_path / "watch"; src.mkdir()
    h = Harness(); h.watch(src, tmp_path / "out"); h.start()
    try:
        part = src / "big.mp4.part"
        part.write_bytes(b"D" * 500)
        h.settle_n()
        assert h.got == [] and h.w.pending() == [], \
            "partial-download suffix must be ignored outright, not queued"
        final = src / "big.mp4"
        os.rename(part, final)
        _wait_until(lambda: len(h.got) == 1, what="renamed final dispatched")
        assert h.got[0] == final
    finally:
        h.stop()


def test_ignore_rules_hidden_skip_guard_unsupported(tmp_path):
    src = tmp_path / "watch"; (src / ".cache").mkdir(parents=True)
    h = Harness(); h.watch(src, tmp_path / "out"); h.start()
    try:
        (src / ".secret.png").write_bytes(b"p" * 10)              # hidden
        (src / ".cache" / "cached.png").write_bytes(b"c" * 10)    # under hidden dir
        guarded = src / "hold.jpg"
        guarded.write_bytes(b"j" * 10)
        guarded.with_name("hold.jpg.skip").write_bytes(b"")       # .skip guard
        (src / "notes.xyz").write_bytes(b"u" * 10)                # unsupported ext
        control = src / "ok.jpg"
        control.write_bytes(b"k" * 10)
        _wait_until(lambda: h.got == [control], what="only control file dispatches")
        h.settle_n()
        assert h.got == [control]
    finally:
        h.stop()


# ------------------------------------------------------------------- storms
def test_rename_storm_no_dupes_no_strays(tmp_path):
    """30 files move in and out rapidly; 12 stay. Storm must produce exactly
    the 12 settled files, each once, and the watcher must stay alive."""
    src = tmp_path / "watch"; src.mkdir()
    away = tmp_path / "away"; away.mkdir()
    h = Harness(); h.watch(src, tmp_path / "out"); h.start()
    try:
        churn = []
        for i in range(30):  # moved in then straight back out — no dispatch
            p = src / f"storm{i}.mp4"
            p.write_bytes(bytes([i]) * 50)
            churn.append(p)
        kept = [src / f"final{i}.mp4" for i in range(12)]
        for p in kept:
            p.write_bytes(b"K" * 60)
        for p in churn:  # atomic renames away as fast as the FS allows
            os.rename(p, away / p.name)
        _wait_until(lambda: len(h.got) == 12, what="all 12 kept files dispatch")
        h.settle_n()
        assert sorted(h.got) == sorted(kept), "storm produced wrong dispatch set"
        assert len(set(map(str, h.got))) == len(h.got), "duplicate dispatch!"
        assert h.w.errors == []
        assert h.w._poll_thread is not None and h.w._poll_thread.is_alive()
    finally:
        h.stop()


def test_deleted_while_pending_never_dispatches(tmp_path):
    """Deterministic race: long quiet window guarantees the file is pending
    when it is removed, so this is not a timing flake."""
    src = tmp_path / "watch"; src.mkdir()
    h = Harness(quiet_s=5.0); h.watch(src, tmp_path / "out"); h.start()
    try:
        f = src / "ghost.mp4"
        f.write_bytes(b"g" * 40)
        _wait_until(lambda: any(str(p) == str(f) for p in h.w.pending()),
                    what="file enters settle-pending", timeout=5)
        os.remove(f)
        time.sleep(1.0)  # far longer than a poll interval; quiet window is 5s
        assert h.got == [], "deleted-while-pending file must not dispatch"
        assert h.w.pending() == []
    finally:
        h.stop()


# ------------------------------------------------------------- output hygiene
def test_outputs_in_dst_tree_are_not_reingested(tmp_path):
    """dst inside src is legal (mirror subfolder); its contents are outputs."""
    src = tmp_path / "watch"
    dst = src / "transmuted"
    src.mkdir(); dst.mkdir()
    h = Harness(); h.watch(src, dst); h.start()
    try:
        (dst / "already.webp").write_bytes(b"W" * 30)   # produced earlier
        control = src / "in.jpg"
        control.write_bytes(b"i" * 30)
        _wait_until(lambda: h.got == [control], what="control dispatches, output ignored")
        h.settle_n()
        assert h.got == [control]
    finally:
        h.stop()


def test_registration_guards(tmp_path):
    src = tmp_path / "watch"; src.mkdir()
    w = FolderWatcher(on_settled=lambda f, p: None)
    with pytest.raises(ValueError, match="watch root"):
        w.add_folder(tmp_path / "nope", tmp_path / "out")
    with pytest.raises(ValueError, match="re-ingested"):
        w.add_folder(src, src)                          # dst == src
    w.add_folder(src, tmp_path / "out")
    with pytest.raises(ValueError, match="already watched"):
        w.add_folder(src, tmp_path / "out2")
    with pytest.raises(ValueError, match="inside destination tree"):
        w.add_folder(tmp_path / "out" / "sub", tmp_path / "out2")  # src inside a dst


def test_callback_failure_captured_not_swallowed(tmp_path):
    src = tmp_path / "watch"; src.mkdir()
    def boom(folder, path):
        if path.name == "bad.mp4":
            raise RuntimeError("queue exploded")
        h.got.append(path)
    h = Harness(on_settled=boom); h.watch(src, tmp_path / "out"); h.start()
    try:
        (src / "bad.mp4").write_bytes(b"b" * 20)
        _wait_until(lambda: len(h.w.errors) == 1, what="error captured")
        good = src / "good.mp4"
        good.write_bytes(b"g" * 20)
        _wait_until(lambda: h.got == [good], what="watcher survives callback error")
    finally:
        h.stop()


# ------------------------------------------------------- queue wiring (F24+F25)
from transmute.gui.controller import TargetInfo  # noqa: E402
from transmute.paths import temp_paths  # noqa: E402
from transmute.runner.queue import JobQueue  # noqa: E402
from transmute.watch import make_queue_callback  # noqa: E402

_AV1 = TargetInfo(target="av1", label="AV1", available=True, reason="",
                  compat_flag=False, recommended=True, hw=False, note="test")


class _QueueCtrl:
    """Smallest controller surface make_queue_callback needs, backed by a real
    JobQueue. dst mirroring: relative path re-rooted under dst, .av1 suffix."""

    def __init__(self, queue):
        self.queue = queue

    def default_target(self, cat):
        return _AV1

    def enqueue(self, src, dst_dir, cat, target, **kw):
        src = Path(src)
        dst = Path(dst_dir) / src.with_suffix(".av1").name
        return self.queue.add(src, dst, category=cat)


class _SkipCtrl(_QueueCtrl):
    def default_target(self, cat):
        return None


def test_queue_callback_queues_with_target_and_skips_without(tmp_path):
    q = JobQueue.__new__(JobQueue)  # surface check only: no journal touched
    ctrl = _QueueCtrl(q)
    calls = []
    q.add = lambda *a, **k: calls.append((a, k)) or _info(a[1])
    cb = make_queue_callback(ctrl)
    cb(WatchFolder(tmp_path, tmp_path / "out"), tmp_path / "a" / "movie.mp4")
    assert calls and calls[0][0][0] == tmp_path / "a" / "movie.mp4"
    assert calls[0][0][1] == tmp_path / "out" / "a" / "movie.av1"  # F25 mirror
    assert calls[0][1]["category"] == "video"
    # honest skip: no default target -> nothing queued, nothing raised
    cb2 = make_queue_callback(_SkipCtrl(q))
    n = len(calls)
    cb2(WatchFolder(tmp_path, tmp_path / "out"), tmp_path / "b" / "notes.xyz")
    cb2(WatchFolder(tmp_path, tmp_path / "out"), tmp_path / "b" / "movie.mp4")
    assert len(calls) == n  # neither queued: unsupported type, and no target


def _info(dst):
    from types import SimpleNamespace
    return SimpleNamespace(dst=dst)


def test_watcher_to_journal_end_to_end(tmp_path):
    """Gate-adjacent: a real drop travels watcher -> JobQueue -> journal files
    under the (portable-overridable) app root — nothing is queued in memory only."""
    src = tmp_path / "inbox"; src.mkdir()
    appdata = tmp_path / "appdata"
    with temp_paths(appdata):
        q = JobQueue()
        ctrl = _QueueCtrl(q)
        h = Harness(on_settled=make_queue_callback(ctrl))
        h.watch(src, tmp_path / "out")
        h.start()
        journal_file = appdata / "state" / "queue-journal.jsonl"
        try:
            (src / "sub" / "deep.mp3").parent.mkdir(parents=True)
            (src / "sub" / "deep.mp3").write_bytes(b"M" * 50)
            _wait_until(lambda: journal_file.exists()
                        and "queued" in journal_file.read_text(),
                        what="drop travels watcher -> JobQueue -> journal")
            journal = journal_file.read_text()
            assert "deep.mp3" in journal and ".av1" in journal
            assert "sub" in journal  # src rel-path under watch root, mirrored dst
            events = (appdata / "logs" / "events.jsonl").read_text()
            assert "job_queued" in events
            assert h.w.errors == []
            assert h.w.dispatched == 1
        finally:
            h.stop()


def test_cli_argparsing_helps_without_probing(capsys):
    import pytest as _pt
    with _pt.raises(SystemExit) as e:
        watch_main(["--help"])
    assert e.value.code == 0
    assert "Watch a folder" in capsys.readouterr().out


def test_watch_root_inside_dot_directory_still_dispatches(tmp_path):
    """P4 smoke regression: a watch root *under* a dot-named folder used to
    silently ignore every drop (hidden check saw the root's own ancestors)."""
    root = tmp_path / ".cache" / "inbox"
    root.mkdir(parents=True)
    h = Harness()
    h.watch(root, tmp_path / "out")
    h.start()
    try:
        (root / "ok.jpg").write_bytes(b"\xff\xd8" + b"j" * 60)
        (root / ".hidden-dir" / "x.jpg").parent.mkdir()
        (root / ".hidden-dir" / "x.jpg").write_bytes(b"\xff\xd8" + b"j" * 60)
        _wait_until(lambda: len(h.got) == 1, what="drop under dot-root dispatched")
        assert h.got == [root / "ok.jpg"]  # hidden BELOW the root stays ignored
    finally:
        h.stop()


def test_cli_sigterm_stops_cleanly(tmp_path):
    """TERM must exit 0 (graceful stop), never the default kill (143):
    handlers are installed before any slow import, so 'watching' on stdout
    already implies the stop path owns the process."""
    import subprocess
    import sys

    src = tmp_path / "in"
    dst = tmp_path / "out"
    src.mkdir()
    dst.mkdir()
    env = dict(os.environ, TRANSMUTE_ROOT=str(tmp_path / "root"),
               PYTHONUNBUFFERED="1")  # stdout is a pipe: keep the banner live
    p = subprocess.Popen(
        [sys.executable, "-m", "transmute.watch", str(src), str(dst)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        line = p.stdout.readline()  # blocks until startup prints
        assert "watching" in line
        p.terminate()  # SIGTERM
        assert p.wait(timeout=30) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)
