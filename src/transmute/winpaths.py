"""P6: Windows path semantics - UNC and >MAX_PATH ("long path") handling.

Windows refuses Win32-file-API paths longer than 259 chars unless the
caller uses the extended-length form: "\\\\?\\" + fully-normalised absolute
path (drive form) or "\\\\?\\UNC\\server\\share\\..." for UNC. The prefix
disables ALL parser magic, so "." and ".." and "/" must be resolved BEFORE
adding it (we normalise with ntpath, which is pure string logic and fully
unit-testable on Linux).

Nothing here assumes a Windows *machine*: `protect()` is a no-op unless
`IS_WIN` (read at call time so tests can flip it). The doctrine is the same
as everywhere else in Transmute - the app runs correctly on the OS it is
actually on, and every degradation carries a reason.
"""
from __future__ import annotations

import ntpath
import os
import sys

IS_WIN = os.name == "nt"          # flipped by tests to exercise win logic on Linux
MAX_PATH = 260                    # CreateFile limit incl. NUL: usable length is 259
LONG = "\\\\?\\"
LONG_UNC = "\\\\?\\UNC\\"


def is_unc(path: str | os.PathLike) -> bool:
    s = str(path).replace("/", "\\")
    return s.startswith("\\\\") and not s.startswith(LONG)


def absolutize(path: str | os.PathLike, *, cwd: str | None = None) -> str:
    """ntpath.abspath WITHOUT touching the running OS's cwd.

    On Windows os.getcwd() would be used (default `cwd=None`). Off-Windows
    callers (tests) must pass an explicit windows-form `cwd`.
    """
    s = str(path).replace("/", "\\")
    if ntpath.splitdrive(s)[0] or is_unc(s):
        return ntpath.normpath(s)
    if cwd is None:
        if not IS_WIN:
            raise ValueError("absolutize: pass cwd= off Windows")
        cwd = os.getcwd()
    return ntpath.normpath(ntpath.join(cwd, s))


def needs_long_prefix(path: str | os.PathLike) -> bool:
    return len(str(path)) >= MAX_PATH


def to_extended(path: str | os.PathLike, *, cwd: str | None = None) -> str:
    """Fully-normalised extended-length form. Idempotent.

    'C:\\a\\b\\..\\c'          -> '\\\\?\\C:\\a\\c'
    '\\\\srv\\share\\x'         -> '\\\\?\\UNC\\srv\\share\\x'
    'C:/mixed/sep'             -> '\\\\?\\C:\\mixed\\sep'
    '\\\\?\\C:\\already\\long'  -> unchanged (no double prefix)
    """
    s = str(path)
    if s.startswith(LONG):
        return s
    p = absolutize(s, cwd=cwd)     # normalises separators, '.', '..'
    if is_unc(p):
        return LONG_UNC + p.lstrip("\\")
    return LONG + p


def protect(path: str | os.PathLike) -> str:
    """The path to actually hand to the OS. No-op off Windows and for paths
    short enough that the legacy parser handles them (keeps diagnostics
    readable; the \\?\\ form is ugly in logs and in ffmpeg's own messages).
    """
    if not IS_WIN:
        return str(path)
    s = absolutize(path)
    return to_extended(s) if needs_long_prefix(s) else s


def protect_argv(argv: list[str]) -> list[str]:
    """Apply protect() to arguments that are windows paths (absolute drive
    or UNC). Flags, filters, codec names and numbers pass through untouched:
    only tokens ntpath considers absolute are candidates, and only ones the
    legacy parser would choke on get rewritten. Off Windows this returns the
    list unchanged, byte for byte.
    """
    if not IS_WIN:
        return argv
    out = []
    for a in argv:
        if (a[:1] not in ("-", "/") and ntpath.splitdrive(a)[0]) or is_unc(a):
            out.append(protect(a))
        else:
            out.append(a)
    return out


def _win32_long_paths_enabled() -> bool | None:
    """Per-OS opt-in (Win10+ manifests usually enable it for apps; ffmpeg
    without such a manifest still hits 259). Returns None when unmeasurable.
    We MEASURE via the registry rather than assume (P6; cached by caller).
    """
    if not (IS_WIN and sys.platform == "win32"):
        return None
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            v, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
        return bool(v)
    except OSError:
        return None
