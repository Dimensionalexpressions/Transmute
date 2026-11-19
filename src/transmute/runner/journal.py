"""Append-only job journal (F23 core): makes the queue restart-safe.

Every state transition is one JSONL line (fsync'd). A torn trailing line after
a crash is skipped on load, so the journal survives power loss. Replay yields
the LATEST state per job; a previous process's 'running' records become
'interrupted' - never silently assumed done. Nothing is ever replaced in place
without a prior journal line (and verification).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..jsonl import JsonlWriter, read_jsonl

STATES = ("queued", "running", "verifying", "replaced", "kept", "skipped",
          "failed", "interrupted", "abandoned", "paused")
TERMINAL = {"replaced", "kept", "skipped", "abandoned"}


@dataclass
class JournalRecord:
    job_id: str
    src: str
    dst: str
    category: str
    options: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    attempts: int = 0
    next_attempt_at: float | None = None
    reason: str | None = None
    encoder: str | None = None
    src_fp: dict[str, Any] | None = None  # size+mtime: idempotence fingerprint
    paused_from: str | None = None  # state to restore on resume_all()
    updated: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def eligible(self, now: float | None = None) -> bool:
        if self.terminal:
            return False
        if self.state in ("queued", "failed", "interrupted"):
            return self.next_attempt_at is None or self.next_attempt_at <= (
                now if now is not None else time.time())
        return False

    def to_dict(self) -> dict[str, Any]:
        d = {"job_id": self.job_id, "src": self.src, "dst": self.dst,
             "category": self.category, "options": self.options, "state": self.state,
             "attempts": self.attempts, "next_attempt_at": self.next_attempt_at,
             "reason": self.reason, "encoder": self.encoder,
             "src_fp": self.src_fp, "paused_from": self.paused_from,
             "updated": self.updated}
        return {k: v for k, v in d.items() if v is not None}


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._writer = JsonlWriter(self.path)
        self.records: dict[str, JournalRecord] = {}
        self.load()

    # ------------------------------------------------------------- replay
    def load(self) -> dict[str, JournalRecord]:
        self.records = {}
        for rec in read_jsonl(self.path):
            state = rec.get("state")
            job_id = rec.get("job_id")
            if not job_id or state not in STATES:
                continue
            if state in ("running", "verifying"):
                # No live encode can exist in a journal read at startup: the
                # process that wrote it died (crash, kill -9, power loss).
                # Recover it as interrupted whether or not an earlier queued
                # line preceded it - "prev is None" is NOT the signature; a
                # normal job always has queued->running lines, so checking
                # only the orphan case left every real crashed job stuck in
                # "running" forever (resumable() skips it, GUI could neither
                # resume nor discard it).
                state = "interrupted"
            self.records[job_id] = JournalRecord(
                job_id=job_id, src=str(rec.get("src", "")), dst=str(rec.get("dst", "")),
                category=str(rec.get("category", "")),
                options=dict(rec.get("options") or {}),
                state=state,
                attempts=int(rec.get("attempts") or 0),
                next_attempt_at=rec.get("next_attempt_at"),
                reason=rec.get("reason"), encoder=rec.get("encoder"),
                src_fp=rec.get("src_fp"),
                paused_from=rec.get("paused_from"),
                updated=float(rec.get("ts") or 0.0))
        return self.records

    # ------------------------------------------------------------- transitions
    def append(self, rec: JournalRecord, **overrides: Any) -> JournalRecord:
        payload = rec.to_dict()
        payload.update({k: v for k, v in overrides.items() if v is not None or k in overrides})
        payload["updated"] = time.time()
        rec.updated = payload["updated"]
        self._writer.append("journal", **payload)
        self.records[rec.job_id] = rec
        return rec

    def transition(self, rec: JournalRecord, state: str, **fields: Any) -> JournalRecord:
        assert state in STATES, f"bad state {state}"
        rec.state = state
        for k, v in fields.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        return self.append(rec, state=state)

    # ------------------------------------------------------------- queries
    def pending(self, *, now: float | None = None) -> list[JournalRecord]:
        return [r for r in self.records.values() if r.eligible(now)]

    def resumable(self) -> list[JournalRecord]:
        return [r for r in self.records.values() if r.state == "interrupted"]
