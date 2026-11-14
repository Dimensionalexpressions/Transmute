"""is_ignored / iter_supported_files: ignore rules are scoped to the chosen
root. A dot-named ancestor ABOVE the scan/watch root (e.g. a downloads folder
under ~/.cache) must not poison every file beneath that root — P4 watch smoke
caught exactly this: watching .smoke-watch/in dispatched nothing, silently.
"""
from pathlib import Path

import pytest

from transmute.sources import is_ignored, iter_supported_files

pytestmark = pytest.mark.unit


def test_ancestor_dots_above_scope_do_not_ignore(tmp_path, monkeypatch):
    root = tmp_path / ".cache" / "downloads"
    root.mkdir(parents=True)
    f = root / "photo.jpg"
    f.write_bytes(b"\xff\xd8" + b"x" * 40)
    monkeypatch.chdir(tmp_path)  # a relative probe would hit the wrong file
    rel = f.relative_to(root)
    assert not is_ignored(rel, real_path=f)     # scoped: the drop is visible
    assert is_ignored(f)                        # absolute view unchanged


def test_name_level_rules_stay_inside_scope():
    assert is_ignored(Path(".secret.png"))                    # hidden name
    assert is_ignored(Path("sub/.git/config.png"))            # hidden BELOW root
    assert is_ignored(Path("movie.mp4.part"))                 # partial suffix


def test_skip_marker_checked_against_real_path(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "movie.mkv"
    f.write_bytes(b"x")
    (root / "movie.mkv.skip").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    assert is_ignored(Path("movie.mkv"), real_path=f)  # marker found via anchor


def test_iter_supported_files_under_dot_named_root(tmp_path):
    root = tmp_path / ".hidden" / "inbox"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "deep.jpg").write_bytes(b"\xff\xd8xx")
    (root / ".secret.png").write_bytes(b"x")   # hidden below root: excluded
    (root / "notes.xyz").write_bytes(b"x")     # unsupported: excluded
    got = sorted(p.name for p in iter_supported_files(root))
    assert got == ["deep.jpg"]
