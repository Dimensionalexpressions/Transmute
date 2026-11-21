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


def test_already_installed_is_success_not_failure():
    # live-fire from the Windows test box 2026-11: winget refused to
    # "reinstall" QPDF.QPDF (exact version present) and exited with this
    # error - microsoft/winget-cli#4262. Both display forms must count.
    assert ins.install_succeeded(-1978335189)
    assert ins.install_succeeded(2316632107)
    assert ins.install_code_is_already_installed(2316632107)
    # tool found on disk after the run: installed one way or another
    assert ins.install_succeeded(1, engine_present=True)
    # winget's own words also settle it
    assert ins.install_succeeded(1, False,
                                 "Found an existing package already installed.")
    # a genuinely failed install stays a failure
    assert not ins.install_succeeded(5, False, "package not found")
    assert not ins.install_code_is_already_installed(5)


def test_qpdf_versioned_install_dir_is_searched(tmp_path, monkeypatch):
    # winget QPDF.QPDF = NSIS installer: DefaultInstallLocation
    # "%ProgramFiles%qpdf 12.4.2" (SPACE) with the EXE under bin\. The
    # fixed "qpdf\bin" probe never looked there, so installed qpdf kept
    # showing as missing (and the caveats listed qpdf repair/optimise).
    from transmute.probe import engines as eng
    prog = tmp_path / "Program Files"
    exe = prog / "qpdf 12.4.2" / "bin" / "qpdf.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(eng, "_IS_WINDOWS", True)
    monkeypatch.setitem(os.environ, "ProgramFiles", str(prog))
    monkeypatch.setitem(os.environ, "ProgramW6432", str(prog))
    monkeypatch.delitem(os.environ, "ProgramFiles(x86)", raising=False)
    dirs = eng._conventional_dirs()
    assert exe.parent in dirs


def test_soffice_never_resolves_to_the_com_console_shim(tmp_path, monkeypatch):
    # soffice.COM is a console wrapper that prompts "press any key" and
    # hangs headless - shutil.which follows PATHEXT and finds .COM first.
    # On Windows the probe must ask for soffice.exe explicitly.
    from transmute.probe import engines as eng
    monkeypatch.setattr(eng, "_IS_WINDOWS", True)
    spec = eng.KNOWN_ENGINES["libreoffice"]
    names = eng._names_for(spec)
    assert "soffice.exe" in names
    assert "soffice" not in names           # bare name = PATHEXT roulette


def test_libreoffice_version_probe_is_always_headless():
    # user laptop 2026-11: v0.2.5 "kept launching soffice.exe" - a plain
    # `soffice --version` can open LibreOffice's window (and first-run
    # wizard); the probe must never let that happen.
    from transmute.probe.engines import KNOWN_ENGINES
    assert KNOWN_ENGINES["libreoffice"].version_args[0] == "--headless"
    assert "--version" in KNOWN_ENGINES["libreoffice"].version_args
