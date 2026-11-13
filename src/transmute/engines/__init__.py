"""engines/: one wrapper per content category (P1 deliverable 3).

Category registry maps sources.py categories to engine singletons. Every
engine exposes: availability(), convert(src, dst_path, **opts), verify(src,
out_path) and never raises - failures are EngineResult(outcome='failed').
"""
from __future__ import annotations

from .base import Availability, CommandResult, EngineResult, EngineUnavailable, \
    run_command  # noqa: F401


def all_engines():
    """category -> engine instance (engines are cheap, stateless wrappers)."""
    from .audio import AudioEngine
    from .document import DocumentEngine
    from .image import ImageEngine
    from .pdf import PdfEngine
    from .video import VideoEngine
    return {
        "video": VideoEngine(),
        "image": ImageEngine(),
        "audio": AudioEngine(),
        "pdf": PdfEngine(),
        "document": DocumentEngine(),
    }


def engine_for(category: str):
    return all_engines().get(category)
