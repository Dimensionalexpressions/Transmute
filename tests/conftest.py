"""Shared fixtures: isolated app root, self-generated media fixtures, engine info.

Everything is generated locally (lavfi sources, Pillow-made images/GIFs/PDFs) -
the suite never downloads or ships sample content.
"""
from __future__ import annotations

import os
import subprocess
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]


@pytest.fixture(autouse=True)
def _gpu_lane_stays_put():
    """Restore TRANSMUTE_DISABLE_GPU after every test.

    The GUI's "use graphics card" checkbox deliberately rewrites this env var
    in-process (that IS the app's runtime toggle) — and GUI tests legitimately
    click it. Without restoring, the first such click silently deletes the
    GPU-off guarantee for the rest of the process: the tonemap test later in
    the same run then auto-picked h264_nvenc and died with ffmpeg -22. Each
    test must see the lane it was declared with, whatever earlier tests' GUIs
    clicked."""
    had = "TRANSMUTE_DISABLE_GPU" in os.environ
    saved = os.environ.get("TRANSMUTE_DISABLE_GPU")
    yield
    if had:
        os.environ["TRANSMUTE_DISABLE_GPU"] = saved
    else:
        os.environ.pop("TRANSMUTE_DISABLE_GPU", None)


def make_text_pdf(pages: list[str]) -> bytes:
    """Hand-built minimal PDF: one text line per page, Helvetica core font,
    xref offsets computed exactly (strict parsers must accept it). Used for
    text-extraction tests - the Pillow-made noise.pdf has NO text layer.
    """
    n = len(pages)
    page_ids = [3 + 2 * i for i in range(n)]
    cont_ids = [4 + 2 * i for i in range(n)]
    font_id = 3 + 2 * n
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>"
         % (" ".join(f"{p} 0 R" for p in page_ids), n)).encode(),
    ]
    for i, text in enumerate(pages):
        objs.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                     % (font_id, cont_ids[i])).encode())
        stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode()
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref_at)).encode()
    return bytes(out)


@pytest.fixture(scope="session")
def text_pdf(tmp_path_factory):
    """Two-page PDF with extractable text (poppler must read it)."""
    p = tmp_path_factory.mktemp("pdffix") / "letter.pdf"
    p.write_bytes(make_text_pdf(["Hello Transmute Page One",
                                 "Second page says goodbye"]))
    return p


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
