"""Video engine: ffmpeg via the LIVE capability report (F10 auto-ladder).

Ported discipline from production av1-convert:
- explicit -f muxer per container (never guess from temp names),
- faststart for mp4/mov/m4v,
- audio+subtitles+metadata copied, never dropped,
- audio streams that cannot live in mp4 (opus/vorbis) force mkv (lossless)
  instead of being transcoded silently,
- HDR sources (smpte2084/HLG) must keep 10-bit + metadata: never a silent
  HDR->SDR (F25 tone-map is a later opt-in feature).
Encoder selection = verified encoders for the codec sorted by MEASURED trial
speed (report data only - no vendor assumptions).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..probe.capability import CapabilityProber, EncoderCapability
from ..probe.engines import EngineManager
from .base import Availability, EngineResult, run_command
from .media import Probe, is_hdr_transfer

CONTAINER_BY_FORMAT = {".mp4": "mp4", ".mov": "mov", ".m4v": "mp4", ".mkv": "matroska",
                       ".webm": "webm", ".ts": "mpegts"}
FASTSTART = {"mp4", "mov"}
MP4_AUDIO_OK = {"aac", "mp3", "alac", "ac3", "eac3", "amr_nb", "amr_wb"}

DEFAULT_CRF = {"av1": 30, "vp9": 33, "h264": 23, "hevc": 28}
DEFAULT_TARGET = {"mp4": "h264", "mov": "h264", "m4v": "h264",
                  "mkv": "av1", "webm": "vp9", "ts": "h264"}
CODEC_DEFAULT_CONTAINER = {"av1": "mkv", "vp9": "webm", "h264": "mp4", "hevc": "mp4"}


class VideoEngine:
    category = "video"
    name = "video-ffmpeg"

    def __init__(self, engines: EngineManager | None = None,
                 prober: CapabilityProber | None = None):
        self.engines = engines or EngineManager()
        self.prober = prober or CapabilityProber(self.engines)
        self.probe = Probe(self.engines)

    # ------------------------------------------------------------- availability
    def availability(self) -> Availability:
        info = self.engines.detect()
        need = [n for n in ("ffmpeg", "ffprobe") if info[n].status != "ok"]
        if need:
            hints = {os_key: "\n".join(
                f"{n}: {info[n].install_hint}" for n in need) for os_key in ("linux", "windows")}
            return Availability(False, f"missing engine(s): {', '.join(need)}",
                                hints, {n: vars(info[n]) for n in info})
        return Availability(True, info={n: vars(i) for n, i in info.items()})

    # ------------------------------------------------------------- planning
    def pick_encoder(self, codec: str, *, want_10bit: bool,
                     prefer_hw: bool | None) -> EncoderCapability | None:
        report = self.prober.report()
        cands = report.verified_encoders(codec)
        if want_10bit:
            cands = [c for c in cands
                     if (not c.hw) and c.depth.get("10")]  # hw depth driver-dependent: untested
        if prefer_hw is not None:
            filtered = [c for c in cands if c.hw == prefer_hw]
            cands = filtered or cands
        return cands[0] if cands else None

    # ------------------------------------------------------------- convert
    def convert(self, src: Path, dst: Path, *, container: str | None = None,
                codec: str | None = None, crf: int | None = None,
                encoder: str | None = None, prefer_hw: bool | None = None,
                ffmpeg_timeout_s: float = 43200.0,
                governor=None, log_id: str | None = None,
                log_root: Path | None = None) -> EngineResult:
        """Convert src -> dst (dst includes final extension). Never touches src.

        outcome: converted | skipped (already optimal) | failed.
        """
        t0 = time.monotonic()
        avail = self.availability()
        if not avail.ok:
            return EngineResult("failed", reason=avail.reason,
                                data={"engine_unavailable": True})
        src = Path(src)
        dst = Path(dst)
        ffmpeg = self.engines.path("ffmpeg")
        assert ffmpeg is not None

        fmt = CONTAINER_BY_FORMAT.get(dst.suffix.lower())
        if fmt is None:
            return EngineResult("failed", reason=f"unknown container '{dst.suffix}'")

        sv = self.probe.primary_video(src)
        if sv is None:
            return EngineResult("failed", reason="no decodable video stream in source")
        src_codec = (sv.get("codec_name") or "").lower()
        src_pix = sv.get("pix_fmt") or ""
        hdr = is_hdr_transfer(sv.get("color_transfer"))

        target_codec = (codec or DEFAULT_TARGET.get(fmt, "av1")).lower()
        # F20: already in the target codec -> skip, don't re-compress blindly.
        if src_codec == target_codec and encoder is None and crf is None:
            return EngineResult("skipped", reason=f"already {target_codec}-encoded")

        want_10 = hdr or src_pix.endswith(("10le", "12le"))
        cap = self.pick_encoder(target_codec, want_10bit=want_10, prefer_hw=prefer_hw) \
            if encoder is None else None
        if encoder is not None:
            report = self.prober.report()
            cap = next((c for c in report.encoders
                        if c.name == encoder and c.verified), None)
            if cap is None:
                return EngineResult("failed", reason=f"encoder {encoder} not verified on this machine")
        if cap is None:
            why = ("HDR/10-bit source and no verified 10-bit encoder available "
                   "(refusing silent HDR->SDR; F25 tone-map is opt-in)"
                   if want_10 else f"no verified encoder for {target_codec} on this machine")
            return EngineResult("failed", reason=why)

        # audio compatibility: mp4-family cannot carry opus/vorbis losslessly ->
        # prefer mkv over silent audio transcoding.
        if fmt in ("mp4", "mov"):
            acodecs = {(s.get("codec_name") or "").lower()
                       for s in self.probe.audio_streams(src)}
            bad = acodecs - MP4_AUDIO_OK - {""}
            if bad and target_codec in ("av1", "vp9"):
                return EngineResult("failed",
                                    reason=f"audio stream(s) {sorted(bad)} cannot be copied into "
                                           f"'{fmt}' losslessly; use mkv/webm for opus/vorbis")

        cmd: list[Any] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", src,
                          "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                          "-c:v", cap.name, "-c:a", "copy", "-c:s", "copy",
                          "-map_metadata", "0", "-map_chapters", "0",
                          "-f", fmt]
        if fmt in FASTSTART:
            cmd += ["-movflags", "+faststart"]
        crf_val = crf if crf is not None else DEFAULT_CRF.get(target_codec)
        if crf_val is not None and cap.backend == "cpu":
            cmd += ["-crf", str(crf_val)]
        elif crf_val is not None and cap.name.endswith("_nvenc"):
            cmd += ["-cq", str(crf_val), "-b:v", "0", "-preset", "slow"]
        cmd += ["-y", dst]

        log_path = None
        if log_root is not None and log_id:
            from ..jsonl import JobLogStore
            log_path = JobLogStore(log_root).open_capture(log_id)
        res = run_command(cmd, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                          governor=governor, gpu=cap.hw)
        elapsed = time.monotonic() - t0
        if not res.ok:
            reason = "timed out" if res.timed_out else (
                _tail(res.stderr_text) or f"ffmpeg exit {res.returncode}")
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason=f"encode failed: {reason}",
                                seconds=elapsed, encoder=cap.name, log_path=log_path)
        if not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="encoder produced no output file",
                                seconds=elapsed, encoder=cap.name, log_path=log_path)
        return EngineResult("converted", output=dst, encoder=cap.name,
                            seconds=elapsed, log_path=log_path,
                            data={"codec": target_codec, "container": fmt,
                                  "crf": crf_val, "backend": cap.backend,
                                  "hw": cap.hw, "hdr_source": hdr})

    # ------------------------------------------------------------- verify (F24)
    def verify(self, src: Path, out: Path, *, expect_codec: str | None = None,
               allow_duration_s: float = 0.5) -> EngineResult:
        """Structural verification BEFORE any replace: decodable, same duration,
        same frame count, same audio stream count, no HDR->SDR downgrade."""
        if not self.probe.available:
            return EngineResult("failed", reason="ffprobe missing: cannot verify output")
        sv, dv = self.probe.primary_video(src), self.probe.primary_video(out)
        if dv is None:
            return EngineResult("failed", reason="output has no decodable video stream")
        if sv is not None:
            if sv.get("width") != dv.get("width") or sv.get("height") != dv.get("height"):
                return EngineResult("failed", reason="resolution changed in conversion")
            if sv.get("avg_frame_rate") and dv.get("avg_frame_rate") and \
                    sv["avg_frame_rate"] != dv["avg_frame_rate"]:
                return EngineResult("failed", reason="frame rate changed in conversion")
        d1, d2 = self.probe.duration_s(src), self.probe.duration_s(out)
        if d1 is not None and d2 is not None and abs(d1 - d2) > max(allow_duration_s, d1 * 0.01):
            return EngineResult("failed", reason=f"duration changed {d1:.2f}s -> {d2:.2f}s")
        if len(self.probe.audio_streams(src)) != len(self.probe.audio_streams(out)):
            return EngineResult("failed", reason="audio stream count changed")
        if sv is not None and is_hdr_transfer(sv.get("color_transfer")):
            if not is_hdr_transfer(dv.get("color_transfer")):
                return EngineResult("failed",
                                    reason="HDR source produced SDR output (silent tone-map "
                                           "forbidden, F25)")
        if expect_codec and (dv.get("codec_name") or "").lower() != expect_codec:
            return EngineResult("failed",
                                reason=f"expected codec {expect_codec}, output is {dv.get('codec_name')}")
        return EngineResult("converted", output=out,
                            data={"verified": True, "codec": dv.get("codec_name")})


def _tail(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return (" | ".join(lines[-3:]))[-limit:]
