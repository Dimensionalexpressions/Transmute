"""Boot-path regression (P2): the REAL capability-probe path of the
controller. Unit tests inject a fake report, which once let a crash
(CapabilityProber got an unexpected `paths=` kwarg) survive the whole suite
while the actual app could not start. This test executes the production path.
"""
from __future__ import annotations

import pytest

from transmute.paths import AppPaths
from transmute.gui.controller import ConverterController

pytestmark = pytest.mark.e2e


def test_controller_real_probe_no_qt_and_isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")  # probe must never need GPU
    c = ConverterController(paths=AppPaths(root=tmp_path / "root"))
    rep = c.report                     # used to raise TypeError here
    assert rep.matrix
    assert any(r.category == "image" and r.available for r in rep.matrix)
    # cache landed inside the injected root, not the user's real config
    cached = list((tmp_path / "root").rglob("*.json"))
    assert cached, "probe cache must be written under the injected AppPaths root"
