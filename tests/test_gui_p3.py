"""P3 GUI-surface tests: the controller decides remux/tonemap/CRF-vs-size
and the first-frame preview. Unit tier: no Qt, no engines. e2e tier: real
ffprobe/ffmpeg only - still no display (Qt-free paths of the controller)."""
from __future__ import annotations

import time

import pytest

from transmute.gui.controller import ConverterController
from transmute.probe.capability import CapabilityReport

pytestmark = pytest.mark.unit


def _empty_report() -> CapabilityReport:
    return CapabilityReport(schema=1, probe_key="k", generated_at=time.time(),
                            app_version="t", machine={}, engines={},
                            encoders=[], filters={}, gpu_nodes={},
                            image_backends={}, pdf_backends={}, caveats=[],
                            matrix=[])


def _ctl(filters: dict | None = None) -> ConverterController:
    rep = _empty_report()
    rep.filters = filters or {}
    return ConverterController(report_provider=lambda: rep)


# ---------------------------------------------------------------- options_for
def test_remux_options_are_the_whole_story():
    # F2: remux is an operation, not a codec choice - nothing else rides along
    c = _ctl()
    opts = c.options_for("video", "h264", remux=True, encoder="libx264",
                         crf=20, tonemap=True, audio_replace=True)
    assert opts == {"remux": True}


def test_p3_video_flags_map_to_engine_kwargs():
    c = _ctl()
    assert c.options_for("video", "av1", tonemap=True) == {
        "codec": "av1", "tonemap": True}
    assert c.options_for("video", "av1", audio_replace=True) == {
        "codec": "av1", "audio": "replace"}
    assert c.options_for("video", "av1", encoder="libsvtav1", crf=26) == {
        "codec": "av1", "encoder": "libsvtav1", "crf": 26}
    assert c.options_for("video", "av1", size_mb=1.5) == {
        "codec": "av1", "size_mb": 1.5}


def test_fine_tuning_knobs_map_and_stay_out_when_unset():
    # Advanced panel: preset/audio bitrate ride to the engine ONLY when chosen
    c = _ctl()
    assert c.options_for(
        "video", "h264", audio_replace=True, audio_bitrate="96k",
        preset="veryfast") == {
        "codec": "h264", "audio": "replace", "audio_bitrate": "96k",
        "preset": "veryfast"}
    # and never leak into plain runs or other categories
    assert c.options_for("video", "h264") == {"codec": "h264"}
    assert c.options_for("image", "png", preset="slow") == {}


def test_crf_and_size_are_mutually_exclusive_at_the_gui_too():
    # the engine refuses both; the GUI must refuse before enqueuing (F1/F2)
    with pytest.raises(ValueError):
        _ctl().options_for("video", "av1", crf=20, size_mb=1.0)


def test_tonemap_available_reflects_probed_chain_only():
    # F15: capability comes from the executed trial in the report, never a guess
    assert _ctl({"tonemap_chain": True}).tonemap_available is True
    assert _ctl({"tonemap_chain": False}).tonemap_available is False
    assert _ctl({}).tonemap_available is False
    assert _ctl({"zscale": True}).tonemap_available is False


def test_dst_for_honors_container_override_for_remux():
    from pathlib import Path
    c = _ctl()
    # remux must keep the USER-PICKED container, not the codec-default one
    d = c.dst_for(Path("/x/clip.mp4"), "video", "copy", Path("/out"), ext=".mkv")
    assert d == Path("/out/clip.mkv")
    # without override the old suffix logic still rules
    assert c.dst_for(Path("/x/clip.mp4"), "video", "vp9", Path("/out")) == \
        Path("/out/clip.webm")


# ------------------------------------------------- engine-backed (real binaries)
@pytest.mark.e2e
def test_remux_block_reason_agrees_with_engine_tables(fixtures):
    if not fixtures.get("mp4"):
        pytest.skip("needs real ffmpeg to build the mp4 fixture")
    c = _ctl()
    # h264+aac rides losslessly into mkv and stays in mp4
    assert c.remux_block_reason(fixtures["mp4"], ".mkv") == ""
    assert c.remux_block_reason(fixtures["mp4"], ".mp4") == ""
    # h264 cannot be copied into webm: reason must name the codec and the way out
    why = c.remux_block_reason(fixtures["mp4"], ".webm")
    assert "h264" in why and "remux never re-encodes" in why
    # unknown container is refused with words, not a crash
    assert "container" in c.remux_block_reason(fixtures["mp4"], ".xyz")


@pytest.mark.e2e
def test_first_frame_returns_png_or_none(fixtures, tmp_path):
    if not fixtures.get("mp4"):
        pytest.skip("needs real ffmpeg to build the mp4 fixture")
    c = _ctl()
    png = c.first_frame(fixtures["mp4"])
    assert png is not None and png.is_file()
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"   # real PNG magic
    # repeated calls reuse one preview file (no per-click temp leak)
    png2 = c.first_frame(fixtures["mp4"])
    assert png2 == png
    # unmeasurable input -> None, never a made-up picture (F3)
    bogus = tmp_path / "notvideo.mp4"
    bogus.write_bytes(b"nope")
    assert c.first_frame(bogus) is None
