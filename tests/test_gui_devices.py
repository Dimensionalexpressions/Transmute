"""CPU/GPU device picker (user request): the machine's REAL hardware, named
from /proc and /sys reads - never a build-time assumption - and every speed
ladder scoped to the device the user picked. Qt-free, engines-free."""
from __future__ import annotations

import time

import pytest

from transmute.gui import controller as C
from transmute.gui.controller import ConverterController
from transmute.probe.capability import (
    CapabilityReport, EncoderCapability, MatrixRow,
)

pytestmark = pytest.mark.unit


def _rep() -> CapabilityReport:
    """Two working CPU encoders, a working VA-API one, a broken NVENC (the
    RTX-5090-under-load case this box really hit)."""
    enc = [
        EncoderCapability(name="libsvtav1", codec="av1", backend="cpu",
                          hw=False, verified=True, fps=500.0),
        EncoderCapability(name="libaom-av1", codec="av1", backend="cpu",
                          hw=False, verified=True, fps=140.0),
        EncoderCapability(name="libx264", codec="h264", backend="cpu",
                          hw=False, verified=True, fps=760.0),
        EncoderCapability(name="libx264_vaapi", codec="h264",
                          backend="va-api", hw=True, verified=True,
                          fps=900.0),
        EncoderCapability(name="h264_nvenc", codec="h264",
                          backend="nvidia-nvenc", hw=True, verified=False,
                          fps=None,
                          reason="trial encode failed: NVENC err -22"),
    ]
    return CapabilityReport(schema=1, probe_key="k",
                            generated_at=time.time(), app_version="t",
                            machine={}, engines={}, encoders=enc,
                            filters={}, gpu_nodes={}, image_backends={},
                            pdf_backends={}, caveats=[], matrix=[
                                MatrixRow(category="video", target="av1",
                                          path="x", available=True,
                                          verified=True),
                                MatrixRow(category="video", target="h264",
                                          path="x", available=True,
                                          verified=True),
                            ])


@pytest.fixture
def hw(monkeypatch):
    """Fake 'this machine': one NVIDIA dGPU + one AMD iGPU (dual-GPU is the
    case the user asked about); no subprocesses run in tests."""
    monkeypatch.setattr(C, "cpu_model", lambda: "Test CPU 9900")
    monkeypatch.setattr(C, "gpu_render_nodes", lambda: [
        {"driver": "nvidia", "vendor": "0x10de", "device": "0x2b85",
         "slot": "01:00.0"},
        {"driver": "amdgpu", "vendor": "0x1002", "device": "0x13c0",
         "slot": "00:02.0"},
    ])
    monkeypatch.setattr(C, "nvidia_gpu_names",
                        lambda: ["NVIDIA GeForce RTX 9000"])
    monkeypatch.setattr(C, "lspci_product",
                        lambda slot: "Granite Ridge [Radeon Graphics]")
    return monkeypatch


@pytest.fixture
def ctrl(hw, tmp_path):
    from transmute.paths import AppPaths
    # prefs go to a temp root: the suite must not pin devices in the real
    # .gui-live config a developer is running against
    return ConverterController(report_provider=_rep, queue=object(),
                               paths=AppPaths(root=tmp_path))


