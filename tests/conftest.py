"""Shared fixtures: isolated app root, self-generated media fixtures, engine info.

Everything is generated locally (lavfi sources, Pillow-made images/GIFs/PDFs) -
the suite never downloads or ships sample content.
"""
from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]


@pytest.fixture(scope="session", autouse=True)
def app_root(tmp_path_factory):
    """Whole app footprint (config/state/cache/logs) inside pytest tmp."""
    from transmute.paths import AppPaths, set_paths
    root = tmp_path_factory.mktemp("tmut-root")
    set_paths(AppPaths(root=root))
    return root


@pytest.fixture(scope="session")
def engine_info():
    from transmute.probe.engines import EngineManager
    return EngineManager().detect()


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory, engine_info):
    """Session-wide self-generated media; ffmpeg parts skipped when absent."""
    d = tmp_path_factory.mktemp("fixtures")
    out: dict[str, Path] = {}
    from PIL import Image

    noise = Image.effect_noise((256, 256), 100).convert("RGB")
    out["jpg"] = d / "noise.jpg"
    noise.save(out["jpg"], quality=90)

    frames = [Image.effect_noise((96, 96), 120).convert("P") for _ in range(6)]
    out["gif"] = d / "anim.gif"
    frames[0].save(out["gif"], save_all=True, append_images=frames[1:],
                   duration=80, loop=0)

    pages = [Image.effect_noise((400, 500), 160).convert("RGB") for _ in range(4)]
    out["pdf"] = d / "noise.pdf"
    pages[0].save(out["pdf"], format="PDF", save_all=True,
                  append_images=pages[1:])

    if engine_info.get("ffmpeg") and engine_info["ffmpeg"].available:
        out["mp3"] = d / "tone.mp3"
        subprocess.run(FF + ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                             "-c:a", "libmp3lame", "-b:a", "128k", str(out["mp3"])],
                       check=True)
        out["mp4"] = d / "clip.mp4"
        subprocess.run(FF + ["-f", "lavfi", "-i",
                             "testsrc2=size=128x128:rate=25:duration=1",
                             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-c:v", "libx264", "-preset", "veryfast",
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "1",
                             str(out["mp4"])], check=True)
        # HDR10-tagged clip: bt2020 primaries + smpte2084 (PQ) transfer.
        # x264 eats the colour options itself, so tag via -x264-params.
        out["hdr"] = d / "hdr.mkv"
        subprocess.run(FF + ["-f", "lavfi", "-i",
                             "testsrc2=size=128x128:rate=25:duration=1",
                             "-c:v", "libx264", "-preset", "veryfast",
                             "-pix_fmt", "yuv420p10le",
                             "-x264-params",
                             "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                             str(out["hdr"])], check=True)
    return out


@pytest.fixture
def no_engines(monkeypatch):
    """Simulate a machine with ZERO external engines installed (F26 worst case).

    shutil.which finds nothing AND conventional dirs are empty. Engines that
    are pure-Python in-process (Pillow for images, pikepdf for PDFs) must
    still work; every ffmpeg/gs/LO-dependent path must report unavailable.
    """
    import shutil

    import transmute.probe.engines as pe
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(pe, "_conventional_dirs", lambda: [])
    monkeypatch.setattr(pe, "_run_version",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("no engines on this fake box")))
    return None
