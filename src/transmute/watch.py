"""F24 watch folders: drop a file in, it gets queued once it finishes writing.

The watcher never converts and never picks a target. It hands *settled* files to
an `on_settled(folder, path)` callback; queue/target policy stays where it lives
(JobQueue / ConverterController — GUI doctrine: the controller decides).

Doctrine:
- Settle is MEASURED, never inferred from names: file size and mtime_ns must be
  identical across a full quiet window and the file must be non-empty. The
  `sources.py` rules (hidden / partial-download suffixes / `.skip` guards) are
  still the front-line filter, so a `.mp4.part` is ignored even mid-rename.
- Real kernel events via watchdog: inotify on Linux; the Windows backend
  (ReadDirectoryChangesW) comes with the same API in P6.
- Outputs are never re-ingested: anything under a registered destination root
  is not a candidate, `dst == src` is rejected up front, and watching inside
  another folder's destination is rejected too.
- Rename storms are handled honestly: a move-away cancels the pending entry,
  an atomic `.part -> final` rename enqueues the destination name, and each
  (path, size, mtime_ns) snapshot dispatches at most once.
- Callback failures never kill the watcher (F26): they are captured in
  `watcher.errors` and logged, never swallowed.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sources import category_of, is_ignored

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchFolder:
    """A watched root and the destination tree its outputs mirror into (F25)."""

    src: Path
    dst: Path


@dataclass
class _Pending:
    folder: WatchFolder
    size: int
    mtime_ns: int
    stable_since: float | None = None  # monotonic; None = changed at last sample


class FolderWatcher:
    """Watch folders; dispatch each settled new file to `on_settled` exactly once.

    `quiet_s` is the measured stability window a file must show before it is
    considered done writing; `poll_s` is the sample interval. Threads are
    daemons; `stop()` joins them.
    """

    def __init__(self, *, on_settled: Callable[[WatchFolder, Path], None],
                 quiet_s: float = 2.0, poll_s: float = 0.25):
        if quiet_s <= 0 or poll_s <= 0:
            raise ValueError("quiet_s and poll_s must be > 0")
        self.on_settled = on_settled
        self.quiet_s = quiet_s
        self.poll_s = poll_s
        self.folders: list[WatchFolder] = []
        self.errors: list[tuple[Path, BaseException]] = []
        self._pending: dict[Path, _Pending] = {}
        self._dispatched: dict[Path, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._observer = None
        self.dispatched = 0  # count of on_settled deliveries (for tests/ops)

    # ------------------------------------------------------------ registration
    def add_folder(self, src: Path | str, dst: Path | str) -> WatchFolder:
        """Register a watch root -> destination mirror. Call before `start()`."""
        src, dst = Path(src).resolve(), Path(dst).resolve()
        if src == dst:
            raise ValueError(
                f"destination is the watched folder ({src}) — outputs would be "
                "re-ingested; pick a separate destination tree")
        for f in self.folders:
            if f.src == src:
                raise ValueError(f"already watched: {src}")
            if _inside(src, f.dst):
                raise ValueError(
                    f"{src} is inside destination tree {f.dst} of watched "
                    f"folder {f.src} — its outputs would be watched back")
        if not src.is_dir():  # last: loop guards must also cover not-yet-created dirs
            raise ValueError(f"watch root is not a directory: {src}")
        folder = WatchFolder(src=src, dst=dst)
        self.folders.append(folder)
        if self._observer is not None:  # already running: schedule live
            self._observer.schedule(self._handler, str(src), recursive=True)
        return folder

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._observer is not None:
            raise RuntimeError("watcher already started")
        if not self.folders:
            raise RuntimeError("no folders registered — call add_folder first")
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    watcher._consider(Path(event.src_path))

            def on_modified(self, event):
                if not event.is_directory:
                    watcher._consider(Path(event.src_path))

            def on_moved(self, event):
                # storm-honest: name moved away cancels, moved in is considered
                watcher._forget(Path(event.src_path))
                if not event.is_directory:
                    watcher._consider(Path(event.dest_path))

            def on_deleted(self, event):
                watcher._forget(Path(event.src_path))

        self._handler = _Handler()
        self._observer = Observer()
        for f in self.folders:
            self._observer.schedule(self._handler, str(f.src), recursive=True)
        self._observer.start()
        self._stop_evt.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop,
                                             name="transmute-settle-poll",
                                             daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=10)
            self._observer = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=10)
            self._poll_thread = None

    def __enter__(self) -> "FolderWatcher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------- internals
    def _consider(self, path: Path) -> None:
        """Kernel event front door: cheap filters only; settle is measured."""
        folder = self._folder_for(path)
        if folder is None:
            return
        try:
            # ignore rules are scoped to the watch root: a dot-named ancestor
            # ABOVE the root (e.g. ~/.cache/inbox) must not poison every drop
            try:
                rel = path.relative_to(folder.src)
            except ValueError:
                rel = Path(path.name)
            if is_ignored(rel, real_path=path):
                return
            if category_of(path) is None:  # unsupported type: stays visible elsewhere
                return
            st = os.stat(path)
        except OSError:
            return  # vanished mid-event: nothing to do
        if not _is_plain_file(st):
            return
        with self._lock:
            snap = (st.st_size, st.st_mtime_ns)
            if self._dispatched.get(path) == snap:
                return  # same content already handed over
            self._pending[path] = _Pending(folder, *snap)

    def _forget(self, path: Path) -> None:
        with self._lock:
            self._pending.pop(path, None)

    def _folder_for(self, path: Path) -> WatchFolder | None:
        """Longest-src match; skip candidates inside any destination tree."""
        with self._lock:
            folders = list(self.folders)
        best: WatchFolder | None = None
        for f in folders:
            if _inside(path, f.dst):
                return None  # an output, not an input
            if _inside(path, f.src) and (best is None or len(str(f.src)) > len(str(best.src))):
                best = f
        return best

    def _poll_loop(self) -> None:
        while not self._stop_evt.wait(self.poll_s):
            try:
                self._poll_once()
            except Exception:  # poll must outlive one bad iteration (F26)
                log.exception("settle poll iteration failed")

    def _poll_once(self) -> None:
        """Settle is measured here: identical (size, mtime_ns) for quiet_s."""
        now = time.monotonic()
        ready: list[tuple[WatchFolder, Path, tuple[int, int]]] = []
        with self._lock:
            for path, pend in list(self._pending.items()):
                try:
                    st = os.stat(path)
                except OSError:
                    del self._pending[path]  # deleted/moved before settling
                    continue
                snap = (st.st_size, st.st_mtime_ns)
                if not _is_plain_file(st) or snap != (pend.size, pend.mtime_ns):
                    pend.size, pend.mtime_ns = snap
                    pend.stable_since = None  # writer touched it — restart clock
                    continue
                if st.st_size == 0:
                    pend.stable_since = None  # empty: still 'being written', honest wait
                    continue
                if pend.stable_since is None:
                    pend.stable_since = now
                elif now - pend.stable_since >= self.quiet_s:
                    del self._pending[path]
                    if self._dispatched.get(path) != snap:
                        self._dispatched[path] = snap
                        ready.append((pend.folder, path, snap))
        for folder, path, _snap in ready:
            try:
                self.on_settled(folder, path)
            except BaseException as exc:  # noqa: BLE001 - callback must not kill us
                self.errors.append((path, exc))
                log.exception("on_settled callback failed for %s", path)
            else:
                self.dispatched += 1

    def pending(self) -> list[Path]:
        """Files being measured for settle (observability / tests)."""
        with self._lock:
            return list(self._pending)


def _inside(path: Path, root: Path) -> bool:
    rp, rr = str(path.resolve()), str(root)
    return rp == rr or rp.startswith(rr.rstrip("/") + "/")


def _is_plain_file(st: os.stat_result) -> bool:
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


# ----------------------------------------------------------------- headless CLI
def make_queue_callback(ctrl) -> Callable[[WatchFolder, Path], None]:
    """Wire settled files to a controller (duck-typed: `default_target`,
    `enqueue`) so they land in its JobQueue with tree-mirrored destinations.

    JobQueue journals under AppPaths — which honours the portable.ini marker /
    TRANSMUTE_ROOT override — so F25 portable mode flows through watching with
    zero extra plumbing. Unsupported or unconvertible files print an honest
    skip reason (F3/F26); nothing is queued blindly.
    """
    def on_settled(folder: WatchFolder, path: Path) -> None:
        cat = category_of(path)
        tgt = ctrl.default_target(cat) if cat else None
        if tgt is None or not tgt.available:
            reason = tgt.reason if tgt is not None else "unsupported file type"
            log.info("watch skip %s: %s", path.name, reason)
            return
        # F25 tree mirroring: keep the drop's subfolder under the dst root, so
        # inbox/sub/x.mp4 -> dst/sub/x.<target>. (dst_for keeps only the stem.)
        try:
            rel = path.parent.relative_to(folder.src)
        except ValueError:
            rel = Path(".")
        info = ctrl.enqueue(path, folder.dst / rel, cat, tgt.target)
        log.info("watch queued %s -> %s (%s/%s)", path.name,
                 Path(info.dst).name, cat, tgt.target)
    return on_settled


def main(argv: list[str] | None = None) -> int:
    """python -m transmute.watch SRC DST — F24: queue-only (spec says add to
    queue; conversions run from the GUI). Data lives in the standard per-user
    dir, or next to the app in portable mode (portable.ini / TRANSMUTE_ROOT)."""
    import argparse
    import signal

    ap = argparse.ArgumentParser(
        prog="python -m transmute.watch",
        description="Watch a folder; settled drop-ins are queued for conversion "
                    "with destinations mirrored under DST.")
    ap.add_argument("src", type=Path, help="folder to watch (recursive)")
    ap.add_argument("dst", type=Path, help="destination tree to mirror into")
    args = ap.parse_args(argv)

    # Install handlers BEFORE any slow import: Ctrl-C/TERM must own the whole
    # startup window, otherwise the default kill disposition answers first.
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .gui.controller import ConverterController  # Qt-free by doctrine

    ctrl = ConverterController()
    print(f"watching {args.src} -> {args.dst} (Ctrl-C to stop)", flush=True)
    watcher = None
    try:
        watcher = FolderWatcher(on_settled=make_queue_callback(ctrl))
        watcher.add_folder(args.src, args.dst)
        watcher.start()
        stop.wait()
    finally:
        if watcher is not None:
            watcher.stop()
    if watcher is not None and watcher.errors:
        print(f"{len(watcher.errors)} callback error(s) — see log", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
