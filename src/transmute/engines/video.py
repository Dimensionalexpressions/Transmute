"""Video engine: ffmpeg via the LIVE capability report (F10 auto-ladder).

Ported discipline from production av1-convert:
- explicit -f muxer per container (never guess from temp names),
- faststart for mp4/mov,
- audio+subtitles+metadata copied, never dropped,
- HDR sources must keep 10-bit + metadata: tone-map to SDR is OPT-IN only (F15),
  and the tone-map chain must have passed the probe's executed trial.

P3 adds:
- F2 remux-only mode: `-c copy` whose output is PROVEN identical by hashing the
  demuxed video elementary stream of source and output (weaker packet-manifest
  tier for codecs without a raw muxer, labelled honestly; never claimed as a
  bitstream hash).
- audio policy: 'copy' (default, fails fast with a codec-named reason when the
  target container cannot carry a stream losslessly) or 'replace' (transcodes
  audio ONLY, to a verified encoder for a container-appropriate codec).
- quality: CRF (default) or a size target in MB - exact 2-pass for x264/x265,
  single-pass VBV labelled 'approximate' everywhere else.
Encoder selection = verified encoders for the codec sorted by MEASURED trial
speed (report data only - no vendor assumptions).
"""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..probe.capability import (CapabilityProber, EncoderCapability,
                             TONEMAP_VFILTER, gpu_force_disabled)
from ..probe.engines import EngineManager
from .base import Availability, EngineResult, run_command
from .media import (Probe, extract_video_bitstream_sha256, is_hdr_transfer,
                    packet_manifest_sha256)

CONTAINER_BY_FORMAT = {".mp4": "mp4", ".mov": "mov", ".m4v": "mp4", ".mkv": "matroska",
                       ".webm": "webm", ".ts": "mpegts"}
FASTSTART = {"mp4", "mov"}
MP4_AUDIO_OK = {"aac", "mp3", "alac", "ac3", "eac3", "amr_nb", "amr_wb"}
WEBM_AUDIO_OK = {"vorbis", "opus", "flac"}
TS_AUDIO_OK = {"mp1", "mp2", "mp3", "aac", "ac3", "eac3"}
# None = container carries any audio codec losslessly (matroska).
AUDIO_OK_BY_FORMAT: dict[str, set[str] | None] = {
    "mp4": MP4_AUDIO_OK, "mov": MP4_AUDIO_OK, "matroska": None,
    "webm": WEBM_AUDIO_OK, "mpegts": TS_AUDIO_OK,
}
# Remux = never re-encodes: video codec must be natively allowed by the muxer.
REMUX_VIDEO_OK: dict[str, set[str] | None] = {
    "mp4": {"h264", "hevc", "mpeg4", "mpeg1video", "mpeg2video", "mjpeg", "prores", "av1"},
    "mov": {"h264", "hevc", "mpeg4", "mpeg1video", "mpeg2video", "mjpeg", "prores"},
    "matroska": None,
    "webm": {"vp8", "vp9", "av1"},
    "mpegts": {"h264", "hevc", "mpeg1video", "mpeg2video", "vc1"},
}
REMUX_AUDIO_OK = AUDIO_OK_BY_FORMAT
AUDIO_REPLACE_TO = {"mp4": "aac", "mov": "aac", "matroska": "opus",
                    "webm": "opus", "mpegts": "aac"}
AUDIO_REPLACE_BITRATE = "160k"

# x264/x265 speed ladder - the GUI combo and this guard must agree:
# nothing else may ever reach an ffmpeg command line.
ENCODER_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                   "medium", "slow", "slower", "veryslow", "placebo")

# Only these encoders speak that vocabulary: SVT-AV1/aom/Rav1e take NUMBERS
# and hardware encoders carry their own ladders - feeding a word to them
# breaks ffmpeg (-22), so a preset is simply not applicable there and the
# GUI greys the row. Estimates use the exact same rule as real conversions.
WORD_PRESET_ENCODERS = ("libx264", "libx265")
# Encoders with documented reliable 2-pass rate control (measured, not vendor claim).
TWO_PASS_ENCODERS = {"libx264", "libx265", "x264", "x265"}

