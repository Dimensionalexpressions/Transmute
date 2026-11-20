"""Live capability probe with real ffmpeg, trials stubbed to run fast.

Proves: full report builds and caches; TRANSMUTE_DISABLE_GPU hides every hw
row; on a GPU-less box hw rows are skipped with an honest device reason.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

from transmute.probe import capability as cap_mod


@pytest.fixture()
def fast_report(engine_info, monkeypatch, tmp_path):
    if not (engine_info.get("ffmpeg") and engine_info["ffmpeg"].available):
        pytest.skip("ffmpeg not available")
    import transmute.paths as paths_mod
    from transmute.paths import AppPaths
    monkeypatch.setattr(paths_mod, "_default",
                        AppPaths(root=tmp_path / "caproot").ensure())
    calls: list[str] = []

    def fake_trial(cmd):
        calls.append(" ".join(str(c) for c in cmd))
        return True, 100.0, ""

    monkeypatch.setattr(cap_mod, "_run_trial", fake_trial)
    monkeypatch.setattr(cap_mod, "_parse_pix_fmts", lambda *a, **k: [])
    monkeypatch.setattr(cap_mod, "_probe_ffmpeg_filters", lambda *a, **k: {"scale": True})
    monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
    from transmute.probe.capability import CapabilityProber
    prober = CapabilityProber()
    return prober, calls


def test_live_report_structure(fast_report):
    prober, calls = fast_report
    rep = prober.report(refresh=True)
    assert len(rep.encoders) > 50, "real ffmpeg must parse many encoders"
    assert any(e.verified for e in rep.encoders)
    assert calls, "trials were actually executed (measured ladder)"
    ib = rep.image_backends
    assert ib.get("version"), "Pillow version must be reported"
    feats = ib.get("features", {})
    assert feats.get("jpg") is True and feats.get("png") is True  # built-in plugins
    # second call served from cache: no new trial subprocesses
    n = len(calls)
    prober.report()
    assert len(calls) == n


def test_hw_rows_skipped_without_gpu(fast_report, monkeypatch):
    # Force a GPU-less machine deterministically: dev boxes DO have render
    # nodes, and the stubbed trial would verify every hw encoder there -
    # the skip-on-absent-nodes rule must be proven regardless of host.
    import sys
    if sys.platform == "win32":
        pytest.skip("the device-node gate is a Linux mechanism: Windows "
                    "MF/AMF/NVENC upload system frames with no /dev nodes, "
                    "so h264_mf VERIFYING on a GPU-less Windows runner is "
                    "honest behaviour, not a leak (CI run 44826691434 proved "
                    "the trial really encodes there via the software MFT).")
    monkeypatch.setattr(cap_mod, "_has_dri_render_node", lambda: False)
    monkeypatch.setattr(cap_mod, "_has_nvidia_nodes", lambda: False)
    prober, _ = fast_report
    rep = prober.report(refresh=True)
    hw = [e for e in rep.encoders if e.hw]
    if not hw:
        pytest.skip("this ffmpeg build lists no hardware encoders")
    for e in hw:
        assert not e.verified
        assert e.skipped, f"hw encoder {e.name} must carry a skip reason"


def test_disable_gpu_env_hides_hw(fast_report, monkeypatch):
    prober, _ = fast_report
    monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")
    rep = prober.report(refresh=True)
    for e in rep.encoders:
        if e.hw:
            assert not e.verified
            assert "gpu-probe-disabled" in (e.skipped or "") or "DISABLE_GPU" in (e.skipped or "")
    assert any("DISABLE_GPU" in c or "disabled" in c.lower() for c in rep.caveats)
