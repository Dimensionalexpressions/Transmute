r"""P6: long-path / UNC handling - Windows path semantics tested on Linux.

ntpath is pure string logic, so every rule (\\?\ prefixing, \\?\UNC\ for
shares, '..' resolution BEFORE prefixing, MAX_PATH threshold) is locked
here on the Arch bench. protect()'s no-op behaviour off Windows is what
keeps the Linux code paths byte-identical - asserted, not assumed.
"""
from __future__ import annotations

import pytest

from transmute import winpaths as W

pytestmark = pytest.mark.unit

CWD = "C:\\work\\proj"


class TestAbsolutize:
    def test_drive_absolute_passthrough_normalised(self):
        assert W.absolutize("C:\\a\\b\\..\\c", cwd=CWD) == "C:\\a\\c"

    def test_forward_slashes_converted(self):
        assert W.absolutize("C:/mixed/sep", cwd=CWD) == "C:\\mixed\\sep"

    def test_relative_joined_against_given_cwd(self):
        assert W.absolutize("out\\x.mkv", cwd=CWD) == "C:\\work\\proj\\out\\x.mkv"

    def test_unc_stays_unc(self):
        assert W.absolutize("\\\\srv\\share\\dir\\f.png", cwd=CWD) == "\\\\srv\\share\\dir\\f.png"

    def test_off_windows_requires_cwd(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", False)
        with pytest.raises(ValueError):
            W.absolutize("rel\\path")


class TestToExtended:
    def test_drive_form(self):
        assert W.to_extended("C:\\a\\b", cwd=CWD) == "\\\\?\\C:\\a\\b"

    def test_unc_form(self):
        assert W.to_extended("\\\\srv\\share\\x", cwd=CWD) == "\\\\?\\UNC\\srv\\share\\x"

    def test_dotdot_resolved_BEFORE_prefixing(self):
        # \\?\ disables ALL parser magic - '..' must be gone by then.
        got = W.to_extended("C:\\a\\b\\..\\c", cwd=CWD)
        assert got == "\\\\?\\C:\\a\\c"
        assert ".." not in got

    def test_idempotent(self):
        once = W.to_extended("C:\\x\\y", cwd=CWD)
        assert W.to_extended(once, cwd=CWD) == once

    def test_relative_resolved_via_cwd(self):
        assert W.to_extended("deep\\f.mkv", cwd=CWD) == "\\\\?\\C:\\work\\proj\\deep\\f.mkv"


class TestProtect:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", False)
        long_path = "C:\\" + "d" * 300
        assert W.protect(long_path) == long_path          # untouched string
        assert W.protect_argv(["ffmpeg", "-i", long_path]) == ["ffmpeg", "-i", long_path]

    def test_short_path_left_alone_on_windows(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", True)
        monkeypatch.setattr(W.os, "getcwd", lambda: CWD)
        assert W.protect("C:\\a\\b.mkv") == "C:\\a\\b.mkv"

    def test_long_path_gets_prefix_on_windows(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", True)
        monkeypatch.setattr(W.os, "getcwd", lambda: CWD)
        deep = "C:\\" + "\\".join(["d" * 40] * 10)
        got = W.protect(deep)
        assert W.needs_long_prefix(got.lstrip("\\?"))  # underlying path is >259
        assert got.startswith("\\\\?\\C:\\")

    def test_long_unc_gets_unc_prefix_on_windows(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", True)
        monkeypatch.setattr(W.os, "getcwd", lambda: CWD)
        deep = "\\\\srv\\share\\" + "\\".join(["d" * 40] * 10)
        got = W.protect(deep)
        assert got.startswith("\\\\?\\UNC\\srv\\share\\")

    def test_threshold_is_max_path(self):
        assert not W.needs_long_prefix("x" * (W.MAX_PATH - 1))
        assert W.needs_long_prefix("x" * W.MAX_PATH)


class TestProtectArgv:
    def test_only_absolute_windows_paths_are_candidates(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", True)
        monkeypatch.setattr(W.os, "getcwd", lambda: CWD)
        argv = ["ffmpeg", "-i", "C:\\in\\a.mkv", "-vf", "fps=24",
                "-b:v", "2M", "\\\\srv\\share\\b.mkv"]
        got = W.protect_argv(argv)
        assert got[0] == "ffmpeg"
        assert got[1] == "-i"
        assert got[2] == "C:\\in\\a.mkv"          # short: untouched
        assert got[3] == "-vf" and got[4] == "fps=24" and got[5] == "-b:v"
        assert got[6] == "2M"
        assert got[7] == "\\\\srv\\share\\b.mkv"  # short UNC: legacy parser OK

    def test_filter_graph_and_numbers_never_rewritten(self, monkeypatch):
        monkeypatch.setattr(W, "IS_WIN", True)
        monkeypatch.setattr(W.os, "getcwd", lambda: CWD)
        argv = ["-vf", "scale=1280:-2", "-crf", "28", "1234"]
        assert W.protect_argv(argv) == argv
