"""Qt-free brain behind the GUI (P2).

Every rule the widgets show (available vs greyed-out-with-reason, ℹ️
compatibility flags, HDR warnings, defaults) is decided HERE, so it is
unit-testable without Qt, without a display, and without engines beyond what
the report already probed.  `transmute.gui.app` binds widgets to this; the
controller must never import PySide6.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..paths import AppPaths
from ..probe.capability import CapabilityReport, CapabilityProber

# Target -> output suffix. Video gets a container known to carry the codec
# without exotic muxing (webm for VP8/VP9, mkv otherwise); everything else
# mirrors the target name.
_SUFFIX = {"vp8": ".webm", "vp9": ".webm", "gif": ".gif", "webp": ".webp"}


# Suffixes the GUI offers as remux targets (F2). .ts is included although
# rare: h264/hevc copy fine there and it is a real interop format.
REMUX_CONTAINERS = (".mkv", ".mp4", ".webm", ".ts")

# PDF picker target id -> engine transform name (A1). Transforms, not
# compression: the engine routes them away from the lossless ladder.
PDF_EXPORT = {"png (per page)": "png", "jpg (per page)": "jpg", "txt (text)": "txt"}

# Document export target ids (A2) - same words engine-side and picker-side;
# the engine reads them as its format list, so import, don't duplicate.
from ..engines.document import DOC_TARGETS as DOC_EXPORT  # noqa: E402

# ------------------------------------------------------------- device names
# The CPU/GPU selector (user request) must show the machine's REAL hardware,
# read at runtime: /proc/cpuinfo, /sys/class/drm render nodes, and the NVIDIA
# management tool where the driver ships one. Nothing is assumed from build
# time; a GPU that cannot be used stays listed with its measured reason.

_GPU_BACKENDS_OF_DRIVER = {
    "nvidia": ("nvidia-nvenc",),
    "amdgpu": ("va-api", "amd-amf"),
    "i915": ("va-api", "intel-oneapi"),
    "xe": ("va-api", "intel-oneapi"),
}
_GPU_NAME_FALLBACK = {
    "nvidia": "NVIDIA graphics", "amdgpu": "AMD graphics",
    "i915": "Intel graphics", "xe": "Intel graphics",
}


# probe records this on rows it skipped because GPUs were held out - a
# skipped row is the app's OWN past, never a GPU's measured verdict
_PROBE_OFF_MARK = "gpu-probe-disabled"


def _gpu_gpu_tools_enabled() -> bool:
    """GPU ENCODE MEASUREMENT switch (probe + picker reasons). It gates
    encoders, never the device INVENTORY: reading a name from nvidia-smi or
    lspci encodes nothing, so the picker always shows real device names."""
    import os
    return os.environ.get("TRANSMUTE_DISABLE_GPU", "").lower() not in (
        "1", "true", "yes")


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(
                encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip() or "your processor"
    except OSError:
        pass
    return "your processor"


def gpu_render_nodes() -> list[dict[str, str]]:
    """[{driver, vendor, device, slot}] straight from /sys - never cached."""
    import glob
    import os
    out: list[dict[str, str]] = []
    try:
        paths = sorted(glob.glob("/sys/class/drm/renderD*/device"))
    except OSError:
        return out
    for dev in paths:
        try:
            driver = os.path.basename(os.path.realpath(
                os.path.join(dev, "driver")))
            vendor = Path(os.path.join(dev, "vendor")).read_text().strip()
            device = Path(os.path.join(dev, "device")).read_text().strip()
        except OSError:
            continue
        slot = ""
        try:
            for line in Path(os.path.join(dev, "uevent")).read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    slot = line.split("=", 1)[1].strip()
        except OSError:
            pass
        out.append({"driver": driver, "vendor": vendor,
                    "device": device, "slot": slot})
    return out


def nvidia_gpu_names() -> list[str]:
    """Exact per-GPU names when the NVIDIA driver's tool is present."""
    import shutil
    import subprocess
    import sys
    if not shutil.which("nvidia-smi"):
        return []
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=6,
            # Windows: nvidia-smi is a console tool - without this every
            # probe flashed a black window behind the GUI (end-user scary).
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if sys.platform == "win32" else {}))
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _strip_lspci_product(line: str) -> str:
    """'00:02.0 VGA compatible controller: Advanced Micro Devices, Inc.
    [AMD/ATI] Granite Ridge [Radeon Graphics]' -> 'Granite Ridge [Radeon
    Graphics]' (pure; no shell, no network)."""
    if ": " not in line:
        return ""
    product = line.rsplit(": ", 1)[1].strip()
    for junk in (" rev ", " (rev"):
        if junk in product:
            product = product.split(junk)[0].strip()
    for cut in ("[AMD/ATI] ", "[Intel Technologies] ", "Inc. "):
        if cut in product:
            product = product.split(cut, 1)[1].strip()
    return product


