"""Probe: golden parser samples + ladder/matrix semantics, no ffmpeg required.

These lock the exact bugs we hit building P1:
- the 'ffmpeg -encoders' separator is ' ------' WITH a leading space
- legend rows (' V..... = Video') sit BEFORE the table and must not parse
- hw encoders classify by name suffix; TRANSMUTE_DISABLE_GPU hides hw rows
"""
from __future__ import annotations

import time
from pathlib import Path as _P

import pytest

from transmute.probe.capability import (
    CapabilityReport,
    EncoderCapability,
    MatrixRow,
    _classify,
    _parse_encoder_list,
    gpu_force_disabled,
)

GOLDEN = """
FFmpeg version 9.0.2
Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .B.... = Modified-data
 ..M... = Attached-picture
 -----
 V....D libx264                 libx264 H.264 / AVC (codec h264)
 V....D libsvtav1               libdav1d AV1 (codec av1)
 V....D av1_nvenc               NVIDIA NVENC av1 encoder (codec av1)
 V....D h264_vaapi              VAAPI H.264 encode (codec h264)
 A....D libopus                 libopus Opus (codec opus)
 A....D flac                    FLAC (codec flac)
"""


def test_parse_encoder_list_golden():
    rows = _parse_encoder_list(GOLDEN)
    names = [n for n, _ in rows]
    assert names == ["libx264", "libsvtav1", "av1_nvenc", "h264_vaapi",
                     "libopus", "flac"]
    declared = dict(rows)
    assert declared["libx264"] == "h264"
    assert declared["libsvtav1"] == "av1"
    assert declared["flac"] == "flac"
    # legend rows and headers must never appear
    assert not any(n in ("=", "V.....", "Encoders:") for n in names)


def test_parse_encoder_list_no_separator_yields_nothing():
    # a header-only stdout (e.g. stderr leaked in) must not hallucinate rows
    assert _parse_encoder_list("Encoders:\n V..... = Video\n") == []


def test_classify_sw_vs_hw():
    assert _classify("libx264", "h264")["hw"] is False
    assert _classify("libsvtav1", "av1")["codec"] == "av1"
    assert _classify("av1_nvenc", "av1")["hw"] is True
    assert _classify("h264_vaapi", "h264")["hw"] is True
    assert _classify("hevc_amf", "hevc")["hw"] is True


def _cap(name, codec, fps, verified=True, hw=False):
    return EncoderCapability(name=name, codec=codec, backend="nvenc" if hw else "cpu",
                             hw=hw, verified=verified, fps=fps, reason="", skipped=None,
                             pix_fmts=[], depth={"8": True, "10": False},
                             compat_flag=codec in ("h264", "hevc"))


def _mkreport(encoders):
    return CapabilityReport(schema=1, probe_key="k", generated_at=time.time(),
                            app_version="0.0.0-test", machine={}, engines={},
                            encoders=encoders, filters={}, gpu_nodes={},
                            image_backends={}, pdf_backends={}, caveats=[],
                            matrix=[MatrixRow(category="video", target="av1", path="x",
                                              available=True, hw=False, verified=True,
                                              recommended=True, compat_flag=False,
                                              note="")])


def test_verified_ladder_sorted_fastest_first():
    rep = _mkreport([_cap("libaom-av1", "av1", 143.0),
                     _cap("libsvtav1", "av1", 529.0),
                     _cap("librav1e", "av1", 62.0),
                     _cap("broken", "av1", 999.0, verified=False)])
    order = [e.name for e in rep.verified_encoders("av1")]
    assert order == ["libsvtav1", "libaom-av1", "librav1e"]  # F10 measured order


def test_report_dict_roundtrip():
    rep = _mkreport([_cap("libsvtav1", "av1", 529.0)])
    back = CapabilityReport.from_dict(rep.to_dict())
    assert back.verified_encoders("av1")[0].fps == 529.0
    assert back.row("video", "av1") is not None


def test_gpu_disable_flag(monkeypatch):
    monkeypatch.delenv("TRANSMUTE_DISABLE_GPU", raising=False)
    assert gpu_force_disabled() is False
    monkeypatch.setenv("TRANSMUTE_DISABLE_GPU", "1")
    assert gpu_force_disabled() is True


class _FakeEM:
    """EngineManager stand-in: nothing installed, never raises."""
    def detect(self, refresh: bool = False):
        from transmute.probe.engines import EngineInfo, STATUS_MISSING
        from transmute.probe.engines import KNOWN_ENGINES
        return {n: EngineInfo(n, STATUS_MISSING, reason="not found") for n in KNOWN_ENGINES}

    def get(self, name):
        from transmute.probe.engines import EngineInfo, STATUS_MISSING
        return EngineInfo(name, STATUS_MISSING, reason="not found")

    def path(self, name):
        return None

    def is_available(self, name):
        return False


