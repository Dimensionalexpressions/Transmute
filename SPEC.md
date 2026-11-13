# Transmute — SPEC

Requirements are **testable statements**. Every ✅ line names the test that
proves it; every planned line names the phase that will. Nothing in this file
may claim a capability the code cannot demonstrate on a GPU-less machine.

> Numbering note: F1–F26 were fixed in the founding plan (F10 = measured
> encoder ladder, F15 = HDR). Wording here was re-derived during P1; if a
> number ever disagrees with the user's original list, the user's list wins.

## A. Conversion features

| ID | Requirement | Acceptance | Status |
| --- | --- | --- | --- |
| F1 | Convert video between codecs (transcode) with correct A/V streams. | `test_e2e_real.py::test_video_convert_verify` (mp4→AV1 mkv, ffprobe-verified) | ✅ P1 |
| F2 | Remux into another container without re-encoding when streams are compatible. | P3 (video engine `container=` path exists; remux-only rung planned) | P3 |
| F3 | Video quality control: CRF-based, and encoder/picker override. | video engine `crf=`/`encoder=` kwargs; ladder test `test_verified_ladder_sorted_fastest_first` | ✅ P1 |
| F4 | Convert audio between codecs. | `test_audio_to_opus_and_flac` (mp3→Opus, mp3→FLAC) | ✅ P1 |
| F5 | Audio bitrate/quality control, incl. lossless rung (FLAC). | audio engine `bitrate=`/`quality=`; FLAC e2e covered | ✅ P1 |
| F6 | Convert pictures between formats (jpg/png/webp/avif/gif/tiff/bmp…). | `test_image_jpg_to_webp_and_gif_stays_animated (Pillow-only, zero external engines) | ✅ P1 |
| F7 | Lossy image compression only if it actually saves ≥1% (`kept` otherwise; original never touched when smaller). | image engine `min_savings_pct`/`keep_original_smaller`; `test_kept_does_not_touch_dst` | ✅ P1 |
| F8 | Animated GIF→WebP keeps animation, frame count and loop. | `test_image_jpg_to_webp_and_gif_stays_animated` | ✅ P1 |
| F9 | PDF **lossless** compression (pikepdf → qpdf → ghostscript ladder), output page count identical. | `test_pdf_lossless_engine_result_is_honest` + `test_pdf_engine_in_process_without_qpdf` | ✅ P1 |
| F10 | Pick the encoder that is **measurably fastest** on *this* machine (trial-encoded fps, never folklore). | `test_verified_ladder_sorted_fastest_first`, live probe trials in `test_probe_live.py` | ✅ P1 |
| F11 | Everything the machine cannot do is hidden or greyed **with a reason** — no dead buttons. | matrix rows carry `available/hw/verified/note`; `test_capability_report_survives_missing_ffmpeg`; probe skips carry reasons | ✅ P1 (core), GUI P2 |
| F12 | Hardware acceleration only when **probed and trial-verified**; absent GPU ⇒ clean skip with reason, never a failed job. | `test_hw_rows_skipped_without_gpu`, `TRANSMUTE_DISABLE_GPU` tests | ✅ P1 |
| F13 | Recommended defaults are royalty-free: AV1/VP9, WebP/AVIF/PNG, Opus/FLAC, lossless PDF. | matrix `recommended` flags + README doctrine | ✅ P1 |
| F14 | Patent-encumbered outputs (H.264/HEVC/AAC/MP3/VC-1) stay available where supported but carry an ℹ️ compat flag. | `COMPAT_CODECS`, `MatrixRow.compat_flag`, `_classify` tests | ✅ P1 |
| F15 | HDR (PQ/HLG) is **detected**; conversions that would tone-map must say so; SDR sources never labelled HDR. | `test_hdr_detection_f15` (smpte2084-tagged mkv vs plain mp4) | ✅ P1 detection; policy in P3 |
| F16 | Document conversion (odt/docx/html/… ↔ PDF etc.) via LibreOffice/pandoc, engine-free formats pass through honestly. | `test_document_odt_to_pdf` (slow) | ✅ P1 |

## B. Queue, safety, resilience

| ID | Requirement | Acceptance | Status |
| --- | --- | --- | --- |
| F17 | Batch queue: add jobs by category with per-job options; idempotent re-add. | `test_add_idempotent_for_done_jobs` | ✅ P1 |
| F18 | **Verify before replace**: output validated (streams/duration/codec/pages) before the destination is replaced; staging cleaned either way. | `test_replaced_and_staging_cleaned`, `test_verify_failure_preserves_original` | ✅ P1 |
| F19 | Crash-safe journal: append-only JSONL; torn last line tolerated; orphan `running` → `interrupted` on restart. | `test_torn_journal_line_tolerated`, `test_ghost_running_replays_as_interrupted_then_discard` | ✅ P1 |
| F20 | Retry ladder 10 min → 1 h → 24 h → `abandoned`; truly terminal causes (engine missing, invalid options) fail immediately with reason. | `test_retry_ladder_then_abandoned`, `test_engine_unavailable_result_is_terminal`, `test_engine_unavailable_availability_is_terminal` | ✅ P1 |
| F21 | A crashing engine must never crash the runner or destroy sources. | `test_crashing_engine_does_not_break_queue` | ✅ P1 |

## C. Resource & system behaviour

| ID | Requirement | Acceptance | Status |
| --- | --- | --- | --- |
| F22 | Self-throttling governor keeps CPU ≈80% cap using feedback; measured load test on ≥8 cores: mean CPU ≤85% under 1×-core-count FFmpeg hammers. | `test_cpu_cap_under_load_slow` (32 hammers on bench) | ✅ P1 (Linux) |
| F23 | Governor throttles only its own children (process groups), throttles its own IO (token bucket), caps GPU encode sessions; Windows has honest stubs. | `test_register_resume_child_never_left_stopped` (child exits cleanly after unregister), `status()` exposes counters; Windows = declared stub | ✅ P1 core, Windows P6 |
| F24 | Watch folder: drop files in, auto-add to queue (ignored-name and partial-file rules). | `sources.py` rules exist; watcher itself | P4 |
| F25 | App data in standard per-user dir (platformdirs), opt-in portable mode; outputs mirrored to destination tree by default. | `paths.py` root + `portable.ini` marker honored | ✅ root logic; GUI P2 |
| F26 | Honest logs: per-job log files, machine-readable reasons for every non-`converted` outcome, no swallowed exceptions. | `logs_root` per-job files; every `EngineResult` carries `reason`; JSONL journal | ✅ P1 |

## D. Capability matrix (draft, generated at runtime)

`build_matrix()` emits rows per **category × target**, e.g.:

| category | target | path / ladder | availability source |
| --- | --- | --- | --- |
| video | av1 / vp9 / h264 / hevc / vp8 | FFmpeg encoders sorted by **measured fps**; hw rungs (`*_nvenc`, `*_vaapi`, `*_qsv`) added only when trial-verified | `-encoders` parse + 1-second trial encode per candidate |
| audio | opus / flac / vorbis / mp3 / aac… | same ladder mechanism | same |
| image | webp, webp(animated), avif, png | Pillow — capabilities measured by **real in-memory encodes** (Pillow 13 removed `Image.formats`) | trial save round-trips |
| pdf | pdf (lossless smaller) | pikepdf → qpdf → ghostscript (last rung **lossy**, labelled) | import probe + engine presence |
| pdf | pdf (verify renders) | pdftoppm (poppler) | engine presence |
| document | any ↔ pdf | LibreOffice (broad) / pandoc (markup formats) | engine presence |

Rows never lie: `available=False` always carries a `note`/reason, and
`hw=True` rows outside a verified GPU context are `verified=False` with
`skipped="gpu-probe-disabled…"` or "no render nodes".

## E. Honest caveats (contract, not footnotes)

1. **Governor overshoot is real and expected.** SIGSTOP/SIGCONT is a
   sawtooth: single samples transiently show ~100% before feedback bites.
   The contract is the **measured windowed mean ≤ cap + margin**, which the
   load test asserts; instantaneous samples are *not* asserted.
2. **Throttled wall-clock counts toward command timeouts.** A job stopped
   by the governor spends fewer CPU-seconds inside its timeout — acceptable
   and documented, not "hanging".
3. **Ghostscript deep PDF pass is lossy** (re-compresses images). It is only
   offered as the explicit `deep=True` rung and the UI must label it so.
4. **Trial fps are 1-second micro-benchmarks**: right for *ranking*
   encoders, not predictions of throughput on real media.
5. qpdf/pandoc are absent on the dev bench; their ladder behaviour is
   proven by simulation tests, real-engine coverage lands in P5 CI matrix.
