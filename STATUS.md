# Transmute — STATUS (living handoff)

**State: P2 gate PASSED ✅** — GUI added on top of the P1 core; 61/61 tests
green on a GPU-less sandboxed Linux box (CachyOS bench, 32 cores, no
`/dev/dri`, no `/dev/nvidia*`); GUI verified offscreen (no X11 session in
this sandbox). Next: **P3 (video depth)** on user "next".

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

## P2 gate evidence (GUI; commands from repo root)

```
$ .venv/bin/python -m pytest -q tests/test_gui_controller.py
14 passed in 0.49s                    # Qt-free controller: targets, reasons, ladder, options, queue e2e

$ .venv/bin/python -m pytest -q tests/test_gui_widgets.py
10 passed in 0.84s                    # QT_QPA_PLATFORM=offscreen, real QTest clicks, no pytest-qt

$ .venv/bin/python -m pytest -q tests/test_gui_probe_boot.py
1 passed in 7.61s                     # REAL cold probe through the controller (production boot path)

$ .venv/bin/python -m pytest -q tests/
61 passed in 32.39s                   # full suite; no Qt warnings at teardown

$ mkdir -p .noqt && echo 'raise ImportError("simulated absent/broken")' > .noqt/PySide6.py
$ PYTHONPATH=$PWD/.noqt .venv/bin/python -m pytest -q tests/test_gui_widgets.py tests/test_gui_controller.py
SKIPPED [1] tests/test_gui_widgets.py:21: could not import 'PySide6': simulated absent/broken
14 passed, 1 skipped in 0.39s         # absent/broken Qt: GUI skips, core+controller unaffected

$ TRANSMUTE_ROOT=$PWD/.smoke-boot QT_QPA_PLATFORM=offscreen timeout -s KILL 40 .venv/bin/python -m transmute.gui
This plugin does not support propagateSizeHints()   # QWindow realized (offscreen platform)
exit=137                              # 40 s healthy event loop, killed by timeout; no traceback
```

Gate deviation (honest): ROADMAP said "GUI smoke via `pytest-qt`" —
pytest-qt is not on the allowed dependency list, so the same coverage runs
with a manual `QApplication` + `QTest.qWait` under `QT_QPA_PLATFORM=offscreen`
in `tests/test_gui_widgets.py`. **No interactive X11/Wayland session was run**
from this sandbox; window boot is proven offscreen only.

What P2 shows (tested, not claimed):

- Every picker entry traces to a capability-matrix row: unavailable targets
  stay **visible + greyed with the machine's own reason** as tooltip (F3);
  compat codecs badged ℹ️, verified ✓; "verify renders" bookkeeping rows
  never surface.
- Encoder dropdown = measured ladder, fastest verified first, default
  "auto (fastest verified)"; hidden and disabled for image/pdf categories.
- HDR source → tone-map notice before converting (F15); HDR *unknown* (no
  ffprobe) → honest unknown notice, no invented claim.
- Worker thread never touches timers: run completion travels via `Signal`
  (queued connection) — the teardown `killTimer` warnings disappeared and a
  deterministic `QTest.qWait` test pins it.
- GUI is an optional extra (`pip install transmute[gui]`); without PySide6
  only the widget tests skip — headless core untouched.

## P3 gate evidence (video depth; commands from repo root)

