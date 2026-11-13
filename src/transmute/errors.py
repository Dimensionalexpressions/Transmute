"""Exception hierarchy.

Rule: engine/runner boundary functions return *result objects*; exceptions are
for programming errors and for the CLI/GUI to render. A missing engine must
never crash a caller - `EngineUnavailable` carries the human guidance, and
callers that treat availability as data use `Availability` instead.
"""
from __future__ import annotations


class TransmuteError(Exception):
    """Base class for all Transmute errors."""


class EngineUnavailable(TransmuteError):
    """A required external engine binary is missing or not runnable.

    Carries everything the UI needs to explain the situation per-OS (F26).
    """

    def __init__(self, engine: str, reason: str, guidance: dict[str, str] | None = None):
        self.engine = engine
        self.reason = reason
        self.guidance = guidance or {}
        super().__init__(f"engine '{engine}' unavailable: {reason}")


class CapabilityError(TransmuteError):
    """The capability probe could not produce a trustworthy report."""


class JobError(TransmuteError):
    """A conversion job failed in a way that should be logged, not raised."""


class VerifyFailed(JobError):
    """Output failed verification - the source must never be replaced."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"verification failed: {reason}")


class GovernorError(TransmuteError):
    """Resource governor setup failed in a way the caller must know about."""
