"""The release-gate self-check itself must never be the broken link.

run_smoke() is what CI's Windows/Linux VMs run against the FROZEN build
before an asset is packed (2026-11: field bugs shipped "green" because no
VM ever probed LibreOffice or started serve inside the shipped binary).
"""
import shutil

import pytest


def test_smoke_flag_routes_to_selfcheck(monkeypatch):
    # the frozen entry point is the GUI; --smoke must bypass Qt entirely
    import transmute.selfcheck as sc
    calls: list[int] = []
    monkeypatch.setattr(sc, "run_smoke", lambda: calls.append(1) or 0)
    from transmute.gui.app import main
    assert main(["--smoke"]) == 0
    assert calls == [1]


@pytest.mark.skipif(not shutil.which("ffmpeg"),
                    reason="self-check requires the real engines")
def test_selfcheck_passes_with_real_engines_and_serve(monkeypatch):
    import os
    import transmute.selfcheck as sc
    # engines installed on dev boxes vary; demand only what is actually
    # present, exactly like the Linux CI step does
    want = [n for n in ("libreoffice", "gs", "qpdf") if shutil.which(n)]
    monkeypatch.setenv("TRANSMUTE_SMOKE_ENGINES", " ".join(want))
    assert sc.run_smoke() == 0
    os.remove(os.path.join(os.getcwd(), "transmute-smoke.log"))
