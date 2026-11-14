"""Job queue: journal-driven, verify-before-replace, atomic os.replace (F23/F24).

Every conversion writes into ``.transmute-tmp/`` NEXT TO the destination (same
filesystem -> os.replace is atomic), verifies BEFORE any replace, and only
then swaps. The source is never touched. Failures never leak exceptions:
they land in the journal with a reason, the full engine capture path, and a
retry schedule from the ladder (10 min -> 1 h -> 24 h). 'engine_unavailable'
is terminal (retrying won't install anything).

Restart semantics: run() processes everything the journal considers eligible
(queued / failed-and-due / interrupted); discard() abandons them explicitly.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..jsonl import JsonlWriter
from ..paths import AppPaths, paths as app_paths
from . import preflight
from .journal import Journal, JournalRecord

# F23 retry ladder: transient failures get 10 min, 1 h, 24 h before the next
# attempt; after the ladder the job is 'abandoned' (visible, never silent).
RETRY_BACKOFF_S: tuple[float, ...] = (600.0, 3600.0, 86400.0)
STAGING_DIRNAME = ".transmute-tmp"


def make_job_id(src: Path, dst: Path) -> str:
    key = f"{os.path.abspath(src)}|{os.path.abspath(dst)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _fingerprint(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return None


@dataclass
class RunSummary:
    processed: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)

    def add(self, state: str, job_id: str, **info: Any) -> None:
        self.counts[state] = self.counts.get(state, 0) + 1
        self.results.append({"job_id": job_id, "state": state, **info})


class JobQueue:
    def __init__(self, *, journal: Journal | None = None,
                 events: JsonlWriter | None = None,
                 logs_root: Path | None = None,
                 engine_get: Callable[[str], Any] | None = None,
                 backoff_s: tuple[float, ...] = RETRY_BACKOFF_S,
                 governor=None,
                 paths: AppPaths | None = None):
        p = paths or app_paths()
        self.journal = journal if journal is not None else Journal(p.state_dir / "queue-journal.jsonl")
        self.events = events if events is not None else JsonlWriter(p.logs_dir / "events.jsonl")
        self.logs_root = Path(logs_root) if logs_root is not None else p.logs_dir / "captures"
        self.backoff_s = tuple(backoff_s)
        self.governor = governor
        self._lock = threading.Lock()
        if engine_get is None:
            from ..engines import engine_for  # late import: keeps journal-only use light
            engine_get = engine_for
        self._engine_get = engine_get
        self._stop = threading.Event()
        self._paused = threading.Event()

    # ------------------------------------------------------------- enqueue
    def add(self, src: Path, dst: Path, *, category: str | None = None,
            options: dict[str, Any] | None = None) -> JournalRecord:
        src, dst = Path(src), Path(dst)
        if category is None:
            from ..sources import category_of
            category = category_of(src) or "unknown"
        job_id = make_job_id(src, dst)
        fp = _fingerprint(src)
        existing = self.journal.records.get(job_id)
        if existing is not None and existing.state != "abandoned":
            if existing.src_fp == fp and existing.state in ("replaced", "kept", "skipped"):
                return existing  # idempotent: unchanged source, already done
        rec = JournalRecord(job_id=job_id, src=str(src), dst=str(dst),
                            category=category, options=dict(options or {}),
                            src_fp=fp)
        self.journal.transition(rec, "queued")
        self.events.append("job_queued", job_id=job_id, src=src, dst=dst,
                           category=category)
        return rec

    # ------------------------------------------------------------- run
    def run(self, *, max_workers: int = 4, max_jobs: int | None = None) -> RunSummary:
        """Process every eligible journal record (bounded thread pool)."""
        summary = RunSummary()
        with self._lock:
            eligible = self.journal.pending()
        if max_jobs is not None:
            eligible = eligible[:max_jobs]
        if not eligible:
            return summary
        workers = max(1, min(max_workers, len(eligible)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="transmute-job") as pool:
            futures = [pool.submit(self._process, rec, summary) for rec in eligible]
            for f in futures:
                f.result()  # _process never raises; re-raise here would be a bug
        return summary

    def stop(self) -> None:
        self._stop.set()

    def discard(self, job_ids: list[str] | None = None) -> int:
        """Explicit 'discard' for interrupted runs (F19): abandons the jobs."""
        targets = ([self.journal.records[j] for j in job_ids if j in self.journal.records]
                   if job_ids else self.journal.resumable())
        n = 0
        for rec in targets:
            if not rec.terminal:
                self.journal.transition(rec, "abandoned", reason="discarded by user")
                self.events.append("job_discarded", job_id=rec.job_id)
                n += 1
        return n

    # ------------------------------------------------- pause / resume / cancel (P5)
    PAUSABLE = ("queued", "failed", "interrupted")

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause_all(self) -> int:
        """Recoverable stop for everything not in flight: running/verifying
        jobs FINISH (verify-before-replace is never interrupted); queued and
        retry-pending jobs move to 'paused' remembering where to come back."""
        self._paused.set()
        n = 0
        for rec in list(self.journal.records.values()):
            if rec.state in self.PAUSABLE:
                self.journal.transition(rec, "paused", paused_from=rec.state,
                                        reason="paused by user")
                n += 1
        self.events.append("queue_paused", paused=n)
        return n

    def resume_all(self) -> int:
        """Undo pause_all(): each job returns to its remembered state
        (a paused 'failed' keeps its retry schedule; others requeue now)."""
        self._paused.clear()
        n = 0
        for rec in list(self.journal.records.values()):
            if rec.state == "paused":
                back = rec.paused_from if rec.paused_from in self.PAUSABLE else "queued"
                self.journal.transition(rec, back, paused_from=None, reason=None)
                n += 1
        self.events.append("queue_resumed", resumed=n)
        return n

    def cancel_all(self) -> int:
        """Terminal cancel for everything not in flight (visible 'abandoned'
        with a reason — never silent). In-flight running/verifying jobs
        finish; their output passes verify-before-replace as always."""
        n = 0
        for rec in list(self.journal.records.values()):
            if not rec.terminal and rec.state not in ("running", "verifying"):
                self.journal.transition(rec, "abandoned", reason="cancelled by user",
                                        next_attempt_at=None)
                self.events.append("job_cancelled", job_id=rec.job_id)
                n += 1
        self.events.append("queue_cancelled", cancelled=n)
        return n

    # ------------------------------------------------------------- per-job
    def _process(self, rec: JournalRecord, summary: RunSummary) -> None:
        # skip unless still startable: stop/pause flags OR a state that a
        # concurrent pause/cancel already moved out from under this future
        if (self._stop.is_set() or self._paused.is_set()
                or rec.state not in self.PAUSABLE):
            return
        src, dst = Path(rec.src), Path(rec.dst)
        self.journal.transition(rec, "running", attempts=rec.attempts)
        self.events.append("job_start", job_id=rec.job_id, src=src, dst=dst,
                           category=rec.category, attempt=rec.attempts + 1)

        engine = self._engine_get(rec.category)
        if engine is None:
            self._fail(rec, summary, reason=f"no engine for category '{rec.category}'",
                       terminal=True)
            return
        avail = engine.availability()
        if not avail.ok:
            self._fail(rec, summary,
                       reason=f"engine unavailable: {avail.reason}"
                              + (f" | install: {(avail.guidance or {}).get('linux', '')}"
                                 if avail.guidance else ""),
                       terminal=True)
            return

        # Disk preflight (P5): measure the destination volume BEFORE writing;
        # an unmeasurable probe never blocks work (see runner/preflight.py).
        try:
            src_size = src.stat().st_size
        except OSError:
            src_size = 0  # engine reports its own source error; floor applies
        short = preflight.shortage(dst.parent, src_size, rec.options)
        if short is not None:
            self._fail(rec, summary, reason=preflight.reason_for(short))
            return

        staged = self._staged_path(dst, rec.job_id)
        staged.parent.mkdir(parents=True, exist_ok=True)
        log_id = f"{rec.job_id}.a{rec.attempts + 1}"
        try:
            result = engine.convert(src, staged, governor=self.governor,
                                    log_id=log_id, log_root=self.logs_root,
                                    **rec.options)
        except TypeError as exc:
            # bad option for this engine: terminal (user option error, not transient)
            staged.unlink(missing_ok=True)
            self._fail(rec, summary, reason=f"invalid options: {exc}", terminal=True)
            return
        except Exception as exc:  # noqa: BLE001 - engines must not raise, but belt+braces
            staged.unlink(missing_ok=True)
            self._fail(rec, summary, reason=f"engine crashed: {type(exc).__name__}: {exc}")
            return

        outcome = getattr(result, "outcome", None)
        if outcome == "skipped":
            staged.unlink(missing_ok=True)
            self.journal.transition(rec, "skipped", reason=result.reason,
                                    encoder=result.encoder)
            self.events.append("job_skipped", job_id=rec.job_id, reason=result.reason)
            summary.add("skipped", rec.job_id, reason=result.reason)
            return
        if outcome == "kept":
            staged.unlink(missing_ok=True)
            self.journal.transition(rec, "kept", reason=result.reason, encoder=result.encoder)
            self.events.append("job_kept", job_id=rec.job_id, reason=result.reason)
            summary.add("kept", rec.job_id, reason=result.reason)
            return
        if outcome != "converted":
            staged.unlink(missing_ok=True)
            data = getattr(result, "data", None) or {}
            terminal = bool(data.get("engine_unavailable")) if isinstance(data, dict) else False
            self._fail(rec, summary, reason=result.reason or "engine failure",
                       terminal=terminal)
            return

        # ---- verify BEFORE replace (F24): never swap unverified bytes.
        # An engine may self-verify inside convert() and say so via
        # data["verified_by_engine"] (e.g. tone-mapped output, where the
        # standard verify would legitimately flag the intended HDR->SDR step).
        self.journal.transition(rec, "verifying")
        vreason: str | None = None
        if (getattr(result, "data", None) or {}).get("verified_by_engine"):
            self.events.append("verify_self", job_id=rec.job_id,
                               detail="engine verified output during convert")
        else:
            try:
                vres = engine.verify(src, staged)
            except Exception as exc:  # noqa: BLE001
                vres = None
                vreason = f"verifier crashed: {type(exc).__name__}: {exc}"
            else:
                vreason = vres.reason if not getattr(vres, "ok", False) else None
        if vreason is not None:
            staged.unlink(missing_ok=True)
            self.events.append("verify_failed", job_id=rec.job_id, reason=vreason)
            self._fail(rec, summary, reason=f"verification failed: {vreason}")
            return

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, dst)  # atomic within same filesystem
        except OSError as exc:
            staged.unlink(missing_ok=True)
            self._fail(rec, summary, reason=f"atomic replace failed: {exc}")
            return

        self.journal.transition(rec, "replaced", encoder=result.encoder,
                                src_fp=_fingerprint(src))
        self.events.append("job_replaced", job_id=rec.job_id, output=dst,
                           encoder=result.encoder, seconds=round(result.seconds or 0.0, 2),
                           size=dst.stat().st_size)
        summary.add("replaced", rec.job_id, output=str(dst), encoder=result.encoder)

    # ------------------------------------------------------------- failure ladder
    def _fail(self, rec: JournalRecord, summary: RunSummary, *, reason: str,
              terminal: bool = False) -> None:
        rec.attempts += 1
        now = time.time()
        if terminal or rec.attempts > len(self.backoff_s):
            self.journal.transition(rec, "abandoned", reason=reason,
                                    next_attempt_at=None)
            self.events.append("job_abandoned", job_id=rec.job_id, reason=reason,
                               attempts=rec.attempts, terminal=terminal)
            summary.add("abandoned", rec.job_id, reason=reason)
            return
        delay = self.backoff_s[rec.attempts - 1]
        self.journal.transition(rec, "failed", reason=reason,
                                next_attempt_at=now + delay)
        self.events.append("job_failed", job_id=rec.job_id, reason=reason,
                           attempts=rec.attempts, retry_in_s=delay)
        summary.add("failed", rec.job_id, reason=reason, retry_in_s=delay)

    # ------------------------------------------------------------- staging
    @staticmethod
    def _staged_path(dst: Path, job_id: str) -> Path:
        # same dir as dst -> same fs -> atomic replace; engine sees the real
        # final suffix so muxer/format selection from extension still works.
        return dst.parent / STAGING_DIRNAME / f"{dst.stem}.{job_id}.part{dst.suffix}"
