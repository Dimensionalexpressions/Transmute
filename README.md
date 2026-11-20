# Transmute

Open-source, **runs-on-your-machine** converter for pictures, audio, video,
PDFs and documents. Same feature class as the classic Windows converter
suites — but source-available under the **Free Use Attribution License (FUAL-1.0)**, Linux-first
(Windows actively validated), with **zero bundled codecs and zero copied assets**.

Transmute does not ship or copy any encoder. It drives the FFmpeg,
Ghostscript, qpdf, LibreOffice, pandoc and poppler tools **already installed
on your machine**, as external processes, and it *measures* what your machine
can actually do before it ever offers you an option.

## Status: Alpha

Transmute is at **Alpha** stage. It is genuinely usable and its test suite
runs on Linux and Windows, but expect rough edges: command-line and file
formats coverage keep growing, GUI labels and flows will change, and
release notes may carry breaking changes between versions without warning.
Releases are tagged and labelled accordingly. Use under the license terms —
it ships as-is, with no warranty, exactly as stated in `LICENSE`.

## Downloads

Source-only for now — Transmute needs the engines installed on your machine
anyway (FFmpeg, Ghostscript, qpdf, LibreOffice, pandoc, poppler), so a zip
buys you little. Pick one:

| How | Get it |
|---|---|
| Source archive | [Releases](https://github.com/Dimensionalexpressions/transmute/releases) → latest, then the Quickstart below |
| Nix flake | `nix run github:Dimensionalexpressions/transmute` — engines are baked into PATH by the flake |
| Git checkout | the Quickstart below |

## Publisher

**Dimensional Expressions Software**  
Published by **Dimensional Expressions LLC** (Ohio).
Coding assisted with local LLM model

Originally conceived and developed by **Coty Julien**.

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
.venv/bin/python -m pip install -e ".[gui]"             # optional: PySide6 desktop GUI
.venv/bin/python -m pytest -q tests/                    # no GPU needed; GUI tests skip without PySide6
```

Desktop GUI (formats are greyed out with **your machine's** reasons, ℹ️ compat
badges, HDR tone-map notices, measured-ladder encoder picker):

```bash
.venv/bin/python -m transmute.gui
```

Headless/core usage (the GUI is a thin layer over exactly this):

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
programs. In the GUI this is the **"Throttle while converting"** checkbox
(default on, remembered per machine): unchecked, engines run flat out.
The server always throttles unless the operator sets `TRANSMUTE_GOVERNOR=0`
in its environment — an uploaded file should never own somebody's machine.

## Where things live

- `src/transmute/probe/` — engine discovery + capability report (the brain)
- `src/transmute/engines/` — video / audio / image / pdf / document wrappers (never raise)
- `src/transmute/runner/` — journal-backed queue: staging → verify → replace, retry ladder
- `src/transmute/governor/` — CPU/IO/GPU self-throttling (Linux real, Windows stub)
- `src/transmute/gui/` — Qt shell; `controller.py` holds every decision and is Qt-free
- `SPEC.md` / `ROADMAP.md` / `STATUS.md` — what it must do, what it will do, where we are

## License

This project is distributed under the **Free Use Attribution License (FUAL-1.0)** — the same
license as our [Duplicate Finder](https://github.com/Dimensionalexpressions/duplicate-finder).

- Personal and other noncommercial use is free; **Attribution is required for all use** (credit + clickable link to this project).
- Commercial use is free and does not require payment, royalties, or donations; it requires the same Attribution, including for internal company use.
- You may modify the software and keep your modifications and proprietary additions private.
- The software is provided "AS IS", without warranties, subject to the license terms.

Read the complete [FUAL-1.0 license](LICENSE) before using or redistributing this project.

### Required attribution for all use

Include this notice in a reasonably accessible location appropriate to your product or deployment:

> Originally conceived and developed by Coty Julien. Published by Dimensional Expressions LLC.  
> Original project: [Transmute](https://github.com/Dimensionalexpressions/transmute)

Engines (ffmpeg, Ghostscript, qpdf, LibreOffice, poppler, pandoc) stay external and on your own
system's packaging; this repository bundles **no** codec or engine binaries, and every third-party
component keeps its own license. When redistributing the software, include
`THIRD-PARTY-NOTICES.md` (or an equivalent notice) as well (FUAL-1.0 §6).

<!-- report-a-bug:start -->
## Report a bug

Something broken, or something that should work differently? Open an issue on this repository's
**Issues** page — the form asks a few short questions, and one command fills in the technical part
for you (versions, engines it found, nothing personal unless you ask for it):

Linux/macOS:

```
./scripts/bugreport.sh --command "the exact command you ran"
```

Windows (PowerShell):

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bugreport.ps1 -Command "the exact command you ran"
```

What is asked, and why, is written out in [REPORTING.md](REPORTING.md).

<!-- report-a-bug:end -->
