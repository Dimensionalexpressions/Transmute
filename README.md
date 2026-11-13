# Transmute

Open-source, **runs-on-your-machine** converter for pictures, audio, video,
PDFs and documents. Same feature class as the classic Windows converter
suites — but open (Apache-2.0), Linux-first, and with **zero bundled codecs
and zero copied assets**.

Transmute does not ship or copy any encoder. It drives the FFmpeg,
Ghostscript, qpdf, LibreOffice, pandoc and poppler tools **already installed
on your machine**, as external processes, and it *measures* what your machine
can actually do before it ever offers you an option.

## The doctrine

1. **Nothing is assumed; everything is measured.** Which codecs work, how
   fast each encoder is, whether a GPU is usable, which image formats can be
   written — all of it comes from runtime probes (including 1-second trial
   encodes), cached under a key that invalidates when your FFmpeg changes.
   Anything the probe cannot prove works is hidden or greyed out **with a
   reason**. No dead buttons.
2. **Your files are never at risk.** Output is written to a staging file
   next to the destination, verified (duration/streams/codec/page-count as
   appropriate), and only then `os.replace`d into place. A crash, a cancel or
   a bad conversion can never destroy the source.
3. **Royalty-free first.** Recommended defaults are AV1/VP9, WebP/AVIF/PNG,
   Opus/FLAC, lossless PDF. H.264/HEVC/AAC/MP3 remain available and are
   marked ℹ️ (patent-encumbered) — and only when your system can even encode
   them.
4. **Be honest when it can't.** Engines that are missing don't throw stack
   traces into your face; they report `engine unavailable` with an install
   hint for your OS, and the job queue keeps everything else moving.

## Quickstart (Linux)

```bash
git clone <this repo> && cd transmute
uv venv .venv && .venv/bin/python -m pip install -e .   # or: python -m venv .venv
.venv/bin/python -m pytest -q tests/                    # 36 tests, no GPU needed
```

Core usage (GUI comes in P2; the headless core is fully functional now):

```python
from transmute import CapabilityProber, JobQueue

report = CapabilityProber().report()          # probed once, cached
print(report.verified_encoders("av1"))        # measured ladder, fastest first
print(report.row("video", "av1").note)

# queue anything you like - every job is journaled, staged, verified:
q = JobQueue()
q.add("clip.mp4", "out/clip.mkv", options={"codec": "av1"})
q.run()                                       # resumable after a crash
```

## Controls & environment

| Variable | Effect |
| --- | --- |
| `TRANSMUTE_DISABLE_GPU=1` | Skip GPU capability probes entirely (also what CI/sandboxes use). |
| `TRANSMUTE_GOVERNOR=0` | Disable the resource governor (default: on, Linux caps active). |
| `TRANSMUTE_ROOT` | Override the app data root (journal, logs, capability cache). |

The governor keeps Transmute at ~80% CPU by suspending its *own* child
process groups (SIGSTOP/SIGCONT sawtooth), throttles its own file IO, and
runs at most a few GPU encode sessions at once. It never touches other
programs.

## Where things live

- `src/transmute/probe/` — engine discovery + capability report (the brain)
- `src/transmute/engines/` — video / audio / image / pdf / document wrappers (never raise)
- `src/transmute/runner/` — journal-backed queue: staging → verify → replace, retry ladder
- `src/transmute/governor/` — CPU/IO/GPU self-throttling (Linux real, Windows stub)
- `SPEC.md` / `ROADMAP.md` / `STATUS.md` — what it must do, what it will do, where we are

## Licensing

App code: Apache-2.0 (see `LICENSE`). Engines stay external and on your own
system's packaging; this repository bundles **no** codec or engine binaries.
`LICENSES.md` (added at P8 packaging) will be compiled from the actual
fetched license texts of whatever we choose to bundle — never from memory.
