"""probe/: live engine detection (F26) + runtime capability report (F4/F10)."""
from .engines import (EngineInfo, EngineManager, STATUS_ERROR, STATUS_MISSING,
                      STATUS_OK, KNOWN_ENGINES)
from .capability import (CapabilityCache, CapabilityProber, CapabilityReport,
                         EncoderCapability, MatrixRow, build_matrix,
                         gpu_force_disabled)

__all__ = [
    "EngineInfo", "EngineManager", "STATUS_OK", "STATUS_MISSING", "STATUS_ERROR",
    "KNOWN_ENGINES", "CapabilityCache", "CapabilityProber", "CapabilityReport",
    "EncoderCapability", "MatrixRow", "build_matrix", "gpu_force_disabled",
]
