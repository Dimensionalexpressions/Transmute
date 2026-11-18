"""GPU frame upload (2026-11): the probe verified *_vaapi encoders at
~200 fps, but the engine could not RUN them - no -init_hw_device, no
hwupload - so Auto mode could pick a measured encoder and fail at convert
time. The engine now mirrors the probe's executed trial: walk the render
nodes, name the one that runs the encoder NOW, and if none does, fail the
job instantly with the machine's own reason (drivers change; the benchmark
may no longer describe reality).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import transmute.engines.video as vid
from transmute.engines.video import VideoEngine, _hw_upload_needed
from transmute.probe.capability import EncoderCapability

CPU = EncoderCapability(name="libsvtav1", codec="av1", backend="cpu",
                        hw=False, verified=True, fps=500.0)
VAAPI = EncoderCapability(name="h264_vaapi", codec="h264", backend="va-api",
                          hw=True, verified=True, fps=190.0)
NVENC = EncoderCapability(name="hevc_nvenc", codec="hevc",
                          backend="nvidia-nvenc", hw=True, verified=True,
                          fps=115.0)


class _Res:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


def test_cpu_encoder_needs_no_upload():
    assert _hw_upload_needed(CPU) is None
    assert _hw_upload_needed(None) is None


def test_nvenc_not_in_upload_table():
    # measured in the probe: nvenc/amf reject forced hwupload - they take
    # system frames and upload inside the driver, so the engine must NOT
    # touch their command shape (this exact wrong guess was the bug class)
    assert _hw_upload_needed(NVENC) is None


def test_plan_names_the_node_whose_trial_passed(tmp_path, monkeypatch):
    eng = object.__new__(VideoEngine)
    good, bad = tmp_path / "renderD128", tmp_path / "renderD129"
    monkeypatch.setattr(vid, "render_nodes", lambda: [bad, good])

    def fake_run(cmd, **kw):
        # only the iGPU node's trial succeeds - exactly the multi-GPU trap
        return _Res(ok=cmd[cmd.index("-init_hw_device") + 1] == f"vaapi=va:{good}")

    monkeypatch.setattr(vid, "run_command", fake_run)
    pre, vf, why = eng._hw_upload_plan(Path("ffmpeg"), VAAPI)
    assert why == ""
    assert pre == ["-init_hw_device", f"vaapi=va:{good}",
                   "-filter_hw_device", "va"]
    assert vf == "format=nv12,hwupload"


def test_plan_reports_honestly_when_no_node_runs_it(tmp_path, monkeypatch):
    eng = object.__new__(VideoEngine)
    monkeypatch.setattr(vid, "render_nodes",
                        lambda: [tmp_path / "renderD128", tmp_path / "renderD129"])
    monkeypatch.setattr(vid, "run_command", lambda cmd, **kw: _Res(ok=False))
    pre, vf, why = eng._hw_upload_plan(Path("ffmpeg"), VAAPI)
    assert pre == [] and vf == ""
    assert "no render node" in why and "Re-benchmark" in why


def test_plan_tries_every_node_before_refusing(tmp_path, monkeypatch):
    eng = object.__new__(VideoEngine)
    nodes = [tmp_path / f"renderD{n}" for n in (128, 129, 130)]
    monkeypatch.setattr(vid, "render_nodes", lambda: nodes)
    tried: list[str] = []

    def fake_run(cmd, **kw):
        tried.append(str(cmd[cmd.index("-init_hw_device") + 1]))
        return _Res(ok=False)

    monkeypatch.setattr(vid, "run_command", fake_run)
    eng._hw_upload_plan(Path("ffmpeg"), VAAPI)
    assert tried == [f"vaapi=va:{n}" for n in nodes]


_HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs ffmpeg")
def test_vaapi_conversion_end_to_end_on_a_capable_machine(tmp_path):
    """Real encode on real hardware (2026-11 user machine: the probe verified
    h264_vaapi at 192 fps and the engine could not run it). Skips where GPU
    probing is off or VAAPI isn't verified - then it is NOT a pass, just
    silence; run this file WITHOUT TRANSMUTE_DISABLE_GPU to actually exercise
    the hardware path."""
    from transmute.probe.capability import CapabilityProber
    # gate on the PROBE's own facts, not os.environ: a GUI widget test pops
    # the env var mid-suite by design (app.py syncs it to the user's pick),
    # so only the report is a trustworthy statement about this launch
    cap = next((c for c in CapabilityProber().report().encoders
                if c.backend == "va-api" and c.verified), None)
    if cap is None:
        pytest.skip("no verified VAAPI encoder on this machine")
    src = tmp_path / "in.mp4"
    subprocess.run([  # a tiny real clip: too small for the >=144px trap
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    eng = VideoEngine()
    res = eng.convert(src, tmp_path / "out.mp4", codec="h264",
                      encoder=cap.name)
    assert res.ok, res.reason
    assert res.encoder == cap.name
    got = eng.probe.primary_video(res.output)
    assert (got or {}).get("codec_name") == "h264"