DEFAULT_CRF = {"av1": 30, "vp9": 33, "h264": 23, "hevc": 28}

# Usable quality range per encoder - (low, high, default), low = best.
# Each encoder carries its own knob and scale; feeding x264's 0..51 to the
# others is what produced the live bug where "better" quality measured a
# SMALLER file. Measured on this machine's ffmpeg against a real meeting
# recording (screen-share content), sweep per CRF:
#   libsvtav1: crf 0 collapses (360 MB - SMALLER than crf 10's 698 MB),
#              crf 1..2 invert; strictly monotonic 2..63 (1025→212 MB).
#   libvpx-vp9: monotonic from 0 (809→211 MB).
#   librav1e: has NO -crf at all - FFmpeg discards it with a warning and
#              encodes at rav1e's own default; its knob is -qp 0..255.
#   libaom-av1: never wins the speed ladder; documented safe floor 4.
#   hardware: NVENC -cq 0..51; others pass no quality flag yet.
QUALITY_BOUNDS: dict[str, tuple[int, int, int]] = {
    "libx264": (0, 51, 23),
    "libx265": (0, 51, 28),
    "libsvtav1": (2, 63, 30),
    "libaom-av1": (4, 63, 32),
    "libvpx-vp9": (0, 63, 33),
    "librav1e": (20, 255, 100),   # -qp scale, not CRF
}


def quality_bounds(encoder: str | None) -> tuple[int, int, int]:
    """(low, high, default) of the quality knob for one encoder name.
    Unknown/hardware encoders get the conservative x264-style scale."""
    if encoder and encoder in QUALITY_BOUNDS:
        return QUALITY_BOUNDS[encoder]
    return (0, 51, 23)


def quality_flags(cap, crf_val: int) -> list[str]:
    """The quality knob in the form THIS encoder actually understands -
    anything else is a silent lie (librav1e discards -crf) or a hard ffmpeg
    failure. Estimates must build flags the SAME way real conversions do."""
    if cap.name == "librav1e":
        return ["-qp", str(crf_val)]
    if cap.backend == "cpu":
        return ["-crf", str(crf_val)]
    if cap.name.endswith("_nvenc"):
        return ["-cq", str(crf_val), "-b:v", "0", "-preset", "slow"]
    return []
DEFAULT_TARGET = {"mp4": "h264", "mov": "h264", "m4v": "h264",
                  "mkv": "av1", "webm": "vp9", "ts": "h264"}
CODEC_DEFAULT_CONTAINER = {"av1": "mkv", "vp9": "webm", "h264": "mp4", "hevc": "mp4"}


def _human_secs(s: float) -> str:
    if s < 45:
        return f"{max(1, int(round(s)))} s"
    m = s / 60.0
    if m < 2:
        return "about a minute"
    if m < 90:
        return f"{round(m)} min"
    h = m / 60.0
    return (f"{h:.1f} hours" if h < 10 else f"{round(h)} hours")