class TestDevicesListing:
    def test_cpu_first_then_one_entry_per_gpu(self, ctrl):
        got = ctrl.devices()
        assert [d["id"] for d in got] == \
            ["cpu", "gpu:nvidia:0x2b85", "gpu:amdgpu:0x13c0"]
        assert [d["kind"] for d in got] == ["cpu", "gpu", "gpu"]

    def test_devices_carry_the_machines_own_names(self, ctrl):
        by_id = {d["id"]: d for d in ctrl.devices()}
        assert by_id["cpu"]["name"] == "Test CPU 9900"
        assert by_id["gpu:nvidia:0x2b85"]["name"] == "NVIDIA GeForce RTX 9000"
        assert by_id["gpu:amdgpu:0x13c0"]["name"] == \
            "Granite Ridge [Radeon Graphics]"

    def test_broken_gpu_stays_visible_with_its_reason(self, ctrl, monkeypatch):
        """Doctrine: nothing is hidden; the machine's probe words travel
        with the entry so the picker can say WHY it is marked off. This is
        the state WHILE MEASURING - the off-switch case is tested below."""
        monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
        nv = next(d for d in ctrl.devices() if d["kind"] == "gpu"
                  and "nvidia" in d["id"])
        assert nv["works"] is False and nv["disabled"] is False
        assert "-22" in nv["why"]
        amd = next(d for d in ctrl.devices() if d["id"].startswith(
            "gpu:amdgpu"))
        assert amd["works"] is True and amd["why"] == ""

    def test_identical_cards_collapse_to_one_honest_entry(self, ctrl, hw):
        hw.setattr(C, "gpu_render_nodes", lambda: [
            {"driver": "amdgpu", "vendor": "0x1002", "device": "0x13c0",
             "slot": "00:02.0"},
            {"driver": "amdgpu", "vendor": "0x1002", "device": "0x13c0",
             "slot": "03:00.0"},
        ])
        ctrl.set_device("gpu:amdgpu:0x13c0")  # drops the cached listing
        got = [d for d in ctrl.devices() if d["kind"] == "gpu"]
        assert len(got) == 1
        assert "2 identical GPUs" in got[0]["name"]

    def test_no_gpu_machine_lists_cpu_only(self, ctrl, hw):
        hw.setattr(C, "gpu_render_nodes", lambda: [])
        ctrl.set_device("cpu")  # invalidate the cached listing
        assert [d["id"] for d in ctrl.devices()] == ["cpu"]


class TestDeviceScopedLadders:
    def test_auto_means_the_whole_verified_ladder(self, ctrl):
        assert ctrl.device_backends() is None
        assert ctrl.encoders("video", "h264") == \
            ["libx264_vaapi", "libx264"]  # fastest first stays fastest

    def test_pinning_cpu_narrows_every_ladder_and_estimates(self, ctrl):
        ctrl.set_device("cpu")
        assert ctrl.device_backends() == {"cpu"}
        assert ctrl.encoders("video", "h264") == ["libx264"]
        assert ctrl.encoders("video", "av1") == ["libsvtav1", "libaom-av1"]
        # 'auto' on a pinned device resolves to a concrete encoder, so the
        # measured estimate and the queue job provably run on that device
        assert ctrl.auto_encoder("video", "h264") == "libx264"

    def test_pinning_a_gpu_scopes_to_that_gpus_backends(self, ctrl):
        ctrl.set_device("gpu:amdgpu:0x13c0")
        assert ctrl.encoders("video", "h264") == ["libx264_vaapi"]
        assert ctrl.auto_encoder("video", "h264") == "libx264_vaapi"
        ctrl.set_device("gpu:nvidia:0x2b85")  # the broken one: nothing runs
        assert ctrl.encoders("video", "h264") == []
        assert ctrl.auto_encoder("video", "h264") is None

    def test_device_choice_carries_name_and_reason(self, ctrl):
        assert ctrl.device_choice() is None  # auto
        ctrl.set_device("gpu:nvidia:0x2b85")
        d = ctrl.device_choice()
        assert d["name"] == "NVIDIA GeForce RTX 9000" and not d["works"]

    def test_stale_device_pref_falls_back_to_auto(self, ctrl):
        """A saved device id from an older machine/boot must never hide the
        working paths - it degrades to Auto, honestly."""
        ctrl.set_device("gpu:nvidia:0xdead")
        assert ctrl.device_backends() is None
        assert ctrl.encoders("video", "h264") == \
            ["libx264_vaapi", "libx264"]

    def test_choice_survives_prefs_roundtrip(self, tmp_path, ctrl, hw):
        from transmute.paths import AppPaths
        c2 = ConverterController(report_provider=_rep, queue=object(),
                                 paths=AppPaths(root=tmp_path))
        c2.set_device("cpu")
        c3 = ConverterController(report_provider=_rep, queue=object(),
                                 paths=AppPaths(root=tmp_path))
        assert c3.device() == "cpu"


