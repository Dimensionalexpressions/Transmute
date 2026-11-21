"""GPU decode offload (ladder rung 1, 2026-11): the engine may add
-hwaccel ONLY where an executed trial proved this machine's GPU really
decodes H.264. This machine's ffmpeg (Lavc63) names the decoder
"h264 (native)" even while NVDEC does the work - the stream line LIES -
so the trial is believed only when the DECODED FRAME FORMAT changes from
the plain-CPU baseline (yuv420p -> nv12). A driver that declines keeps
the baseline format and gets reported honestly as "no GPU decode", and
the engine then decodes on CPU exactly like before this feature.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import transmute.engines.video as vid
import transmute.probe.capability as cap
from transmute.engines.video import VideoEngine, _hw_decode_pre
from transmute.probe.capability import (EncoderCapability, _decoded_pix_fmt,
                                        _probe_gpu_decode)
from transmute.engines import engine_for

CPUX = EncoderCapability(name="libx264", codec="h264", backend="cpu",
                         hw=False, verified=True, fps=60.0)
VAAPI = EncoderCapability(name="h264_vaapi", codec="h264", backend="va-api",
                          hw=True, verified=True, fps=200.0)

GPU_OK = {"attempted": True, "available": True, "api": "vaapi", "reason": ""}


# ------------------------------------------------------------------ engine rule
def test_flag_only_for_cpu_encoders_without_tonemap(monkeypatch):
    # the rule itself, GPU on: environment is irrelevant here
    monkeypatch.setattr(vid, "gpu_force_disabled", lambda: False)
    assert _hw_decode_pre(GPU_OK, CPUX, False) == ["-hwaccel", "vaapi"]
    # a GPU encoder drives its own decoder through the driver: adding
    # -hwaccel there re-routes frames through extra copies
    assert _hw_decode_pre(GPU_OK, VAAPI, False) == []
    assert _hw_decode_pre(GPU_OK, None, False) == []
    # the tone-map chain must receive plain sw frames (verified CPU path)
    assert _hw_decode_pre(GPU_OK, CPUX, True) == []


def test_silence_is_no_verdict_not_gpu():
    assert _hw_decode_pre({}, CPUX, False) == []
    assert _hw_decode_pre({"available": False, "api": "cuda"}, CPUX, False) == []
    assert _hw_decode_pre({"available": True, "api": ""}, CPUX, False) == []
    assert _hw_decode_pre(None, CPUX, False) == []


def test_gpu_off_lane_stays_cpu(monkeypatch):
    monkeypatch.setattr(vid, "gpu_force_disabled", lambda: True)
    assert _hw_decode_pre(GPU_OK, CPUX, False) == []


# ------------------------------------------------------------------ proof parser
def test_pixfmt_read_from_output_stream_line():
    cpu = "Stream #0:0(und): Video: wrapped_avframe, yuv420p(progressive), 640x480"
    gpu = "Stream #0:0(und): Video: wrapped_avframe, nv12(progressive), 640x480"
    assert _decoded_pix_fmt(cpu) == "yuv420p"
    assert _decoded_pix_fmt(gpu) == "nv12"
    assert _decoded_pix_fmt("no stream line here") == ""


# ------------------------------------------------------------------ trial logic
class _Res:
    def __init__(self, ok, stderr="", rc=0):
        self.ok, self.stderr_text, self.returncode = ok, stderr, rc
        self.timed_out, self.seconds = False, 0.5


BASE = "  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))\n" \
       "  Stream #0:0(und): Video: wrapped_avframe, yuv420p(progressive), 640x480"
GPUF = BASE.replace("yuv420p", "nv12")


def _fake_ffmpeg(monkeypatch, tmp_path, trial_fmt):
    """Fake run_command: the sample encode writes a file; the baseline says
    yuv420p; the -hwaccel trials answer with trial_fmt (str or dict per api)."""
    calls = []

    def fake(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if "-preset" in argv:  # sample encode
            Path(argv[-1]).write_bytes(b"mp4")
            return _Res(True)
        api = (argv[argv.index("-hwaccel") + 1] if "-hwaccel" in argv
               else "")
        fmt = trial_fmt.get(api, "yuv420p") if isinstance(trial_fmt, dict) \
            else trial_fmt
        return _Res(True, BASE.replace("yuv420p", fmt))

    monkeypatch.setattr(cap, "run_command", fake)
    monkeypatch.setattr(cap, "_has_nvidia_nodes", lambda: True)
    monkeypatch.setattr(cap, "_has_dri_render_node", lambda: True)
    monkeypatch.setattr(cap.tempfile, "mkdtemp",
                        lambda **k: str(tmp_path / "dec"))
    (tmp_path / "dec").mkdir()
    return calls


def test_trial_believed_only_when_frames_leave_the_cpu(tmp_path, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, tmp_path, {"cuda": "nv12"})
    r = _probe_gpu_decode(Path("ffmpeg"))
    assert r["available"] is True and r["api"] == "cuda"
    for c in calls:
        if "-hwaccel" in c:
            # the flag must sit BEFORE -i or it parses as an output option
            assert c.index("-hwaccel") < c.index("-i")


def _allowed_decode_apis(exclude: str = "") -> set[str]:
    """Which device APIs THIS OS may trial - derived from production, never
    hardcoded per file (Windows CI ran these very trials' Linux list and
    failed green-locally-red-on-the-VM: 2026-11 windows-latest job)."""
    return {api for api, node in cap._DECODE_APIS
            if api != exclude and cap._decode_api_allowed(node)}


def test_driver_decline_reported_never_claimed(tmp_path, monkeypatch):
    # every allowed API accepted the flag, none produced GPU frames: honest NO
    calls = _fake_ffmpeg(monkeypatch, tmp_path, "yuv420p")
    r = _probe_gpu_decode(Path("ffmpeg"))
    assert r["attempted"] is True and r["available"] is False and r["api"] == ""
    assert "decoded to CPU frames" in r["reason"]
    # every device API this OS allows gets its own trial
    assert {c[c.index("-hwaccel") + 1] for c in calls if "-hwaccel" in c} \
        == _allowed_decode_apis()


def test_nvdec_declines_vaapi_takes_the_job(tmp_path, monkeypatch):
    # NVIDIA declines: another GPU API this OS allows (VA-API on Linux,
    # D3D11VA on Windows) takes the decode job
    winner = sorted(_allowed_decode_apis(exclude="cuda"))
    assert winner, "this platform must have a non-NVIDIA decode rung"
    _fake_ffmpeg(monkeypatch, tmp_path, {winner[0]: "nv12"})
    r = _probe_gpu_decode(Path("ffmpeg"))
    assert r["available"] is True and r["api"] == winner[0]


# ------------------------------------------------------------------ schema
def test_old_cache_dict_without_decode_field_still_loads():
    rep = cap.CapabilityReport(
        schema=cap.SCHEMA_VERSION, probe_key="k", generated_at=0.0,
        app_version="x", machine={}, engines={}, encoders=[], filters={},
        gpu_nodes={"dri_render": [], "nvidia": []}, image_backends={},
        pdf_backends={}, caveats=[], matrix=[],
        gpu_decode={"attempted": True, "available": True, "api": "vaapi",
                    "reason": ""})
    d = rep.to_dict()
    assert d["gpu_decode"]["api"] == "vaapi"
    assert cap.CapabilityReport.from_dict(d).gpu_decode == rep.gpu_decode
    legacy = dict(d)
    legacy.pop("gpu_decode")
    assert cap.CapabilityReport.from_dict(legacy).gpu_decode == {}


# ------------------------------------------------------------------ real wiring
@pytest.mark.e2e
def test_engine_offloads_decode_only_where_proven(tmp_path, fixtures,
                                                  monkeypatch):
    """End-to-end on a machine that HAS GPU decode: the real job's ffmpeg
    command carries -hwaccel <api> before -i (argv proof) and the encode
    still verifies - the worst case of the honest flag is today's CPU path."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    if vid.gpu_force_disabled():
        pytest.skip("GPU held out of this lane (TRANSMUTE_DISABLE_GPU)")
    verdict = _probe_gpu_decode(Path(ffmpeg))
    if not verdict["available"]:
        pytest.skip(f"this machine declined GPU decode: {verdict['reason']}")
    eng = engine_for("video")
    if eng.availability().ok is False:
        pytest.skip("video engine unavailable")
    seen = []
    real = vid.run_command

    def spy(argv, **kw):
        seen.append([str(a) for a in argv])
        return real(argv, **kw)

    monkeypatch.setattr(vid, "run_command", spy)
    monkeypatch.setattr(VideoEngine, "_hw_upload_plan",
                        lambda self, f, c, cancel=None: ([], "", ""))
    dst = tmp_path / "out.mp4"
    # explicit crf: never the F20 "already optimal" skip
    res = eng.convert(fixtures["mp4"], dst, codec="h264", encoder="libx264",
                      crf=28)
    assert res.outcome == "converted", res.reason
    main = [c for c in seen if "-map" in c and "-i" in c]
    assert main, seen
    assert "-hwaccel" in main[0][:main[0].index("-i")]
    assert main[0][main[0].index("-hwaccel") + 1] == verdict["api"]


