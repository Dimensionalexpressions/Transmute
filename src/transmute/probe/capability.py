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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import EngineUnavailable
from ..engines.base import CommandResult, run_command
from ..paths import paths
from .engines import EngineManager, STATUS_OK

SCHEMA_VERSION = 4
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
    "libx264": {"codec": "h264", "trial": ["-crf", "55", "-preset", "ultrafast"], "compat": True},
    "libx264rgb": {"codec": "h264", "trial": ["-crf", "55", "-preset", "ultrafast"], "compat": True},
    "libx265": {"codec": "hevc", "trial": ["-crf", "55", "-preset", "ultrafast"], "compat": True},
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
        )

    # -- queries ------------------------------------------------------------
    def verified_encoders(self, codec: str) -> list[EncoderCapability]:
        """Verified encoders for a codec, fastest measured first (F10 ladder)."""
        got = [e for e in self.encoders if e.verified and e.codec == codec]
        return sorted(got, key=lambda e: -(e.fps or 0))

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


def _trial_cmd(ffmpeg: Path, cap: EncoderCapability) -> list[Any]:
    """Synthetic 1s sample -> trial encode; hw paths upload via GPU."""
    base: list[Any] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if cap.hw:
        if cap.backend == "va-api":
            node = None
            try:
                renders = sorted(Path("/dev/dri").glob("renderD*"))
                node = str(renders[0]) if renders else "/dev/dri/card0"
            except OSError:
                node = "/dev/dri/card0"
            base += ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va"]
            vf = "scale=128:128,format=nv12,hwupload"
        elif cap.backend == "intel-oneapi":
            try:
                renders = sorted(Path("/dev/dri").glob("renderD*"))
                dev = str(renders[0]) if renders else ""
            except OSError:
                dev = ""
            if dev:
                base += ["-init_hw_device", f"qsv=qs:{dev}"]
            vf = "scale=128:128,format=nv12,hwupload"
        else:  # nvenc/amf take system frames (sw decode -> GPU enc path internally)
            vf = "scale=128:128,format=nv12"
        base += ["-f", "lavfi", "-i", "testsrc2=size=128x128:rate=25:duration=1",
                 "-frames:v", "24", "-vf", vf, "-c:v", cap.name]
    else:
        base += ["-f", "lavfi", "-i", "testsrc2=size=128x128:rate=25:duration=1",
                 "-frames:v", "24", "-c:v", cap.name]
    if cap.name in SW_ENCODER_INFO:
        base += SW_ENCODER_INFO[cap.name].get("trial", [])
    base += ["-f", "null", "-"]
    return base


def _trial_cmd_audio(ffmpeg: Path, cap: EncoderCapability) -> list[Any]:
    base: list[Any] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if cap.name in SW_ENCODER_INFO:
        base += SW_ENCODER_INFO[cap.name].get("trial", [])
    base += ["-c:a", cap.name, "-f", "null", "-"]
    return base


def _run_trial(cmd: list[Any]) -> tuple[bool, float | None, str]:
    """Returns (verified, fps, reason).

    fps is MEASURED wall-clock: 24 frames / seconds of the trial encode.
    The launch overhead is constant across encoders, so the ladder ordering
    is fair; we deliberately do NOT parse ffmpeg's 'speed=' line because
    -loglevel error suppresses it and stderr must stay clean for reasons.
    """
    res = run_command(cmd, timeout_s=25)
    if res.ok:
        fps = 24.0 / max(res.seconds, 0.01)
        return True, fps, ""
    if res.timed_out:
        return False, None, "trial encode timed out (25s)"
    tail = _stderr_tail(res.stderr_text)
    return False, None, tail or f"trial encode failed (exit {res.returncode})"


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

    # -- public ------------------------------------------------------------
    def report(self, refresh: bool = False) -> CapabilityReport:
        """Cached report when valid; re-probes when ffmpeg/Pillow identities change."""
        store = CapabilityCache(paths().capability_cache)
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
            # device gate: on POSIX, no device nodes => nothing for hw encoders to open.
            hw_possible = gpu_nodes["dri_render"] or gpu_nodes["nvidia"]
            for name, codec in declared:
                cls = _classify(name, codec)
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
                    ok, fps, reason = _run_trial(_trial_cmd_audio(ffmpeg, cap))
                elif cap.backend == "cpu" and cap.codec in ("png", "webp"):
                    ok, fps, reason = (False, None, "")
                    cap.skipped = "image encoding handled via Pillow path"
                else:
                    ok, fps, reason = _run_trial(_trial_cmd(ffmpeg, cap))
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
            filters = _probe_ffmpeg_filters(ffmpeg)

        if disable_gpu:
            caveats.append("GPU probing disabled by TRANSMUTE_DISABLE_GPU "
                           "(used to validate the GPU-less code path).")
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
            caveats=caveats, matrix=[],
        )
        rep.matrix = build_matrix(rep)
        return rep


KNOWN_OPTIONAL: dict[str, bool] = {"ffmpeg": False, "ffprobe": False, "qpdf": True,
                                   "gs": True, "pdftoppm": True, "libreoffice": True,
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
                          available=bool(feat.get("webp_anim") or feat.get("gif")),
                          note="animated GIF -> animated WebP keeps frames/loop"))
    rows.append(MatrixRow("image", "avif", "Pillow/AVIF", available=bool(feat.get("avif")),
                          recommended=bool(feat.get("avif")),
                          note="shown only if the local Pillow build supports AVIF"))
    rows.append(MatrixRow("image", "png", "Pillow/PNG", available=bool(feat.get("png"))))

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

    doc_paths = []
    if rep.engines.get("libreoffice", {}).get("status") == STATUS_OK:
        doc_paths.append(("LibreOffice", True))
    if rep.engines.get("pandoc", {}).get("status") == STATUS_OK:
        doc_paths.append(("pandoc", False))
    if not doc_paths:
        rows.append(MatrixRow("document", "pdf", "LibreOffice/pandoc", False,
                              note="install LibreOffice or pandoc for document conversion"))
    for name, direct in doc_paths:
        rows.append(MatrixRow("document", "pdf", name, available=True,
                              note="native PDF export" if direct
                              else "pandoc needs a PDF engine (LaTeX) on the system"))
    return rows


# ---------------------------------------------------------------- cache

class CapabilityCache:
    """One JSON per probe-key under cache dir; newest matching key wins."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, rep: CapabilityReport) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"{rep.probe_key}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
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
