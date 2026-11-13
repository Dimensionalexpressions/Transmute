"""Probe: golden parser samples + ladder/matrix semantics, no ffmpeg required.

These lock the exact bugs we hit building P1:
- the 'ffmpeg -encoders' separator is ' ------' WITH a leading space
- legend rows (' V..... = Video') sit BEFORE the table and must not parse
- hw encoders classify by name suffix; TRANSMUTE_DISABLE_GPU hides hw rows
"""
from __future__ import annotations

import time

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
