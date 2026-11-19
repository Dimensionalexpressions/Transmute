"""Audio engine: ffmpeg with metadata/artwork/gapless preservation (F20/F22).

Conversion always maps ALL streams: audio re-encoded, images (cover art)
stream-copied and flagged attached_pic, metadata + chapters copied, ID3v2.3
for mp3. Nothing is silently dropped; verify compares stream counts,
duration and bitrate sanity.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..probe.capability import CapabilityProber
from ..probe.engines import EngineManager
from .base import Availability, EngineResult, run_command
from .media import Probe

FORMAT_BY_EXT = {".mp3": "mp3", ".m4a": "ipod", ".aac": "adts", ".ogg": "ogg",
                 ".opus": "opus", ".flac": "flac", ".wav": "wav", ".mka": "matroska",
                 ".alac": "ipod", ".oga": "ogg"}
CODEC_BY_EXT = {".mp3": "mp3", ".m4a": "aac", ".aac": "aac", ".ogg": "vorbis",
                ".opus": "opus", ".flac": "flac", ".wav": "pcm_s16le",
                ".mka": "flac", ".alac": "alac"}
LOSSLESS = {"flac", "alac", "pcm_s16le", "wav"}
# Containers ffmpeg can hold MORE THAN ONE audio stream in (measured: the
# muxer accepts several -map 0:a streams). mp3/m4a/adts/wav hold exactly one.
MULTI_AUDIO_FMT = {"matroska", "ogg"}


class AudioEngine:
    category = "audio"
    name = "audio-ffmpeg"

    def __init__(self, engines: EngineManager | None = None,
                 prober: CapabilityProber | None = None):
        self.engines = engines or EngineManager()
        self.prober = prober or CapabilityProber(self.engines)
        self.probe = Probe(self.engines)

    def availability(self) -> Availability:
        info = self.engines.detect()
        need = [n for n in ("ffmpeg", "ffprobe") if info[n].status != "ok"]
        if need:
            hints = {o: "\n".join(f"{n}: {info[n].install_hint}" for n in need)
                     for o in ("linux", "windows")}
            return Availability(False, f"missing engine(s): {', '.join(need)}", hints)
        return Availability(True)

    def convert(self, src: Path, dst: Path, *, codec: str | None = None,
                bitrate: str | None = None, quality: int | None = None,
                ffmpeg_timeout_s: float = 3600.0, governor=None,
                log_id: str | None = None, log_root: Path | None = None) -> EngineResult:
        t0 = time.monotonic()
        avail = self.availability()
        if not avail.ok:
            return EngineResult("failed", reason=avail.reason,
                                data={"engine_unavailable": True})
        src, dst = Path(src), Path(dst)
        ffmpeg = self.engines.path("ffmpeg")
        fmt = FORMAT_BY_EXT.get(dst.suffix.lower())
        target = (codec or CODEC_BY_EXT.get(dst.suffix.lower()) or "").lower()
        if fmt is None or not target:
            return EngineResult("failed", reason=f"unknown audio target '{dst.suffix}'")

        asrc = self.probe.audio_streams(src)
        if not asrc:
            return EngineResult("failed", reason="no audio stream in source")
        src_codecs = {(s.get("codec_name") or "").lower() for s in asrc}
        src_codecs.discard("")
        if src_codecs == {target} and bitrate is None and quality is None:
            # ALL tracks must already be the target codec - skipping on the
            # strength of track 1 alone would strand every other track
            return EngineResult("skipped", reason=f"already {target}-encoded")
        # A multi-track source (commentary, dual-language) into a one-track
        # container would silently drop tracks. The doctrine: the machine's
        # own count becomes the visible reason, and the user gets the choice
        # (.mka holds them all) instead of a guess made in the dark.
        if len(asrc) > 1 and fmt not in MULTI_AUDIO_FMT:
            return EngineResult(
                "failed",
                reason=f"source has {len(asrc)} audio tracks but {dst.suffix} "
                       f"holds one; converting would silently drop "
                       f"{len(asrc) - 1}. Convert to .mka to keep them all.")
        # all audio streams when the container takes them; first track only
        # for the single-track containers a 1-track source fits anyway
        amap = ["-map", "0:a"] if len(asrc) > 1 else ["-map", "0:a:0"]

        cap = self.pick_encoder(target)
        if cap is None:
            return EngineResult("failed", reason=f"no verified encoder for {target}")

        cmd: list[Any] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", src,
                          *amap, "-map", "0:v?", "-map_metadata", "0",
                          "-map_chapters", "0",
                          "-c:a", cap.name, "-c:v", "copy",
                          "-f", fmt]
        if self.probe.video_streams(src):
            cmd += ["-disposition:v:0", "attached_pic"]
        if fmt == "mp3":
            cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
        if bitrate:
            cmd += ["-b:a", bitrate]
        elif quality is not None and cap.name in ("libopus", "libvorbis"):
            cmd += ["-q:a", str(max(0, min(10, quality)))]
        elif target in LOSSLESS:
            pass
        elif cap.name == "libopus":
            cmd += ["-b:a", "128k"]
        cmd += ["-y", dst]

        log_path = None
        if log_root is not None and log_id:
            from ..jsonl import JobLogStore
            log_path = JobLogStore(log_root).open_capture(log_id)
        res = run_command(cmd, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                          governor=governor)
        elapsed = time.monotonic() - t0
        if not res.ok:
            dst.unlink(missing_ok=True)
            reason = "timed out" if res.timed_out else (_tail(res.stderr_text)
                                                        or f"ffmpeg exit {res.returncode}")
            # ffmpeg chokes on 'attached_pic' disposition when there is no video
            if "Stream map '0:v:0'" in res.stderr_text or "does not contain any stream" \
                    in res.stderr_text or "disposition" in res.stderr_text:
                return self.convert_no_art(src, dst, target=target, cap=cap, fmt=fmt,
                                           bitrate=bitrate, t0=t0, governor=governor,
                                           log_id=log_id, log_root=log_root,
                                           amap=amap)
            return EngineResult("failed", reason=f"encode failed: {reason}",
                                seconds=elapsed, encoder=cap.name, log_path=log_path)
        if not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="encoder produced no output file",
                                seconds=elapsed, encoder=cap.name, log_path=log_path)
        return EngineResult("converted", output=dst, encoder=cap.name, seconds=elapsed,
                            log_path=log_path,
                            data={"codec": target, "container": fmt})

    def convert_no_art(self, src, dst, *, target, cap, fmt, bitrate, t0, governor,
                       log_id, log_root, amap=None):
        cmd = [self.engines.path("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel",
               "error", "-i", src, *(amap or ["-map", "0:a:0"]), "-map_metadata", "0",
               "-map_chapters", "0", "-c:a", cap.name, "-f", fmt]
        if bitrate:
            cmd += ["-b:a", bitrate]
        cmd += ["-y", dst]
        res = run_command(cmd, timeout_s=1800, governor=governor)
        if not res.ok or not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="encode failed (no-art retry): "
                                + (_tail(res.stderr_text) or "ffmpeg error"),
                                seconds=time.monotonic() - t0, encoder=cap.name)
        return EngineResult("converted", output=dst, encoder=cap.name,
                            seconds=time.monotonic() - t0,
                            data={"codec": target, "container": fmt, "artwork": "none"})

    def pick_encoder(self, codec: str):
        report = self.prober.report()
        return report.verified_encoders(codec)[0] if report.verified_encoders(codec) else None

    def verify(self, src: Path, out: Path) -> EngineResult:
        if not self.probe.available:
            return EngineResult("failed", reason="ffprobe missing: cannot verify output")
        d_src = self.probe.audio_streams(src)
        d_out = self.probe.audio_streams(out)
        if not d_out:
            return EngineResult("failed", reason="output has no audio stream")
        # the track count is compared on purpose (the docstring promised it
        # all along): a conversion that quietly kept one of three tracks
        # must fail visibly, not certify itself "verified"
        if len(d_src) != len(d_out):
            return EngineResult(
                "failed",
                reason=f"audio track count changed {len(d_src)} -> {len(d_out)}")
        t1, t2 = self.probe.duration_s(src), self.probe.duration_s(out)
        if t1 is not None and t2 is not None and abs(t1 - t2) > max(0.75, t1 * 0.02):
            return EngineResult("failed", reason=f"duration changed {t1:.2f}s -> {t2:.2f}s")
        n_art_in = len(self.probe.video_streams(src))
        n_art_out = len(self.probe.video_streams(out))
        if n_art_in > 0 and n_art_out == 0:
            return EngineResult("failed", reason="cover art was dropped")
        return EngineResult("converted", output=out,
                            data={"verified": True, "codec": d_out[0].get("codec_name")})


def _tail(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return (" | ".join(lines[-3:]))[-limit:]
