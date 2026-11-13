# Transmute — STATUS (living handoff)

**State: P1 gate PASSED ✅** — headless core complete, 36/36 tests green on a
GPU-less sandboxed Linux box (CachyOS bench, 32 cores, no `/dev/dri`, no
`/dev/nvidia*`). Next: **P2 (PySide6 GUI)** on user "next".

Bench quirks reflected in the tests on purpose: qpdf **and** pandoc are not
installed here (ladder fallbacks proven by simulation), systemd user bus is
unreachable (governor therefore uses psutil feedback, not cgroup controls).

## Gate evidence (commands run 2025-12 epoch, all from repo root)

```
$ .venv/bin/python -m pytest -q tests/test_probe_parse.py tests/test_queue.py tests/test_engines_missing.py
24 passed in 0.48s                     # hermetic: no external engines used

$ .venv/bin/python -m pytest -q -m "not slow" tests/
34 passed, 2 deselected in 10.29s      # real ffmpeg: probe, conversions, HDR, queue, governor resume

$ .venv/bin/python -m pytest -q -m slow tests/
2 passed, 34 deselected in 11.58s      # governor 32-hammer load test (mean<=85% under cap80)
                                        # LibreOffice odt->pdf

$ .venv/bin/python -m pytest -q tests/
36 passed in 22.36s                    # full suite

$ ls /dev/dri /dev/nvidia*             # proves the "GPU-less machine" claim
ls: cannot access '/dev/dri': No such file or directory
ls: cannot access '/dev/nvidia*': No such file or directory
```

No leftover processes after the run (`pgrep -c -x ffmpeg` → 0; governor test
kills its own process groups in `finally`).

## What P1 actually proves (measured, not claimed)

- **Capability probe** parses 226 encoders from real ffmpeg, trial-encodes
  99 of them (1 s, wall-clock fps) and produces the F10 ladder — on this
  box: av1 → libsvtav1 (529 fps) > libaom-av1 (143) > librav1e (62);
  h264 → x264rgb 800 / x264 763; opus 822; flac 846. Cache key = sha256 of
  engine identity (+`_PROBE_SALT`); re-probes when ffmpeg changes.
- **No-engine box simulation** (`tests/conftest.py::no_engines`): every
  engine `MISSING` with install hint, video/audio/document converts return
  `failed + engine_unavailable` (never raise), **image engine still works**
  (Pillow-only) and PDF lossless still works (pikepdf in-process). Queue
  abandons with a clean reason.
- **Verify-before-replace**: verify failure leaves the original bytes
  (`SENTINEL` assertion) and staging is cleaned.
- **Governor load test**: 32 ffmpeg hammers, CPU feedback loop, measured
  window mean ≤85% (observed ~55% with cap 80 — sawtooth stops more than
  needed), ≥1 child stopped at peak, all children resume/exit cleanly.

## Bugs found & fixed during P1 (kept as regression tests)

1. `queue.py` assumed `EngineResult.data` was a dict → `isinstance` guard.
2. Encoder-list parser ate the legend table: gate on the ` ------` separator
   with `.strip().startswith("---")` (header/legend rows sit above it).
3. ffmpeg `speed=` line vanishes under `-loglevel error` → fps is measured
   wall-clock instead of parsed (doctrine: measure, don't parse).
4. HDR fixture: x264 validates `color_primaries` as its own int option and
   ffmpeg's name for BT.2020 is `bt2020-10`, not `bt2020nc` → tag via
   `-x264-params colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc`.
5. **Pillow 13 removed `Image.formats`** — the probe silently reported
   png/gif/jpg unsupported. Now all image capabilities are *measured* by
   real in-memory encodes (`_probe_pillow`); animated-WebP probe must use
   two **distinct** frames (libwebp dedups identical ones → false negative).
6. Governor load test must assert the **mean of governor samples**, never an
   instantaneous cpu_percent — transient ~100% single samples are real
   sawtooth behaviour (documented in SPEC E.1, not hidden).

## Honest gaps (by design, tracked in ROADMAP)

- GUI doesn't exist yet (P2) — core is headless by design; nothing user-
  facing promises a window.
- Windows governor = declared stubs (`status()["windows_job_object"]`
  reports the state); real Job Object control is P6.
- qpdf/pandoc real-engine paths untested on this bench (tools absent) —
  simulated here, real CI matrix in P5.
- Deep PDF (ghostscript) is lossy — surfaced only via explicit `deep=True`
  and must be labelled in the P2 GUI.
- F2 remux, F24 watch folders, packaging, LICENSES.md: P3/P4/P8. LICENSES.md
  will be compiled from fetched license files only (P8) — nothing is
  bundled yet, so there is nothing to honestly document today.

## Dev notes

- `uv` needs `UV_CACHE_DIR="$PWD/.uv-cache"` in this sandbox (cache dir
  outside workspace is read-only); `.venv/bin/python` is the interpreter.
- `TRANSMUTE_GOVERNOR=0` disables the governor; `TRANSMUTE_DISABLE_GPU=1`
  skips GPU probes (tests use it to pin hw-path behaviour).
- Fixtures are self-generated (lavfi clips, HDR10-tagged mkv, animated GIF,
  4-page PDF) — no binary test assets are committed.
- Journal/staging contract and all kwargs signatures of `convert()` are
  documented at their call sites in `runner/queue.py` and `engines/*`.

## Phase protocol

Each phase: re-read SPEC/ROADMAP/STATUS → work → full suite green (incl.
GPU-less path) → append ✅ gate evidence here → ≤5-line report → **stop**
until user says "next". P8 packaging requires explicit user OK.
