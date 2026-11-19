"""Static integrity of the ENGINES spec table - the argv typos that only a
REAL installed engine can catch, so we catch them here instead.

Background: the qpdf spec shipped with version_args=("--version") - a bare
string, not a tuple (missing trailing comma). It looked fine while qpdf was
absent (status MISSING never reached the argv), and exploded on the first
machine that actually had qpdf installed (bench, 2026-11, qpdf 12.4.2): the
string splatted character-by-character into argv. qpdf's real --version
output then parses fine, proving the argv was the whole failure.
"""
from __future__ import annotations

from transmute.probe.engines import KNOWN_ENGINES as ENGINES


def test_version_args_are_sequences_of_str_never_bare_strings():
    for name, spec in ENGINES.items():
        assert isinstance(spec.version_args, tuple), (
            f"{name}: version_args must be a tuple, got {type(spec.version_args).__name__} "
            f"- a bare string splats into per-character argv"
        )
        assert spec.version_args, f"{name}: empty version_args"
        for a in spec.version_args:
            assert isinstance(a, str) and a, f"{name}: bad argv token {a!r}"
            assert " " not in a, (
                f"{name}: {a!r} looks like multiple args crammed into one token"
            )


def test_engine_names_match_spec_names():
    for key, spec in ENGINES.items():
        assert key == spec.name, f"table key {key} != spec.name {spec.name}"


def test_every_spec_has_a_primary_binary_name():
    for name, spec in ENGINES.items():
        assert spec.binary_names and all(spec.binary_names), (
            f"{name}: binary_names must be non-empty strings"
        )


class TestHwTrialCommands:
    """Real-machine lessons baked in as code, guarded as tests:
    hardware encoders reject tiny (<~144px) samples with EINVAL, and on a
    multi-GPU box the first render node may lack the encoder backend -
    every node must get its own trial candidate."""

    def _cap(self, name, codec, backend):
        from transmute.probe.capability import EncoderCapability
        return EncoderCapability(name=name, codec=codec, backend=backend,
                                 hw=True, verified=False, fps=None)

    def test_vaapi_gets_one_candidate_per_render_node(self, monkeypatch):
        from pathlib import Path
        from transmute.probe import capability as c
        monkeypatch.setattr(c, "_render_nodes", lambda: [
            Path("/dev/dri/renderD128"), Path("/dev/dri/renderD129")])
        cmds = c._trial_cmds(Path("ffmpeg"), self._cap("hevc_vaapi", "hevc", "va-api"))
        joined = [" ".join(map(str, cmd)) for cmd in cmds]
        assert len(cmds) == 2
        assert "renderD128" in joined[0] and "renderD129" in joined[1]
        assert all("1280x720" in j for j in joined), (
            "hw samples must exceed NVENC min size AND be big enough to "
            "measure real-world speed (user directive: numbers reflect "
            "THIS machine's hardware, benchmark-style)")

    def test_nvenc_trial_never_forces_nv12_system_frames(self):
        from pathlib import Path
        from transmute.probe import capability as c
        cmd = " ".join(map(str, c._trial_cmds(
            Path("ffmpeg"), self._cap("h264_nvenc", "h264", "nvidia-nvenc"))[0]))
        assert "format=yuv420p" in cmd and "format=nv12" not in cmd

    def test_run_trials_succeeds_on_second_node(self, monkeypatch):
        from transmute.probe import capability as c
        monkeypatch.setattr(c, "_run_trial", lambda cmd: (
            (False, None, "entrypoint missing")
            if "renderD128" in " ".join(map(str, cmd)) else (True, 42.0, "")))
        cmds = [["x", "renderD128"], ["x", "renderD129"]]
        assert c._run_trials(cmds) == (True, 42.0, "")

    def test_run_trials_keeps_last_reason_when_all_fail(self, monkeypatch):
        from transmute.probe import capability as c
        monkeypatch.setattr(c, "_run_trial", lambda cmd: (False, None, f"fail {cmd[0]}"))
        assert c._run_trials([["a"], ["b"]]) == (False, None, "fail b")


def test_webp_animated_row_follows_the_engine_gate(tmp_path):
    """The engine refuses animated GIF->WebP unless Pillow's WebP backend
    works (_supports("webp")); the row used to infer it from GIF support
    and showed AVAILABLE exactly where the engine would then refuse."""
    from types import SimpleNamespace
    from transmute.probe.capability import build_matrix

    def rows_for(feat):
        rep = SimpleNamespace(engines={}, encoders=[], pdf_backends={},
                              image_backends={"features": feat})
        return build_matrix(rep)

    rows = rows_for({"webp": False, "gif": True, "avif": False, "png": True})
    anim = [r for r in rows if r.target == "webp(animated)"]
    assert anim and anim[0].available is False
    assert "WebP" in anim[0].note
    rows = rows_for({"webp": True, "gif": False, "avif": False, "png": True})
    anim = [r for r in rows if r.target == "webp(animated)"]
    assert anim and anim[0].available is True


def test_probe_key_moves_when_gpu_devices_change(tmp_path, monkeypatch):
    """A machine's no-GPU verdict must not outlive a hardware change: the
    cached probe key now contains the device count, so plugging in (or
    failing) a render node invalidates the cache."""
    from transmute.probe import capability as C
    import glob as glob_mod

    state = {"n": 1}

    def fake_glob(pat):
        return ["x"] * state["n"] if "*" in pat else []

    monkeypatch.setattr(glob_mod, "glob", fake_glob)   # imported per call
    t1 = C._device_token()
    assert C._device_token() == t1            # stable while nothing changes
    state["n"] = 2
    assert C._device_token() != t1            # device appeared: key changes