```
$ TRANSMUTE_DISABLE_GPU=1 TRANSMUTE_ROOT=$PWD/.smoke-p3 .venv/bin/python -m pytest -q tests/test_video_p3.py
10 passed in 19.46s                   # e2e, real ffmpeg: remux bitstream-hash
                                      # proof, HDR tone-map executed, size
                                      # two-pass + VBV correction, audio
                                      # replace, bad-audio copy-video path

$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_gui_p3.py tests/test_queue.py
19 passed in 1.14s                    # controller-level P3 doctrine: remux
                                      # refuses non-copy codecs by name,
                                      # CRF-vs-size exclusivity, tonemap gate

$ QT_QPA_PLATFORM=offscreen TRANSMUTE_ROOT=$PWD/.smoke-w .venv/bin/python -m pytest -q tests/test_gui_widgets.py
19 passed in 0.67s                    # P2's 10 + 9 new widget tests (remux
                                      # toggle, machine verdict line, tonemap
                                      # greyed-with-reason, radios, preview)

$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_gui_p3.py tests/test_gui_controller.py tests/test_gui_probe_boot.py tests/test_queue.py
34 passed in 10.97s                   # GUI tier regression

$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/
91 passed in 37.78s                   # full suite, every tier, exit 0

$ TRANSMUTE_ROOT=$PWD/.smoke-rgb TRANSMUTE_DISABLE_GPU=1 .venv/bin/python .dsh-staging/diag_rgb.py
# (diag script is scratch, not committed; the same assertions are pinned by
#  tests/test_probe_parse.py::test_rgb_only_is_measured_from_pix_fmts_not_names
#  and ::test_rgb_only_encoder_never_wins_auto_pick)
RGB-only encoders measured on this box: 43 (incl. libx264rgb=True)
libx264: fps=869 rgb=False
libx264rgb: fps=976 rgb=True
auto-pick h264 -> libx264
RGB-DISQUALIFICATION OK               # rgb_output is MEASURED per encoder

$ TRANSMUTE_ROOT=$PWD/.smoke-boot QT_QPA_PLATFORM=offscreen timeout -s KILL 40 .venv/bin/python -m transmute.gui
This plugin does not support propagateSizeHints()
exit=137                              # healthy event loop, killed by timeout
```

What P3 actually delivers (tested, not claimed):

- **Remux (F2)** is `-map 0 -c copy` only — never re-encodes. Proof: both
  video bitstreams extracted from source and output via raw muxers and
  sha256-compared; `remux_proof="bitstream-hash"` recorded in the journal
  (fallback `"packet-manifest"` when a raw muxer is absent). Containers
  checked against an explicit table; an impossible copy fails fast with the
  machine's named reason ("'vp9' cannot be copied into '.mp4' losslessly"),
  and the GUI shows that verdict on the picker **before** enqueueing.
- **Audio policy**: same-codec skip is refused when audio is `bad_audio`
  (F20); the video stream still rides `-c:v copy` while only audio
  transcodes (`audio="replace"` → AAC 160k). `remux=True, audio="replace"`
  fails fast — "remux never re-encodes audio".
- **Size target**: two-pass x264/x265 first; if the result exceeds 1.10×
  target, ONE pass-2 re-run under a VBV ceiling (`-maxrate =v_bps
  -bufsize 2*v_bps`, target bitrate ×0.92). Measured on a 6 s noise clip at
  0.75 MB: plain pass-2 140.6%, bitrate-only correction 123.2%, VBV-capped
  **109.6% (822,221 B)** — chosen. `size_note` records `vbv_correction`,
  `achieved_bytes`, `within_budget`; over-budget says "content is too
  complex for this budget - consider a smaller frame size". Non-x264/265
  encoders get the same intent labelled **approximate** in the GUI tooltip.
- **HDR tone-map (F15)**: never silent. The GUI's tone-map checkbox exists
  only if the probe actually executed the full zscale+tonemap chain on an
  HDR-tagged ≥10-bit PQ sample (`tonemap_chain` in the matrix); otherwise
  greyed with "this ffmpeg lacks the zscale+tonemap chain (probed)". HDR
  notice now names the real option the machine has.
- **RGB-encoder disqualification** (found via tone-map): auto-pick dropped
  the verified-fastest `libx264rgb` and produced RGB-space h264 no hardware
  player decodes. Disqualifier is a measured output property (`rgb_output`:
  ffmpeg lists only RGB pixel formats for that encoder), applied to
  auto-pick only — explicit user choice stays honoured. Deliberately NOT
  compat-flag-based: libx264/libx265/libopenh264 are all ℹ️ compat too.
- **First-frame preview (F13 partial)**: one still via `ffmpeg
  -frames:v 1` into a temp dir; un-decodable input says "preview
  unavailable" — measured as unmeasurable, never a invented picture (F3).

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

## Bugs found & fixed during P3 (kept as regression tests)

