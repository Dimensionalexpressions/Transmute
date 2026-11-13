"""JSONL structured event log (F13) + per-job raw output capture.

One JSON object per line. Writers are append-only and crash-safe by design:
a partially written final line is tolerated on read (skipped), which is also
what makes the queue journal (runner.journal) restart-safe.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator


class JsonlWriter:
    """Append-only JSONL writer; thread-safe, flushes per event (small volumes)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": time.time(), "event": event}
        record.update({k: _jsonable(v) for k, v in fields.items() if v is not None})
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return record


def read_jsonl(path: Path, *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield records; a torn trailing line (crash mid-write) is skipped unless strict."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    yield json.loads(ln)
                except json.JSONDecodeError:
                    if strict:
                        raise
                    continue  # torn line: journal must stay usable
    except FileNotFoundError:
        return


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    return v


class JobLogStore:
    """Full raw stderr/stdout of every engine invocation, one file per attempt.

    events.jsonl stays small and structured; when a user hits 'export
    diagnostics' (F13) or inspects a failed job, the complete engine output is
    here, addressed from the event record by ``log_id``.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, log_id: str) -> Path:
        safe = "".join(c for c in log_id if c.isalnum() or c in "-_.")
        return self.root / f"{safe}.log"

    def open_capture(self, log_id: str) -> Path:
        """Truncate/create the capture file and return its path (caller writes)."""
        p = self.path_for(log_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return p
