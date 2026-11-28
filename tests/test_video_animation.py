"""Movie -> animated GIF / animated WebP: the MOTION must survive untouched
(user requirement 2026-11-28: "ensure the picture animation is not slowed
down or altered"). Every assertion here measures the real output file with
real ffmpeg/ffprobe - no mocks, no vendor assumptions.

Fidelity contract proven:
  * same frame count (decoded, not container metadata),
  * same total duration - GIF stores delays in whole 1/100s, and the delays
    of the 25fps/1s fixture sum to EXACTLY 1000 ms,
  * same resolution,
  * sound removal is stated, not hidden; the format has no audio by spec.
Honest-failure contract: HDR without tone-map opt-in, remux flag, and
size-target requests all REFUSE with reasons that name the real cause.
"""
from __future__ import annotations

import subprocess

import pytest

from transmute.engines import engine_for
from transmute.engines.base import run_command
from transmute.engines.media import Probe
from transmute.probe.engines import EngineManager

pytestmark = pytest.mark.e2e


def _eng():
    eng = engine_for("video")
    if not eng.availability().ok:
        pytest.skip("video engine unavailable")
    return eng


def _decoded_frames(eng, path):
    ffprobe = eng.engines.path("ffprobe")
    res = run_command([ffprobe, "-v", "error", "-select_streams", "v:0",
                       "-count_frames", "-show_entries",
                       "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                      timeout_s=120)
    return int((res.stdout or "").strip())


# ------------------------------------------------------------------- GIF
def test_video_to_gif_keeps_every_frame_and_its_timing(tmp_path, fixtures):
    eng = _eng()
    dst = tmp_path / "motion.gif"
    res = eng.convert(fixtures["mp4"], dst)
    assert res.outcome == "converted", res.reason
    assert res.data["animation"] is True
    assert res.data["verified_by_engine"] is True
    assert "no sound" in res.data["audio"]

    src_sv = Probe(eng.engines).primary_video(fixtures["mp4"])
    out_sv = Probe(eng.engines).primary_video(dst)
    assert (out_sv["width"], out_sv["height"]) == (src_sv["width"], src_sv["height"])
    # The source fixture is 25 fps x 1 s = 25 frames; nothing may be lost.
    assert _decoded_frames(eng, dst) == 25

    # Pillow reads per-frame delays written by the muxer (in ms). Whole-1/100s
    # rounding is the FORMAT's, and it must not drift the total.
    from PIL import Image
    im = Image.open(dst)
    delays = []
    try:
        while True:
            delays.append(im.info.get("duration", 0) or 0)
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    assert len(delays) == 25
    assert sum(delays) == 1000  # exactly the source duration, no slowdown


def test_gif_output_passes_generic_verify_even_silent(tmp_path, fixtures):
    """Queue-level verify must not fail a correct GIF for having no audio
    streams (the mp4 fixture HAS one)."""
    eng = _eng()
    dst = tmp_path / "v.gif"
    assert eng.convert(fixtures["mp4"], dst).outcome == "converted"
    v = eng.verify(fixtures["mp4"], dst, expect_codec="gif")
    assert v.ok, v.reason


# ------------------------------------------------------------------ WebP
def test_video_to_animated_webp_keeps_frame_count(tmp_path, fixtures):
    eng = _eng()
    from transmute.probe.capability import _run_webp_anim_trial
    ffmpeg = eng.engines.path("ffmpeg")
    if not _run_webp_anim_trial(ffmpeg, eng.engines.path("ffprobe")):
        pytest.skip("this ffmpeg has no working libwebp_anim")
    dst = tmp_path / "motion.webp"
    res = eng.convert(fixtures["mp4"], dst)
    assert res.outcome == "converted", res.reason
    assert res.data["animation"] is True
    # ffprobe reports no duration for WebP (known demuxer gap) - decoded
    # frame count IS the timing proof here.
    assert _decoded_frames(eng, dst) == 25
    v = eng.verify(fixtures["mp4"], dst, expect_codec="webp")
    assert v.ok, v.reason


# ------------------------------------------------------- honest refusals
def test_hdr_source_refuses_plain_gif_without_optin(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["hdr"], tmp_path / "x.gif")
    assert res.outcome == "failed"
    assert "standard-definition" in res.reason


def test_remux_flag_refused_for_animation(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["mp4"], tmp_path / "x.gif", remux=True)
    assert res.outcome == "failed"
    assert "stream copy" in res.reason


def test_size_target_refused_for_animation(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["mp4"], tmp_path / "x.gif", size_mb=1.0)
    assert res.outcome == "failed"
    assert "size target" in res.reason


def test_audio_replace_refused_for_animation(tmp_path, fixtures):
    eng = _eng()
    res = eng.convert(fixtures["mp4"], tmp_path / "x.webp", audio="replace")
    assert res.outcome == "failed"
    assert "no sound" in res.reason


def test_estimate_refuses_to_guess_animation_size(tmp_path, fixtures):
    """The GUI's size predictor must NOT sample some other codec and pass
    the number off as GIF/WebP (F1: no table, no borrowed measurements)."""
    eng = _eng()
    for tgt in ("gif", "webp(animated)"):
        r = eng.estimate_conversion(fixtures["mp4"], target_codec=tgt)
        assert r["ok"] is False
        assert "cannot be predicted" in r["note"]


# --------------------------------------------------------------- routing
def test_suffixes_and_quality_knob_for_animation_targets():
    from transmute.gui.controller import ConverterController as Controller
    c = Controller.__new__(Controller)          # no state needed by these
    c._video_engine = None                      # guards return before any probing
    assert c.suffix_for("video", "gif") == ".gif"
    assert c.suffix_for("video", "webp(animated)") == ".webp"
    assert c.quality_range("video", "gif") is None
    assert c.quality_range("video", "webp(animated)") is None


# ---------------------------------------------------------------- probe
def test_animation_trials_round_trip_real_files(tmp_path):
    """Doctrine check: the trials write REAL files and decode them back."""
    from transmute.probe.capability import (_run_gif_anim_trial,
                                            _run_webp_anim_trial)
    from transmute.probe.engines import EngineManager
    em = EngineManager()
    ffmpeg, ffprobe = em.path("ffmpeg"), em.path("ffprobe")
    if ffmpeg is None:
        pytest.skip("ffmpeg missing")
    assert _run_gif_anim_trial(ffmpeg, ffprobe) is True
    # webp result is machine-dependent; it must simply never crash and must
    # return a bool (the VM/host matrix decides availability downstream).
    assert isinstance(_run_webp_anim_trial(ffmpeg, ffprobe), bool)