# --- v0.2.5: the Windows decode rung (live-fire: Ryzen AI 9 HX 370 / ---
# --- Radeon 880M said "no GPU decode device nodes here" - POSIX-only list)

def test_windows_decode_apis_are_on_the_ladder():
    from transmute.probe import capability as cap
    apis = [a for a, _ in cap._DECODE_APIS]
    assert "d3d11va" in apis and "dxva2" in apis


def test_decode_gate_follows_the_os_device_model(monkeypatch):
    import sys as _sys
    from transmute.probe import capability as cap
    monkeypatch.setattr(_sys, "platform", "win32")
    assert cap._decode_api_allowed("win")            # D3D11: no nodes needed
    assert cap._decode_api_allowed("nvidia")         # NVDEC exists sans /dev
    assert not cap._decode_api_allowed("dri_render")  # no vaapi on Windows
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(cap, "_has_dri_render_node", lambda: False)
    monkeypatch.setattr(cap, "_has_nvidia_nodes", lambda: False)
    assert not cap._decode_api_allowed("dri_render")
    assert not cap._decode_api_allowed("nvidia")


def test_hwaccel_route_is_the_windows_decode_evidence():
    from transmute.probe import capability as cap
    win_gpu = ("Stream #0:0: Video: h264 (native) (d3d11va -> d3d11), "
               "yuv420p(progressive), 640x480, 25 fps")
    cpu = ("Stream #0:0: Video: h264 (native), yuv420p(progressive), "
           "640x480, 25 fps")
    # -f null DOWNLOADS frames: output fmt looks CPU-equal either way,
    # only the route proves GPU work happened.
    assert cap._decoded_pix_fmt(win_gpu) == cap._decoded_pix_fmt(cpu)
    assert cap._hwaccel_route(win_gpu) == "d3d11"
    assert cap._hwaccel_route(cpu) == ""


