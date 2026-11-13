"""All application-owned files go through platformdirs (F: platform hygiene).

Linux   -> ~/.config / ~/.local/share / ~/.cache (XDG)
Windows -> %LOCALAPPDATA%\\Transmute
Portable mode: a ``portable.ini`` file next to the executable (frozen build)
collapses everything into that directory instead.

Tests and the CLI can override the whole footprint with the environment
variable ``TRANSMUTE_ROOT`` (a single directory), which keeps test runs from
touching the real user directories.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from platformdirs import AppDirs

from . import APP_ID, APP_NAME

_ENV_ROOT = "TRANSMUTE_ROOT"


def _exe_dir() -> Path:
    """Directory of the executable / entry script (portable anchor)."""
    if getattr(sys, "frozen", False):  # PyInstaller onedir
        return Path(sys.executable).resolve().parent
    # dev checkout: do not treat source tree as portable root unless asked
    return Path(sys.executable).resolve().parent


def portable_root() -> Path | None:
    """Return the portable root if portable.ini sits beside the binary."""
    cand = _exe_dir() / "portable.ini"
    try:
        if cand.is_file():
            return cand.parent
    except OSError:
        pass
    return None


class AppPaths:
    """Resolved on-disk locations owned by the app (never None, lazy mkdir)."""

    def __init__(self, root: Path | str | None = None):
        env = os.environ.get(_ENV_ROOT)
        if root is not None:
            base = Path(root)
            self.portable = True
        elif env:
            base = Path(env)
            self.portable = True
        else:
            pr = portable_root()
            if pr is not None:
                base = pr
                self.portable = True
            else:
                base = Path(AppDirs(APP_NAME.lower(), APP_ID).user_data_dir)
                self.portable = False
        self.base = base
        # config/state are durable; logs/cache are rebuildable
        self.config_dir = base / "config"
        self.state_dir = base / "state"
        self.logs_dir = base / "logs"
        self.cache_dir = base / "cache"

    def ensure(self) -> "AppPaths":
        for d in (self.config_dir, self.state_dir, self.logs_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def capability_cache(self) -> Path:
        return self.cache_dir / "capability"

    @property
    def event_log(self) -> Path:
        return self.logs_dir / "events.jsonl"


_default: AppPaths | None = None


def paths() -> AppPaths:
    """Process-wide default paths (override via env or set_paths())."""
    global _default
    if _default is None:
        _default = AppPaths().ensure()
    return _default


def set_paths(p: AppPaths) -> None:
    global _default
    _default = p.ensure()


@contextmanager
def temp_paths(root: Path) -> Iterator[AppPaths]:
    """Context manager used by tests: point the whole app footprint at a tmp dir."""
    saved = _default
    try:
        p = AppPaths(root=root)
        set_paths(p)
        yield p
    finally:
        _default = saved
