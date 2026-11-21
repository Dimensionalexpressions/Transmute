"""--smoke: exercise the shipped app end to end, then exit with a verdict.

This is a release gate, not a user feature. CI runs the FROZEN build with
``--smoke`` on both operating systems before a zip/tarball is packed, so the
exact things a user does on a first launch - engine detection (a real
LibreOffice subprocess: the 2026-11 laptops proved a probe that pops a
console window or stalls is a visible, infuriating bug), serving files (the
launcher + an HTTP round trip), and the governor - must work in the very
binary we are about to publish. If they do not, the release fails and no
asset is uploaded.

No Qt, no windows: safe to run headless on any CI VM.

Exit codes: 0 = SMOKE OK. 1 = a step failed (its words are printed).
"""
from __future__ import annotations

import os
import sys
import time


def _log_lines(lines: list[str]) -> None:
    """Print AND keep a file copy: a windowed frozen exe has no console,
    so CI reads transmute-smoke.log next to the executable."""
    text = "\n".join(lines)
    try:  # stdout exists when run from a terminal / source install
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - windowed builds: stdout is None
        pass
    try:
        if getattr(sys, "frozen", False):
            path = os.path.join(os.path.dirname(sys.executable),
                                "transmute-smoke.log")
        else:
            path = os.path.join(os.getcwd(), "transmute-smoke.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def _watchdog(seconds: int = 300) -> None:
    # A hung probe (a LibreOffice waiting for Enter would hang 25s PER
    # candidate; several of those stack past CI patience) must fail loud
    # and early with OUR words, not as an opaque runner cancellation.
    import threading

    def _boom() -> None:
        _log_lines([f"SMOKE FAIL: exceeded {seconds}s wall clock",
                    "a probe or server hung - look for the last line above"])
        os._exit(1)

    t = threading.Timer(seconds, _boom)
    t.daemon = True
    t.start()


def _check_engines(log: list[str]) -> None:
    from .probe.engines import EngineManager
    det = EngineManager().detect()
    for name in sorted(det):
        i = det[name]
        log.append(f"engine {name}: {i.status}"
                   + (f" {i.version}" if i.version else "")
                   + (f" ({i.reason})" if i.reason else ""))
    if det["ffmpeg"].status != "ok":
        raise RuntimeError(f"ffmpeg must be detected: {det['ffmpeg'].status} "
                           f"{det['ffmpeg'].reason}")
    # engines CI has definitely installed MUST be ok - a MISSING/ERROR here
    # means the probe itself is broken on this OS (that is how the soffice
    # console-window bug would have been caught before shipping).
    for name in (os.environ.get("TRANSMUTE_SMOKE_ENGINES", "")
                 .replace(",", " ").split()):
        if det[name].status != "ok":
            raise RuntimeError(
                f"{name} is installed in this VM but the probe says "
                f"{det[name].status}: {det[name].reason or 'no reason given'}")


def _check_serve(log: list[str]) -> None:
    # exactly what the GUI's serve dialog drives: same launcher, same
    # bind, same HTTP surface. (v0.2.5 laptops: toggling serve appeared
    # stuck on off while a probe popup held the machine.)
    import urllib.error
    import urllib.request
    from .serve.launcher import ServeLauncher
    ln = ServeLauncher()
    st = ln.start("local")
    try:
        if not ln.running or ln.mode != "local":
            raise RuntimeError(f"serve did not start: {st}")
        url = st.get("url") or ""
        if not url:
            raise RuntimeError(f"serve started with no url: {st}")
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:  # a real reply is fine
            code = exc.code
        if code >= 500:
            raise RuntimeError(f"serve replied {code} on {url}")
        log.append(f"serve: replied {code} on {url}")
    finally:
        ln.stop()
    if ln.running:
        raise RuntimeError("serve would not stop cleanly")


def _check_governor(log: list[str]) -> None:
    # start/stop must be safe on this OS even when the governor is a
    # passive monitor (or disabled via TRANSMUTE_GOVERNOR=0)
    from .governor import make_governor
    gov = make_governor()
    gov.start()
    gov.stop()
    log.append("governor: start/stop ok")


def run_smoke() -> int:
    _watchdog()
    log: list[str] = []
    t0 = time.time()
    steps = (("engines", _check_engines), ("serve", _check_serve),
             ("governor", _check_governor))
    for label, step in steps:
        try:
            step(log)
        except Exception as exc:  # noqa: BLE001 - the verdict is the point
            log.append(f"SMOKE FAIL at {label}: {exc}")
            _log_lines(log)
            return 1
    log.append(f"SMOKE OK in {time.time() - t0:.1f}s")
    _log_lines(log)
    return 0


if __name__ == "__main__":  # python -m transmute.selfcheck
    sys.exit(run_smoke())
