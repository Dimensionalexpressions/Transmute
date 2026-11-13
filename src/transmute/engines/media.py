"""ffprobe helpers shared by video/audio engines (verification, F24 skip logic)."""
from __future__ import annotations

import json
from pathlib import Path

from ..probe.engines import EngineManager
from .base import CommandResult, run_command


class Probe:
    """Thin ffprobe wrapper. Methods return None/{} on any failure - callers
    treat missing data as 'cannot verify' (which blocks replace, never crashes)."""

    def __init__(self, engines: EngineManager | None = None):
        self.engines = engines or EngineManager()

    @property
    def available(self) -> bool:
        return self.engines.is_available("ffprobe")

    def _json(self, path: Path, args: list[str]) -> dict | None:
        ffprobe = self.engines.path("ffprobe")
        if ffprobe is None:
            return None
        res = run_command([ffprobe, "-v", "error", *args, str(path)], timeout_s=60)
        if not res.ok:
            return None
        try:
            return json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            return None

    def format_info(self, path: Path) -> dict | None:
        return self._json(path, ["-show_format", "-of", "json"])

    def video_streams(self, path: Path) -> list[dict]:
        d = self._json(path, ["-show_streams", "-select_streams", "v", "-of", "json"])
        return list((d or {}).get("streams", []))

    def audio_streams(self, path: Path) -> list[dict]:
        d = self._json(path, ["-show_streams", "-select_streams", "a", "-of", "json"])
        return list((d or {}).get("streams", []))

    def subtitle_count(self, path: Path) -> int:
        d = self._json(path, ["-show_streams", "-select_streams", "s", "-of", "json"])
        return len((d or {}).get("streams", []))

    def primary_video(self, path: Path) -> dict | None:
        vs = [s for s in self.video_streams(path)
              if s.get("disposition", {}).get("attached_pic", 0) != 1]
        if not vs:
            vs = self.video_streams(path)
        return vs[0] if vs else None

    def duration_s(self, path: Path) -> float | None:
        fmt = self.format_info(path)
        try:
            return float((fmt or {}).get("format", {}).get("duration", "nan"))
        except (TypeError, ValueError):
            return None

    def frame_count(self, path: Path) -> int | None:
        """Frame count: metadata nb_frames when present, exact counted decode
        otherwise (authoritative for verification)."""
        v = self.primary_video(path)
        if v:
            nf = v.get("nb_frames")
            if nf not in (None, "N/A", 0, "0"):
                try:
                    return int(nf)
                except (TypeError, ValueError):
                    pass
        ffprobe = self.engines.path("ffprobe")
        if ffprobe is None:
            return None
        res = run_command([ffprobe, "-v", "error", "-select_streams", "v:0",
                           "-count_frames", "-show_entries", "stream=nb_read_frames",
                           "-of", "csv=p=0", str(path)], timeout_s=600)
        if not res.ok:
            return None
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None

    def color_signature(self, v: dict) -> tuple[str, str, str]:
        return (v.get("color_transfer") or "", v.get("color_space") or "",
                v.get("color_primaries") or "")

    def is_hdr(self, path: Path) -> bool | None:
        """True/False from live stream metadata; None = cannot tell."""
        v = self.primary_video(path)
        if v is None:
            return None
        transfer = (v.get("color_transfer") or "").lower()
        if transfer in ("smpte2084", "arib-std-b67", "smpte2086"):
            return True
        if v.get("side_data_list"):
            for sd in v["side_data_list"]:
                if "Mastering display metadata" in str(sd.get("side_data_type", "")) \
                        or "Content light level" in str(sd.get("side_data_type", "")):
                    return True
        return False


def is_hdr_transfer(transfer: str | None) -> bool:
    return (transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte2086")