def test_capability_report_survives_missing_ffmpeg(tmp_path, monkeypatch):
    """No ffmpeg at all: report() must return empty-but-valid, with caveats."""
    import transmute.paths as paths_mod
    from transmute.paths import AppPaths
    from transmute.probe.capability import CapabilityProber
    monkeypatch.setattr(paths_mod, "_default", AppPaths(root=tmp_path / "caproot").ensure())
    rep = CapabilityProber(engines=_FakeEM()).report(refresh=True)  # type: ignore[arg-type]
    assert rep.encoders == []
    assert any("ffmpeg" in c.lower() for c in rep.caveats)
    assert all(r.available is False for r in rep.matrix if r.category in ("video", "audio"))
    # matrix must still enumerate every target so the GUI can grey things out
    assert {r.category for r in rep.matrix} >= {"video", "audio", "image", "pdf", "document"}


def test_probe_pillow_is_measured_not_assumed():
    """Pillow 13 removed Image.formats - the probe must measure real encodes.

    Regression guard: an earlier probe keyed off a registry that no longer
    exists and reported png/gif/jpg as unsupported while conversion worked.
    """
    import io

    from PIL import Image

    from transmute.probe.capability import _probe_pillow
    info = _probe_pillow()
    assert info.get("version")
    feats = info["features"]
    assert set(feats) >= {"png", "gif", "jpg", "webp", "avif", "webp_anim"}
    assert all(isinstance(v, bool) for v in feats.values())
    # built-in plugins must report True here, matching a direct encode
    assert feats["jpg"] is True and feats["png"] is True and feats["gif"] is True
    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, format="JPEG")
    assert (buf.tell() > 0) == feats["jpg"]


FFMPEG9_FILTER_LIST = """Filters:
  Supported filters:
  The following placeholders are supported ...
 ... aresample           A->A       Resample audio data.
 .S tonemap           V->V       Conversion to/from different dynamic ranges.
 .. tonemap_opencl    V->V       Perform HDR to SDR conversion with tonemapping.
 .S zscale            V->V       Apply resizing, colorspace and bit depth conversion.
"""


def test_filter_probe_handles_narrow_flag_columns_and_trials_chain(tmp_path, monkeypatch):
    """Regression (P3): ffmpeg 9 prints a 2-char flag column; a fixed 6-char
    regex silently reported zscale/tonemap missing on a libzimg build.
    Listing is necessary but NOT sufficient: the chain must trial-run."""
    from pathlib import Path
    from transmute.probe import capability as cap_mod

    class FakeRes:
        stdout = FFMPEG9_FILTER_LIST

    monkeypatch.setattr(cap_mod, "run_command", lambda *a, **k: FakeRes())
    calls: list = []
    monkeypatch.setattr(cap_mod, "_run_tonemap_trial",
                        lambda f: calls.append(f) or True)
    got = cap_mod._probe_ffmpeg_filters(Path("/fake/ffmpeg"))
    assert got == {"zscale": True, "tonemap": True, "tonemap_chain": True}
    assert calls == [Path("/fake/ffmpeg")]
    # listed but chain trial fails (e.g. libzimg-less): must report False
    monkeypatch.setattr(cap_mod, "_run_tonemap_trial", lambda f: False)
    assert cap_mod._probe_ffmpeg_filters(Path("/fake/ffmpeg"))["tonemap_chain"] is False


# ------------------------------------------- RGB-only encoder gate (P3 bug)

def test_rgb_only_is_measured_from_pix_fmts_not_names():
    from transmute.probe.capability import rgb_only
    # libx264rgb shape: ffmpeg lists ONLY RGB formats for it -> True
    assert rgb_only(["gbrp", "gbrp10le", "bgr0", "rgb24"]) is True
    # libx264 shape: YUV formats present -> False even if RGB formats ride along
    assert rgb_only(["yuv420p", "gbrp", "yuv420p10le"]) is False
    # empty/unknown list: claim nothing (F3: no invented facts)
    assert rgb_only([]) is False


class _Rep:
    """Duck-typed CapabilityReport: pick_encoder needs only these members."""

    def __init__(self, caps):
        self.encoders = caps

    def verified_encoders(self, codec):  # same semantics as the real report
        got = [e for e in self.encoders if e.verified and e.codec == codec]
        return sorted(got, key=lambda e: -(e.fps or 0))


def _h264(name, fps, rgb):
    from transmute.probe.capability import EncoderCapability
    return EncoderCapability(name=name, codec="h264", backend="cpu", hw=False,
                             verified=True, fps=fps, rgb_output=rgb)


