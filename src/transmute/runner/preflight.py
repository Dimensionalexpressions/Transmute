"""Disk-space preflight (P5): MEASURE the destination volume before an
engine writes a single byte.

This is a floor check, not a byte-exact forecast: it stops the certain
failures — nearly-full disks and size-target jobs that need more than the
source is — while never blocking work on a probe that cannot measure
(unreadable path -> proceed; ENOSPC mid-run still lands in the retry ladder
as an ordinary transient failure, visible with the engine's own error).

The estimate itself is honest about its parts:
- staging writes the whole output next to dst before os.replace, so the
  volume must hold the output; for the conversions we offer, output is
  typically at most ~source size (transcode down/around, lossless ops drop
  objects) — source size is used as the documented standing estimate;
- a size-target job needs its budget + measured VBV slack (~10% overshoot
  was measured in P3), so the budget, not the source, is the floor there;
- a small absolute headroom covers engine temp files and fs metadata.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

MIN_FREE_BYTES = 128 * 1024 * 1024   # engine temp files + fs metadata headroom
SIZE_TARGET_SLACK = 1.15             # P3 measured size-target overshoot ceiling


def required_bytes(src_size: int, options: dict[str, Any] | None = None) -> int:
    """Floor requirement for one job on the destination volume."""
    need = max(int(src_size), MIN_FREE_BYTES)
    size_mb = (options or {}).get("size_mb")
    if size_mb:
        need = max(need, int(float(size_mb) * 1e6 * SIZE_TARGET_SLACK))
    return need


def _mib(n: float) -> str:
    return f"{n / 2 ** 20:,.0f} MiB"


def shortage(dst_dir: Path, src_size: int,
             options: dict[str, Any] | None = None) -> tuple[int, int] | None:
    """Return (required_bytes, free_bytes) when the volume CANNOT fit the
    job; None when there is room OR free space is unmeasurable."""
    need = required_bytes(src_size, options)
    # dst dir may not exist yet: probe the nearest existing ancestor
    probe = Path(dst_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        du = shutil.disk_usage(probe)
    except OSError:
        return None  # cannot measure -> do not block real work on the probe
    if du.free >= need:
        return None
    return (need, du.free)


def reason_for(short: tuple[int, int]) -> str:
    need, free = short
    return (f"insufficient space on destination volume: need >= {_mib(need)} "
            f"free, measured {_mib(free)} — free up space or choose another "
            f"destination")