def _human_size(n: float) -> str:
    if n < 1e6:
        return f"{int(n // 1024)} KB"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.2f} GB"


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
        if gpu_force_disabled():
            # user switched "use graphics card" off in More…: honour it at
            # selection time too, not just in the (possibly stale) report
            cands = [c for c in cands if not c.hw]
        if want_10bit:
            cands = [c for c in cands
                     if (not c.hw) and c.depth.get("10")]  # hw depth driver-dependent: untested
        if prefer_hw is not None:
            filtered = [c for c in cands if c.hw == prefer_hw]
            cands = filtered or cands
        # Auto-chosen encoders must produce broadly playable output. RGB-only
        # encoders (probe-measured: ffmpeg lists only bgr/rgb pixel formats for
        # them, e.g. libx264rgb -> RGB h264 most hw players choke on) stay
        # selectable by explicit name but never win the speed race unasked.
        # NOTE: compat_flag is NOT the filter - libx264 itself is compat-flagged
        # (royalty info, F14); disqualification is RGB output only.
        preferred = [c for c in cands if not c.rgb_output]
        cands = preferred or cands
        return cands[0] if cands else None

    def quality_range(self, target_codec: str,
                      encoder: str | None = None) -> tuple[int, int, int]:
        """(low, high, default) of the quality knob for the encoder that
        WILL RUN on this target - the slider scale, low = best quality.
        Rationale per encoder: QUALITY_BOUNDS."""
        if encoder:
            cap = next((c for c in self.prober.report().verified_encoders(
                target_codec) if c.name == encoder), None)
            return quality_bounds(cap.name if cap else encoder)
        cap = self.pick_encoder(target_codec, want_10bit=False,
                                prefer_hw=None)
        return quality_bounds(cap.name if cap else None)

    # ------------------------------------------------------------- convert
    def convert(self, src: Path, dst: Path, *, container: str | None = None,
                codec: str | None = None, crf: int | None = None,
                encoder: str | None = None, prefer_hw: bool | None = None,
                remux: bool = False, tonemap: bool = False,
                audio: str = "copy", size_mb: float | None = None,
                preset: str | None = None, audio_bitrate: str | None = None,
                ffmpeg_timeout_s: float = 43200.0,
                governor=None, log_id: str | None = None,
                log_root: Path | None = None) -> EngineResult:
        """Convert src -> dst (dst includes final extension). Never touches src.

        outcome: converted | skipped (already optimal) | failed.
        remux=True: container change with -c copy ONLY, proven by bitstream
        hash (F2). tonemap=True: opt-in HDR->SDR (F15), only honoured when the
        probe's executed chain trial passed. audio: copy|replace.
        size_mb: MB budget; exact 2-pass for x264/x265 else labelled approximate.
        preset: x264/x265 speed/range (CPU encoders only - hardware encoders
        carry their own). audio_bitrate: e.g. "160k", applies with
        audio='replace' only.
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

        log_path = None
        if log_root is not None and log_id:
            from ..jsonl import JobLogStore
            log_path = JobLogStore(log_root).open_capture(log_id)

        if remux:
            return self._remux(src, dst, fmt=fmt, ffmpeg=ffmpeg, sv=sv,
                               audio=audio, ffmpeg_timeout_s=ffmpeg_timeout_s,
                               governor=governor, log_path=log_path, t0=t0)

        target_codec = (codec or DEFAULT_TARGET.get(fmt, "av1")).lower()

        # Audio streams decide whether a matching video codec is really
        # "already optimal": streams the target container cannot legally carry
        # (e.g. opus -> mp4) mean the job's real work is AUDIO; the job must
        # not skip - the video rides along as a pure stream copy instead.
        acodecs = {(s.get("codec_name") or "").lower()
                   for s in self.probe.audio_streams(src)}
        acodecs.discard("")
        ok_audio = AUDIO_OK_BY_FORMAT.get(fmt)
        bad_audio = sorted(acodecs - ok_audio) if ok_audio is not None else []

        # F20: already in the target codec -> skip, don't re-compress blindly.
        same_video = src_codec == target_codec and encoder is None and crf is None \
            and size_mb is None and not tonemap
        if same_video and not bad_audio:
            return EngineResult("skipped", reason=f"already {target_codec}-encoded")

        # ------------------------------------------------ F15 tone-map (opt-in)
        tone = bool(tonemap and hdr)
        if tonemap and hdr and not self.prober.report().filters.get("tonemap_chain"):
            return EngineResult(
                "failed",
                reason="this ffmpeg cannot tone-map: the zscale+tonemap chain "
                       "failed its executed trial (probed on this machine)")
        want_10 = (hdr or src_pix.endswith(("10le", "12le"))) and not tone

        cap: EncoderCapability | None = None
        if not same_video:
            cap = self.pick_encoder(target_codec, want_10bit=want_10,
                                    prefer_hw=prefer_hw) if encoder is None else None
            if encoder is not None:
                report = self.prober.report()
                cap = next((c for c in report.encoders
                            if c.name == encoder and c.verified), None)
                if cap is None:
                    return EngineResult("failed", reason=f"encoder {encoder} not verified on this machine")
            if cap is None:
                why = ("HDR/10-bit source and no verified 10-bit encoder available "
                       "(refusing silent HDR->SDR; tone-map is opt-in: enable it or "
                       "pick mkv/hdr target)"
                       if want_10 else f"no verified encoder for {target_codec} on this machine")
                return EngineResult("failed", reason=why)

        # ------------------------------------------------ audio policy (copy/replace)
        # acodecs/ok_audio/bad_audio were computed above (they gate the F20 skip).
        audio_cmd: list[str] = ["-c:a", "copy"]
        audio_note: dict[str, Any] | None = None
        audio_bps = sum(int(s.get("bit_rate") or 128_000)
                        for s in self.probe.audio_streams(src)
                        if (s.get("codec_name") or "").lower())
        if audio not in ("copy", "replace"):
            return EngineResult("failed", reason="audio policy must be 'copy' or 'replace'")
        if audio_bitrate is not None and not re.fullmatch(r"\d{1,4}k", audio_bitrate):
            return EngineResult("failed",
                                reason="audio bitrate must look like '160k'")
        if bad_audio and audio == "copy":
            repl = AUDIO_REPLACE_TO.get(fmt, "aac")
            return EngineResult(
                "failed",
                reason=f"audio stream(s) {bad_audio} cannot be copied into "
                       f"'{fmt}' losslessly; use audio='replace' (transcodes "
                       f"audio only, to {repl}) or a mkv/webm target")
        if audio == "replace":
            # explicit request (or container forcing it): audio is re-encoded,
            # at the bitrate the user chose - this is the "fine-tune audio" path
            repl = AUDIO_REPLACE_TO.get(fmt, "aac")
            cap_a = self.pick_encoder(repl, want_10bit=False, prefer_hw=None)
            if cap_a is None:
                return EngineResult(
                    "failed",
                    reason=f"audio must be transcoded to {repl} for '{fmt}' "
                           f"but no verified {repl} encoder exists on this machine")
            br = audio_bitrate or AUDIO_REPLACE_BITRATE
            audio_cmd = ["-c:a", cap_a.name, "-b:a", br]
            audio_bps = int(br[:-1]) * 1000 * max(len(acodecs), 1)
            audio_note = {"from": bad_audio or sorted(acodecs) or ["audio"],
                          "to": repl, "encoder": cap_a.name, "bitrate": br}

        # ------------------------------------------------ quality: CRF or size target
        cmd: list[Any] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", src,
                          "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                          "-c:v", cap.name if cap else "copy",
                          *audio_cmd, "-c:s", "copy",
                          "-map_metadata", "0", "-map_chapters", "0",
                          "-f", fmt]
        if fmt in FASTSTART:
            cmd += ["-movflags", "+faststart"]
        if tone:
            cmd += ["-vf", TONEMAP_VFILTER]

        pre_flags: list[str] = []
        if preset:
            if cap is not None and cap.name in WORD_PRESET_ENCODERS \
                    and cap.backend == "cpu":
                if preset not in ENCODER_PRESETS:
                    return EngineResult(
                        "failed",
                        reason=f"unknown encoder preset {preset!r} "
                               f"(allowed: {', '.join(ENCODER_PRESETS)})")
                pre_flags = ["-preset", preset]
            # hardware encoders carry their own speed ladder: silently ignoring
            # a CPU preset there is honest because the GUI hides it per encoder
        cmd += pre_flags

        crf_val: int | None = None
        size_note: dict[str, Any] | None = None
        two_pass = False
        v_bps = 0
        if crf is not None and size_mb is not None:
            return EngineResult("failed", reason="choose CRF or a size target, not both")
        if size_mb is not None:
            dur = self.probe.duration_s(src)
            if not dur:
                return EngineResult("failed",
                                    reason="cannot compute size target: source duration "
                                           "unknown (ffprobe cannot read it)")
            total_bps = size_mb * 8e6 / dur
            v_bps = int(total_bps - audio_bps)
            if v_bps < 100_000:
                return EngineResult(
                    "failed",
                    reason=f"{size_mb} MB is too small for {dur:.1f}s: audio alone "
                           f"needs ~{audio_bps // 1000} kbps of the budget")
            two_pass = cap.name in TWO_PASS_ENCODERS
            size_note = {"size_mb": size_mb, "video_bitrate_bps": v_bps,
                         "mode": "two-pass" if two_pass else "approximate-single-pass"}
        else:
            if crf is not None and cap is not None:
                lo_, hi_, _d = quality_bounds(cap.name)
                if not lo_ <= int(crf) <= hi_:
                    return EngineResult(
                        "failed",
                        reason=f"quality {crf} is outside {cap.name}'s usable "
                               f"range {lo_}-{hi_} (measured on this box): "
                               f"pick a value inside it - below the floor "
                               f"that encoder stops behaving")
            crf_val = None if cap is None else \
                (crf if crf is not None else DEFAULT_CRF.get(target_codec))
            if crf_val is not None:
                cmd += quality_flags(cap, crf_val)
        cmd += ["-y", dst]

        # ------------------------------------------------ run
        res = None
        if size_mb is not None:
            if two_pass:
                with tempfile.TemporaryDirectory(prefix="transmute-2pass-") as td:
                    pfx = str(Path(td) / "passlog")
                    p1 = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", src, "-map", "0:v:0", "-c:v", cap.name,
                          "-b:v", str(v_bps), "-pass", "1", "-passlogfile", pfx,
                          "-an", *pre_flags]
                    if tone:
                        p1 += ["-vf", TONEMAP_VFILTER]
                    p1 += ["-f", "null", "-"]
                    res = run_command(p1, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                                      governor=governor, gpu=cap is not None and cap.hw)

                    def _p2(bps: int, vbv: bool) -> list:
                        extra = ["-b:v", str(bps)]
                        if vbv:
                            # structural ceiling: average rate cannot exceed
                            # maxrate, no matter how hard the content is
                            extra += ["-maxrate", str(v_bps),
                                      "-bufsize", str(v_bps * 2)]
                        return [c for c in cmd if c != dst] + extra + \
                               ["-pass", "2", "-passlogfile", pfx, "-y", dst]

                    if res.ok:
                        res = run_command(_p2(v_bps, False), timeout_s=ffmpeg_timeout_s,
                                          log_path=log_path, governor=governor,
                                          gpu=cap is not None and cap.hw)
                        target_bytes = max(int(size_mb * 1e6), 1)
                        if res.ok and dst.is_file() and \
                                dst.stat().st_size > target_bytes * 1.10:
                            # ABR curve hit the complexity floor (measured, not
                            # assumed): re-run pass 2 with a VBV cap. One retry,
                            # then we report the truth about the content.
                            dst.unlink(missing_ok=True)
                            res = run_command(_p2(int(v_bps * 0.92), True),
                                              timeout_s=ffmpeg_timeout_s,
                                              log_path=log_path, governor=governor,
                                              gpu=cap is not None and cap.hw)
                            size_note["vbv_correction"] = True
                        if res.ok and dst.is_file():
                            actual = dst.stat().st_size
                            size_note["achieved_bytes"] = actual
                            size_note["within_budget"] = actual <= target_bytes * 1.15
                            if not size_note["within_budget"]:
                                size_note["note"] = (
                                    "content is too complex for this budget - "
                                    "consider a smaller frame size")
            else:
                idx = cmd.index("-y")
                cmd[idx:idx] = ["-b:v", str(v_bps),
                                "-maxrate", str(int(v_bps * 1.5)),
                                "-bufsize", str(v_bps * 2)]
                res = run_command(cmd, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                                  governor=governor, gpu=cap is not None and cap.hw)
        else:
            res = run_command(cmd, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                              governor=governor, gpu=cap is not None and cap.hw)
        elapsed = time.monotonic() - t0
        if res is None or not res.ok:
            reason = "timed out" if (res and res.timed_out) else (
                _tail(res.stderr_text if res else "") or
                f"ffmpeg exit {res.returncode if res else '?'}")
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason=f"encode failed: {reason}",
                                seconds=elapsed, encoder=cap.name if cap else "copy", log_path=log_path)
        if not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="encoder produced no output file",
                                seconds=elapsed, encoder=cap.name if cap else "copy", log_path=log_path)

        data: dict[str, Any] = {"codec": target_codec, "container": fmt,
                                "crf": crf_val,
                                "backend": cap.backend if cap else "copy",
                                "hw": cap.hw if cap else False, "hdr_source": hdr}
        if same_video:
            data["video_stream"] = "copy"
        if audio_note:
            data["audio_replaced"] = audio_note
        if size_note:
            data["size_target"] = size_note
        if tone:
            # HDR->SDR is intentional and must never TRIP the queue's standard
            # verify (which forbids silent tone-maps); verify here instead,
            # then tell the queue it is already checked.
            data["tonemapped"] = True
            v = self.verify(src, dst, expect_codec=target_codec, allow_hdr_loss=True)
            if not v.ok:
                dst.unlink(missing_ok=True)
                return EngineResult("failed",
                                    reason=f"tone-map verification failed: {v.reason}",
                                    seconds=elapsed, encoder=cap.name if cap else "copy", log_path=log_path)
            data["verified_by_engine"] = True
        return EngineResult("converted", output=dst, encoder=cap.name if cap else "copy",
                            seconds=elapsed, log_path=log_path, data=data)

    # ------------------------------------------------------------------- remux
    # --------------------------------------------------- estimate (GUI preview)
    def estimate_conversion(self, src: Path, *, target_codec: str,
                            encoder: str | None = None, crf: int | None = None,
                            size_mb: float | None = None,
                            preset: str | None = None,
                            audio_mode: str = "copy",
                            audio_bitrate: str | None = None,
                            stop: "threading.Event | None" = None) -> dict:
        """Numbers for the GUI: how long and how big THIS file becomes,
        measured by encoding a real ~2-second piece of it with the SAME
        encoder, preset, quality and audio plan on THIS machine (user
        directive: every number on this screen comes from this hardware's own
        behaviour, never from tables). Returns
        {"ok": bool, "est_s": float, "bytes": float, "size_is_target": bool,
         "sec": float, "note": str} - note carries the machine's own reason
        when ok is False ("" = nothing to say). stop: set the Event and the
        sample encode is killed and nothing is claimed - the GUI replaces
        measurements as the user moves selectors, and superseded work must
        stop burning the machine at once (workstation-lockup fix)."""
        none = {"ok": False, "est_s": 0.0, "bytes": 0.0,
                "size_is_target": False, "sec": 0.0, "note": ""}
        if stop is not None and stop.is_set():
            return none
        dur = self.probe.duration_s(src)
        if not dur or dur <= 0:
            return none
        if encoder:
            cap = next((c for c in self.prober.report().encoders
                        if c.name == encoder and c.verified), None)
        else:
            cap = self.pick_encoder(target_codec, want_10bit=False,
                                    prefer_hw=None)
        if cap is None:
            return none
        ffmpeg = self.engines.path("ffmpeg")
        if ffmpeg is None:
            return none
        astreams = [st for st in self.probe.audio_streams(src)
                    if (st.get("codec_name") or "").lower()]
        if audio_mode == "replace" and astreams:
            br = (audio_bitrate if audio_bitrate
                  and re.fullmatch(r"\d{1,4}k", audio_bitrate)
                  else AUDIO_REPLACE_BITRATE)
            audio_bps = int(br[:-1]) * 1000 * len(astreams)
        else:
            # stream-copied audio keeps the file's real bitrates (measured from
            # this file, not typed in)
            audio_bps = sum(int(st.get("bit_rate") or 128_000)
                            for st in astreams)
        sec = min(2.0, max(0.5, dur / 4.0))
        start = max(0.0, dur / 3.0)
        td = tempfile.mkdtemp(prefix="transmute-est-")
        out = Path(td) / "s.mkv"
        cmd: list[Any] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel",
                          "error", "-stats",
                          "-ss", f"{start:.2f}", "-i", src,
                          "-t", f"{sec:.2f}", "-map", "0:v:0", "-an",
                          "-c:v", cap.name, "-f", "matroska"]
        if crf is not None:
            # same refusal as convert(): never measure a degenerate setting
            lo_, hi_, _d = quality_bounds(cap.name)
            if not lo_ <= int(crf) <= hi_:
                return {**none, "note":
                        f"quality {crf} is outside {cap.name}'s usable range "
                        f"{lo_}-{hi_} - nothing measured"}
        crf_val = crf if crf is not None else DEFAULT_CRF.get(target_codec)
        if crf_val is not None:
            cmd += quality_flags(cap, crf_val)
            if preset and cap.name in WORD_PRESET_ENCODERS \
                    and preset in ENCODER_PRESETS:
                cmd += ["-preset", preset]
        if size_mb is not None:  # rate-control mode: sample at the job bitrate
            v_bps = max(int(size_mb * 8e6 / dur) - audio_bps, 100_000)
            cmd += ["-b:v", str(v_bps)]
        cmd += ["-y", str(out)]
        try:
            t0 = time.monotonic()
            res = run_command(cmd, timeout_s=120, cancel=stop)
            wall = (res.seconds if res is not None and res.seconds
                    else time.monotonic() - t0)
            stderr = ((res.stderr_text or "") + (res.stdout or "")) if res else ""
            frames = 0
            m = re.findall(r"frame=\s*(\d+)", stderr)
            if m:
                frames = int(m[-1])
            out_bytes = out.stat().st_size if out.exists() else 0
        finally:
            shutil.rmtree(td, ignore_errors=True)
        if stop is not None and stop.is_set():
            return none  # superseded mid-sample: no number, no claim
        if not frames or not out_bytes:
            if res is not None and res.timed_out:
                return {"ok": False, "est_s": 0.0, "bytes": 0.0,
                        "size_is_target": False, "sec": sec,
                        "note": "this PC could not finish even a 2 s sample "
                                "in 2 minutes - expect very slow conversions"}
            return none
        src_fps = 0.0
        v = self.probe.primary_video(src)
        if v:
            num, _, den = str(v.get("r_frame_rate", "")).partition("/")
            try:
                src_fps = float(num) / float(den or 1)
            except ValueError:
                src_fps = 0.0
        # ffmpeg's stats "fps" charges process startup + sample seek to the
        # encode - on a 2 s sample that flatters nothing but the error bar.
        # Per-frame wall time with the fixed cost (measured ~0.4 s) taken out
        # is the honest extrapolation for this machine on this file.
        frames_total = dur * src_fps if src_fps > 0 else 0.0
        if frames > 0 and frames_total > 0:
            wall_work = max(wall - 0.4, wall / 4)
            est_s = (wall_work / frames) * frames_total
        else:
            est_s = wall * (dur / sec)
        size = out_bytes * (dur / sec) + audio_bps * dur
        if size_mb is not None:
            # hit-your-target is the claim; the sample above proved the rate
            # control runs at usable speed on this machine
            size = size_mb * 1e6
        return {"ok": True, "est_s": est_s, "bytes": size,
                "size_is_target": size_mb is not None, "sec": sec, "note": ""}

    def _remux(self, src: Path, dst: Path, *, fmt: str, ffmpeg: Path,
               sv: dict, audio: str, ffmpeg_timeout_s: float,
               governor, log_path: Path | None, t0: float) -> EngineResult:
        """F2: container change, -c copy, PROVEN no re-encode by hash."""
        if audio != "copy":
            return EngineResult("failed",
                                reason="remux never re-encodes audio; drop the "
                                       "remux request to transcode audio")
        vcodec = (sv.get("codec_name") or "").lower()
        ok_v = REMUX_VIDEO_OK.get(fmt)
        if ok_v is not None and vcodec not in ok_v:
            return EngineResult(
                "failed",
                reason=f"'{vcodec}' cannot be copied into '{dst.suffix}' losslessly; "
                       f"remux never re-encodes - use mkv, or convert (re-encode) instead")
        acodecs = {(s.get("codec_name") or "").lower()
                   for s in self.probe.audio_streams(src)}
        acodecs.discard("")
        ok_a = REMUX_AUDIO_OK.get(fmt)
        if ok_a is not None:
            bad = sorted(acodecs - ok_a)
            if bad:
                return EngineResult(
                    "failed",
                    reason=f"audio stream(s) {bad} cannot be copied into '{dst.suffix}' "
                           f"losslessly; remux never re-encodes - use mkv, or convert "
                           f"with audio='replace'")

        cmd: list[Any] = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                          "-i", src,
                          "-map", "0", "-c", "copy",
                          "-map_metadata", "0", "-map_chapters", "0",
                          "-f", fmt]
        if fmt in FASTSTART:
            cmd += ["-movflags", "+faststart"]
        cmd += ["-y", dst]
        res = run_command(cmd, timeout_s=ffmpeg_timeout_s, log_path=log_path,
                          governor=governor, gpu=False)
        elapsed = time.monotonic() - t0
        if not res.ok:
            reason = "timed out" if res.timed_out else (
                _tail(res.stderr_text) or f"ffmpeg exit {res.returncode}")
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason=f"remux failed: {reason}",
                                seconds=elapsed, encoder="copy", log_path=log_path)
        if not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="remux produced no output file",
                                seconds=elapsed, encoder="copy", log_path=log_path)

        # ---- proof: the compressed bitstream itself must be identical ----
        proof = "unavailable"
        h_src = extract_video_bitstream_sha256(self.engines, src, vcodec)
        h_out = extract_video_bitstream_sha256(self.engines, dst, vcodec)
        if h_src and h_out:
            if h_src != h_out:
                dst.unlink(missing_ok=True)
                return EngineResult(
                    "failed",
                    reason="remux proof FAILED: output video bitstream differs from "
                           "source (re-encode or corruption detected)",
                    seconds=elapsed, encoder="copy", log_path=log_path)
            proof = "bitstream-hash"
        else:  # no raw muxer for this codec on this build: weaker tier, honest label
            m_src = packet_manifest_sha256(self.engines, src)
            m_out = packet_manifest_sha256(self.engines, dst)
            if m_src and m_out:
                if m_src != m_out:
                    dst.unlink(missing_ok=True)
                    return EngineResult(
                        "failed",
                        reason="remux proof FAILED: packet manifest differs between "
                               "source and output",
                        seconds=elapsed, encoder="copy", log_path=log_path)
                proof = "packet-manifest"
        return EngineResult("converted", output=dst, encoder="copy",
                            seconds=elapsed, log_path=log_path,
                            data={"remux": True, "container": fmt, "codec": vcodec,
                                  "remux_proof": proof,
                                  "bitstream_identical": proof == "bitstream-hash"})

    # ------------------------------------------------------------- verify (F24)
    def verify(self, src: Path, out: Path, *, expect_codec: str | None = None,
               allow_hdr_loss: bool = False,
               allow_duration_s: float = 0.5) -> EngineResult:
        """Structural verification BEFORE any replace: decodable, same duration,
        same resolution/framerate, same audio stream count, and no HDR->SDR
        downgrade UNLESS the tone-map was explicitly opted into (F15)."""
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
        if sv is not None and is_hdr_transfer(sv.get("color_transfer")) \
                and not allow_hdr_loss:
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