class TestHardwareParsers:
    def test_lspci_line_to_product_name(self):
        assert C._strip_lspci_product(
            "00:02.0 VGA compatible controller: Advanced Micro Devices, Inc. "
            "[AMD/ATI] Granite Ridge [AMD Radeon 820M] (rev c5)") == \
            "Granite Ridge [AMD Radeon 820M]"
        assert C._strip_lspci_product("no colon here") == ""

    def test_cpu_model_never_raises(self, monkeypatch):
        class Boom:
            def __init__(self, *a): pass
            def read_text(self, *a, **k):
                raise OSError("no cpuinfo here")
        monkeypatch.setattr(C, "Path", Boom)
        assert C.cpu_model() == "your processor"  # unreadable -> honest filler

    def test_gpu_names_are_inventory_not_encoding(self, monkeypatch):
        """Switched-off GPU ENCODING must not hide the device list: reading
        names (nvidia-smi/lspci) encodes nothing, so the picker keeps real
        names instead of degrading to vendor words."""
        import shutil as sh
        import subprocess as sp
        monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")
        monkeypatch.setattr(sh, "which", lambda n: "/usr/bin/" + n)
        seen = []

        class P:
            returncode = 0
            stdout = ""

        def fake_run(cmd, **kw):
            seen.append(cmd[0])
            p = P()
            p.stdout = ("NVIDIA GeForce RTX 9999\n" if cmd[0] == "nvidia-smi"
                        else "00:02.0 VGA controller: NVIDIA Inc. GeForce RTX 9999\n")
            return p

        monkeypatch.setattr(sp, "run", fake_run)
        assert C.nvidia_gpu_names() == ["NVIDIA GeForce RTX 9999"]
        assert "RTX 9999" in C.lspci_product("00:02.0")
        assert seen == ["nvidia-smi", "lspci"]

    def test_idle_gpu_says_why_in_user_words(self, hw, tmp_path, monkeypatch):
        """GPU measurement off -> report has no GPU rows -> the entry's own
        reason must state the CAUSE in user words, never 'not in this ffmpeg
        build' (which told a healthy RTX 5090 it was broken - real user,
        2026-11). And the bare word 'switched off' confused the user too:
        the message must say WHO holds it out and HOW to change that."""
        import dataclasses
        from transmute.paths import AppPaths
        rep = _rep()
        no_gpu = dataclasses.replace(
            rep, encoders=[e for e in rep.encoders if not e.hw])
        monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")
        c = ConverterController(report_provider=lambda *a, **k: no_gpu,
                                queue=object(), paths=AppPaths(root=tmp_path))
        gpus = [d for d in c.devices() if d["kind"] == "gpu"]
        assert len(gpus) == 2
        for d in gpus:
            assert d["works"] is False and d["disabled"] is True
            # no device chosen -> a launch-time hold; cause named anyway
            assert d["disabled_kind"] == "launch"
            assert "GPU use is disabled for this launch" in d["why"]
            assert "switched off" not in d["why"]  # the confusing word, banned
            assert "ffmpeg build" not in d["why"]
        cpu = next(d for d in c.devices() if d["kind"] == "cpu")
        assert cpu["disabled"] is False and cpu["works"] is True
        # the everyday case: the CPU / GPU choice IS the cause - say so
        c.set_device("cpu")
        for d in c.devices():
            if d["kind"] == "gpu":
                assert d["disabled_kind"] == "cpu-selected"
                assert "because CPU is chosen" in d["why"]
                assert "measures it on this PC" in d["why"]

    def test_cpu_choice_outranks_stale_measurements(self, hw, tmp_path,
                                                    monkeypatch):
        """A GPU whose probe rows carry an old measured failure is STILL
        merely idle while CPU is chosen - yesterday's verdict must not
        masquerade as today's state (the cache said so, 2026-11)."""
        from transmute.paths import AppPaths
        monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")
        c = ConverterController(report_provider=_rep, queue=object(),
                                paths=AppPaths(root=tmp_path))
        c.set_device("cpu")
        nv = next(d for d in c.devices() if "nvidia" in d["id"])
        assert nv["disabled"] is True and "CPU is chosen" in nv["why"]
        assert "-22" not in nv["why"]
