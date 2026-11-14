"""ffprobe helpers shared by video/audio engines (verification, F24 skip logic).

Also hosts the F2 remux proof: the ONLY acceptable evidence that a "remux"
never re-encoded is hash-comparing the actual compressed video bitstream of
source and output after demuxing both through ffmpeg (raw-stream extraction
normalises container differences: MP4 length-prefixed vs MKV Annex-B both
become the same elementary stream).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
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


# ------------------------------------------------------------------ F2 remux proof
# ffmpeg raw muxers per codec: extracting through these normalises container
# framing, so identical compressed samples -> identical bytes -> identical hash.
RAW_MUXER_BY_CODEC = {"h264": "h264", "hevc": "hevc",
                      "vp8": "ivf", "vp9": "ivf", "av1": "ivf", "mp3": "mp3"}


def extract_video_bitstream_sha256(engines: EngineManager | None, path: Path,
                                   codec: str | None) -> str | None:
    """sha256 of the demuxed video elementary stream, or None if this box's
    ffmpeg cannot extract that codec (unknown codec / no raw muxer / failure).
    Writes to a temp FILE: run_command captures text, which is not binary-safe."""
    raw = RAW_MUXER_BY_CODEC.get((codec or "").lower())
    ffmpeg = (engines or EngineManager()).path("ffmpeg")
    if raw is None or ffmpeg is None:
        return None
    with tempfile.TemporaryDirectory(prefix="transmute-proof-") as td:
        rawp = Path(td) / f"stream.{raw}"
        res = run_command(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", path, "-map", "0:v:0", "-c:v", "copy",
             "-f", raw, "-y", rawp], timeout_s=600)
        if not res.ok or not rawp.is_file() or rawp.stat().st_size == 0:
            return None
        h = hashlib.sha256()
        with open(rawp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()


def packet_manifest_sha256(engines: EngineManager | None, path: Path) -> str | None:
    """Weaker fallback proof for codecs without a raw muxer: hash of the
    ordered per-packet (size, flags, stream) metadata of every stream.
    Catches re-encode (packet sizes change) but is labelled honestly -
    it is NOT a bitstream hash."""
    ffprobe = (engines or EngineManager()).path("ffprobe")
    if ffprobe is None:
        return None
    res = run_command(
        [ffprobe, "-v", "error", "-show_packets",
         "-show_entries", "packet=stream_index,size,flags,duration",
         "-of", "csv=p=0", str(path)], timeout_s=600)
    if not res.ok or not (res.stdout or "").strip():
        return None
    return hashlib.sha256(res.stdout.encode("utf-8", "surrogateescape")).hexdigest()
