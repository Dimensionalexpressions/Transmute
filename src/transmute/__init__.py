"""Transmute - open-source media & document converter.

Design principle (non-negotiable): the app never assumes anything about the
machine it runs on. Every encoder / GPU / bit-depth / HDR capability comes
from a runtime probe executed on the user's system; anything the probe cannot
verify is hidden or greyed with a reason - never promised and broken.

P1 scope: headless core (probe / engines / runner / governor). The GUI and the
web portal are thin frontends over this same core (F18, F24).
"""

__version__ = "0.2.6.3"

APP_NAME = "Transmute"
APP_ID = "transmute"


def __getattr__(name):
    """Lazy top-level re-exports so README examples work without importing
    submodules eagerly (keeps import of `transmute` cheap)."""
    _map = {
        "EngineManager": "transmute.probe.engines",
        "CapabilityProber": "transmute.probe.capability",
        "CapabilityReport": "transmute.probe.capability",
        "JobQueue": "transmute.runner.queue",
    }
    if name in _map:
        import importlib
        mod = importlib.import_module(_map[name])
        return getattr(mod, name)
    raise AttributeError(f"module 'transmute' has no attribute {name!r}")
