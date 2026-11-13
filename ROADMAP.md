# Transmute — ROADMAP

Phases ship **behind gates**: each phase ends with tests + a written report
in `STATUS.md`, and the next one starts only after the user says "next".

| Phase | Deliverable | Gate (evidence, not vibes) |
| --- | --- | --- |
| **P1 ✅** | Headless core: capability probe (measured ladder, GPU/HDR honest), engines (video/audio/image/pdf/document; never raise), runner (journal, staging→verify→replace, retry ladder), governor (Linux real, Windows stub), SPEC/ROADMAP/STATUS, 36 tests incl. load test. | `pytest -q tests/` green **on a GPU-less box**; no-engines simulation starts and reports cleanly. → evidence in STATUS.md |
| **P2** | PySide6 desktop GUI: capability-driven format pickers (grey-out **with reason**), target presets per category, queue view with live progress, ℹ️ compat badges, HDR warnings (F15 policy shown to user). | GUI smoke via `pytest-qt`; every picker entry traceable to a matrix row. |
| **P3** | Video depth: remux path (F2), CRF/size-target UX, HDR tone-map policy surfaced (F15), audio pass-through/replace rules, per-preset previews (first-frame stills). | e2e per rung incl. an HDR source; remux never re-encodes (bitstream hash proof). |
| **P4** | Watch folders (F24): drop-in add, settle detection, ignore rules, tree mirroring options, portable mode wiring. | Inotify-based test incl. partial-file and rename storms. |
| **P5** | Robustness & breadth: CI matrix (multiple ffmpeg builds, distro package variants), qpdf/pandoc real coverage (bench lacks them), long-file soak, cancel-all/resume-all UX, disk-space preflight. | CI green on ≥2 ffmpeg flavours; soak report. |
| **P6** | Windows support: governor via Job Objects (CPU rate control, IO priority), Job Object stubs replaced, path/UNC/long-path handling, engine discovery in Program Files chains. | Windows CI runner: same suite green; governor load test on Windows. |
| **P7** | Performance & UX polish: GPU session scheduler real test on NVIDIA + AMD boxes, measured-ladder staleness revalidation, incremental resume of giant files (`.part` continuation where safe). | Bench report with before/after wall-clocks on reference corpus. |
| **P8** | Packaging (⚠️ needs explicit user OK): AppImage + .deb/.rpm, **zero bundled engine binaries**, `LICENSES.md` compiled from *fetched* license texts only, update channel design (no codec downloads without consent). | `LicenseHeader` scan + bundle audit script proves no third-party binaries; fetched-text evidence committed. |
| **P9** | Docs site + handoff: user guide per category, capability-report explainer, contributor spec; may end as a handoff note. | `mkdocs build --strict` clean. |

## Standing rules across all phases

- Engines remain **external processes**; nothing in the repo or bundle may
  embed a codec/engine binary.
- Any new dependency needs a user-approved reason (allowed list is fixed:
  PySide6, Pillow, pikepdf, psutil, watchdog, pytest, jinja2, fastapi,
  uvicorn, tinytag, platformdirs).
- Regression duty: full suite (unit + e2e + slow) must stay green before a
  phase gate — including on a machine without GPU nodes.
- Honest labels are features: lossy deep-PDF, ℹ️ compat codecs, transient
  governor overshoot, trial-fps-as-ranking-only. They never get sandbagged.