def lspci_product(slot: str) -> str:
    """Best-effort human product name for a PCI slot ("" when pciutils or
    the entry is absent - the caller then uses a vendor word)."""
    import shutil
    import subprocess
    if not slot or not shutil.which("lspci"):
        return ""
    try:
        p = subprocess.run(["lspci", "-s", slot], capture_output=True,
                           text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _strip_lspci_product(p.stdout.strip()) if p.returncode == 0 else ""



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
    detail: str = ""           # per-backend tooltip lines (merged backends)

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
                 paths: AppPaths | None = None,
                 governor: Any | None = None) -> None:
        self._report_provider = report_provider
        self._paths = paths
        self._queue = queue
        # the throttle: a ResourceGovernor when the user wants the machine
        # left usable, None when they want full speed (GUI checkbox).
        # None reaches JobQueue exactly like an absent throttle.
        self._governor = governor
        self._report: CapabilityReport | None = None
        # single reusable preview dir: each first_frame() call replaces the
        # previous PNG in place - no temp dir per click
        self._preview_dir: Path | None = None
        # per-(file, choices) measured estimates; see conversion_estimate()
        self._est_cache: dict = {}
        self._video_engine = None

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

    # ------------------------------------------------------------- prefs
    def _prefs_file(self) -> Path:
        from ..paths import paths as get_paths
        root = self._paths if self._paths is not None else get_paths()
        return root.config_dir / "preferences.json"

    def prefs(self) -> dict:
        try:
            return json.loads(self._prefs_file().read_text("utf-8"))
        except Exception:
            return {}

    def set_pref(self, key: str, value) -> None:
        data = self.prefs()
        data[key] = value
        f = self._prefs_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=1), "utf-8")

    def conversion_estimate(self, cat: str, target: str, src, *,
                            encoder: str = "", crf: int | None = None,
                            size_mb: float | None = None,
                            preset: str | None = None,
                            audio_mode: str = "copy",
                            audio_bitrate: str | None = None,
                            stop=None) -> dict:
        """One-line time+size estimate for THIS file. Every number comes from
        encoding a real piece of the file with the chosen encoder on THIS
        machine (user directive: hardware-measured, never table-guessed).
        {"ok": False} when there is nothing honest to say."""
        from pathlib import Path
        none = {"ok": False, "est_s": 0.0, "bytes": 0.0,
                "size_is_target": False, "sec": 0.0, "note": ""}
        if cat != "video" or not target or not src:
            return none
        try:
            mt = Path(src).stat().st_mtime_ns
        except OSError:
            return none
        key = (str(src), mt, target, encoder, crf, size_mb, preset,
               audio_mode, audio_bitrate)
        cached = self._est_cache.get(key)
        if cached is not None:
            return cached
        if self._video_engine is None:
            from ..engines.video import VideoEngine
            self._video_engine = VideoEngine()
        from ..paths import paths as get_paths, set_paths
        prev = None
        if self._paths is not None:
            prev = get_paths()
            set_paths(self._paths)
        line: dict = none
        try:
            line = self._video_engine.estimate_conversion(
                Path(src), target_codec=target, encoder=encoder or None,
                crf=crf, size_mb=size_mb, preset=preset,
                audio_mode=audio_mode, audio_bitrate=audio_bitrate,
                stop=stop)
        except Exception:
            line = none  # estimate is advisory - never break the GUI over it
        finally:
            if prev is not None:
                set_paths(prev)
        if stop is not None and stop.is_set():
            # superseded mid-flight: return the wreckage but never cache it
            return line
        if len(self._est_cache) > 64:
            self._est_cache.clear()
        self._est_cache[key] = line
        return line

    def reprobe(self, progress=None, cancel=None) -> CapabilityReport:
        """Benchmark button: re-measure THIS machine from scratch (cache
        bypassed, old entry superseded - results always come from the
        hardware the app is actually running on)."""
        from ..paths import paths as get_paths, set_paths
        prev = None
        if self._paths is not None:
            prev = get_paths()
            set_paths(self._paths)
        try:
            self._report = CapabilityProber().report(refresh=True,
                                                     progress=progress,
                                                     cancel=cancel)
        finally:
            if prev is not None:
                set_paths(prev)
        self._vaapi_cache = None  # devices may have changed
        return self._report

    @property
    def queue(self):
        if self._queue is None:
            from ..runner.queue import JobQueue
            self._queue = JobQueue(paths=self._paths,
                                   governor=self._governor)
        return self._queue

    # ------------------------------------------------------------ pickers
    @staticmethod
    def categories() -> list[str]:
        return ["video", "audio", "image", "pdf", "document"]

    # ------------------------------------------------------- human UI copy
    # The matrix is per ENCODER BACKEND (8 rows say "av1"); the picker shows
    # one row per TARGET and says in words what choosing it means. Names here
    # are UI copy only - capability itself stays 100% measured (F1/F3).
    _PRETTY = {
        "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
        "h264": "H.264", "hevc": "H.265/HEVC",
        "opus": "Opus", "mp3": "MP3", "aac": "AAC", "flac": "FLAC",
        "alac": "ALAC", "vorbis": "Vorbis",
        "webp": "WebP", "webp(animated)": "WebP (animated)",
        "gif": "GIF (animated)",
        "avif": "AVIF", "png": "PNG", "jpeg": "JPEG",
        "pdf (page)": "PDF page",
        "pdf (lossless smaller)": "Smaller PDF",
        "png (per page)": "PNG image (one file per page)",
        "jpg (per page)": "JPEG image (one file per page)",
        "txt (text)": "Plain text",
        "docx": "DOCX (Word)", "odt": "ODT (OpenDocument)", "rtf": "RTF",
        "html": "HTML page", "markdown": "Markdown", "txt": "Plain text",
    }
    _TAGLINE = {
        "av1": "smallest file, slowest to convert",
        "vp9": "good for web, slower",
        "vp8": "older web format",
        "h264": "plays on almost every device",
        "hevc": "smaller than H.264, some apps can't play it",
        "opus": "best quality for its size",
        "mp3": "works everywhere, older",
        "aac": "standard for phones and video",
        "flac": "lossless - exact audio, larger",
        "alac": "Apple lossless (iPhone/Music)",
        "vorbis": "older Ogg format audio",
        "webp": "much smaller than JPEG",
        "webp(animated)": "keeps every frame and its exact timing, loop preserved",
        "gif": "keeps every frame and its timing; sound is removed (GIF has no sound)",
        "avif": "newest and smallest, needs recent apps",
        "pdf (page)": "print/share - one page per picture",
        "png": "lossless, exact pixels",
        "jpeg": "classic photo format",
        "pdf (lossless smaller)": "pixel-identical recompression",
        "png (per page)": "exact pixels, larger",
        "jpg (per page)": "smaller page images",
        "txt (text)": "text only, images dropped",
        "docx": "Word document",
        "odt": "OpenDocument text",
        "rtf": "rich text - opens everywhere",
        "html": "web page",
        "markdown": "plain markup text",
        "txt": "text only, formatting dropped",
    }
    # raw encoder ids -> words a human chose on purpose (F10 ladder order kept)
    _GPU_SUFFIX = {"nvenc": "GPU - NVIDIA", "vaapi": "GPU - VA-API",
                   "qsv": "GPU - Intel QSV", "videotoolbox": "GPU - macOS",
                   "v4l2m2m": "GPU - V4L2", "vulkan": "GPU - Vulkan"}
    _ENC_HUMAN = {
        "libaom-av1": "AOM reference - smallest, slowest",
        "libsvtav1": "SVT-AV1 - fast AV1",
        "librav1e": "Rav1e - balanced",
        "libvpx-vp9": "VP9 reference",
        "libvpx": "VP8 reference",
        "libx264": "x264 - proven CPU encoder",
        "libx264rgb": "x264 lossless RGB",
        "libx265": "x265 - CPU encoder",
        "libmp3lame": "LAME MP3",
        "libopus": "Opus",
        "libvorbis": "Vorbis",
        "aac": "AAC (FFmpeg built-in)",
        "flac": "FLAC (FFmpeg built-in)",
        "alac": "ALAC (FFmpeg built-in)",
    }

    @classmethod
    def human_encoder(cls, name: str) -> str:
        """Friendly words for an encoder id; the raw id stays in logs/tooltips."""
        for suffix, who in cls._GPU_SUFFIX.items():
            if name.endswith(suffix):
                return f"{who} {name[:-(len(suffix) + 1)]}"
        return cls._ENC_HUMAN.get(name, name)

    @staticmethod
    def _garbled(reason: str) -> bool:
        """ffmpeg stderr noise masquerading as a reason is still the machine's
        own failure text (F3) - too raw for a label, kept in the tooltip."""
        return any(t in reason for t in ("Terminating thread", "@ 0x",
                                         "vost#", "Stream mapping"))

    # -------------------------------------------------- plain-language layer
    # One honest sentence a person who has never heard of codecs can act on.
    _ABOUT = {
        "h264": "The safe choice: almost every screen, phone, and website made "
                "in the last 15 years plays it. Medium size.",
        "hevc": "About 30% smaller than H.264 with the same quality. Recent "
                "iPhones, Androids and TVs play it; older apps may not",
        "av1": "Smallest files - best for archiving and slow connections. "
               "Needs fairly new devices to play it.",
        "vp9": "The free web format - smaller than H.264. Browsers and Android "
               "play it; most TVs and iPhones do not.",
        "vp8": "Older web format. For new files prefer VP9 or AV1.",
        "mp3": "Works on literally any player ever built. Fine for music.",
        "aac": "The standard for phone ringtones, video soundtracks, and "
               "streaming.",
        "opus": "Best quality for its size - what Discord and WhatsApp use. "
                "Needs modern apps.",
        "flac": "Exact copy of the original audio, compressed losslessly. "
                "Large; some players ignore its tags.",
        "alac": "Apple's lossless - for iPhone and Apple Music.",
        "vorbis": "Older Ogg audio. Prefer Opus for new files.",
        "jpeg": "Classic photo - small, opens everywhere.",
        "png": "Pixel-perfect, no compression artifacts. Larger files.",
        "webp": "Much smaller than JPEG, opens in almost every current app.",
        "avif": "Newest and smallest photo format; needs recent apps.",
        "pdf (page)": "Makes the picture a document page - for printing, email, "
                      "and opening anywhere. Transparent areas become white, and "
                      "an animated file becomes a single page of its first frame.",
    }

    @staticmethod
    def _speed_word(fps: float) -> str:
        """Plain words for measured speed tiers - user feedback: say HOW
        FAST, don't explain how it was measured."""
        if fps >= 500:
            return "very fast"
        if fps >= 150:
            return "fast"
        if fps >= 40:
            return "decent"
        return "slow"

    def _vaapi_word(self) -> str:
        """Which GPU sits behind /dev/dri VAAPI nodes - read from /sys now,
        never assumed at build time."""
        cached = getattr(self, "_vaapi_cache", None)
        if cached is not None:
            return cached
        drivers = set()
        try:
            import glob
            import os
            for p in glob.glob("/sys/class/drm/renderD*/device/driver"):
                drivers.add(os.path.basename(os.path.realpath(p)))
        except OSError:
            pass
        if "amdgpu" in drivers:
            w = "AMD built-in graphics"
        elif drivers & {"i915", "xe"}:
            w = "Intel built-in graphics"
        else:
            w = "your GPU (via VA-API)"
        self._vaapi_cache = w
        return w

    def _device_word(self, enc) -> str:
        # the machine's own device name when this report enumerated one
        try:
            for d in self.devices():
                if enc.backend in d["backends"]:
                    return d["name"]
        except Exception:  # noqa: BLE001 - words must never break the picker
            pass
        if enc.backend == "nvidia-nvenc":
            return "NVIDIA graphics"
        if enc.backend == "va-api":
            return self._vaapi_word()
        if enc.backend == "intel-oneapi":
            return "Intel Quick Sync"
        if enc.backend == "amd-amf":
            return "AMD AMF graphics"
        return "CPU"

    # ------------------------------------------------------------- devices
    # "CPU / GPU" selector (user request): one entry per device THIS machine
    # actually has, named from what it actually reports. A device that cannot
    # encode stays VISIBLE with the probe's own measured reason (doctrine:
    # no hardware assumptions - neither absence nor capability).
    def gpu_decode_status(self) -> dict[str, Any]:
        """This machine's GPU-DECODE verdict from the probe's executed trial
        ({} when no report exists yet). Decoding is offloaded to the GPU
        only where this says available - the machine decides, not us."""
        try:
            return dict(getattr(self.report, "gpu_decode", None) or {})
        except Exception:  # noqa: BLE001 - a missing verdict stays silent
            return {}

    def devices(self) -> list[dict[str, Any]]:
        """[{id, kind, name, backends, works, why}] - CPU first, then one
        entry per GPU (identical cards share one entry). Rebuilt whenever a
        new report arrives (re-probe after a device pick)."""
        report = self.report
        cached = getattr(self, "_devices_cache", None)
        if cached is not None and cached[0] is report:
            return cached[1]
        rows = list(getattr(report, "encoders", []))
        gpu_off = not _gpu_gpu_tools_enabled()
        # WHY the GPUs are idle matters to the user: normally the CPU/GPU
        # choice is set to CPU (so the app itself holds the GPUs out), only
        # rarely is it a launch-time override. "switched off" alone confused
        # the user (2026-11): the entry must name the cause, in user words.
        off_kind = "cpu-selected" if self.device() == "cpu" else "launch"

        def status(backends: tuple[str, ...]) -> tuple[bool, str, str]:
            """(works, why, state). state: "" healthy/measured-broken,
            "cpu-selected"/"launch" idle-by-choice, "measuring" the picker
            switched measuring back on and the re-benchmark is still
            running. Doctrine: never dress one truth as another - a stale
            'probe was switched off' row is NOT a measurement, and a
            mid-benchmark device is not 'not working' (a healthy RTX 5090
            was shown as broken exactly this way, user screenshot 2026-11)."""
            if gpu_off and "cpu" not in backends:
                if off_kind == "cpu-selected":
                    return False, ("Transmute isn't using this GPU right now, "
                                   "because CPU is chosen under CPU / GPU. "
                                   "Choose Auto or this GPU and Transmute "
                                   "measures it on this PC, then uses it."), \
                        "cpu-selected"
                return False, ("GPU use is disabled for this launch of "
                               "Transmute (TRANSMUTE_DISABLE_GPU). Choose "
                               "Auto or this device and Transmute measures "
                               "it on this PC."), "launch"
            mine = [e for e in rows if e.backend in backends]
            if "cpu" not in backends and mine:
                # rows skipped because the probe ran with GPUs held out are
                # the app's own yesterday, not this GPU's verdict
                stale = [e for e in mine
                         if _PROBE_OFF_MARK in (e.skipped or "")
                         or _PROBE_OFF_MARK in (e.reason or "")]
                if len(stale) == len(mine):
                    return False, ("Transmute is measuring this GPU on this "
                                   "PC right now - results appear when the "
                                   "benchmark finishes."), "measuring"
                mine = [e for e in mine if e not in stale]
            if any(e.verified for e in mine):
                return True, "", ""
            for e in mine:
                if e.reason or e.skipped:
                    return False, (e.reason or e.skipped), ""
            if mine:
                return False, "never verified on this machine", ""
            return False, "no encoder for it in this ffmpeg build", ""

        ok, why, _state = status(("cpu",))
        devs: list[dict[str, Any]] = [{
            "id": "cpu", "kind": "cpu", "name": cpu_model(),
            "backends": ["cpu"], "works": ok, "why": why, "disabled": False,
            "disabled_kind": "",
        }]
        gpu_nodes = gpu_render_nodes()
        nv_names = nvidia_gpu_names()
        groups: dict[tuple[str, str], int] = {}
        for n in gpu_nodes:
            key = (n["driver"], n["device"])
            groups[key] = groups.get(key, 0) + 1
        nv_groups = sum(1 for (drv, _dev) in groups if drv == "nvidia")
        for (driver, devid), count in groups.items():
            backends = list(_GPU_BACKENDS_OF_DRIVER.get(driver, ("va-api",)))
            if driver == "nvidia" and nv_names:
                name = nv_names[0]
                if nv_groups > 1 and len(nv_names) > 1:
                    name = f"{name} (+{len(nv_names) - 1} more)"
            else:
                name = (lspci_product(next(
                    (n["slot"] for n in gpu_nodes
                     if n["driver"] == driver and n["device"] == devid), ""))
                    or _GPU_NAME_FALLBACK.get(driver, "your GPU"))
                if count > 1:
                    name = f"{name} ({count} identical GPUs)"
            ok, why, state = status(tuple(backends))
            devs.append({
                "id": f"gpu:{driver}:{devid}", "kind": "gpu", "name": name,
                "backends": backends, "works": ok, "why": why,
                "disabled": state in ("cpu-selected", "launch"),
                "disabled_kind": state,
            })
        self._devices_cache = (report, devs)
        return devs

    def device(self) -> str:
        return str(self.prefs().get("device") or "auto")

    def set_device(self, dev_id: str) -> None:
        self.set_pref("device", dev_id or "auto")
        self._devices_cache = None

    # ------------------------------------------------- performance wish (Option 2)
    PERF_MODES = ("auto", "quality", "speed")

    def perf_mode(self) -> str:
        """What the user wants ENCODING to optimise for: "auto" = whatever
        this PC measured fastest (the shipped behaviour), "quality" = keep
        compression on the CPU encoders, "speed" = keep it on the graphics
        card. Decoding is decided separately by the probe's decode trial -
        a wish never turns decode off. Persisted, machine-independent; a
        value this build does not know falls back to auto."""
        mode = str(self.prefs().get("perf_mode") or "auto")
        return mode if mode in self.PERF_MODES else "auto"

    def set_perf_mode(self, mode: str) -> None:
        self.set_pref("perf_mode",
                      mode if mode in self.PERF_MODES else "auto")

    def perf_note(self, category: str, target: str) -> str:
        """Empty when this PC can honour the Quality/Speed wish; otherwise
        the sentence explaining the visible fallback. The mode narrows the
        verified ladder, it never invents an encoder and never hides that
        it had to give in."""
        mode = self.perf_mode()
        if mode == "auto":
            return ""
        rows = self._ladder(category, target)
        if not rows:
            return ""
        if mode == "quality" and not any(
                not getattr(c, "hw", False) for c in rows):
            return ("no working CPU encoder for this format here - the "
                    "graphics-card one does the encoding instead")
        if mode == "speed" and not any(
                getattr(c, "hw", False) for c in rows):
            return ("no graphics-card encoder works on this machine - the "
                    "fastest CPU encoder does it instead")
        return ""

    def device_backends(self) -> set[str] | None:
        """Backends the chosen device covers; None = Auto (whole ladder).
        A device id that no longer exists falls back to Auto - stale prefs
        must never hide the machine's working paths."""
        dev = self.device()
        if dev in ("", "auto"):
            return None
        if dev == "cpu":
            return {"cpu"}
        for d in self.devices():
            if d["id"] == dev:
                return set(d["backends"])
        return None

    def device_choice(self) -> dict[str, Any] | None:
        """The chosen device entry (None for Auto) - the GUI uses its name
        and reason words."""
        dev = self.device()
        for d in self.devices():
            if d["id"] == dev:
                return d
        return None

    def about_lines(self, category: str, target: str,
                    encoder: str = "") -> list[str]:
        """What the picker shows under the choice: what you get, and how fast
        each path actually ran on THIS machine (measured trials, F10 data -
        never estimates)."""
        lines: list[str] = []
        text = self._ABOUT.get(target)
        if text:
            lines.append(text)
        base = target.split(" ")[0]
        rel = [e for e in getattr(self.report, "encoders", [])
               if e.verified and (e.codec == base or e.name == base)]
        tried = [e for e in rel if e.fps]
        if not tried and rel:
            # Works on this machine (it encoded), just never finished the
            # benchmark in time - say so instead of showing nothing.
            lines.append("Works here, but this PC could not finish the speed "
                         "benchmark - expect slow conversions.")
        if tried:
            tried.sort(key=lambda e: -e.fps)
            # one line per DEVICE (fastest encoder of it), not per raw
            # encoder: "your CPU" twice with different numbers confuses.
            per_dev: dict[str, object] = {}
            for e in tried:
                per_dev.setdefault(self._device_word(e), e)
            devs = list(per_dev.values())
            best = devs[0]
            # user directive (screenshot feedback): RECOMMEND the graphics
            # card whenever this format has one that works here - it keeps
            # the CPU free; recommend CPU only when no GPU can encode this.
            hw = [e for e in devs if getattr(e, "hw", False)]
            summary = (
                "On this PC: "
                + ", ".join(f"{self._device_word(e)} "
                            f"{self._speed_word(e.fps)}" for e in devs))
            if hw:
                lines.append(summary + ".")
                lines.append(
                    f"Recommended: {self._device_word(hw[0])} - it leaves "
                    "your CPU free while you work.")
            else:
                lines.append(
                    summary
                    + (f" - fastest: {self._device_word(best)}."
                       if len(devs) > 1 else "."))
            if encoder:
                sel = next((e for e in tried
                            if e.name == encoder.rsplit("/", 1)[-1]), None)
                if sel is not None:
                    lines.append(
                        f"This encoder: {self._device_word(sel)} - "
                        f"{self._speed_word(sel.fps)} "
                        f"({sel.fps:.0f} fps).")
        return lines

    def targets(self, category: str) -> list[TargetInfo]:
        """One row per conversion target, available and not (F3: greyed rows
        stay visible and say WHY in the label itself, full machine reasons in
        the tooltip). Multiple encoder backends for one codec merge into the
        one row the user actually cares about.
        """
        groups: dict[str, list] = {}
        for row in self.report.matrix:
            if row.category != category:
                continue
            if "verify renders" in row.target:   # diagnostic, not a target
                continue
            groups.setdefault(row.target, []).append(row)
        out: list[TargetInfo] = []
        for target, rows in groups.items():
            live = [r for r in rows if r.available]
            available = bool(live)
            pretty = self._PRETTY.get(target, target.upper())
            tagline = self._TAGLINE.get(target)
            detail = "\n".join(
                f"{'✓' if r.available else '✗'} {r.path or r.note or 'backend'}"
                + ("" if r.available or not (r.note or "")
                   or self._garbled(r.note) else f": {r.note}")
                for r in rows)
            if available:
                # Simplified per user feedback: the label is just format +
                # short hint; hardware/performance details go to the details
                # block (about_lines), not crammed into the option text.
                label = f"{pretty} - {tagline}" if tagline else pretty
                if any(r.compat_flag for r in live):
                    label += "  (!)"
                elif any(r.recommended for r in live):
                    label += "  ✓"
                reason = ""
            else:
                raw = [r.note or r.path for r in rows if (r.note or r.path)]
                clean = next((x for x in raw if not self._garbled(x)), "")
                short = clean.splitlines()[0][:60] if clean else \
                    "no working encoder found"
                label = f"{pretty} - not available here ({short})"
                if any(r.compat_flag for r in rows):
                    label += "  (!)"
                reason = "; ".join(dict.fromkeys(raw)) or \
                    "not available on this machine"
            first = rows[0]
            out.append(TargetInfo(target=target, label=label,
                                  available=available, reason=reason,
                                  compat_flag=any(r.compat_flag for r in rows),
                                  recommended=any(r.recommended for r in live),
                                  hw=any(r.hw for r in live),
                                  note=first.path, detail=detail))
        return out

    def default_target(self, category: str) -> TargetInfo | None:
        for t in self.targets(category):
            if t.available:
                return t
        return None

    def _ladder(self, category: str, target: str):
        """Verified encoder ROWS (fastest first, F10), device-scoped but
        BEFORE any performance-mode narrowing - shared by encoders() and
        perf_note() so the note always describes the very list the picker
        would show unfiltered. Empty for non-codec targets (image/pdf/
        document): the engine picks internally; the UI shows the chain
        label from TargetInfo.note."""
        if category not in ("video", "audio"):
            return []
        try:
            got = self.report.verified_encoders(target)
        except Exception:
            return []
        backends = self.device_backends()
        if backends is not None:
            got = [c for c in got if c.backend in backends]
        return list(got)

    def _mode_rows(self, rows):
        """Performance mode filters WHICH verified encoders qualify; it
        never invents one. A wish this machine cannot honour keeps the full
        ladder - visibly: perf_note() tells the user why."""
        mode = self.perf_mode()
        if mode != "auto" and rows:
            want_hw = mode == "speed"
            keep = [c for c in rows
                    if bool(getattr(c, "hw", False)) is want_hw]
            if keep:
                return keep
        return rows

    def encoders(self, category: str, target: str) -> list[str]:
        """Verified encoder ladder for a codec target (fastest first),
        scoped to the CPU/GPU device the user picked (Auto = whole ladder)
        and narrowed by the performance wish (Auto = no narrowing)."""
        return [c.name for c in self._mode_rows(self._ladder(category, target))]

    def auto_encoder(self, category: str, target: str) -> str | None:
        """The encoder "auto" means HERE AND NOW: fastest verified on this
        machine for this target, on the chosen device. The GUI passes it
        explicitly whenever a device is pinned, so queue jobs and estimates
        provably run - and were measured - on that device."""
        got = self.encoders(category, target)
        return got[0] if got else None

    def quality_range(self, category: str, target: str):
        """(low, high, default) of the quality knob for the encoder that
        will actually run - the slider scale (low = best). Encoders do NOT
        share x264's 0..51: SVT-AV1 collapses below 2 (measured), librav1e
        speaks -qp 0..255. None for non-video targets (engine has no
        quality knob there)."""
        if category != "video" or not target:
            return None
        if target.split("(")[0].strip() in ("gif", "webp"):
            # Animated pictures: the engine ignores the quality knob (GIF has
            # a fixed 256-colour palette pipeline; WebP uses a fixed default).
            # Better no slider than a slider that does nothing.
            return None
        if self._video_engine is None:
            from ..engines.video import VideoEngine
            self._video_engine = VideoEngine()
        try:
            enc = (self.encoders(category, target) or [None])[0]
            return self._video_engine.quality_range(target, encoder=enc)
        except Exception:
            return (0, 51, 23)

    # -------------------------------------------------------------- jobs
    def suffix_for(self, category: str, target: str) -> str:
        base = target.split("(")[0].strip()
        if category == "audio":
            # codec names are targets, but ffmpeg muxes by CONTAINER suffix:
            # vorbis lives in .ogg and raw PCM in .wav - ".vorbis"/".pcm_s16le"
            # are not containers, so the engine refused targets the picker
            # advertised (WinBoat VM, 2026-11-27).
            base = {"vorbis": "ogg", "pcm_s16le": "wav"}.get(base, base)
            return "." + base
        if category == "video":
            return _SUFFIX.get(base, ".mkv")
        if category == "document":
            if target == "pdf":
                return ".pdf"
            if target == "markdown":
                return ".md"       # the extension humans actually use
            return "." + target    # docx / odt / rtf / html / txt
        return "." + base

    def dst_for(self, src: Path | str, category: str, target: str,
                dst_dir: Path | str, *, ext: str | None = None) -> Path:
        src = Path(src)
        stem = src.stem if src.stem else "file"
        suffix = ext if ext else self.suffix_for(category, target)
        return Path(dst_dir) / (stem + suffix)

    # ----------------------------------------------------- P3 video surfaces
    @property
    def tonemap_available(self) -> bool:
        """True only when the zscale+tonemap chain PASSED its executed trial
        on this machine (F15: capability is measured, never parsed)."""
        try:
            return bool(self.report.filters.get("tonemap_chain"))
        except Exception:
            return False

    def remux_block_reason(self, src: Path | str, ext: str) -> str:
        """'' when a lossless remux into `ext` is provably possible here.

        Mirrors the engine's compat tables so the checkbox can stay visible
        but greyed with the machine's own reason (F3) instead of failing at
        run time; the engine still re-checks everything at execution.
        """
        from ..engines.media import Probe
        from ..engines.video import (AUDIO_OK_BY_FORMAT, CONTAINER_BY_FORMAT,
                                     REMUX_VIDEO_OK)
        probe = Probe()
        if not probe.available:
            return "ffprobe missing: remux safety cannot be checked"
        sv = probe.primary_video(Path(src))
        if sv is None:
            return "no video stream to remux"
        fmt = CONTAINER_BY_FORMAT.get((ext or "").lower())
        if fmt is None:
            return f"unknown container '{ext}'"
        vc = (sv.get("codec_name") or "").lower()
        ok_v = REMUX_VIDEO_OK.get(fmt)
        if ok_v is not None and vc not in ok_v:
            return (f"'{vc}' cannot be copied into '{ext}' losslessly - "
                    "use convert (remux never re-encodes)")
        ok_a = AUDIO_OK_BY_FORMAT.get(fmt)
        if ok_a is not None:
            bad = sorted({(s.get("codec_name") or "").lower()
                          for s in probe.audio_streams(Path(src))} - ok_a - {""})
            if bad:
                return (f"audio {bad} cannot be copied into '{ext}' losslessly - "
                        "use convert (remux never re-encodes)")
        return ""

    def first_frame(self, src: Path | str) -> Path | None:
        """Extract the first frame as a PNG for the preview; None when not
        measurable (F3: no ffmpeg, no video stream, decode error - the UI
        then shows 'preview unavailable', never a made-up picture).

        Each call replaces the previous preview file in place: no leak.
        """
        import tempfile
        from ..engines.base import run_command
        from ..probe.engines import EngineManager
        src = Path(src)
        ffmpeg = EngineManager().path("ffmpeg")
        if ffmpeg is None or not src.is_file():
            return None
        # claim the temp dir BEFORE decoding: a failed decode must not
        # orphan the mkdtemp - the dir stays owned and is reused/cleaned
        # like every other preview call
        if self._preview_dir is None:
            self._preview_dir = Path(tempfile.mkdtemp(prefix="transmute-prev-"))
        td = self._preview_dir
        out = td / "frame.png"
        res = run_command([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                           "-i", src, "-frames:v", "1", "-y", out], timeout_s=60)
        if not res.ok or not out.is_file() or out.stat().st_size == 0:
            return None
        return out

    def options_for(self, category: str, target: str,
                    *, quality: int | None = None,
                    encoder: str | None = None,
                    deep_pdf: bool = False,
                    remux: bool = False,
                    tonemap: bool = False,
                    audio_replace: bool = False,
                    crf: int | None = None,
                    size_mb: float | None = None,
                    preset: str | None = None,
                    audio_bitrate: str | None = None) -> dict[str, Any]:
        # Remux is a different operation, not a codec choice: no codec/encoder
        # keys ride along (F2 - the engine refuses to mix the two).
        if category == "video" and remux:
            return {"remux": True}
        opts: dict[str, Any] = {}
        if category in ("video", "audio"):
            opts["codec"] = target
            if encoder:
                opts["encoder"] = encoder
        if category == "video":
            if tonemap:
                opts["tonemap"] = True
            if audio_replace:
                opts["audio"] = "replace"
                if audio_bitrate:
                    opts["audio_bitrate"] = audio_bitrate
            if preset:
                # engine applies it only to CPU encoders (hardware encoders
                # carry their own ladder - the GUI hides it there)
                opts["preset"] = preset
            if crf is not None and size_mb is not None:
                raise ValueError("choose CRF or a size target, not both")
            if size_mb is not None:
                opts["size_mb"] = float(size_mb)
            elif crf is not None:
                opts["crf"] = int(crf)
        if category == "image" and quality is not None:
            opts["quality"] = int(quality)
        if category == "pdf":
            if target in PDF_EXPORT:
                opts["target"] = PDF_EXPORT[target]   # transform (A1), not ladder
            elif deep_pdf:
                opts["deep"] = True   # lossy recompress - the UI must label it so
        if category == "document" and target in DOC_EXPORT:
            opts["target"] = target   # A2 export transform; "pdf" keeps old path
        return opts

    def enqueue(self, src: Path | str, dst_dir: Path | str, category: str,
                target: str, *, ext: str | None = None, **kw: Any) -> JobInfo:
        # Per-page PDF export is N jobs, one page each (progress + retry are
        # page-local); everything else is the classic single src->dst job.
        if category == "pdf" and target in ("png (per page)", "jpg (per page)"):
            return self._enqueue_pages(Path(src), Path(dst_dir), target)
        # `ext` is the GUI's container override (remux picker); conversion
        # options never see it - it only chooses the output suffix.
        dst = self.dst_for(src, category, target, dst_dir, ext=ext)
        rec = self.queue.add(Path(src), dst, category=category,
                             options=self.options_for(category, target, **kw))
        return self._info(rec)

    def _enqueue_pages(self, src: Path, dst_dir: Path, target: str) -> JobInfo:
        """Fan-out for 'one file per page' targets. The page count is READ
        from the file (pikepdf/qpdf) - never guessed; an unreadable PDF
        raises ValueError with the machine's own reason (F3)."""
        from ..engines.pdf import PdfEngine
        try:
            pages = PdfEngine().page_count(src)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc
        if pages < 1:
            raise ValueError("this PDF reports 0 pages")
        suffix = self.suffix_for("pdf", target)
        opts = self.options_for("pdf", target)
        stem = src.stem or "file"
        width = max(2, len(str(pages)))
        first: JobInfo | None = None
        for i in range(1, pages + 1):
            dst = dst_dir / f"{stem}-{i:0{width}d}{suffix}"
            rec = self.queue.add(src, dst, category="pdf",
                                 options={**opts, "page": i})
            if first is None:
                first = self._info(rec)
        assert first is not None
        return first

    def process(self, *, max_workers: int = 2) -> Any:
        return self.queue.run(max_workers=max_workers)

    # Queue-wide controls (P5). The semantics live in JobQueue (single
    # source of truth): pause/resume is the recoverable pair, cancel is the
    # terminal one; in-flight jobs always finish their verify-before-replace.
    def pause_queue(self) -> int:
        return self.queue.pause_all()

    def resume_queue(self) -> int:
        return self.queue.resume_all()

    def cancel_pending(self) -> int:
        return self.queue.cancel_all()

    def snapshot(self) -> list[JobInfo]:
        # list() first: the watch thread inserts new records into this dict
        # at any moment; iterating a live dict raises RuntimeError inside
        # the GUI refresh timer.
        return [self._info(r) for r in list(self.queue.journal.records.values())]

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