def test_rgb_only_encoder_never_wins_auto_pick():
    """The real P3 bug: auto-pick chose the verified-FASTEST libx264rgb and
    produced RGB-space h264 that hardware players choke on. The disqualifier
    is the measured rgb_output flag, NOT the ℹ️ compat flag (libx264 is
    compat too and must still win). Explicit user choice is honoured via a
    direct lookup in VideoEngine.convert - pick_encoder only serves auto."""
    from types import SimpleNamespace
    from transmute.engines.video import VideoEngine

    eng = object.__new__(VideoEngine)
    rgb_fast, yuv_slower = _h264("libx264rgb", 1000.0, True), _h264("libx264", 800.0, False)
    eng.prober = SimpleNamespace(report=lambda: _Rep([rgb_fast, yuv_slower]))
    assert eng.pick_encoder("h264", want_10bit=False, prefer_hw=False).name == "libx264"
    # honest last resort: RGB-only is ALL this machine verified -> still offer it
    eng.prober = SimpleNamespace(report=lambda: _Rep([rgb_fast]))
    assert eng.pick_encoder("h264", want_10bit=False, prefer_hw=False).name == "libx264rgb"


def test_capability_cache_concurrent_save_no_race(tmp_path):
    """P5 soak regression: 8 threads saving the SAME probe key must never
    raise. The old shared '.json.tmp' name made the loser of os.replace hit
    FileNotFoundError (two video jobs lost the engine that way in the soak).
    """
    import threading
    from transmute.probe.capability import CapabilityCache, CapabilityReport

    rep = CapabilityReport(schema=1, probe_key="k", generated_at=0.0,
                           app_version="test", machine={}, engines={},
                           encoders=[], filters={}, gpu_nodes={},
                           image_backends={}, pdf_backends={}, caveats=[],
                           matrix=[])
    cache = CapabilityCache(tmp_path)
    errs: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(30):
                cache.save(rep)
        except BaseException as exc:  # noqa: BLE001 - the test collects these
            errs.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, f"concurrent cache save raised: {errs!r}"
    assert (tmp_path / "k.json").is_file()
    assert not list(tmp_path.glob("*.tmp")), "temp file leaked"


# -- audio encoder trials: REAL round-trip, never a null sink -----------------
# Live-fire Windows bug (VM hunt, 2026-11): MediaFoundation mp3_mf 'encodes'
# into -f null with exit 0 and then truncates real files to 0.60s of a 2.00s
# source, STILL exit 0. The trial must write an actual file and ask ffprobe
# how long it is, or the picker serves that liar to every conversion.

def _mp3_cap() -> EncoderCapability:
    return EncoderCapability(name="mp3_mf", codec="mp3", backend="cpu",
                             hw=False, verified=False, fps=None)


def _fake_run(monkeypatch, out_duration: float | None, empty: bool = False):
    """run_command double: ffmpeg trial writes a (fake) file, ffprobe reports it."""
    from pathlib import Path as _P
    from transmute.engines.base import CommandResult
    from transmute.probe import capability as C

    def fake(cmd, *a, **k):
        cmd = [str(x) for x in cmd]
        if "ffprobe" in cmd[0]:
            import json as _j
            out = _j.dumps({"format": {}} if out_duration is None
                           else {"format": {"duration": f"{out_duration}"}})
            return CommandResult(cmd, 0, out, "", None, 0.0)
        # ffmpeg encode: create the output file (last argument), unless sink
        if cmd[-1] != "-":
            _P(cmd[-1]).write_bytes(b"" if empty else b"\x00" * 4096)
        return CommandResult(cmd, 0, "", "", None, 0.1)

    monkeypatch.setattr(C, "run_command", fake)


def test_audio_trial_rejects_encoder_that_truncates(tmp_path, monkeypatch):
    from transmute.probe.capability import _run_trial_audio
    _fake_run(monkeypatch, 0.60)  # the exact mp3_mf failure mode
    ok, fps, reason = _run_trial_audio(_P("ffmpeg"), _mp3_cap(), _P("ffprobe"))
    assert not ok and "truncated" in reason


def test_audio_trial_rejects_undecodable_output(monkeypatch):
    from transmute.probe.capability import _run_trial_audio
    _fake_run(monkeypatch, None)
    ok, fps, reason = _run_trial_audio(_P("ffmpeg"), _mp3_cap(), _P("ffprobe"))
    assert not ok and "not decodable" in reason


def test_audio_trial_rejects_empty_output(monkeypatch):
    from transmute.probe.capability import _run_trial_audio
    _fake_run(monkeypatch, 2.0, empty=True)
    ok, fps, reason = _run_trial_audio(_P("ffmpeg"), _mp3_cap(), _P("ffprobe"))
    assert not ok and "empty" in reason


def test_audio_trial_accepts_honest_encoder(monkeypatch):
    from transmute.probe.capability import _run_trial_audio
    _fake_run(monkeypatch, 2.0)
    ok, fps, reason = _run_trial_audio(_P("ffmpeg"), _mp3_cap(), _P("ffprobe"))
    assert ok and not reason and fps


def test_audio_trial_falls_back_to_null_sink_without_ffprobe(monkeypatch):
    # no ffprobe available: never crash, degrade to the old sink behaviour
    from transmute.probe.capability import _run_trial_audio
    _fake_run(monkeypatch, 0.10)  # would truncate if the file path ran
    ok, fps, reason = _run_trial_audio(_P("ffmpeg"), _mp3_cap(), None)
    assert ok  # sink trial: exit 0 is all it can see (documented fallback)
