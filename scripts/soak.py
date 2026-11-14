#!/usr/bin/env python3
"""P5 long-file soak: minutes of real engine work through the queue, with
resource sampling every 5s. This is a measurement, not a test: it prints and
writes a report with REAL numbers (RSS / fds / threads / journal growth /
staging leftovers) and exits non-zero if the machine-visible growth breaches
the leak budget.

Usage:  python scripts/soak.py --minutes 8 --report docs/reports/soak.md
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
SAMPLE_S = 5.0
# Leak budgets (measured, not guessed): growth AFTER the first 2 minutes of
# warm-up. Generous on purpose - the point is proving "no upward trend",
# a few MiB of allocator slack is not a leak.
BUDGET_FD_GROWTH = 3
BUDGET_RSS_GROWTH_KIB = 65536


def emit(msg: str) -> None:
    print(msg, flush=True)  # pipe-buffered liveness lesson from P4 watch CLI


def gen_media(src_dir: Path, clip_s: int, clips: int = 4) -> dict:
    src_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[Path]] = {}
    from PIL import Image
    paths = []
    for i in range(clips):
        p = src_dir / f"long{i}.mp4"
        subprocess.run(FF + ["-f", "lavfi", "-i",
                             f"testsrc2=size=1280x720:rate=25:duration={clip_s}",
                             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                             str(p)], check=True)
        paths.append(p)
    out["video"] = paths
    emit(f"generated {len(paths)} x {clip_s}s 720p clips "
         f"({paths[0].stat().st_size / 1e6:.1f} MiB each)")

    auds = []
    for i in range(3):
        p = src_dir / f"sine{i}.mp3"
        subprocess.run(FF + ["-f", "lavfi", "-i",
                             f"sine=frequency=440:duration={max(clip_s, 300)}",
                             "-c:a", "libmp3lame", "-b:a", "192k", str(p)],
                       check=True)
        auds.append(p)
    out["audio"] = auds

    imgs = []
    for i in range(8):
        p = src_dir / f"noise{i}.jpg"
        Image.effect_noise((2400, 1600), 120).convert("RGB").save(p, quality=92)
        imgs.append(p)
    out["image"] = imgs

    pages = [Image.effect_noise((1200, 1600), 150).convert("RGB")
             for _ in range(6)]
    pdf = src_dir / "big.pdf"
    pages[0].save(pdf, format="PDF", save_all=True, append_images=pages[1:])
    out["pdf"] = [pdf]
    return out


def sample(proc) -> dict:
    return {"t": time.monotonic(), "rss_kib": proc.memory_info().rss // 1024,
            "fds": proc.num_fds(), "threads": proc.num_threads()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=8.0)
    ap.add_argument("--clip-seconds", type=int, default=240)
    ap.add_argument("--work", default=None, help="work dir (default: tmp)")
    ap.add_argument("--report", default="docs/reports/soak.md")
    args = ap.parse_args()

    import psutil
    from transmute.paths import AppPaths, set_paths
    from transmute.runner.queue import JobQueue

    work = Path(args.work) if args.work else Path("/tmp/transmute-soak")
    work.mkdir(parents=True, exist_ok=True)
    set_paths(AppPaths(root=work / "app-root"))
    media = gen_media(work / "media", args.clip_seconds)

    dst_root = work / "out"
    dst_root.mkdir(exist_ok=True)
    jq = JobQueue()  # production defaults: real engines, app-root journal/events

    jobs = 0
    for p in media["video"]:
        for opts in ({"codec": "av1", "encoder": "libaom-av1"},
                     {"codec": "h264", "crf": 28}):
            jq.add(p, dst_root / (p.stem + ("_av1.mkv" if "av1" in str(opts)
                                            else "_h264.mp4")), options=opts)
            jobs += 1
    for p in media["audio"]:
        jq.add(p, dst_root / (p.stem + ".opus"), options={"codec": "opus"})
        jobs += 1
    for p in media["image"]:
        jq.add(p, dst_root / (p.stem + ".webp"), options={"quality": 80})
        jobs += 1
    for p in media["pdf"]:
        jq.add(p, dst_root / (p.stem + ".slim.pdf"))
        jobs += 1
    emit(f"enqueued {jobs} jobs (video encodes dominate the minutes)")

    proc = psutil.Process()
    samples = [sample(proc)]
    staging = dst_root / ".transmute-tmp"

    # pause/resume pulse mid-soak: exercises the P5 code path with real
    # in-flight engine work (in-flight jobs must NOT be interrupted).
    plan = {"pause_at": time.monotonic() + 45.0,
            "resume_at": time.monotonic() + 105.0}
    pulses = []

    import threading
    stop_watch = threading.Event()

    def watcher() -> None:
        while not stop_watch.wait(SAMPLE_S):
            samples.append(sample(proc))
            now = time.monotonic()
            if plan["pause_at"] and now >= plan["pause_at"]:
                plan["pause_at"] = None
                n = jq.pause_all()
                pulses.append(f"pause_all -> {n} paused at +{now - t0:.0f}s")
                emit(pulses[-1])
            if plan["resume_at"] and now >= plan["resume_at"]:
                plan["resume_at"] = None
                n = jq.resume_all()
                pulses.append(f"resume_all -> {n} resumed at +{now - t0:.0f}s")
                emit(pulses[-1])

    t0 = time.monotonic()
    wt = threading.Thread(target=watcher, daemon=True)
    wt.start()
    try:
        summary = jq.run(max_workers=4)
    finally:
        stop_watch.set()
        wt.join(timeout=2)
    wall = time.monotonic() - t0
    emit(f"drained in {wall / 60:.1f} min; summary={summary}")

    # ---- measurements (post-drain state matters most for leaks)
    final = sample(proc)
    samples.append(final)
    warm = next((s for s in samples if s["t"] - t0 >= 120.0), None)
    fd_growth = final["fds"] - (warm or samples[0])["fds"]
    rss_growth_kib = final["rss_kib"] - (warm or samples[0])["rss_kib"]
    leftover = sorted(p.name for p in staging.glob("*")) if staging.is_dir() else []
    states: dict[str, int] = {}
    for rec in jq.journal.records.values():
        states[rec.state] = states.get(rec.state, 0) + 1
    jlines = sum(1 for _ in open(jq.journal.path, encoding="utf-8"))
    nonterminal = {k: v for k, v in states.items()
                   if k not in ("replaced", "kept", "skipped", "abandoned")}

    ok = (fd_growth <= BUDGET_FD_GROWTH and rss_growth_kib <= BUDGET_RSS_GROWTH_KIB
          and not leftover and not nonterminal)
    report = "\n".join([
        "# Transmute soak report",
        "",
        f"- run: UTC {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}, "
        f"wall {wall / 60:.1f} min, {jobs} jobs, max_workers=4, "
        f"samples={len(samples)} @ {SAMPLE_S:.0f}s",
        f"- machine: {shutil.which('ffmpeg')}, python {sys.version.split()[0]}",
        f"- RESULT: {'PASS' if ok else 'FAIL'} (budgets: fd +{BUDGET_FD_GROWTH}, "
        f"RSS +{BUDGET_RSS_GROWTH_KIB} KiB after 2 min warm; no staging leftovers; "
        "no non-terminal records after drain)",
        "",
        f"- RSS: start {samples[0]['rss_kib'] // 1024} MiB -> final "
        f"{final['rss_kib'] // 1024} MiB (growth after warm-up "
        f"{rss_growth_kib / 1024:.1f} MiB); peak "
        f"{max(s['rss_kib'] for s in samples) // 1024} MiB",
        f"- fds: start {samples[0]['fds']} -> final {final['fds']} "
        f"(peak {max(s['fds'] for s in samples)}, growth after warm-up "
        f"{fd_growth})",
        f"- threads: start {samples[0]['threads']} -> final {final['threads']} "
        f"(peak {max(s['threads'] for s in samples)})",
        f"- journal lines: {jlines} (records={len(jq.journal.records)}); "
        f"states={states}",
        f"- staging leftovers: {leftover or 'none'}",
        f"- pulses: {pulses or 'none'}",
        "",
        "Interpretation: fd/thread counts returning to baseline after drain and",
        "flat RSS across the tail = no per-job leak. Journal growth is bounded",
        "by jobs x transitions, not by elapsed soak time.",
    ])
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report + "\n", encoding="utf-8")
    emit(report)
    emit(f"report written: {rp.resolve()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