def test_every_vendor_has_a_decode_rung_on_each_os(monkeypatch):
    """The support promise: NVIDIA (NVDEC), AMD and Intel (D3D11VA/DXVA2
    on Windows, VA-API on Linux) all get an executed decode trial on both
    OSes - and CUDA is never assumed, it must win its trial to be used."""
    import sys as _sys
    from transmute.probe import capability as cap
    by_node = {a: node for a, node in cap._DECODE_APIS}
    assert by_node.get("cuda") == "nvidia"
    monkeypatch.setattr(_sys, "platform", "win32")
    win_allowed = {a for a, n in cap._DECODE_APIS if cap._decode_api_allowed(n)}
    assert {"cuda", "d3d11va", "dxva2"} <= win_allowed   # NV + AMD + Intel
    assert "vaapi" not in win_allowed
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(cap, "_has_dri_render_node", lambda: True)
    monkeypatch.setattr(cap, "_has_nvidia_nodes", lambda: True)
    nix_allowed = {a for a, n in cap._DECODE_APIS if cap._decode_api_allowed(n)}
    assert {"cuda", "vaapi"} <= nix_allowed              # NV + AMD/Intel
    assert "d3d11va" not in nix_allowed


def test_every_vendor_encodes_on_windows_too():
    # Windows must not skip hardware encoder trials (device gate is
    # POSIX-only) and every vendor's encoders must classify as hardware.
    from transmute.probe import capability as cap
    for enc in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "av1_qsv",
                "h264_amf", "hevc_amf", "h264_vaapi", "hevc_vaapi"):
        c = cap._classify(enc, None)
        assert c["hw"] and c["backend"] in cap._GPU_BACKENDS, enc
    src = (Path(__file__).resolve().parents[1] / "src" / "transmute"
           / "probe" / "capability.py").read_text(encoding="utf-8")
    assert "sys.platform != \"win32\" and not hw_possible" in src
