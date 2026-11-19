"""Serve mode: convert files over HTTP from another device (Phase B).

This is NOT a second converter. Every decision and every byte still goes
through the SAME code as the desktop GUI: ConverterController (probe-driven
targets, F1/F3) and the journal-driven JobQueue (verify-before-replace,
retry ladder, restart-safe journal - F23/F24). Serve only adds an HTTP
front door, an upload folder, and one worker thread that wakes the queue.

Consequences that follow from that design:
  * the browser picker lists exactly what THIS machine can do right now,
    unavailable targets included with the machine's own reason;
  * outputs pass verify-before-replace before the download link appears;
  * a crash or restart of the server resumes interrupted jobs from the
    journal, same as the desktop app.

v1 scope, stated honestly: one conversion per request (the GUI keeps the
watch-folder/batch surface), and conversions run one at a time so a phone
upload can never stampede the machine you are sitting at. Anything that
binds off-loopback carries a token (see app.py) - localhost needs none,
because being on this machine already means being trusted.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..jsonl import JsonlWriter
from ..paths import AppPaths, paths as app_paths
from ..runner.journal import Journal
from ..runner.queue import JobQueue
from ..sources import category_of

# The picker's category set (sources.py owns the extension map; this is only
# the order the UI walks them in).
CATEGORIES = ("video", "audio", "image", "pdf", "document")

_SAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def safe_name(raw: str) -> str:
    """Browser-provided filenames are UNTRUSTED: keep only the basename
    (Path.name collapses every path part, '..' included), replace anything
    outside a small safe alphabet, never let it start with '.' or be empty.
    Backslashes become separators first: a Windows browser may send a whole
    'C:\\dir\\file' path, and on Linux '\\' is otherwise a harmless letter."""
    name = Path(str(raw or "").replace("\\", "/")).name
    name = _SAFE.sub("_", name).strip(" .")[:120].strip()
    return name or "file"


class UploadTooLarge(Exception):
    def __init__(self, limit_mb: float):
        super().__init__(f"upload exceeds the {limit_mb:.0f} MB limit "
                         "(raise it with --max-upload-mb)")
        self.limit_mb = limit_mb


@dataclass
class Upload:
    id: str
    name: str
    path: Path
    category: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"upload_id": self.id, "name": self.name,
                "category": self.category,
                "size": self.path.stat().st_size if self.path.is_file() else 0}


class UploadWriter:
    """Streams one upload to uploads/<id>.part; abort() leaves nothing behind.

    save_upload() hands chunks straight from the HTTP layer - the file never
    lives fully in RAM, so a 4 GB phone video cannot OOM the server.
    """

    def __init__(self, state: "ServeState", raw_name: str):
        self._state = state
        self.name = safe_name(raw_name)
        self.id = uuid.uuid4().hex[:12]
        self._part = state.uploads_dir / f"{self.id}.part"
        self._fh = self._part.open("wb")
        self.size = 0

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self._state.max_upload_bytes:
            self.abort()
            raise UploadTooLarge(self._state.max_upload_mb)
        self._fh.write(chunk)

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            self._part.unlink(missing_ok=True)

    def finish(self) -> Upload:
        self._fh.close()
        final_dir = self._state.uploads_dir / self.id
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / self.name
        self._part.replace(final)
        up = Upload(id=self.id, name=self.name, path=final,
                    category=category_of(final))
        self._state.uploads[up.id] = up
        return up


@dataclass
class ServeState:
    """Wires queue + controller + uploads; owns the worker thread."""

    max_upload_mb: float = 2048.0
    paths: AppPaths | None = None
    report_provider: Callable[[], Any] | None = None
    queue: JobQueue | None = None
    governor: Any = None
    # None => no login (default on loopback: this machine already IS the
    # trust boundary). Anything reachable from off-box must carry a token -
    # see serve.app.resolve_token, which generates one for non-loopback binds.
    auth_token: str | None = None

    def token_ok(self, provided: str | None) -> bool:
        """Constant-time check; a token is a key, so no early-exit compare."""
        import secrets
        if not self.auth_token:
            return True
        return bool(provided) and secrets.compare_digest(str(provided),
                                                         self.auth_token)

    def __post_init__(self) -> None:
        p = self.paths or app_paths()
        base = Path(p.base) / "serve"
        self.uploads_dir = base / "uploads"
        self.outputs_dir = base / "outputs"
        for d in (self.uploads_dir, self.outputs_dir):
            d.mkdir(parents=True, exist_ok=True)
        # OWN journal file: a serve restart resumes serve jobs; it can never
        # collide with (or resurrect) the desktop app's watch-folder queue.
        self.journal = Journal(base / "serve-journal.jsonl")
        self.events = JsonlWriter(base / "events.jsonl")
        if self.queue is None:
            self.queue = JobQueue(journal=self.journal, events=self.events,
                                  logs_root=base / "captures",
                                  governor=self.governor, paths=p)
        from ..gui.controller import ConverterController
        self.controller = ConverterController(
            report_provider=self.report_provider, queue=self.queue, paths=p)
        self.uploads: dict[str, Upload] = {}
        self.max_upload_bytes = int(self.max_upload_mb * 1024 * 1024)
        self._wake = threading.Event()
        self._stop = threading.Event()
        # The FIRST capability query on a fresh machine triggers a full
        # benchmark (~a minute of measured encodes). That must never sit
        # inside an HTTP request: one background thread warms it at startup
        # while /api/info honestly answers "still benchmarking", and convert
        # requests wait for readiness instead of double-probing.
        self._report_ready = threading.Event()
        threading.Thread(target=self._warm_report, daemon=True,
                         name="transmute-serve-probe").start()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="transmute-serve-worker",
                                        daemon=True)
        self._worker.start()  # waking once now also resumes an old journal

    def _warm_report(self) -> None:
        try:
            self.controller.report  # loads cache or runs the real benchmark
        except Exception:  # noqa: BLE001 - no probe, no targets: reasons come from the engine later
            pass
        finally:
            self._report_ready.set()

    # ------------------------------------------------------------- uploads
    def begin_upload(self, raw_name: str) -> UploadWriter:
        return UploadWriter(self, raw_name)

    # ------------------------------------------------------------- convert
    def _await_report(self) -> None:
        # capability truth must exist before any verdict; the warm-up thread
        # owns the probe, this request just waits (uploading already bought
        # time - in practice the benchmark finished long ago)
        if not self._report_ready.wait(timeout=300):
            raise ValueError("this machine is still benchmarking - "
                             "try again in a moment")

    def _verdict(self, up: Upload, target: str) -> str | None:
        """None = this machine will take the job. Anything else is the
        machine's OWN reason, said out loud - never a silent drop (F3)."""
        if up.category is None:
            return "not a file type Transmute recognises"
        rows = {t.target: t for t in self.controller.targets(up.category)}
        if target not in rows:
            return f"unknown target '{target}' for {up.category} files"
        if not rows[target].available:
            return rows[target].reason or "not available on this machine"
        return None

    def convert(self, upload_id: str, target: str, **kw: Any) -> str:
        up = self.uploads.get(upload_id)
        if up is None:
            raise LookupError("upload not found - upload the file again")
        self._await_report()
        why = self._verdict(up, target)
        if why:
            raise ValueError(why)
        kw = {k: v for k, v in kw.items() if v not in (None, "")}
        info = self.controller.enqueue(up.path, self.outputs_dir,
                                       up.category, target, **kw)
        self.wake()
        return info.job_id

    def convert_batch(self, items: list[dict]) -> dict[str, Any]:
        """Queue a folder's worth in one breath. Each file gets its own
        verdict; a refusal never kills the files beside it - the response
        says exactly which names were refused and why, in this machine's
        own words. Conversions still run one at a time (the worker owns
        that promise, not this call)."""
        if not items:
            raise ValueError("nothing to convert - upload files first")
        self._await_report()
        jobs: list[str] = []
        refused: list[dict[str, str]] = []
        for it in items:
            up = self.uploads.get(str(it.get("upload_id") or ""))
            target = str(it.get("target") or "")
            uid = str(it.get("upload_id") or "")
            if up is None:
                # upload_id travels back with every refusal: display names
                # are NOT unique (safe_name never dedups), so the page can
                # only attribute "not queued" honestly by the id it sent
                refused.append({"name": uid or "?", "upload_id": uid,
                                "reason": "upload not found - upload the file again"})
                continue
            if not target:
                refused.append({"name": up.name, "upload_id": uid,
                                "reason": "target is required"})
                continue
            why = self._verdict(up, target)
            if why:
                refused.append({"name": up.name, "upload_id": uid, "reason": why})
                continue
            kw = {k: v for k, v in it.items()
                  if k not in ("upload_id", "target") and v not in (None, "")}
            try:
                info = self.controller.enqueue(up.path, self.outputs_dir,
                                               up.category, target, **kw)
                jobs.append(info.job_id)
            except Exception as exc:  # noqa: BLE001 - one bad file must not
                refused.append({"name": up.name, "upload_id": uid,
                                "reason": str(exc)})                       # kill batch
        if jobs:
            self.wake()
        return {"jobs": jobs, "refused": refused}

    # ------------------------------------------------------------- queries
    def info(self) -> dict[str, Any]:
        from .. import __version__
        if not self._report_ready.is_set():
            # honest interim answer - the browser keeps polling; inventing a
            # picker before the benchmark exists would break F1
            return {"version": __version__, "warming": True,
                    "max_upload_mb": self.max_upload_mb, "categories": {}}
        cats: dict[str, Any] = {}
        for cat in CATEGORIES:
            targets = []
            for t in self.controller.targets(cat):
                targets.append({"target": t.target, "label": t.label,
                                "available": t.available, "reason": t.reason,
                                "recommended": t.recommended, "hw": t.hw,
                                "note": t.note,
                                "encoders": self.controller.encoders(cat, t.target)
                                if t.available else [],
                                "about": self.controller.about_lines(cat, t.target)})
            cats[cat] = {"targets": targets}
        return {"version": __version__,
                "max_upload_mb": self.max_upload_mb,
                "categories": cats}

    def jobs(self) -> list[dict[str, Any]]:
        # newest-touched first: every journal transition stamps 'updated'
        recs = sorted(self.journal.records.values(),
                      key=lambda r: -r.updated)
        out = []
        for rec in recs:
            out.append({"job_id": rec.job_id, "name": Path(rec.dst).name,
                        "src": Path(rec.src).name, "category": rec.category,
                        "state": rec.state, "reason": rec.reason or "",
                        "live": rec.state in ("queued", "running", "verifying"),
                        "download": rec.state == "replaced"
                        and Path(rec.dst).is_file()})
        return out

    def output_for(self, job_id: str) -> Path:
        rec = self.journal.records.get(job_id)
        if rec is None or rec.state != "replaced":
            raise LookupError("no finished output for this job yet")
        dst = Path(rec.dst)
        # the journal is the ONLY path authority; uploads never live under
        # outputs, so a crafted id cannot point at a foreign file
        if not dst.is_file() or self.outputs_dir not in dst.parents:
            raise LookupError("output file is gone")
        return dst

    def clear_finished(self) -> int:
        """The web 'Clear finished' button: forget terminal rows and delete
        the outputs this service produced for them. Live rows stay; uploaded
        sources stay (they are the client's copies, and deleting user
        content was never what 'clear the list' means)."""
        ids, dsts = [], []
        for rec in self.journal.records.values():
            if rec.terminal:
                ids.append(rec.job_id)
                dsts.append(Path(rec.dst))
        # delete only files the journal says we authored under outputs_dir
        for dst in dsts:
            if self.outputs_dir in dst.parents:
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass  # a vanished output must never block the clear
        n = self.journal.forget(ids)
        if n:
            self.events.append("jobs_forgotten", cleared=n)
        return n

    # ------------------------------------------------------------- worker
    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.queue.stop()

    def _worker_loop(self) -> None:
        # one worker, max_jobs unbounded per burst but ONE conversion at a
        # time (max_workers=1): this machine belongs to its user first.
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            while not self._stop.is_set():
                try:
                    summary = self.queue.run(max_workers=1)
                except Exception as exc:  # noqa: BLE001
                    # Defense in depth: queue._process catches per-job errors,
                    # but an error escaping the runner must kill THAT burst,
                    # never this thread - a dead worker silently stops every
                    # later upload converting, with no error anywhere the
                    # user can see. Report it in the event log and stay alive.
                    self.queue.events.append(
                        "worker_error",
                        reason=f"{type(exc).__name__}: {exc}")
                    break
                if summary.processed == 0:
                    break
