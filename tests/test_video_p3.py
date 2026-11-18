"""P3 gate tests on the REAL ffmpeg: every rung that touches media bytes.

- F2 remux: `-c copy` proven by hashing the demuxed elementary stream on both
  sides (and independently re-checked with the media helpers themselves).
- F15 tone-map: opt-in only; refusal without the probed chain; standard verify
  still rejects silent HDR->SDR; no tone-map must keep HDR+10-bit.
- audio policy: container-incompatible streams fail fast with the codec named;
  audio='replace' transcodes audio ONLY and names what it did.
- size target: exact 2-pass on x264/x265 (asserted near target), mutually
  exclusive with CRF, refuses impossible budgets.
"""
from __future__ import annotations

import pytest

from transmute.engines import engine_for
from transmute.engines.base import run_command
from transmute.engines.media import (Probe, extract_video_bitstream_sha256)
from transmute.engines.video import TWO_PASS_ENCODERS

pytestmark = pytest.mark.e2e


def _eng():
    eng = engine_for("video")
    if not eng.availability().ok:
        pytest.skip("video engine unavailable")
    return eng


# ------------------------------------------------------------------- remux (F2)
def test_remux_mp4_to_mkv_is_bitstream_identical(tmp_path, fixtures):
    eng = _eng()
    dst = tmp_path / "copy.mkv"
    res = eng.convert(fixtures["mp4"], dst, remux=True)
    assert res.outcome == "converted", res.reason
    assert res.data["remux"] is True
    assert res.data["remux_proof"] == "bitstream-hash"
    assert res.data["bitstream_identical"] is True
    assert "matroska" in Probe(eng.engines).format_info(dst)["format"]["format_name"]
    # independent re-check with the proof helpers themselves
    h_src = extract_video_bitstream_sha256(eng.engines, fixtures["mp4"], "h264")
    h_dst = extract_video_bitstream_sha256(eng.engines, dst, "h264")
    assert h_src and h_src == h_dst
    # source untouched
    assert "mp4" in Probe(eng.engines).format_info(
        fixtures["mp4"])["format"]["format_name"]


def test_remux_same_container_still_runs(tmp_path, fixtures):
    eng = _eng()
    dst = tmp_path / "again.mp4"
    res = eng.convert(fixtures["mp4"], dst, remux=True)
    # explicit user action: never "skipped"
    assert res.outcome == "converted", res.reason
    assert res.data["remux_proof"] == "bitstream-hash"


def test_remux_refuses_codec_container_mismatch_naming_codec(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["mp4"], tmp_path / "no.webm", remux=True)
    assert res.outcome == "failed"
    assert "h264" in res.reason and "never re-encodes" in res.reason
    assert not (tmp_path / "no.webm").exists()


def test_remux_never_reencodes_audio_request(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["mp4"], tmp_path / "x.mkv", remux=True,
                      audio="replace")
    assert res.outcome == "failed" and "remux never re-encodes audio" in res.reason


# ------------------------------------------------------------------ tone-map (F15)
def test_tonemap_refused_when_chain_not_probed(tmp_path, fixtures, monkeypatch):
    from types import SimpleNamespace
    eng = _eng()
    monkeypatch.setattr(eng.prober, "report",
                        lambda: SimpleNamespace(filters={}))
    res = eng.convert(fixtures["hdr"], tmp_path / "o.mp4", tonemap=True)
    assert res.outcome == "failed"
    assert "cannot tone-map" in res.reason
    assert not (tmp_path / "o.mp4").exists()


def test_no_tonemap_keeps_hdr_10bit(tmp_path, fixtures):
    eng = _eng()
    dst = tmp_path / "keep.mkv"
    res = eng.convert(fixtures["hdr"], dst)  # default mkv -> av1, want_10
    assert res.outcome in ("converted", "failed"), res.reason
    if res.outcome == "failed":
        # honest refusal is acceptable; silent downgrade is not
        assert "10-bit" in res.reason or "tone-map" in res.reason
        return
    v = eng.verify(fixtures["hdr"], dst)
    assert v.ok, v.reason
    assert Probe(eng.engines).is_hdr(dst) is True  # metadata survived


