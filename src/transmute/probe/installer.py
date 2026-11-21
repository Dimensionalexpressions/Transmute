"""Get-and-install helpers for the external engines (Option A).

Legality in one paragraph: Transmute never downloads, copies or bundles an
engine. This module only *launches what the user's own machine already has* -
winget, the Windows Package Manager - or opens an engine's official download
page in the user's browser, after the user confirms in a dialog that spells
out the exact command and the upstream license. The engine then lands on the
user's machine, from upstream, under upstream's own terms; the app credits
every project in THIRD-PARTY-NOTICES.md. Nothing here moves an engine into a
Transmute product, which is what keeps us clean with GPL/AGPL-family tools
(especially Ghostscript - for it the button only opens ghostscript.com).

Windows-specific behaviour is guarded; on every other platform the helpers
degrade to "show the instructions", which is the documented fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_IS_WINDOWS = sys.platform == "win32"

#: Verified against the official microsoft/winget-pkgs manifest repository
#: (2026-11): each ID resolves there. Ghostscript has NO winget package -
#: its official .msi page is the only honest route, so it is deliberately
#: absent from this map.
WINGET_IDS: dict[str, str] = {
    "ffmpeg": "Gyan.FFmpeg",
    "ffprobe": "Gyan.FFmpeg",           # ships inside the same package
    "qpdf": "QPDF.QPDF",
    "pdftoppm": "oschwartz10612.Poppler",
    "pdftotext": "oschwartz10612.Poppler",
    "libreoffice": "TheDocumentFoundation.LibreOffice",
    "pandoc": "JohnMacFarlane.Pandoc",
}

#: Friendly names for the picker dialog (plain-language, non-technical users).
DISPLAY_NAMES: dict[str, str] = {
    "ffmpeg": "FFmpeg - video, audio and image conversion",
    "ffprobe": "ffprobe - included in the FFmpeg install",
    "qpdf": "qpdf - PDF repair and optimization",
    "pdftoppm": "Poppler - PDF to images",
    "pdftotext": "Poppler - PDF to text",
    "libreoffice": "LibreOffice - office documents (Word, ...)",
    "pandoc": "pandoc - markdown / epub / docx conversions",
    "gs": "Ghostscript - PDF printing and optimization",
}

#: Canonical upstream pages; used for the "open download page" button.
DOWNLOAD_PAGES: dict[str, str] = {
    "ffmpeg": "https://ffmpeg.org/download.html",
    "ffprobe": "https://ffmpeg.org/download.html",
    "gs": "https://ghostscript.com/releases/gsdnld.html",
    "qpdf": "https://github.com/qpdf/qpdf/releases",
    "pdftoppm": "https://poppler.freedesktop.org",
    "pdftotext": "https://poppler.freedesktop.org",
    "libreoffice": "https://www.libreoffice.org/download/",
    "pandoc": "https://pandoc.org/installing.html",
}

#: Shown verbatim in the consent dialog: whose software it is, under what
#: license, and the fact it installs to the user's system, not into us.
LICENSE_INFO: dict[str, str] = {
    "ffmpeg": "FFmpeg is free software by its own authors (LGPL-2.1+; GPL when "
              "built with GPL components) - https://ffmpeg.org/legal.html",
    "ffprobe": "ffprobe ships inside FFmpeg (same authors and license).",
    "gs": "Ghostscript is free software by Artifex Software (AGPL-3.0; "
          "commercial licensing at https://www.ghostscript.com/copyleft/) - "
          "you install it from their site under their terms.",
    "qpdf": "qpdf is free software by Jay Berkenholtz and contributors "
            "(Apache-2.0) - https://github.com/qpdf/qpdf",
    "pdftoppm": "Poppler's tools are free software by the freedesktop.org "
                "community (GPL-2.0+) - https://poppler.freedesktop.org",
    "pdftotext": "Poppler's tools are free software by the freedesktop.org "
                 "community (GPL-2.0+) - https://poppler.freedesktop.org",
    "libreoffice": "LibreOffice is free software by The Document Foundation "
                   "(MPL-2.0) - https://www.libreoffice.org/about-us/licenses/",
    "pandoc": "pandoc is free software by John MacFarlane and contributors "
              "(GPL-2.0+) - https://pandoc.org",
}


def winget_available() -> bool:
    """True when this machine has winget (Windows Package Manager) itself."""
    return _IS_WINDOWS and shutil.which("winget") is not None


def install_command(engine: str) -> "list[str] | None":
    """The exact command the button will run, or None (no winget package:
    e.g. Ghostscript -> use DOWNLOAD_PAGES instead)."""
    pkg = WINGET_IDS.get(engine)
    if not pkg:
        return None
    return ["winget", "install", "--id", pkg, "-e",
            "--accept-source-agreements", "--accept-package-agreements"]


def run_install(engine: str, on_line) -> int:
    """Run install_command() for real, streaming each output line to
    on_line(str). Returns the installer's exit code (0 = installed).
    Elevation, if the package needs it, is winget's own UAC prompt."""
    cmd = install_command(engine)
    if not cmd:
        raise ValueError(f"no winget package defined for {engine!r}; "
                         "open DOWNLOAD_PAGES[engine] instead")
    flags = 0x08000000 if _IS_WINDOWS else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=flags)
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\r\n"))
    return proc.wait()


def refresh_path_from_registry(read_paths=None) -> "list[str]":
    """Windows: PATH entries added AFTER the app started (a fresh winget or
    MSI install) are invisible to this process. Merge machine+user PATH from
    the registry into os.environ so a re-probe finds the new tool without an
    app restart. Returns the entries added. No-op on other platforms.

    read_paths: optional injectable callable -> (machine_path, user_path),
    used by the tests; default reads the registry via winreg."""
    if not _IS_WINDOWS:
        return []
    added: list[str] = []
    if read_paths is None:
        try:
            import winreg
        except ImportError:
            return []
        def read_paths():  # type: ignore[misc]
            out = []
            for hive, sub in (
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                    (winreg.HKEY_CURRENT_USER, "Environment")):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        out.append(str(winreg.QueryValueEx(key, "Path")[0]))
                except OSError:
                    out.append("")
            return tuple(out)
    machine_path, user_path = read_paths()
    cur = os.environ.get("PATH", "")
    known = {p.strip().lower() for p in cur.split(os.pathsep) if p.strip()}
    for part in (machine_path, user_path):
        for entry in part.split(os.pathsep):
            e = entry.strip()
            if e and e.lower() not in known:
                known.add(e.lower())
                added.append(e)
    if added:
        os.environ["PATH"] = os.pathsep.join(([cur] if cur else []) + added)
    return added
