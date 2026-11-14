"""Qt-free brain behind the GUI (P2).

Every rule the widgets show (available vs greyed-out-with-reason, ℹ️
compatibility flags, HDR warnings, defaults) is decided HERE, so it is
unit-testable without Qt, without a display, and without engines beyond what
the report already probed.  `transmute.gui.app` binds widgets to this; the
controller must never import PySide6.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..paths import AppPaths
from ..probe.capability import CapabilityReport, CapabilityProber

# Target -> output suffix. Video gets a container known to carry the codec
# without exotic muxing (webm for VP8/VP9, mkv otherwise); everything else
# mirrors the target name.
_SUFFIX = {"vp8": ".webm", "vp9": ".webm"}


@dataclass
class TargetInfo:
    """One selectable conversion target for the picker."""

    target: str
    label: str                 # display text incl. badges
    available: bool
    reason: str                # why unavailable ('' when available)
    compat_flag: bool          # ℹ️ royalty/reporting note (F14)
    recommended: bool
    hw: bool
    note: str                  # short provenance (engine chain, measured)

    @property
    def enabled(self) -> bool:
        return self.available


@dataclass
class JobInfo:
    """Flattened journal record for the queue view."""

    job_id: str
    src: str
    dst: str
    state: str
    reason: str = ""
    encoder: str = ""
    attempts: int = 0

    @property
    def live(self) -> bool:
        return self.state in ("queued", "running")

    @property
    def failed(self) -> bool:
        return self.state in ("failed", "backoff", "interrupted")


class ConverterController:
    """Facade the widgets talk to. Report and queue are lazy + injectable."""

    def __init__(self,
                 report_provider: Callable[[], CapabilityReport] | None = None,
                 queue: Any | None = None,
                 paths: AppPaths | None = None) -> None:
        self._report_provider = report_provider
        self._paths = paths
        self._queue = queue
        self._report: CapabilityReport | None = None

    # ------------------------------------------------------------- report
    @property
    def report(self) -> CapabilityReport:
        if self._report is None:
            if self._report_provider is not None:
                self._report = self._report_provider()
            else:
                # CapabilityProber resolves its cache location from the GLOBAL
                # paths config; point it at this controller's root when one was
                # injected, then restore. (Boot-time path - GUI init is single
                # threaded here.)
                from ..paths import paths as get_paths, set_paths
                prev = None
                if self._paths is not None:
                    prev = get_paths()
                    set_paths(self._paths)
                try:
                    self._report = CapabilityProber().report()
                finally:
                    if prev is not None:
                        set_paths(prev)
        return self._report

    @property
    def queue(self):
        if self._queue is None:
            from ..runner.queue import JobQueue
            self._queue = JobQueue(paths=self._paths)
        return self._queue

    # ------------------------------------------------------------ pickers
    @staticmethod
    def categories() -> list[str]:
        return ["video", "audio", "image", "pdf", "document"]

    def targets(self, category: str) -> list[TargetInfo]:
        """Every matrix row for the category, available and not.

        Unavailable rows carry a human reason so the picker greys them out
        with a tooltip instead of silently hiding options (F3).
        """
        out: list[TargetInfo] = []
        for row in self.report.matrix:
            if row.category != category:
                continue
            if "verify renders" in row.target:   # diagnostic, not a target
                continue
            label = row.target
            if row.compat_flag:
                label += "  ℹ️"
            elif row.recommended and row.available:
                label += "  ✓"
            reason = "" if row.available else (
                row.note or f"not available on this machine ({row.path})")
            out.append(TargetInfo(target=row.target, label=label,
                                  available=row.available, reason=reason,
                                  compat_flag=row.compat_flag,
                                  recommended=row.recommended, hw=row.hw,
                                  note=row.path))
        return out

    def default_target(self, category: str) -> TargetInfo | None:
        for t in self.targets(category):
            if t.available:
                return t
        return None

    def encoders(self, category: str, target: str) -> list[str]:
        """Verified encoder ladder for a codec target (fastest first, F10).

        Empty for non-codec targets (image/pdf/document): the engine picks
        internally; the UI shows the chain label from TargetInfo.note.
        """
        if category not in ("video", "audio"):
            return []
        try:
            return [c.name for c in self.report.verified_encoders(target)]
        except Exception:
            return []

    # -------------------------------------------------------------- jobs
    def suffix_for(self, category: str, target: str) -> str:
        base = target.split("(")[0].strip()
        if category == "video":
            return _SUFFIX.get(base, ".mkv")
        if category == "document":
            return ".pdf"
        return "." + base

    def dst_for(self, src: Path | str, category: str, target: str,
                dst_dir: Path | str) -> Path:
        src = Path(src)
        stem = src.stem if src.stem else "file"
        return Path(dst_dir) / (stem + self.suffix_for(category, target))

    def options_for(self, category: str, target: str,
                    *, quality: int | None = None,
                    encoder: str | None = None,
                    deep_pdf: bool = False) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        if category in ("video", "audio"):
            opts["codec"] = target
            if encoder:
                opts["encoder"] = encoder
        if category == "image" and quality is not None:
            opts["quality"] = int(quality)
        if category == "pdf" and deep_pdf:
            opts["deep"] = True   # lossy recompress - the UI must label it so
        return opts

    def enqueue(self, src: Path | str, dst_dir: Path | str, category: str,
                target: str, **kw: Any) -> JobInfo:
        dst = self.dst_for(src, category, target, dst_dir)
        rec = self.queue.add(Path(src), dst, category=category,
                             options=self.options_for(category, target, **kw))
        return self._info(rec)

    def process(self, *, max_workers: int = 2) -> Any:
        return self.queue.run(max_workers=max_workers)

    def snapshot(self) -> list[JobInfo]:
        return [self._info(r) for r in self.queue.journal.records.values()]

    @staticmethod
    def _info(rec: Any) -> JobInfo:
        return JobInfo(job_id=rec.job_id, src=rec.src, dst=rec.dst,
                       state=rec.state, reason=rec.reason or "",
                       encoder=rec.encoder or "", attempts=rec.attempts)

    # ---------------------------------------------------------- warnings
    def hdr_source(self, src: Path | str) -> bool | None:
        """True/False when measurable, None when ffprobe is absent."""
        try:
            from ..engines.media import Probe
            probe = Probe()
            if not probe.available:
                return None
            return probe.is_hdr(Path(src))
        except Exception:
            return None

    @staticmethod
    def category_of(src: Path | str) -> str | None:
        from ..sources import category_of
        return category_of(src)