- Auto-pick chose the verified-**fastest** encoder `libx264rgb` → RGB-space
  h264 that hardware players choke on. First fix attempt used the ℹ️ compat
  flag — wrong (libx264/libx265 are compat too; it would ban normal use).
  Correct disqualifier: probe-**measured** `rgb_output` (ffmpeg lists only
  RGB pixel formats for that encoder); explicit user choice stays honoured.
  Pinned by `test_probe_parse.py::test_rgb_only_encoder_never_wins_auto_pick`
  + `::test_rgb_only_is_measured_from_pix_fmts_not_names`.
- Size target overshoot: plain two-pass = 140.6% of budget; one bitrate-only
  re-run still 123.2% (encoder can't go slower than its qmin floor). Real
  ceiling is VBV (`-maxrate`/`-bufsize` at the budget rate) → measured
  **109.6% (822,221 B vs a 750,000 B budget)**; `size_note` records the
  correction and says so honestly when content still busts the budget.
- GUI **modal `QMessageBox`** froze the whole offscreen test tier (a status
  popup no offscreen clicker can dismiss). All three became non-blocking
  `lbl_status` messages — better UX anyway; P2's tests never clicked that
  path, so the modal sat there unnoticed.
- Two wiring bugs caught pre-commit by the new widget tests: (a) untoggling
  remux left the codec combo disabled — F3 says only per-ITEM greying is
  honest, the combo itself must re-enable; (b) `_run` sent `{"codec": …}`
  where the controller contract demands `{"remux": True}` alone.
- `/tmp` is not persistent between sandbox tool calls — suite logs must be
  written inside the workspace (hit once; rerun wrote `.suite-full.log`).
- My own new signal test flaked under full-suite load: a fixed
  `QTest.qWait(400)` passed idle but failed while e2e encodes hogged the CPU
  — the queued `run_done` signal can be delivered while the worker thread is
  still retiring, so `is_alive()` was racing. Test now polls the condition
  with a 10 s deadline; full suite green after (91 passed).

## Bugs found & fixed during P2 (kept as regression tests)

1. PySide6 arg-order trap: `QListWidgetItem.setData(role, value)` but
   `QStandardItem.setData(value, role)` — wrong order = TypeError in the
   target list (comment at the call site guards it).
2. `QTimer.singleShot` from a worker thread → `QObject::killTimer: Timers
   cannot be stopped from another thread` at teardown; completion now travels
   via `Signal` → queued slot on the GUI thread.
3. **Boot crash that survived the whole unit suite**: controller called
   `CapabilityProber(paths=...)`, which the prober doesn't accept — every
   unit test injects a fake report, so only a real boot hit it. Caught by
   the offscreen boot check; guarded from P2 on by
   `tests/test_gui_probe_boot.py` (e2e, real cold probe, isolated root).
4. Image engine left `EngineResult.encoder` empty ("audit trail" gap) — now
   records `pillow`, matching pdf/document engines; controller e2e asserts a
   non-empty producer.
5. `pytest.importorskip("PySide6")` (pytest ≥ 9.1) skips only on
   `ModuleNotFoundError`, so an installed-but-broken Qt would be a
   collection error; explicit `exc_type=ImportError` makes both cases skip.

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

- GUI verified **offscreen only** (no display in this sandbox); no
  interactive X11/Wayland session has been exercised. First-frame previews
  shipped in P3; progress still ticks per job — byte-level progress is P7.
- Windows governor = declared stubs (`status()["windows_job_object"]`
  reports the state); real Job Object control is P6.
- qpdf/pandoc real-engine paths untested on this bench (tools absent) —
  simulated here, real CI matrix in P5.
- Deep PDF (ghostscript) is lossy — only on explicit opt-in, and the GUI
  checkbox is labelled "Deep PDF pass (LOSSY recompress)" (P2 ✅).
- F24 watch folders, packaging, LICENSES.md: P4/P8. LICENSES.md
  will be compiled from fetched license files only (P8) — nothing is
  bundled yet, so there is nothing to honestly document today.
- Edge gap (documented, deliberately unfixed): a machine where ffmpeg exists
  but **zero video encoders verify** has no video targets in the GUI matrix,
  so remux is unreachable through the GUI even though remux itself needs no
  encoder. The engine API path is e2e-proven either way; a dedicated "copy
  streams only" GUI affordance belongs with P5/P7 GUI polish, not silently
  bolted on now.

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
