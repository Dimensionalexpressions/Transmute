"""Multi-audio-track doctrine: nothing is silently dropped.

A commentary/dual-language file has several audio streams. mp3/m4a/wav
hold exactly ONE - the engine must refuse with the machine's own count
and the alternative (.mka), never quietly keep track 1. Containers that
do hold several (.mka/.ogg) must map them ALL, and verify compares the
track count so a run that lost a track fails visibly.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from transmute.engines import audio as A

pytestmark = pytest.mark.unit

TRACK = {"flac": {"codec_name": "flac"}, "aac": {"codec_name": "aac"}}


class StubProbe:
    def __init__(self, src_tracks, out_tracks=None):
        self.src_tracks = src_tracks
        self.out_tracks = src_tracks if out_tracks is None else out_tracks
        self.available = True

    def audio_streams(self, p):
        return [dict(TRACK[c]) for c in
                (self.src_tracks if Path(p).name.startswith("in")
                 else self.out_tracks)]

    def video_streams(self, p):
        return []

    def duration_s(self, p):
        return 10.0


def _engine(monkeypatch, src_tracks, out_tracks=None, write_output=True):
    eng = A.AudioEngine.__new__(A.AudioEngine)
    eng.probe = StubProbe(src_tracks, out_tracks)
    eng.engines = SimpleNamespace(path=lambda n: "/usr/bin/" + n)
    eng.availability = lambda: SimpleNamespace(ok=True, reason=None, guidance=None)
    eng.pick_encoder = lambda t, tone_map=False: SimpleNamespace(name=t)
    calls: dict = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = [str(c) for c in cmd]
        if write_output:
            Path(cmd[-1]).write_bytes(b"data")
        return SimpleNamespace(ok=True, timed_out=False, returncode=0,
                               stderr_text="", stdout_text="")

    monkeypatch.setattr(A, "run_command", fake_run)
    return eng, calls


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "in.mka"
    p.write_bytes(b"source")
    return p


def test_two_tracks_into_mp3_refuses_with_counts_and_alternative(tmp_path, src,
                                                                 monkeypatch):
    eng, calls = _engine(monkeypatch, ["aac", "aac"])
    res = eng.convert(src, tmp_path / "out.mp3")
    assert res.outcome == "failed"
    assert "2 audio tracks" in res.reason and "drop 1" in res.reason
    assert ".mka" in res.reason
    assert "cmd" not in calls              # refused at the door, no encode
    assert not (tmp_path / "out.mp3").exists()


def test_two_tracks_into_mka_maps_every_stream(tmp_path, src, monkeypatch):
    eng, calls = _engine(monkeypatch, ["aac", "aac"])
    res = eng.convert(src, tmp_path / "out.mka", bitrate="128k")
    assert res.outcome == "converted", res.reason
    cmd = calls["cmd"]
    i = cmd.index("-map")
    assert cmd[i + 1] == "0:a"             # ALL audio, not 0:a:0
    assert "0:a:0" not in cmd


def test_single_track_still_maps_first_only(tmp_path, src, monkeypatch):
    eng, calls = _engine(monkeypatch, ["aac"])
    res = eng.convert(src, tmp_path / "out.mp3")
    assert res.outcome == "converted", res.reason
    assert calls["cmd"][calls["cmd"].index("-map") + 1] == "0:a:0"


def test_skip_requires_every_track_to_match(tmp_path, src, monkeypatch):
    """Track 1 already being the target codec must not skip a file whose
    other tracks still need converting."""
    eng, calls = _engine(monkeypatch, ["flac", "aac"])
    res = eng.convert(src, tmp_path / "out.mka", codec="flac")
    assert res.outcome == "converted"      # NOT skipped
    assert "cmd" in calls


def test_verify_fails_when_track_count_shrank(tmp_path, src, monkeypatch):
    eng, _ = _engine(monkeypatch, ["aac", "aac"], out_tracks=["aac"])
    out = tmp_path / "out.mka"
    out.write_bytes(b"only one track")
    res = eng.verify(src, out)
    assert res.outcome == "failed"
    assert "2 -> 1" in res.reason
