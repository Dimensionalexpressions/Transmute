"""Static integrity of the ENGINES spec table - the argv typos that only a
REAL installed engine can catch, so we catch them here instead.

Background: the qpdf spec shipped with version_args=("--version") - a bare
string, not a tuple (missing trailing comma). It looked fine while qpdf was
absent (status MISSING never reached the argv), and exploded on the first
machine that actually had qpdf installed (bench, 2026-11, qpdf 12.4.2): the
string splatted character-by-character into argv. qpdf's real --version
output then parses fine, proving the argv was the whole failure.
"""
from __future__ import annotations

from transmute.probe.engines import KNOWN_ENGINES as ENGINES


def test_version_args_are_sequences_of_str_never_bare_strings():
    for name, spec in ENGINES.items():
        assert isinstance(spec.version_args, tuple), (
            f"{name}: version_args must be a tuple, got {type(spec.version_args).__name__} "
            f"- a bare string splats into per-character argv"
        )
        assert spec.version_args, f"{name}: empty version_args"
        for a in spec.version_args:
            assert isinstance(a, str) and a, f"{name}: bad argv token {a!r}"
            assert " " not in a, (
                f"{name}: {a!r} looks like multiple args crammed into one token"
            )


def test_engine_names_match_spec_names():
    for key, spec in ENGINES.items():
        assert key == spec.name, f"table key {key} != spec.name {spec.name}"


def test_every_spec_has_a_primary_binary_name():
    for name, spec in ENGINES.items():
        assert spec.binary_names and all(spec.binary_names), (
            f"{name}: binary_names must be non-empty strings"
        )
