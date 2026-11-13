"""Source classification + never-touch rules shared by scan, runner and watch.

Proven ignore-set from production watchers: partial-download markers, hidden
files, per-file `.skip` guards. Extensions map to categories used everywhere
(F2/F3 grouping, F16 watch, F20 skip, matrix UI).
"""
from __future__ import annotations

import os
from pathlib import Path

# Extensions of files still being written by downloaders (never touch, F16).
PARTIAL_SUFFIXES = (
    ".part", ".crdownload", ".download", ".tmp", ".partial",
    ".incomplete", ".transferred", "!utfile", "!aDownload",
)
SKIP_SUFFIX = ".skip"

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "video": {
        ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts",
        ".mts", ".mpg", ".mpeg", ".3gp", ".3g2", ".vob", ".wmv", ".flv",
        ".asf", ".rm", ".rmvb", ".ogv", ".dv", ".mxf", ".nut", ".swf",
    },
    "image": {
        ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif",
        ".heic", ".heif", ".avif", ".jfif",
    },
    "audio": {
        ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
        ".mka", ".wma", ".aiff", ".aif", ".alac", ".m4b",
    },
    "pdf": {".pdf"},
    "document": {
        ".odt", ".ott", ".doc", ".docx", ".odp", ".ods", ".xlsx", ".xls",
        ".rtf", ".txt", ".md", ".html", ".htm", ".epub",
    },
}

_CATEGORY_OF: dict[str, str] = {
    ext: cat for cat, exts in CATEGORY_EXTENSIONS.items() for ext in exts
}


def category_of(path: Path | str) -> str | None:
    """'video' | 'image' | 'audio' | 'pdf' | 'document' | None (unsupported)."""
    return _CATEGORY_OF.get(Path(path).suffix.lower())


def is_ignored(path: Path) -> bool:
    """True when a file must never be queued (hidden / partial / .skip-guarded)."""
    p = Path(path)
    name = p.name
    if any(part.startswith(".") for part in p.parts):
        return True
    low = name.lower()
    if any(low.endswith(suf) for suf in PARTIAL_SUFFIXES):
        return True
    if p.is_file() and p.with_name(name + SKIP_SUFFIX).exists():
        return True
    return p.with_name(name + SKIP_SUFFIX).exists()


def skip_guarded(path: Path) -> bool:
    """True if a sibling '<name>.skip' marker protects this file."""
    p = Path(path)
    return p.with_name(p.name + SKIP_SUFFIX).exists()


def is_hidden(path: Path) -> bool:
    p = Path(path)
    return any(part.startswith(".") for part in p.parts)


def iter_supported_files(root: Path) -> "os.PathLike | None":
    """Yield supported, non-ignored files under root (recursive)."""
    root = Path(root)
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            if fp.suffix.lower() in _CATEGORY_OF and not is_ignored(fp):
                yield fp
