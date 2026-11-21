"""The get-and-install helper (Option A): mappings stay honest, the PATH
refresh merges registry entries, and nothing claims a package exists that
does not (Ghostscript has no winget package - by design it is absent)."""
import os

import pytest

from transmute.probe import installer as ins


def test_winget_map_is_honest_and_verified():
    # Ghostscript: AGPL, official .msi only - must NOT have a winget ID.
    assert "gs" not in ins.WINGET_IDS
    for engine, pkg in ins.WINGET_IDS.items():
        assert "." in pkg and len(pkg.split(".")[0]) > 1   # Publisher.App shape
        assert engine in ins.DISPLAY_NAMES and engine in ins.LICENSE_INFO
        assert ins.DOWNLOAD_PAGES[engine].startswith("https://")
    # The pages we verified against winget-pkgs (2026-11) stay pinned:
    assert ins.WINGET_IDS["ffmpeg"] == "Gyan.FFmpeg"
    assert ins.WINGET_IDS["qpdf"] == "QPDF.QPDF"
    assert ins.WINGET_IDS["pdftoppm"] == "oschwartz10612.Poppler"
    assert ins.WINGET_IDS["libreoffice"] == "TheDocumentFoundation.LibreOffice"
    assert ins.WINGET_IDS["pandoc"] == "JohnMacFarlane.Pandoc"


def test_gs_points_at_artifex_page():
    assert "ghostscript.com" in ins.DOWNLOAD_PAGES["gs"]
    assert "AGPL" in ins.LICENSE_INFO["gs"]


def test_install_command_shape():
    cmd = ins.install_command("ffmpeg")
    assert cmd and cmd[0] == "winget" and "--id" in cmd
    assert "Gyan.FFmpeg" in cmd and "-e" in cmd
    assert ins.install_command("gs") is None
    with pytest.raises(ValueError):
        ins.run_install("gs", lambda line: None)


def test_refresh_path_merges_registry_entries(monkeypatch):
    if not hasattr(ins, "refresh_path_from_registry"):
        pytest.fail("installer API gone")
    monkeypatch.setattr(ins, "_IS_WINDOWS", True)
    # faithful Windows host: PATH separator is ';'
    monkeypatch.setattr(os, "pathsep", ";")
    monkeypatch.setenv("PATH", r"C:\old\bin")
    added = ins.refresh_path_from_registry(read_paths=lambda: (
        r"C:\old\bin;C:\Windows\System32",
        r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links;",
    ))
    assert added == [r"C:\Windows\System32",
                     r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links"]
    assert os.environ["PATH"].endswith(
        r"C:\Windows\System32" + os.pathsep
        + r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links")
    # idempotent: a second call with the same registry adds nothing
    assert ins.refresh_path_from_registry(read_paths=lambda: (
        r"C:\old\bin;C:\Windows\System32",
        r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links")) == []


def test_refresh_path_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(ins, "_IS_WINDOWS", False)
    before = os.environ.get("PATH", "")
    assert ins.refresh_path_from_registry(read_paths=lambda: ("x", "y")) == []
    assert os.environ.get("PATH", "") == before
