"""Runtime capability probing - the zero-hardware-assumptions engine (F4/F10).

Nothing about the machine is assumed: which encoders exist is parsed from the
user's actual ffmpeg build; which of them ACTUALLY ENCODE is proven with a
1-second trial encode of a synthetic lavfi sample (the gate that catches the
nvenc build that links but cannot run, the vaapi AV1 driver that lies, the
qsv stack without a matching CPU/GPU). Results are cached and keyed to the
ffmpeg binary (path+size+mtime) so replacing/updating ffmpeg re-probes
automatically.

The probe MUST be valid on a machine with no GPU: device discovery is live
(/dev/dri render nodes, /dev/nvidia*), absent devices make GPU candidates
'absent' without even attempting, and TRANSMUTE_DISABLE_GPU=1 force-hides
them for testing that code path.

Depth/HDR claims (F9): software encoders' pixel-format lists are parsed
(yuv420p10le/12le present -> claims support); hardware encoders are reported
driver-dependent/untested until their targeted trial lands. zscale+tonemap
presence is recorded for F25 (opt-in tone-mapping), never applied silently.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import EngineUnavailable
from ..engines.base import CommandResult, run_command
from ..paths import paths
from .engines import EngineManager, STATUS_OK

SCHEMA_VERSION = 11  # 10 -> 11: audio encoder trials became REAL file
# round-trips (encode -> ffprobe duration). Old caches can carry encoders
# that pass a -f null sink and then silently truncate real files - measured
# with MediaFoundation mp3_mf on Windows (2.00s in, 0.60s out, exit 0).
# 9 -> 10: gpu_decode field added (GPU decode ladder rung);
# old caches lack it and must re-probe, not fake "no decode".
# 8 -> 9: benchmark sample raised to 1280x720 (numbers must
# reflect THIS machine under realistic load, not a tiny clip that flatters CPUs);
# a timed-out trial now means "works, too slow to measure", never "unavailable".
SCHEMA_VERSION_PREV = 8  # 7 -> 8: libx264-family trial used crf 55, which is
                    # out of x265's 0..51 range - HEVC showed as unavailable
                    # on machines with a perfectly working x265.
                    # 6 -> 7: hw trials rebuilt (320px sample, per-render-
                    # node candidates, yuv420p for nvenc/amf) - old caches
                    # carry falsely-unverified GPU rows. (the ONLY
                    # cache-invalidation lever - _PROBE_SALT is dead code)
_PROBE_SALT = "p3c"  # bump to invalidate caches when trial logic changes
# P3/F15: the ONLY tone-map chain the video engine may use, and the one the
# probe actually executes (measure, don't parse: a build can list zscale/tonemap
# yet fail to instantiate the chain, e.g. ffmpeg without libzimg).
TONEMAP_VFILTER = ("zscale=t=linear:npl=100,format=gbrpf32le,"
                   "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                   "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def _run_tonemap_trial(ffmpeg: Path) -> bool:
    # tonemap REQUIRES input tagged HDR (and >=10-bit): an untagged synthetic
    # source fails its input validation even on a fully capable build.
    # Tagging the probe input PQ/bt2020 makes the trial match real usage.
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-color_primaries", "bt2020", "-color_trc", "smpte2084",
           "-colorspace", "bt2020nc",
           "-f", "lavfi", "-i", "color=c=black:s=64x32:d=0.2,format=yuv420p10le",
           "-vf", TONEMAP_VFILTER, "-f", "null", "-"]
    return run_command(cmd, timeout_s=25).ok

# ---------------------------------------------------------------- encoder knowledge

_VIDEO_CODEC_OF_SUFFIX = {
    "av1_nvenc": "av1", "av1_amf": "av1", "av1_vaapi": "av1", "av1_qsv": "av1",
    "hevc_nvenc": "hevc", "hevc_amf": "hevc", "hevc_vaapi": "hevc", "hevc_qsv": "hevc",
    "h264_nvenc": "h264", "h264_amf": "h264", "h264_vaapi": "h264", "h264_qsv": "h264",
    "vp8_nvenc": "vp8", "vp8_vaapi": "vp8", "mpeg2_nvenc": "mpeg2video",
}
_HW_SUFFIXES = (
    ("_nvenc", "nvidia-nvenc"), ("_amf", "amd-amf"),
    ("_vaapi", "va-api"), ("_qsv", "intel-oneapi"),
)
_GPU_BACKENDS = {"nvidia-nvenc", "amd-amf", "va-api", "intel-oneapi"}

SW_ENCODER_INFO: dict[str, dict[str, Any]] = {
    # codec -> how to classify + trial args; unknown encoders get neutral trials.
    "libaom-av1": {"codec": "av1", "trial": ["-crf", "55", "-cpu-used", "8"]},
    "libsvtav1": {"codec": "av1", "trial": ["-crf", "55", "-preset", "12"]},
    # x264-family CRF is 0..51 - 55 (valid for av1/vpx) makes libx265 refuse
    # to open at all: the probe then reported HEVC unavailable on machines
    # whose x265 encodes fine. Measured, not assumed.
    "libx264": {"codec": "h264", "trial": ["-crf", "51", "-preset", "ultrafast"], "compat": True},
    "libx264rgb": {"codec": "h264", "trial": ["-crf", "51", "-preset", "ultrafast"], "compat": True},
    "libx265": {"codec": "hevc", "trial": ["-crf", "51", "-preset", "ultrafast"], "compat": True},
    "libopenh264": {"codec": "h264", "trial": ["-crf", "55"], "compat": True},
    "libvpx": {"codec": "vp8", "trial": ["-crf", "55", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8"]},
    "libvpx-vp9": {"codec": "vp9", "trial": ["-crf", "55", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8"]},
    "libkvazaar": {"codec": "hevc", "trial": [], "compat": True},
    "aac": {"codec": "aac", "trial": ["-b:a", "24k"], "compat": True},
    "libmp3lame": {"codec": "mp3", "trial": ["-b:a", "24k"], "compat": True},
    "libopus": {"codec": "opus", "trial": ["-b:a", "24k"]},
    "libvorbis": {"codec": "vorbis", "trial": ["-q:a", "2"]},
    "flac": {"codec": "flac", "trial": []},
    "alac": {"codec": "alac", "trial": []},
    "pcm_s16le": {"codec": "pcm_s16le", "trial": []},
    "png": {"codec": "png", "trial": []},
    "webp": {"codec": "webp", "trial": []},
}
AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "alac", "pcm_s16le"}
# ℹ️ compatibility/royalty-flagged codecs: fine when the user's system offers
# them, but recommended defaults live in the royalty-free set.
COMPAT_CODECS = {"h264", "hevc", "aac", "mp3", "vc1"}


def _classify(name: str, declared_codec: str | None) -> dict[str, Any]:
    """backend + codec + kind for an ffmpeg encoder name (from live lists)."""
    for suffix, backend in _HW_SUFFIXES:
        if name.endswith(suffix):
            codec = _VIDEO_CODEC_OF_SUFFIX.get(name)
            if codec is None:  # future encoder with same naming: derive by prefix
                prefix = name.rsplit("_", 1)[0]
                codec = {"av1": "av1", "hevc": "hevc", "h264": "h264", "vp8": "vp8",
                         "vp9": "vp9", "mpeg2": "mpeg2video"}.get(prefix, prefix)
            return {"codec": codec, "backend": backend, "hw": True}
    info = SW_ENCODER_INFO.get(name)
    if info:
        return {"codec": info["codec"], "backend": "cpu", "hw": False,
                "trial": list(info.get("trial", [])), "compat": bool(info.get("compat"))}
    if declared_codec:
        kind = "audio" if declared_codec in AUDIO_CODECS else "video"
        return {"codec": declared_codec, "backend": "cpu", "hw": False, "trial": [],
                "compat": declared_codec in COMPAT_CODECS, "kind": kind}
    return {"codec": declared_codec or name, "backend": "cpu", "hw": False, "trial": [],
            "compat": False}


# ---------------------------------------------------------------- structures

@dataclass
class EncoderCapability:
    name: str
    codec: str
    backend: str            # cpu | nvidia-nvenc | amd-amf | va-api | intel-oneapi
    hw: bool
    verified: bool          # trial encode SUCCEEDED on this machine
    fps: float | None       # measured trial speed (drives the F10 ladder)
    pix_fmts: list[str] = field(default_factory=list)
    depth: dict[str, bool] = field(default_factory=dict)  # {"8": T, "10": ?, "12": ?}
    compat_flag: bool = False
    rgb_output: bool = False  # probe-measured: ffmpeg lists only RGB pixel formats
    reason: str = ""        # why not verified (stderr tail), blank when verified
    skipped: str = ""       # why never attempted ("gpu-disabled", "no gpu device nodes")


@dataclass
class MatrixRow:
    category: str
    target: str             # codec or document target
    path: str               # engine/encoder chain label
    available: bool
    hw: bool = False
    verified: bool = False
    recommended: bool = False
    compat_flag: bool = False
    note: str = ""


@dataclass
class CapabilityReport:
    schema: int
    probe_key: str
    generated_at: float
    app_version: str
    machine: dict[str, str]
    engines: dict[str, dict[str, Any]]
    encoders: list[EncoderCapability]
    filters: dict[str, bool]
    gpu_nodes: dict[str, bool]
    image_backends: dict[str, Any]
    pdf_backends: dict[str, bool]
    caveats: list[str]
    matrix: list[MatrixRow]
    # {"attempted": bool, "available": bool, "api": str, "reason": str} -
    # GPU-DECODE verdict from an executed trial (ladder rung 1); {} when the
    # report predates the field (fresh probe always fills it)
    gpu_decode: dict[str, Any] = field(default_factory=dict)

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CapabilityReport":
        return cls(
            schema=d["schema"], probe_key=d["probe_key"],
            generated_at=d["generated_at"], app_version=d["app_version"],
            machine=d["machine"], engines=d["engines"],
            encoders=[EncoderCapability(**e) for e in d["encoders"]],
            filters=d["filters"], gpu_nodes=d["gpu_nodes"],
            image_backends=d["image_backends"], pdf_backends=d["pdf_backends"],
            caveats=d["caveats"], matrix=[MatrixRow(**r) for r in d["matrix"]],
            gpu_decode=d.get("gpu_decode") or {},
        )

    # -- queries ------------------------------------------------------------
    def verified_encoders(self, codec: str) -> list[EncoderCapability]:
        """Verified encoders for a codec, fastest measured first (F10 ladder)."""
        got = [e for e in self.encoders if e.verified and e.codec == codec]
        return sorted(got, key=lambda e: (e.fps is None, -(e.fps or 0.0), not e.hw))

    def row(self, category: str, target: str) -> MatrixRow | None:
        for r in self.matrix:
            if r.category == category and r.target == target:
                return r
        return None


# ---------------------------------------------------------------- probing

def _parse_encoder_list(stdout: str) -> list[tuple[str, str | None]]:
    """From 'ffmpeg -encoders': [(name, declared_codec)] - only enabled 'D' rows."""
    out: list[tuple[str, str | None]] = []
    in_table = False
    for line in stdout.splitlines():
        if line.strip().startswith("---"):  # table separator (' ------' - note leading space)
            in_table = True
            continue
        if not in_table or not line.strip():
            continue
        m = re.match(r"^\s*([VASBM])([.A-Z]{5})\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        name, rest = m.group(3), m.group(4)
        declared = None
        cm = re.search(r"\(codec ([a-z0-9_]+)\)", rest)
        if cm:
            declared = cm.group(1)
        out.append((name, declared))
    return out


def rgb_only(pix_fmts: list[str]) -> bool:
    """Probe-measured fact: ffmpeg offers this encoder ONLY RGB pixel formats
    (e.g. libx264rgb -> RGB h264 that fails many hardware players). Such an
    encoder must never win an automatic pick; an explicit user choice is
    honoured. Derived from `ffmpeg -h encoder=NAME`, never from a name guess."""
    return bool(pix_fmts) and not any("yuv" in f for f in pix_fmts)


def _parse_pix_fmts(ffmpeg: Path, encoder: str) -> list[str]:
    res = run_command([ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"],
                      timeout_s=20)
    for line in res.stdout.splitlines() + res.stderr_text.splitlines():
        if "pixel formats" in line:
            return line.split(":", 1)[1].split()
    return []


def _has_dri_render_node() -> bool:
    if sys.platform == "win32":
        return False  # Windows device model differs; trials run anyway (see _should_probe_hw)
    try:
        return any(Path("/dev/dri").glob("renderD*"))
    except OSError:
        return False


def _has_nvidia_nodes() -> bool:
    if sys.platform == "win32":
        return False
    try:
        return bool(list(Path("/dev").glob("nvidia*")))
    except OSError:
        return False


def gpu_force_disabled() -> bool:
    return os.environ.get("TRANSMUTE_DISABLE_GPU", "").lower() in ("1", "true", "yes")


class ProbeCancelled(RuntimeError):
    """User cancelled a running benchmark; caller keeps the previous report."""


_BENCH_BACKEND_WORDS = {
    "cpu": "your CPU", "nvidia-nvenc": "NVIDIA graphics",
    "va-api": "graphics (VA-API)", "intel-oneapi": "Intel Quick Sync",
    "amd-amf": "AMD graphics (AMF)",
}


def _render_nodes() -> list:
    try:
        return sorted(Path("/dev/dri").glob("renderD*"))
    except OSError:
        return []


# Public: the engine mirrors the probe's node walk at run time (2026-11) -
# whatever node the probe verified, the engine must find the same way.
render_nodes = _render_nodes


def _trial_cmds(ffmpeg: Path, cap: EncoderCapability) -> list[list[Any]]:
    """Candidate trial commands for one encoder; first success verifies it.

    Two hard-won facts about GPU encoders (both caught on real machines,
    not in the docs):
    * the sample must be >= ~144px wide - NVENC/HEVC-VAAPI reject 128x128
      frames with EINVAL while software encoders happily eat them;
    * a node-bound backend (VAAPI/QSV) may have NO capable engine on the
      first render node (multi-GPU: renderD128 = NVIDIA where VAAPI lacks
      HEVC entrypoints, renderD129 = AMD iGPU where it works). So every
      render node becomes its own candidate instead of blindly taking [0].
    """
    base: list[Any] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    frames: list[Any] = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1",
                         "-frames:v", "24"]
    tail = SW_ENCODER_INFO.get(cap.name, {}).get("trial", [])
    if cap.hw:
        if cap.backend == "va-api":
            nodes = _render_nodes() or [Path("/dev/dri/card0")]
            return [base + ["-init_hw_device", f"vaapi=va:{node}",
                            "-filter_hw_device", "va"]
                    + frames + ["-vf", "format=nv12,hwupload",
                                "-c:v", cap.name] + tail
                    + ["-f", "null", "-"] for node in nodes]
        if cap.backend == "intel-oneapi":
            nodes = _render_nodes() or [Path("/dev/dri/card0")]
            return [base + ["-init_hw_device", f"qsv=qs:{node}"]
                    + frames + ["-vf", "format=nv12,hwupload",
                                "-c:v", cap.name] + tail
                    + ["-f", "null", "-"] for node in nodes]
        # nvenc/amf take system frames (driver uploads internally);
        # forcing nv12 system frames is rejected - yuv420p is accepted.
        return [base + frames + ["-vf", "format=yuv420p", "-c:v", cap.name]
                + tail + ["-f", "null", "-"]]
    return [base + frames + ["-c:v", cap.name] + tail + ["-f", "null", "-"]]


def _trial_cmd_audio(ffmpeg: Path, cap: EncoderCapability) -> list[Any]:
    base: list[Any] = _trial_cmd_audio_sink(ffmpeg, cap, ["-f", "null", "-"])
    return base


def _trial_cmd_audio_sink(ffmpeg: Path, cap: EncoderCapability,
                          sink: list[Any]) -> list[Any]:
    base: list[Any] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    if cap.name in SW_ENCODER_INFO:
        base += SW_ENCODER_INFO[cap.name].get("trial", [])
    base += ["-c:a", cap.name] + sink
    return base


# Real muxers for the audio round-trip trial, by codec. Unknown codec or no
# ffprobe: fall back to the -f null sink (old behaviour).
_AUDIO_TRIAL_FMT: dict[str, str] = {"mp3": "mp3", "aac": "adts", "opus": "ogg",
                                    "vorbis": "ogg", "flac": "flac",
                                    "alac": "mov", "pcm_s16le": "wav"}


def _probe_duration_s(ffprobe: Path, path: Path) -> float | None:
    res = run_command([ffprobe, "-v", "error", "-show_format", "-of", "json",
                       str(path)], timeout_s=30)
    if not res.ok:
        return None
    try:
        return float(json.loads(res.stdout or "{}")["format"]["duration"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def _run_trial_audio(ffmpeg: Path, cap: EncoderCapability,
                     ffprobe: Path | None) -> tuple[bool, float | None, str]:
    """Audio encoders are verified by a REAL round-trip, not a null sink.

    Measured on the WinBoat VM (Windows hunt, 2026-11): MediaFoundation's
    mp3_mf happily 'encodes' into -f null with exit 0 and measures fast -
    then truncates real files to a fraction of the source while STILL
    exiting 0. Only writing an actual file and asking ffprobe how long it
    came out catches that class of liar, and audio is exactly where silent
    truncation destroys the user's trust.
    """
    fmt = _AUDIO_TRIAL_FMT.get(cap.codec)
    if ffprobe is None or fmt is None:
        return _run_trial(_trial_cmd_audio(ffmpeg, cap))
    with tempfile.TemporaryDirectory(prefix="transmute-trial-") as td:
        out = Path(td) / f"trial.{fmt}"
        ok, fps, reason = _run_trial(
            _trial_cmd_audio_sink(ffmpeg, cap, ["-f", fmt, str(out)]))
        if not ok:
            return ok, fps, reason
        if fps is None:   # timed out: doctrine says 'ran, unmeasured'
            return ok, fps, reason
        try:
            size = out.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return False, None, "trial wrote an empty file"
        got = _probe_duration_s(ffprobe, out)
        if got is None:
            return False, None, "trial output is not decodable"
        if got < 1.6:     # encoded >=80% of the 2-second clip
            return False, None, (f"trial output truncated "
                                 f"({got:.2f}s of 2.00s written)")
        return True, fps, ""


def _run_trial(cmd: list[Any]) -> tuple[bool, float | None, str]:
    """Returns (verified, fps, reason).

    fps is MEASURED wall-clock: 24 frames / seconds of the trial encode.
    The launch overhead is constant across encoders, so the ladder ordering
    is fair; we deliberately do NOT parse ffmpeg's 'speed=' line because
    -loglevel error suppresses it and stderr must stay clean for reasons.
    """
    res = run_command(cmd, timeout_s=60)
    if res.ok:
        fps = 24.0 / max(res.seconds, 0.01)
        return True, fps, ""
    if res.timed_out:
        # Multi-hardware honesty: a timeout proves the encoder RAN (ffmpeg
        # alive, codec accepted) - it does not prove absence. Weak machines
        # must see the codec as available, speed unmeasured, never falsely
        # "unavailable: timed out" (user directive: measure THEIR hardware,
        # never penalise it).
        return True, None, "ran, but slower than the 60 s benchmark cap"
    tail = _stderr_tail(res.stderr_text)
    return False, None, tail or f"trial encode failed (exit {res.returncode})"


def _run_trials(cmds: list[list[Any]]) -> tuple[bool, float | None, str]:
    """Try each candidate command; verified if ANY works (one per render
    node - the reason kept is from the last node so a mixed GPU box never
    claims 'entrypoint missing' when some node encoded fine a moment ago)."""
    reason = ""
    got: tuple[bool, float | None, str] = (False, None, "")
    for cmd in cmds:
        ok, fps, r = _run_trial(cmd)
        if ok:
            got = (True, fps, r)
            if fps is not None:
                return got
            # unmeasured-but-working: keep probing other candidates (e.g. a
            # second render node) in search of a measured number
        reason = r or reason
    if got[0]:
        return got
    return False, None, reason


def _stderr_tail(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    tail = " | ".join(lines[-2:])
    return tail[-limit:]


def _depth_claims(pix_fmts: list[str], hw: bool) -> dict[str, bool]:
    if hw:
        # Driver-dependent (VA-API caps vary per GPU/driver): honest 'untested'.
        return {"8": False, "10": False, "12": False}
    fmts = set(pix_fmts)
    return {
        "8": bool(fmts & {"yuv420p", "yuvj420p", "gbrp", "rgb24"}),
        "10": "yuv420p10le" in fmts or "gbrp10le" in fmts,
        "12": "yuv420p12le" in fmts or "gbrp12le" in fmts,
    }


def _probe_pillow() -> dict[str, Any]:
    """Which image formats can Pillow SAVE here - MEASURED, never assumed.

    Pillow 13 removed both features.check() entries (png/gif/webp_anim) and the
    public Image.formats registry, so the only trustworthy signal left is
    attempting an actual in-memory encode (single-digit ms per format).
    """
    import io

    out: dict[str, Any] = {}
    try:
        import PIL
        import PIL.Image as I
        try:
            I.preinit()
        except Exception:  # noqa: BLE001
            pass
        out["version"] = getattr(PIL, "__version__", "")

        def can_save(fmt: str, mode: str = "RGB") -> bool:
            try:
                buf = io.BytesIO()
                I.new(mode, (2, 2)).save(buf, format=fmt)
                return buf.tell() > 0
            except Exception:  # noqa: BLE001
                return False

        checks: dict[str, Any] = {
            "png": can_save("PNG", "RGBA"),
            "gif": can_save("GIF", "P"),
            "jpg": can_save("JPEG"),
            "webp": can_save("WEBP", "RGBA"),
            "avif": can_save("AVIF", "RGBA"),
            "pdf": can_save("PDF"),
        }
        # animated WebP: two DISTINCT frames in, multiple frames back out
        # (identical frames get deduplicated by libwebp - false negative trap)
        checks["webp_anim"] = False
        if checks["webp"]:
            try:
                fr = [I.new("RGB", (2, 2), c) for c in ((255, 0, 0), (0, 255, 0))]
                buf = io.BytesIO()
                fr[0].save(buf, format="WEBP", save_all=True,
                           append_images=fr[1:], duration=100, loop=0)
                buf.seek(0)
                with I.open(buf) as anim:
                    checks["webp_anim"] = getattr(anim, "n_frames", 1) > 1
            except Exception:  # noqa: BLE001
                pass
        out["features"] = checks
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["features"] = {}
    return out


def _probe_ffmpeg_filters(ffmpeg: Path) -> dict[str, bool]:
    res = run_command([ffmpeg, "-hide_banner", "-filters"], timeout_s=25)
    have = {"zscale": False, "tonemap": False, "tonemap_chain": False}
    # ffmpeg's flag-column width varies by version (2 chars in ffmpeg 9) -
    # anchor on the IO spec (e.g. "V->V") instead of a fixed flag width.
    pat = re.compile(r"^\s*[.TSCCUHB]+\s+([a-z0-9_]+)\s+[A-Z]{1,2}->[A-Z]{1,2}\b")
    for line in res.stdout.splitlines():
        m = pat.match(line)
        if m and m.group(1) in have:
            have[m.group(1)] = True
    if have["zscale"] and have["tonemap"]:
        # list-check is necessary, not sufficient: run the actual chain.
        have["tonemap_chain"] = _run_tonemap_trial(ffmpeg)
    return have


# ---------------------------------------------------------------- decode probe
# Ladder rung 1 (GPU decode offload): the engine adds -hwaccel ONLY when this
# trial proves the machine really decodes H.264 on its GPU. cuda is tried
# first where NVIDIA nodes exist (NVDEC); VA-API covers AMD/Intel render
# nodes. On Windows the device model is D3D11: d3d11va/dxva2 need no nodes
# (live-fire: Ryzen AI 9 HX 370 / Radeon 880M reported "no GPU decode
# device nodes here" because only POSIX APIs were on this list).
_DECODE_APIS: tuple[tuple[str, str], ...] = (("cuda", "nvidia"),
                                             ("vaapi", "dri_render"),
                                             ("d3d11va", "win"),
                                             ("dxva2", "win"))


def _decode_api_allowed(node: str) -> bool:
    """Gate a decode trial by this OS's device model. Windows has no
    /dev/dri - D3D11VA/DXVA2 need nothing, and CUDA is worth a try on any
    NVIDIA box even without POSIX nodes; the executed trial (frame-format
    change) decides, never this gate alone."""
    if sys.platform == "win32":
        return node in ("win", "nvidia")
    if node == "nvidia":
        return _has_nvidia_nodes()
    if node == "dri_render":
        return _has_dri_render_node()
    return False  # "win" APIs have no device model on POSIX to try


def _decoded_pix_fmt(text: str) -> str:
    """Pixel format of the DECODED stream from ffmpeg's output stream line
    ("Video: wrapped_avframe, nv12(...)"). Empty when the line is absent."""
    m = re.search(r"Video:\s+\S+,\s*([a-z0-9_]+)[,(]", text or "")
    return m.group(1) if m else ""


def _hwaccel_route(text: str) -> str:
    """The GPU-side format in ffmpeg's decode route, "(cuda -> nv12)" /
    "(d3d11va -> d3d11)" - the arrow only appears when an hwaccel decoded.
    Empty for plain CPU decode (no arrow on the stream line)."""
    m = re.search(r"\([a-z0-9_]+\s*->\s*([a-z0-9_]+)\)", text or "")
    return m.group(1) if m else ""


def _probe_gpu_decode(ffmpeg: Path) -> dict[str, Any]:
    """{"attempted","available","api","reason"} - executed-trial truth.

    Belief rule: this ffmpeg names the decoder "h264 (native)" even while
    NVDEC/VAAPI do the work (Lavc63 routes hwaccel under the native codec),
    so the stream line lies. The decoded FRAME FORMAT cannot: GPU decode
    hands back GPU frames (nv12/vaapi...), CPU decode hands back yuv420p.
    The trial is only believed when the format CHANGES from the plain-CPU
    baseline run - a driver that declines keeps the baseline format and
    gets honestly reported as "no GPU decode"."""
    out_dir = tempfile.mkdtemp(prefix="transmute-dec-")
    sample = Path(out_dir) / "sample.mp4"
    try:
        made = run_command(
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=blue:s=640x480:r=25:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-preset", "ultrafast", sample], timeout_s=60, gpu=True)
        if not (made.ok and sample.is_file()):
            return {"attempted": False, "available": False, "api": "",
                    "reason": "could not make the H.264 decode test sample "
                              "(software encoder unavailable?)"}
        base = run_command([ffmpeg, "-hide_banner", "-nostdin",
                            "-i", sample, "-f", "null", "-"],
                           timeout_s=60, gpu=True)
        base_fmt = _decoded_pix_fmt(base.stderr_text)
        if not (base.ok and base_fmt):
            return {"attempted": False, "available": False, "api": "",
                    "reason": "CPU decode baseline run failed - nothing to "
                              "compare a GPU trial against"}
        notes: list[str] = []
        for api, node in _DECODE_APIS:
            if not _decode_api_allowed(node):
                continue
            res = run_command([ffmpeg, "-hide_banner", "-nostdin",
                               "-hwaccel", api, "-i", sample,
                               "-f", "null", "-"],
                              timeout_s=60, gpu=True)
            fmt = _decoded_pix_fmt(res.stderr_text)
            # Windows caveat: -f null DOWNLOADS GPU frames, so the output
            # format can be the plain yuv420p baseline even though the GPU
            # decoded. ffmpeg's stream line routes the work honestly:
            # "(d3d11va -> d3d11)" - a GPU intermediate format is believed.
            route = _hwaccel_route(res.stderr_text)
            gpu_evidence = bool(route and route not in ("yuv420p", "none"))
            if res.ok and ((fmt and fmt != base_fmt) or gpu_evidence):
                return {"attempted": True, "available": True, "api": api,
                        "reason": ""}
            if res.ok:
                notes.append(f"{api}: ffmpeg accepted the flag but still "
                             f"decoded to CPU frames ({fmt or 'no stream line'})")
            else:
                tail = _stderr_tail(res.stderr_text)
                notes.append(f"{api}: {tail or f'exit {res.returncode}'}")
        return {"attempted": True, "available": False, "api": "",
                "reason": "; ".join(notes) or "no GPU decode device nodes here"}
    finally:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)


def _device_token() -> str:
    """Count of GPU device nodes, as a cache-key ingredient. Cheap stat
    globbing, no subprocess; on platforms without /dev it is a constant
    string (the probe's own device enumeration is what varies there)."""
    import glob as _glob
    return "".join(f"{pat}={len(_glob.glob(pat))};"
                   for pat in ("/dev/dri/renderD*", "/dev/dri/card*",
                               "/dev/nvidia*"))


def _probe_key(ffmpeg_path: Path | None, ffprobe_path: Path | None) -> str:
    h = hashlib.sha256()
    h.update(_PROBE_SALT.encode())
    h.update(f"py{sys.version_info.major}.{sys.version_info.minor}".encode())
    for p in (ffmpeg_path, ffprobe_path):
        if p is None:
            h.update(b"missing;")
            continue
        try:
            st = os.stat(p)
            h.update(f"{p}|{st.st_size}|{st.st_mtime_ns};".encode())
        except OSError:
            h.update(f"{p}?;".encode())
    h.update(("gpu=off;" if gpu_force_disabled() else "gpu=on;").encode())
    # GPU device PRESENCE belongs to the machine identity too: a probe that
    # ran with the GPU powered off cached its "no GPU device node" verdict
    # indefinitely - same binaries, same gpu=on lane, no way back.
    h.update(_device_token().encode())
    try:  # include Pillow build identity: WebP/AVIF support can change on reinstall
        import PIL
        h.update(f"pil{PIL.__version__};".encode())
    except Exception:  # noqa: BLE001
        pass
    return h.hexdigest()[:32]


class CapabilityProber:
    """Builds (or loads cached) CapabilityReport. load_report() handles cache."""

    def __init__(self, engines: EngineManager | None = None):
        self.engines = engines or EngineManager()
        self._progress = None
        self._cancel = None

    def _note(self, done: int, total: int, what: str) -> None:
        if self._progress is not None:
            self._progress(done, total, what)

    # -- public ------------------------------------------------------------
    def report(self, refresh: bool = False, progress=None,
               cancel=None) -> CapabilityReport:
        """Cached report when valid; re-probes when ffmpeg/Pillow identities
        change. progress(done:int,total:int,what:str) is called from the
        CALLING thread (benchmark UI needs a real bar, not vibes);
        cancel: Callable[[],bool] -> abandons the re-probe with
        ProbeCancelled."""
        store = CapabilityCache(paths().capability_cache)
        self._progress = progress
        self._cancel = cancel
        self._note(0, 0, "looking up installed engines")
        engines = self.engines.detect()
        ffmpeg = engines["ffmpeg"].path if engines["ffmpeg"].status == STATUS_OK else None
        ffprobe = engines["ffprobe"].path if engines["ffprobe"].status == STATUS_OK else None
        key = _probe_key(ffmpeg, ffprobe)
        if not refresh:
            cached = store.load(key)
            if cached is not None:
                return cached
        rep = self._build(engines, ffmpeg, ffprobe, key)
        store.save(rep)
        return rep

    # -- internals ----------------------------------------------------------
    def _build(self, engines: dict, ffmpeg: Path | None, ffprobe: Path | None,
               key: str) -> CapabilityReport:
        caveats: list[str] = []
        gpu_nodes = {"dri_render": _has_dri_render_node(), "nvidia": _has_nvidia_nodes()}
        disable_gpu = gpu_force_disabled()

        caps: list[EncoderCapability] = []
        filters = {"zscale": False, "tonemap": False, "tonemap_chain": False}
        if ffmpeg is None:
            caveats.append("ffmpeg not found: no codec capability can be probed; "
                           "video/audio matrix rows will be unavailable.")
        else:
            enc_list = run_command([ffmpeg, "-hide_banner", "-encoders"], timeout_s=25)
            if not enc_list.ok:
                caveats.append("ffmpeg -encoders failed; capability report is partial.")
            declared = _parse_encoder_list(enc_list.stdout) if enc_list.ok else []
            total = len(declared)
            self._note(0, total, "looking up encoders on this machine")
            # device gate: on POSIX, no device nodes => nothing for hw encoders to open.
            hw_possible = gpu_nodes["dri_render"] or gpu_nodes["nvidia"]
            for i, (name, codec) in enumerate(declared, 1):
                if self._cancel is not None and self._cancel():
                    raise ProbeCancelled("benchmark cancelled")
                cls = _classify(name, codec)
                self._note(i - 1, total,
                           _BENCH_BACKEND_WORDS.get(cls["backend"],
                                                    cls["backend"]))
                cap = EncoderCapability(name=name, codec=cls["codec"], backend=cls["backend"],
                                        hw=cls["hw"], verified=False, fps=None,
                                        compat_flag=cls.get("compat", False))
                if cap.hw and disable_gpu:
                    cap.skipped = "gpu-probe-disabled (TRANSMUTE_DISABLE_GPU)"
                    caps.append(cap)
                    continue
                if cap.hw and sys.platform != "win32" and not hw_possible:
                    cap.skipped = "no GPU device nodes present"
                    caps.append(cap)
                    continue
                # video vs audio trial
                if cap.codec in AUDIO_CODECS:
                    ok, fps, reason = _run_trial_audio(ffmpeg, cap, ffprobe)
                elif cap.backend == "cpu" and cap.codec in ("png", "webp"):
                    ok, fps, reason = (False, None, "")
                    cap.skipped = "image encoding handled via Pillow path"
                else:
                    ok, fps, reason = _run_trials(_trial_cmds(ffmpeg, cap))
                    if ok:
                        # Measured for every verified VIDEO encoder: supported
                        # pixel formats decide bit-depth claims AND whether the
                        # encoder is RGB-only (ffmpeg reports plain bgr/rgb
                        # formats, e.g. libx264rgb -> RGB h264 that fails many
                        # hw players - must never win an automatic pick).
                        cap.pix_fmts = _parse_pix_fmts(ffmpeg, cap.name)
                        cap.depth = _depth_claims(cap.pix_fmts, cap.hw)
                        cap.rgb_output = rgb_only(cap.pix_fmts)
                cap.verified = ok
                cap.fps = fps
                cap.reason = reason if not ok else ""
                caps.append(cap)
            self._note(total, total, "checking HDR tone-map chain")
            filters = _probe_ffmpeg_filters(ffmpeg)

        if disable_gpu:
            caveats.append("GPU probing disabled by TRANSMUTE_DISABLE_GPU "
                           "(used to validate the GPU-less code path).")
        # GPU decode rung: executed trial, recorded like everything else -
        # the engine will not add -hwaccel before this says it truly decodes.
        if ffmpeg is None:
            gpu_decode: dict[str, Any] = {
                "attempted": False, "available": False, "api": "",
                "reason": "no ffmpeg on this machine"}
        elif disable_gpu:
            gpu_decode = {"attempted": False, "available": False, "api": "",
                          "reason": "GPU disabled (TRANSMUTE_DISABLE_GPU)"}
        elif self._cancel is not None and self._cancel():
            raise ProbeCancelled("benchmark cancelled")
        else:
            self._note(1, 1, "trying GPU video decode on this machine")
            gpu_decode = _probe_gpu_decode(ffmpeg)
        if gpu_decode.get("attempted") and not gpu_decode.get("available"):
            caveats.append("GPU decode trial: this machine's GPU would not "
                           "decode H.264 for us - conversions decode on CPU "
                           f"({gpu_decode.get('reason', '')[:120]})")
        if not gpu_nodes["dri_render"] and not gpu_nodes["nvidia"] and not disable_gpu \
                and sys.platform != "win32":
            caveats.append("No GPU device nodes on this machine: hardware encode rows "
                           "are hidden; software encoders carry all conversions.")

        image_backends = _probe_pillow()
        pdf_backends = {
            "pikepdf": _module_ok("pikepdf"),
            "qpdf": engines.get("qpdf") and engines["qpdf"].status == STATUS_OK,
            "ghostscript": engines.get("gs") and engines["gs"].status == STATUS_OK,
            "pdftoppm": engines.get("pdftoppm") and engines["pdftoppm"].status == STATUS_OK,
            "pdftotext": engines.get("pdftotext") and engines["pdftotext"].status == STATUS_OK,
        }
        rep = CapabilityReport(
            schema=SCHEMA_VERSION, probe_key=key, generated_at=time.time(),
            app_version=__version__,
            machine={"os": os.name, "platform": platform.system().lower(),
                     "release": platform.release(), "machine": platform.machine(),
                     "python": sys.version.split()[0]},
            engines={n: {"status": i.status, "version": i.version,
                         "path": str(i.path) if i.path else None,
                         "optional": KNOWN_OPTIONAL.get(n, False)}
                     for n, i in engines.items()},
            encoders=caps, filters=filters, gpu_nodes=gpu_nodes,
            image_backends=image_backends, pdf_backends={k: bool(v) for k, v in pdf_backends.items()},
            caveats=caveats, matrix=[], gpu_decode=gpu_decode,
        )
        rep.matrix = build_matrix(rep)
        return rep


KNOWN_OPTIONAL: dict[str, bool] = {"ffmpeg": False, "ffprobe": False, "qpdf": True,
                                   "gs": True, "pdftoppm": True, "pdftotext": True,
                                   "libreoffice": True,
                                   "pandoc": True}


def _module_ok(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- matrix (F4 draft)

_REC_VIDEO = {"av1": "royalty-free AV1 (recommended default)",
              "vp9": "royalty-free VP9"}
_REC_AUDIO = {"opus": "Opus (recommended)", "flac": "FLAC (lossless)"}


def build_matrix(rep: CapabilityReport) -> list[MatrixRow]:
    """Draft capability matrix. One row per (category,target,path) the user
    could possibly pick; availability comes ONLY from live probe data."""
    rows: list[MatrixRow] = []
    ffmpeg_ok = rep.engines.get("ffmpeg", {}).get("status") == STATUS_OK
    by_target: dict[str, list[EncoderCapability]] = {}
    for e in rep.encoders:
        by_target.setdefault(e.codec, []).append(e)

    for codec in ("av1", "vp9", "h264", "hevc"):
        cands = by_target.get(codec, [])
        if not cands:
            rows.append(MatrixRow("video", codec, "ffmpeg", False,
                                  note="no encoder in this ffmpeg build"))
            continue
        for c in sorted(cands, key=lambda c: (not c.verified, not c.hw, c.name)):
            rows.append(MatrixRow(
                "video", codec, f"ffmpeg/{c.name}", available=c.verified, hw=c.hw,
                verified=c.verified, recommended=(not c.hw and c.backend == "cpu" and codec in _REC_VIDEO),
                compat_flag=c.compat_flag,
                note=("" if c.verified else (c.skipped or c.reason or "trial encode failed"))[:300],
            ))

    for codec in ("opus", "flac", "vorbis", "mp3", "aac", "alac"):
        cands = by_target.get(codec, [])
        if not cands:
            rows.append(MatrixRow("audio", codec, "ffmpeg", False,
                                  note="no encoder in this ffmpeg build"))
            continue
        best = next((c for c in cands if c.verified), cands[0])
        rows.append(MatrixRow(
            "audio", codec, f"ffmpeg/{best.name}", available=best.verified,
            verified=best.verified, recommended=codec in _REC_AUDIO,
            compat_flag=codec in COMPAT_CODECS,
            note="" if best.verified else (best.reason or "trial encode failed")[:300],
        ))

    feat = (rep.image_backends or {}).get("features", {})
    rows.append(MatrixRow("image", "webp", "Pillow/WebP", available=bool(feat.get("webp")),
                          recommended=True,
                          note="" if feat.get("webp") else "Pillow lacks WebP support"))
    rows.append(MatrixRow("image", "webp(animated)", "Pillow/WebP",
                          # animated GIF->WebP is saved by the same WebP
                          # backend the engine gates on (_supports("webp")):
                          # inferring it from GIF support showed this row
                          # AVAILABLE on builds whose engine then refused
                          available=bool(feat.get("webp")),
                          note="animated GIF -> animated WebP keeps frames/loop"
                               if feat.get("webp") else
                               "Pillow lacks WebP support"))
    rows.append(MatrixRow("image", "avif", "Pillow/AVIF", available=bool(feat.get("avif")),
                          recommended=bool(feat.get("avif")),
                          note="shown only if the local Pillow build supports AVIF"))
    rows.append(MatrixRow("image", "png", "Pillow/PNG", available=bool(feat.get("png"))))
    # Container change, not compression: availability is only "can THIS
    # Pillow write a PDF" - proven by writing one in the probe (ms).
    rows.append(MatrixRow("image", "pdf (page)", "Pillow/PDF",
                          available=bool(feat.get("pdf")),
                          note="one page per picture; transparency becomes white"
                               if feat.get("pdf") else "local Pillow build cannot write PDF"))

    any_pdf = rep.pdf_backends.get("pikepdf") or rep.pdf_backends.get("qpdf") \
        or rep.pdf_backends.get("ghostscript")
    rows.append(MatrixRow("pdf", "pdf (lossless smaller)", "pikepdf -> qpdf -> ghostscript",
                          available=bool(any_pdf),
                          note="pixel-identical lossless recompression; verify + min-savings"
                               if any_pdf else "install qpdf or ghostscript for extra engines"))
    verify_ok = rep.pdf_backends.get("pdftoppm") or rep.pdf_backends.get("pikepdf")
    if not verify_ok:
        rows.append(MatrixRow("pdf", "pdf (verify renders)", "pdftoppm", False,
                              note="install poppler (pdftoppm) for pixel-level verification"))
    # Export TRANSFORMS (not compression): poppler renders/extracts exactly what
    # the file contains. Availability still comes only from the live probe (F3).
    ppm_ok = bool(rep.pdf_backends.get("pdftoppm"))
    why_ppm = "" if ppm_ok else "pdftoppm (poppler-utils) not found on this machine"
    rows.append(MatrixRow("pdf", "png (per page)", "pdftoppm", available=ppm_ok,
                          note=why_ppm or "renders each page exactly as a PDF viewer shows it"))
    rows.append(MatrixRow("pdf", "jpg (per page)", "pdftoppm", available=ppm_ok,
                          note=why_ppm or "smaller page images (JPEG quality trade-in)"))
    txt_ok = bool(rep.pdf_backends.get("pdftotext"))
    rows.append(MatrixRow("pdf", "txt (text)", "pdftotext", available=txt_ok,
                          note="pdftotext (poppler-utils) not found on this machine"
                          if not txt_ok else
                          "extracts embedded text - scanned PDFs have none (use PNG/JPEG)"))

    doc_paths = []
    lo_ok = rep.engines.get("libreoffice", {}).get("status") == STATUS_OK
    pan_ok = rep.engines.get("pandoc", {}).get("status") == STATUS_OK
    if lo_ok:
        doc_paths.append(("LibreOffice", True))
    if pan_ok:
        doc_paths.append(("pandoc", False))
    if not doc_paths:
        rows.append(MatrixRow("document", "pdf", "LibreOffice/pandoc", False,
                              note="install LibreOffice or pandoc for document conversion"))
    for name, direct in doc_paths:
        rows.append(MatrixRow("document", "pdf", name, available=True,
                              note="native PDF export" if direct
                              else "pandoc needs a PDF engine (LaTeX) on the system"))
    # A2: office/markup EXPORTS. Targets stay visible with this machine's
    # reason (F3); per-source routing happens in the engine at convert time.
    why_none = "install LibreOffice or pandoc for document conversion"
    rows.append(MatrixRow("document", "docx", "LibreOffice/pandoc",
                          available=lo_ok or pan_ok,
                          note=why_none if not (lo_ok or pan_ok) else
                          "Word format - LibreOffice from office files, pandoc from Markdown/HTML/text"))
    rows.append(MatrixRow("document", "odt", "LibreOffice/pandoc",
                          available=lo_ok or pan_ok,
                          note=why_none if not (lo_ok or pan_ok) else
                          "OpenDocument text - office sources need LibreOffice"))
    rows.append(MatrixRow("document", "rtf", "LibreOffice/pandoc",
                          available=lo_ok or pan_ok,
                          note=why_none if not (lo_ok or pan_ok) else
                          "rich text - opens in almost every word processor"))
    rows.append(MatrixRow("document", "html", "LibreOffice/pandoc",
                          available=lo_ok or pan_ok,
                          note=why_none if not (lo_ok or pan_ok) else
                          "web page - either tool converts markup; office files need LibreOffice"))
    rows.append(MatrixRow("document", "markdown", "pandoc", available=pan_ok,
                          note="pandoc not found on this machine" if not pan_ok else
                          "plain Markdown text - pandoc reads Word/ODT/HTML too"))
    rows.append(MatrixRow("document", "txt", "LibreOffice/pandoc",
                          available=lo_ok or pan_ok,
                          note=why_none if not (lo_ok or pan_ok) else
                          "plain text - formatting is dropped"))
    return rows


# ---------------------------------------------------------------- cache

class CapabilityCache:
    """One JSON per probe-key under cache dir; newest matching key wins."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, rep: CapabilityReport) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"{rep.probe_key}.json"
        # Unique tmp per save: queue workers may cache the same probe key
        # concurrently, and one shared ".json.tmp" name makes the loser of
        # os.replace crash on the winner's gone tmp (P5 soak caught this).
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rep.to_dict(), fh, ensure_ascii=False, indent=1)
            try:
                os.replace(tmp, p)
            except FileNotFoundError:
                pass  # another writer won the race - its content is equivalent
        except OSError:
            pass  # cache save is best-effort; the report lives in memory anyway
        finally:
            Path(tmp).unlink(missing_ok=True)
        return p

    def load(self, key: str) -> CapabilityReport | None:
        p = self.root / f"{key}.json"
        if not p.is_file():
            return None
        try:
            rep = CapabilityReport.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - corrupt cache -> re-probe
            return None
        if rep.schema != SCHEMA_VERSION or rep.app_version != __version__:
            return None
        return rep

    def invalidate(self) -> None:
        try:
            for f in self.root.glob("*.json"):
                f.unlink(missing_ok=True)
        except OSError:
            pass
