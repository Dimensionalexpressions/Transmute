"""EngineManager (F26): find external engines on THIS machine, live status.

Engines are external system processes (ffmpeg, ghostscript, qpdf, LibreOffice,
poppler's pdftoppm, pandoc). The app bundle contains zero engine binaries;
the app must survive *any* of them being missing: every lookup resolves to an
EngineInfo with a status, never raises, and carries per-OS install guidance.

Search order: user-configured custom paths (Settings) -> PATH -> conventional
OS locations. Version is read with a short-timeout exec; a binary that exists
but fails to run is status=ERROR (not OK) with the captured reason.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_ERROR = "error"

_IS_WINDOWS = sys.platform == "win32"
_OS_KEY = "windows" if _IS_WINDOWS else "linux"

_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "ffmpeg": {
        "linux": "Install FFmpeg with your distribution's package manager "
                 "(Arch: pacman -S ffmpeg · Fedora: sudo dnf install ffmpeg · "
                 "Debian/Ubuntu: sudo apt install ffmpeg).",
        "windows": "Download FFmpeg from the official site https://www.ffmpeg.org/download.html "
                   "(linked builds for Windows), extract it, and set the path in Settings → Engines, "
                   "or add its bin folder to PATH.",
    },
    "ffprobe": {
        "linux": "Ships with FFmpeg - install the ffmpeg package (same as above).",
        "windows": "Ships with FFmpeg - see https://www.ffmpeg.org/download.html.",
    },
    "qpdf": {
        "linux": "Install qpdf with your package manager (pacman -S qpdf / apt install qpdf).",
        "windows": "Install from the official project page https://qpdf.org (Windows installer).",
    },
    "gs": {
        "linux": "Install Ghostscript with your package manager "
                 "(pacman -S ghostscript / apt install ghostscript).",
        "windows": "Install from the official project page https://ghostscript.com (Windows .msi).",
    },
    "pdftoppm": {
        "linux": "Install poppler-utils (pacman -S poppler / apt install poppler-utils).",
        "windows": "Get a Windows build of Poppler (e.g. https://github.com/oschwartz10612/poppler-windows) "
                   "and set the path in Settings → Engines.",
    },
    "pdftotext": {
        "linux": "Install poppler-utils (pacman -S poppler / apt install poppler-utils).",
        "windows": "Get a Windows build of Poppler (e.g. https://github.com/oschwartz10612/poppler-windows) "
                   "and set the path in Settings → Engines.",
    },
    "libreoffice": {
        "linux": "Install LibreOffice with your package manager (pacman -S libreoffice-fresh / "
                 "apt install libreoffice).",
        "windows": "Install from the official site https://www.libreoffice.org/download/.",
    },
    "pandoc": {
        "linux": "Install pandoc with your package manager (pacman -S pandoc / apt install pandoc).",
        "windows": "Install from the official site https://pandoc.org/installing.html.",
    },
}


@dataclass(frozen=True)
class EngineSpec:
    name: str                       # stable id: ffmpeg, ffprobe, qpdf, gs, pdftoppm, libreoffice, pandoc
    binary_names: tuple[str, ...]   # platform-specific candidates
    version_args: tuple[str, ...]
    version_re: re.Pattern
    optional: bool = False
    timeout_s: float = 10.0
    extra_names: tuple[tuple[str, ...], ...] = ()  # per-platform overrides: (linux, windows)


def _names_for(spec: EngineSpec) -> tuple[str, ...]:
    if spec.extra_names:
        idx = 1 if _IS_WINDOWS else 0
        if len(spec.extra_names) > idx and spec.extra_names[idx]:
            return spec.extra_names[idx]
    return spec.binary_names


KNOWN_ENGINES: dict[str, EngineSpec] = {
    "ffmpeg": EngineSpec(
        "ffmpeg", ("ffmpeg",), ("-hide_banner", "-version"),
        re.compile(r"ffmpeg version n?([0-9][0-9.]*)")),
    "ffprobe": EngineSpec(
        "ffprobe", ("ffprobe",), ("-hide_banner", "-version"),
        re.compile(r"ffprobe version n?([0-9][0-9.]*)")),
    "qpdf": EngineSpec(
        "qpdf", ("qpdf",), ("--version",), re.compile(r"qpdf version ([0-9][0-9.]*)"),
        optional=True),
    "gs": EngineSpec(
        "gs", ("gs", "ghostscript", "gsc", "gswin64c"),
        ("--version",), re.compile(r"([0-9][0-9.]*)"), optional=True,
        extra_names=(("gs", "ghostscript"), ("gswin64c", "gs"))),
    "pdftoppm": EngineSpec(
        "pdftoppm", ("pdftoppm",), ("-v",), re.compile(r"([0-9][0-9.]*)"),
        optional=True),
    "pdftotext": EngineSpec(
        "pdftotext", ("pdftotext",), ("-v",), re.compile(r"([0-9][0-9.]*)"),
        optional=True),
    "libreoffice": EngineSpec(
        "libreoffice", ("libreoffice", "soffice", "soffice.bin", "soffice.exe"),
        ("--version",), re.compile(r"LibreOffice ([0-9][0-9.]*)"), optional=True,
        timeout_s=25.0,
        # P6 (found on the Windows bench): LibreOffice ships `soffice.COM`, a
        # console-launcher shim that HANGS headless. shutil.which("soffice")
        # resolves it first because PATHEXT tries .COM before .EXE - so on
        # Windows never ask for the bare name; soffice.exe IS the binary.
        extra_names=((), ("soffice.exe", "soffice.bin", "libreoffice"))),
    "pandoc": EngineSpec(
        "pandoc", ("pandoc",), ("--version",), re.compile(r"pandoc(?:\.exe)? ([0-9][0-9.]*)"),
        optional=True),
}


@dataclass
class EngineInfo:
    name: str
    status: str                 # STATUS_OK / STATUS_MISSING / STATUS_ERROR
    path: Path | None = None
    version: str | None = None
    reason: str = ""
    checked_at: float = 0.0

    @property
    def available(self) -> bool:
        return self.status == STATUS_OK

    @property
    def install_hint(self) -> str:
        return _INSTALL_HINTS.get(self.name, {}).get(_OS_KEY,
               "Install the tool from its official website, or set a custom path in Settings → Engines.")


def _conventional_dirs() -> list[Path]:
    """OS-conventional install locations searched in addition to PATH."""
    if _IS_WINDOWS:
        pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        dirs = [Path(p) for p in pf if p]
        out: list[Path] = []
        for base in dirs:
            out += [base / "ffmpeg" / "bin", base / "FFmpeg" / "bin",
                    base / "qpdf", base / "qpdf" / "bin", base / "gs", base / "Pandoc"]
            # Ghostscript installs versioned: gs\gs10.13.0\bin - the bin part
            # was missing (P6 fix: candidates pointed at the VERSION dir).
            out += [g / "bin" for g in sorted(base.glob("gs/gs*"))]
            out += [base / "LibreOffice" / "program"]
            # Poppler Windows builds (oschwartz10612 etc) unzip to
            # poppler-*\Library\bin or poppler-*\bin.
            out += [p / "Library" / "bin" for p in sorted(base.glob("poppler*"))]
            out += [p / "bin" for p in sorted(base.glob("poppler*"))]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            out += [Path(local) / "Programs",
                    Path(local) / "Microsoft" / "WinGet" / "Links"]  # winget shims
        pd = os.environ.get("ProgramData", "")
        if pd:
            out.append(Path(pd) / "chocolatey" / "bin")               # choco shims
        try:
            out.append(Path(os.path.expanduser("~")) / "scoop" / "shims")
        except (RuntimeError, KeyError):
            pass
        return out
    # Linux / other POSIX
    return [Path(d) for d in ("/usr/bin", "/usr/local/bin", "/bin", "/snap/bin",
                              "/var/lib/flatpak/exports/bin", "/var/run/flatpak/exports/bin")]


def _run_version(path: Path, args: tuple[str, ...], timeout_s: float) -> str:
    """Return captured combined output; raise on failure/timeout.

    Deliberately NOT subprocess.run(timeout=): on Windows, CPython's own
    timeout path re-drains the pipes with no deadline after killing only the
    direct child - a GUI launcher whose helper child survives (soffice.exe ->
    soffice.bin) keeps those pipes open and the probe hangs forever. Popen +
    tree-kill + bounded drain keeps the probe honest on every machine.
    """
    from ..engines.base import _terminate_tree, communicate_bounded, drain_bounded
    argv = [str(path), *args]
    popen_kw: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, text=True,
                          encoding="utf-8", errors="replace",
                          # own process group: pins pgid == pid so
                          # _terminate_tree can kill the whole tree
                          start_new_session=True)
    if _IS_WINDOWS:
        # no console window flashing behind the GUI while probing engines
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(argv, **popen_kw)
    try:
        out, err = communicate_bounded(proc, timeout_s)
        return (out or "") + (err or "")
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        out, err = drain_bounded(proc, 5)
        raise subprocess.TimeoutExpired(argv, timeout_s,
                                        output=(out or "").encode("utf-8", "replace"),
                                        stderr=(err or "").encode("utf-8", "replace"))


class EngineManager:
    """Detects all KNOWN_ENGINES. Cheap to construct; detect() does the work."""

    def __init__(self, custom_paths: dict[str, str | os.PathLike] | None = None):
        self.custom_paths = {k: Path(v).expanduser() for k, v in (custom_paths or {}).items()}
        self._cache: dict[str, EngineInfo] = {}
        # get() populates single entries lazily; that must NOT masquerade as
        # a complete detect() (partial cache -> KeyError on other engines).
        self._fully_detected = False
        self._lock = threading.Lock()

    # -- public ---------------------------------------------------------
    def detect(self, refresh: bool = False) -> dict[str, EngineInfo]:
        with self._lock:
            if refresh or not self._fully_detected:
                self._cache = {name: self._detect_one(spec) for name, spec in KNOWN_ENGINES.items()}
                self._fully_detected = True
            return dict(self._cache)

    def get(self, name: str) -> EngineInfo:
        with self._lock:
            if name not in self._cache:
                spec = KNOWN_ENGINES[name]
                self._cache[name] = self._detect_one(spec)
            return self._cache[name]

    def path(self, name: str) -> Path | None:
        info = self.get(name)
        return info.path if info.available else None

    def is_available(self, name: str) -> bool:
        return self.get(name).available

    def status_table(self) -> list[dict]:
        """For the F26 status screen (GUI later)."""
        return [
            {"name": n, "status": i.status, "path": str(i.path) if i.path else None,
             "version": i.version, "reason": i.reason, "install_hint": i.install_hint}
            for n, i in sorted(self.detect().items())
        ]

    # -- internals ------------------------------------------------------
    def _candidate_paths(self, spec: EngineSpec) -> list[Path]:
        out: list[Path] = []
        custom = self.custom_paths.get(spec.name)
        if custom is not None:
            out.append(custom)  # user setting wins; if wrong -> STATUS_ERROR, not silent PATH use
        for binary in _names_for(spec):
            found = shutil.which(binary)
            if found:
                out.append(Path(found))
        for d in _conventional_dirs():
            for binary in _names_for(spec):
                # shutil.which honours PATHEXT (finds ffmpeg.exe); a plain
                # directory join does not - on Windows the conventional-dir
                # candidate needs the .exe suffix too (P6).
                names = (binary,) if not _IS_WINDOWS else (binary, binary + ".exe")
                for name in names:
                    cand = d / name
                    if cand.exists():
                        out.append(cand)
        # de-dup preserving order
        seen: set[str] = set()
        uniq = []
        for p in out:
            if str(p) not in seen:
                seen.add(str(p))
                uniq.append(p)
        return uniq

    def _detect_one(self, spec: EngineSpec) -> EngineInfo:
        cands = self._candidate_paths(spec)
        if not cands:
            return EngineInfo(spec.name, STATUS_MISSING,
                              reason=f"not found on PATH or in conventional locations "
                                     f"(looked for: {', '.join(_names_for(spec))})",
                              checked_at=time.time())
        last_err = ""
        for p in cands:
            if not p.is_file():
                last_err = f"{p} is not a file"
                continue
            try:
                out = _run_version(p, spec.version_args, spec.timeout_s)
            except subprocess.TimeoutExpired:
                last_err = f"{p} timed out on version check"
                continue
            except OSError as exc:
                last_err = f"{p}: {exc}"
                continue
            m = spec.version_re.search(out)
            if m:
                return EngineInfo(spec.name, STATUS_OK, path=p,
                                  version=m.group(1), checked_at=time.time())
            last_err = f"{p} ran but version could not be parsed"
        return EngineInfo(spec.name, STATUS_ERROR, path=cands[0],
                          reason=last_err, checked_at=time.time())
