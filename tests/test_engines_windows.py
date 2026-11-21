"""P6: engine discovery on Windows, testable ON LINUX.

The discovery logic branches on a module constant (`_IS_WINDOWS`) and plain
env vars - both monkeypatchable - so the Program Files chain, the .exe
candidate rule and the Ghostscript `gs\\gs*\\bin` layout are all locked
HERE, on the Arch bench, without a Windows box. Behaviour that genuinely
needs Windows (PATHEXT through shutil.which, real registry paths) is
exercised by the windows-latest CI job running the rest of the suite.
"""
from __future__ import annotations

import sys

import pytest

from transmute.probe import engines as E

pytestmark = pytest.mark.unit


@pytest.fixture()
def win_mode(tmp_path, monkeypatch):
    """Pretend to be Windows with a laid-out fake Program Files tree."""
    monkeypatch.setattr(E, "_IS_WINDOWS", True)
    pf = tmp_path / "PF"
    la = tmp_path / "LocalAppData"
    pd = tmp_path / "ProgramData"
    home = tmp_path / "home"
    for p in (pf / "ffmpeg" / "bin", pf / "qpdf" / "bin",
              pf / "gs" / "gs10.13.0" / "bin", pf / "LibreOffice" / "program",
              pf / "Pandoc", pf / "poppler-24.08.0" / "Library" / "bin",
              la / "Programs", la / "Microsoft" / "WinGet" / "Links",
              pd / "chocolatey" / "bin", home / "scoop" / "shims"):
        p.mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("ProgramData", str(pd))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # PATH lookups must find nothing: conventional dirs are under test alone.
    monkeypatch.setattr(E.shutil, "which", lambda name: None)
    return pf, la, pd, home


def test_windows_conventional_dirs_cover_install_chains(win_mode):
    pf, la, pd, home = win_mode
    dirs = [str(p) for p in E._conventional_dirs()]
    assert str(pf / "ffmpeg" / "bin") in dirs
    assert str(pf / "qpdf" / "bin") in dirs
    # Ghostscript is versioned and the CANDIDATE DIR is the bin, not the
    # version directory (regression: P1 globbed gs/gs* and stopped there).
    assert str(pf / "gs" / "gs10.13.0" / "bin") in dirs
    assert str(pf / "gs" / "gs10.13.0") not in dirs
    assert str(pf / "LibreOffice" / "program") in dirs
    assert str(pf / "Pandoc") in dirs
    assert str(pf / "poppler-24.08.0" / "Library" / "bin") in dirs
    assert str(la / "Programs") in dirs
    assert str(la / "Microsoft" / "WinGet" / "Links") in dirs   # winget shims
    assert str(pd / "chocolatey" / "bin") in dirs               # choco shims
    assert str(home / "scoop" / "shims") in dirs


def test_windows_candidate_paths_find_exe_in_conventional_dirs(win_mode):
    pf, *_ = win_mode
    exe = pf / "ffmpeg" / "bin" / "ffmpeg.exe"
    exe.write_bytes(b"@echo off")
    mgr = E.EngineManager()
    cands = [str(p) for p in mgr._candidate_paths(E.KNOWN_ENGINES["ffmpeg"])]
    assert str(exe) in cands, (
        "conventional-dir candidates must try the .exe suffix (shutil.which "
        "honours PATHEXT, a Path join does not)")


def test_windows_names_prefer_native_binaries(win_mode):
    # gs spec carries per-OS overrides; windows mode must use gswin64c first.
    names = E._names_for(E.KNOWN_ENGINES["gs"])
    assert names[0] == "gswin64c"


def test_linux_conventional_dirs_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "_IS_WINDOWS", False)
    # as_posix, not str(): WindowsPath("/usr/bin") renders "\usr\bin"
    dirs = [p.as_posix() for p in E._conventional_dirs()]
    assert "/usr/bin" in dirs
    assert not any("Program Files" in d or "\\PF" in d for d in dirs)


def test_qpdf_version_re_accepts_argv0_echo():
    # Some Windows qpdf builds echo argv[0]: "qpdf.EXE version 12.4.2"
    # (WinBoat VM, 2026-11; the console-less frozen exe failed to parse it
    # while qpdf on every Linux ever tested prints "qpdf version ...").
    rx = E.KNOWN_ENGINES["qpdf"].version_re
    assert rx.search("qpdf.EXE version 12.4.2").group(1) == "12.4.2"
    assert rx.search("qpdf version 11.9.1").group(1) == "11.9.1"