def test_tonemap_optin_produces_sdr_and_self_verifies(tmp_path, fixtures):
    eng = _eng()
    if not eng.prober.report().filters.get("tonemap_chain"):
        pytest.skip("this ffmpeg failed the tone-map chain trial")
    dst = tmp_path / "sdr.mp4"
    res = eng.convert(fixtures["hdr"], dst, codec="h264", tonemap=True)
    assert res.outcome == "converted", res.reason
    assert res.data["tonemapped"] is True
    assert res.data["verified_by_engine"] is True
    probe = Probe(eng.engines)
    assert probe.is_hdr(dst) is False
    sv = probe.primary_video(dst)
    assert (sv.get("pix_fmt") or "").startswith("yuv420p")
    # the STANDARD verify still rejects the intentional downgrade (F25):
    v = eng.verify(fixtures["hdr"], dst, allow_hdr_loss=False)
    assert not v.ok and "tone-map" in v.reason
    # ...and accepts it when the loss was explicitly opted into
    v2 = eng.verify(fixtures["hdr"], dst, allow_hdr_loss=True)
    assert v2.ok, v2.reason


# ------------------------------------------------------------------- audio policy
def test_audio_replace_mp4_from_opus_mkv(tmp_path, fixtures):
    eng = _eng()
    if eng.pick_encoder("opus", want_10bit=False, prefer_hw=None) is None \
            or eng.pick_encoder("aac", want_10bit=False, prefer_hw=None) is None:
        pytest.skip("no verified opus or aac encoder on this machine")
    src = tmp_path / "opus.mkv"
    ffmpeg = eng.engines.path("ffmpeg")
    mk = run_command([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                      "-f", "lavfi", "-i", "testsrc2=s=64x32:d=1:r=25",
                      "-f", "lavfi", "-i", "anullsrc=d=1",
                      "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                      "-c:a", "libopus", "-t", "1", "-y", src], timeout_s=120)
    if not mk.ok:
        pytest.skip("could not build opus-in-mkv fixture")
    # copy policy: fails fast, names the offending codec and the way out
    bad = tmp_path / "copy.mp4"
    res = eng.convert(src, bad, codec="h264")
    assert res.outcome == "failed"
    assert "opus" in res.reason and "replace" in res.reason
    assert not bad.exists()
    # replace policy: audio-only transcode; the h264 video rides along as a
    # pure stream copy (the engine must NOT re-encode matching video).
    dst = tmp_path / "replaced.mp4"
    res2 = eng.convert(src, dst, codec="h264", audio="replace")
    assert res2.outcome == "converted", res2.reason
    note = res2.data.get("audio_replaced")
    assert note and note["from"] == ["opus"] and note["to"] == "aac"
    assert res2.data.get("video_stream") == "copy"
    vcodecs = {(s.get("codec_name") or "").lower()
               for s in Probe(eng.engines).video_streams(dst)}
    assert vcodecs == {"h264"}
    acodecs = {(s.get("codec_name") or "").lower()
               for s in Probe(eng.engines).audio_streams(dst)}
    assert acodecs == {"aac"}
    assert eng.verify(src, dst).ok


# ------------------------------------------------------------------- estimates
def test_estimate_returns_measured_numbers(tmp_path, fixtures):
    """GUI shows sizes/time measured on THIS PC: the engine must return a
    dict of real measured numbers for a real sample, or ok=False honestly."""
    eng = _eng()
    if eng.pick_encoder("h264", want_10bit=False, prefer_hw=None) is None:
        pytest.skip("no verified x264 encoder on this machine")
    d = eng.estimate_conversion(fixtures["mp4"], target_codec="h264",
                                encoder="libx264", crf=23)
    assert d["ok"] is True, d
    assert d["est_s"] > 0 and d["bytes"] > 0
    assert d["size_is_target"] is False
    assert 0.3 <= d["sec"] <= 2.6
    # size-target mode: the claim IS the target, stated as such
    t = eng.estimate_conversion(fixtures["mp4"], target_codec="h264",
                                encoder="libx264", size_mb=3.0)
    assert t["ok"] and abs(t["bytes"] - 3.0e6) < 1
    assert t["size_is_target"] is True


def test_estimate_stop_event_claims_nothing_and_dies_fast(tmp_path, fixtures):
    """Workstation-lockup fix contract: the GUI supersedes measurements as
    selectors move; a stopped/replaced estimate must return promptly and
    claim NOTHING (ok False, empty note) - never a stale half-measure."""
    import threading
    import time as _t
    eng = _eng()
    ev = threading.Event()
    ev.set()
    t0 = _t.monotonic()
    d = eng.estimate_conversion(fixtures["mp4"], target_codec="h264",
                                encoder="libx264", crf=23, stop=ev)
    assert _t.monotonic() - t0 < 2.0
    assert d["ok"] is False and d["note"] == ""


def test_preset_and_audio_bitrate_end_up_in_the_file(tmp_path, fixtures):
    """Advanced fine-tuning must change the OUTPUT, not just the widgets:
    a chosen audio bitrate lands in the file bytes even when the container
    already accepts the original audio (forced re-encode), nonsense presets
    are rejected at the door, and CPU presets are accepted."""
    eng = _eng()
    if eng.pick_encoder("h264", want_10bit=False, prefer_hw=None) is None \
            or eng.pick_encoder("aac", want_10bit=False, prefer_hw=None) is None:
        pytest.skip("no verified x264 or aac encoder on this machine")
    # 3 s mp4 that ALREADY has compatible aac audio: copy would be allowed -
    # the explicit request must still re-encode at OUR bitrate
    src = tmp_path / "src3.mp4"
    ffmpeg = eng.engines.path("ffmpeg")
    mk = run_command([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                      "-f", "lavfi", "-i", "testsrc2=s=64x32:d=3:r=25",
                      # real signal, not anullsrc silence: silent AAC encodes
                      # to ~2k whatever the nominal bitrate is - noise proves
                      # the chosen 48k actually lands in the file
                      "-f", "lavfi", "-i", "anoisesrc=r=48000:d=3:a=0.4",
                      "-c:v", "libx264", "-preset", "veryfast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-t", "3", "-y", src], timeout_s=120)
    if not mk.ok:
        pytest.skip("could not build 3 s aac-in-mp4 fixture")
    dst = tmp_path / "fine.mp4"
    res = eng.convert(src, dst, codec="h264", encoder="libx264",
                      crf=28, preset="ultrafast", audio="replace",
                      audio_bitrate="48k")
    assert res.outcome == "converted", res.reason
    note = res.data.get("audio_replaced") or {}
    assert note.get("bitrate") == "48k" and note.get("to") == "aac"
    acodecs = {(s.get("codec_name") or "").lower()
               for s in Probe(eng.engines).audio_streams(dst)}
    assert acodecs == {"aac"}
    br = int(Probe(eng.engines).audio_streams(dst)[0].get("bit_rate") or 0)
    assert abs(br - 48000) <= 10000, br  # it is REALLY ~48k in the file
    assert eng.verify(src, dst).ok
    # nonsense preset rejected at the door, not deep inside ffmpeg
    bad = eng.convert(src, tmp_path / "x.mp4", codec="h264",
                      encoder="libx264", crf=28, preset="ludicrous")
    assert bad.outcome == "failed" and "preset" in bad.reason


# -------------------------------------------------------------------- size target
def test_size_target_rejects_conflict_and_impossible(tmp_path, fixtures):
    eng = _eng()
    r1 = eng.convert(fixtures["mp4"], tmp_path / "a.mp4", crf=20, size_mb=1.0)
    assert r1.outcome == "failed" and "not both" in r1.reason
    r2 = eng.convert(fixtures["mp4"], tmp_path / "b.mp4", size_mb=0.005)
    assert r2.outcome == "failed" and "too small" in r2.reason


def test_size_target_two_pass_lands_near_target(tmp_path):
    eng = _eng()
    cap = eng.pick_encoder("h264", want_10bit=False, prefer_hw=None)
    if cap is None or cap.name not in TWO_PASS_ENCODERS:
        pytest.skip("fastest verified h264 encoder is not 2-pass capable")
    # x264's rate control needs real duration and entropy to be able to SPEND
    # a bitrate target - a 1s low-noise clip structurally undershoots any
    # plausible target. Build a 6s high-motion clip whose native bitrate is
    # well above the requested target (2-pass must then trim toward it).
    src = tmp_path / "noisy.mp4"
    ffmpeg = eng.engines.path("ffmpeg")
    mk = run_command([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                      "-f", "lavfi",
                      "-i", "testsrc2=s=640x360:d=6:r=25,noise=alls=20:allf=t",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                      "-pix_fmt", "yuv420p", "-an", "-y", src], timeout_s=180)
    if not mk.ok:
        pytest.skip("could not build noisy fixture clip")
    dst = tmp_path / "target.mp4"
    res = eng.convert(src, dst, codec="h264", size_mb=0.75)
    assert res.outcome == "converted", res.reason
    st = res.data.get("size_target")
    assert st and st["mode"] == "two-pass"
    # The engine must report the truth about what it achieved, and on content
    # this brutal it must apply the VBV ceiling correction once, keeping the
    # file within a bounded ceiling (measured: lands ~110% of target).
    actual = dst.stat().st_size
    assert st["achieved_bytes"] == actual
    assert isinstance(st.get("within_budget"), bool)
    actual_mb = actual / 1e6
    assert 0.75 * 0.5 <= actual_mb <= 0.75 * 1.30, actual_mb
    assert eng.verify(src, dst).ok
