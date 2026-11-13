"""PDF engine: LOSSLESS-only ladder pikepdf -> qpdf -> ghostscript (pdfsqueeze precedent).

No image is ever re-encoded or downsampled. Every rung is validated BEFORE
any replace (F24): page count identical, per-page MediaBox/CropBox identical,
pdftoppm render pixel-identical (page 1 by default, all pages with deep=True).
If pixel verification is impossible (pdftoppm missing) the original is KEPT -
verify-or-keep, never verify-or-shrug. Min savings 1% or keep original.
"""
from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

from .base import Availability, EngineResult, run_command
from ..probe.engines import EngineManager


def _pike():
    try:
        import pikepdf  # noqa: F401
        return pikepdf
    except Exception:  # noqa: BLE001
        return None


class PdfEngine:
    category = "pdf"
    name = "pdf-lossless-ladder"

    def __init__(self, engines: EngineManager | None = None):
        self.engines = engines or EngineManager()

    # ------------------------------------------------------------- ladder
    def ladder(self) -> list[tuple[str, bool, str]]:
        """Ordered (method, available, why-unavailable) - from LIVE detection."""
        rungs: list[tuple[str, bool, str]] = []
        rungs.append(("pikepdf", _pike() is not None,
                      "" if _pike() else "pikepdf not importable"))
        info = self.engines.detect()
        rungs.append(("qpdf", info["qpdf"].status == "ok",
                      info["qpdf"].install_hint if info["qpdf"].status != "ok" else ""))
        rungs.append(("ghostscript-lossless", info["gs"].status == "ok",
                      info["gs"].install_hint if info["gs"].status != "ok" else ""))
        return rungs

    def availability(self) -> Availability:
        usable = [r for r in self.ladder() if r[1]]
        if not usable:
            hints = {os_key: "\n".join(
                f"{n}: {self.engines.get(n).install_hint}" for n in ("qpdf", "gs"))
                for os_key in ("linux", "windows")}
            return Availability(False, "no PDF compression engine found", hints)
        pdftoppm_ok = self.engines.is_available("pdftoppm")
        note = "" if pdftoppm_ok else "pdftoppm missing: pixel verify impossible, files will be kept"
        return Availability(True, reason=note or
                            "methods: " + ", ".join(r[0] for r in usable),
                            info={"methods": [r[0] for r in usable],
                                  "pixel_verify": pdftoppm_ok})

    # ------------------------------------------------------------- convert
    def convert(self, src: Path, dst: Path, *, deep: bool = False,
                min_savings_pct: float = 1.0, timeout_s: float = 3600.0,
                governor=None, log_id: str | None = None,
                log_root: Path | None = None) -> EngineResult:
        t0 = time.monotonic()
        src, dst = Path(src), Path(dst)
        avail = self.availability()
        if not avail.ok:
            return EngineResult("failed", reason=avail.reason,
                                data={"engine_unavailable": True})
        if not src.is_file():
            return EngineResult("failed", reason="source file missing")

        log_path = None
        if log_root is not None and log_id:
            from ..jsonl import JobLogStore
            log_path = JobLogStore(log_root).open_capture(log_id)

        tried: list[str] = []
        for method, ok, why in self.ladder():
            if not ok:
                tried.append(f"{method}: unavailable ({why})")
                continue
            dst.unlink(missing_ok=True)
            err = self._run(method, src, dst, timeout_s=timeout_s,
                            governor=governor, log_path=log_path)
            if err:
                tried.append(f"{method}: {err}")
                dst.unlink(missing_ok=True)
                continue
            src_size = src.stat().st_size
            out_size = dst.stat().st_size if dst.is_file() else 0
            if out_size == 0:
                tried.append(f"{method}: produced empty output")
                dst.unlink(missing_ok=True)
                continue
            if out_size >= src_size * (1.0 - min_savings_pct / 100.0):
                dst.unlink(missing_ok=True)
                return EngineResult("kept", reason="already optimal "
                                    f"({out_size} >= {src_size} - {min_savings_pct:g}%)",
                                    seconds=time.monotonic() - t0, encoder=method,
                                    log_path=log_path,
                                    data={"src_size": src_size, "out_size": out_size,
                                          "tried": tried})
            return EngineResult("converted", output=dst, encoder=method,
                                seconds=time.monotonic() - t0, log_path=log_path,
                                data={"src_size": src_size, "out_size": out_size})
        return EngineResult("failed", reason="no method succeeded: " + " | ".join(tried),
                            seconds=time.monotonic() - t0, log_path=log_path)

    def _run(self, method: str, src: Path, dst: Path, *, timeout_s: float,
             governor, log_path) -> str | None:
        """Return None on success or an error string. Never raises."""
        if method == "pikepdf":
            pike = _pike()
            try:
                pdf = pike.open(src)
                pdf.save(dst, compress_streams=True, recompress_flate=True,
                         object_stream_mode=pike.ObjectStreamMode.generate)
            except Exception as exc:  # noqa: BLE001
                return f"pikepdf: {type(exc).__name__}: {exc}"
            return None
        if method == "qpdf":
            qpdf = self.engines.path("qpdf")
            res = run_command([qpdf, "--object-streams=generate",
                               "--compress-streams=y", "--compression-level=9",
                               src, dst], timeout_s=timeout_s, log_path=log_path,
                              governor=governor)
            return None if res.ok else ("timed out" if res.timed_out
                                        else _tail(res.stderr_text) or f"qpdf exit {res.returncode}")
        if method == "ghostscript-lossless":
            gs = self.engines.path("gs")
            res = run_command([gs, "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
                               "-sDEVICE=pdfwrite", "-dPDFSETTINGS=/prepress",
                               "-dDownsampleColorImages=false",
                               "-dDownsampleGrayImages=false",
                               "-dDownsampleMonoImages=false",
                               "-dCompatibilityLevel=1.4",
                               f"-sOutputFile={dst}", src],
                              timeout_s=timeout_s, log_path=log_path, governor=governor)
            return None if res.ok else ("timed out" if res.timed_out
                                        else _tail(res.stderr_text) or f"gs exit {res.returncode}")
        return f"unknown method {method}"

    # ------------------------------------------------------------- verify (F24)
    def verify(self, src: Path, out: Path, *, deep: bool = False, dpi: int = 100) -> EngineResult:
        """Structure + pixel-identity, working dir next to `out` (workspace-safe)."""
        src, out = Path(src), Path(out)
        structural = self._verify_structure(src, out)
        if structural is not None:
            return structural
        if not self.engines.is_available("pdftoppm"):
            return EngineResult("failed",
                                reason="pdftoppm missing: cannot prove pixel-identity - "
                                       "keeping original (verify-or-keep)")
        try:
            sh = self._page_hashes(src, deep=deep, dpi=dpi)
            dh = self._page_hashes(out, deep=deep, dpi=dpi)
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"pixel verification error: {exc}")
        if sh != dh:
            bad = [i + 1 for i, (a, b) in enumerate(zip(sh, dh)) if a != b]
            return EngineResult("failed",
                                reason=f"pixel mismatch on page(s) {bad[:5]}")
        return EngineResult("converted", output=out,
                            data={"verified": True, "pages": len(dh),
                                  "pixel_verify": "deep" if deep else "page-1"})

    def _verify_structure(self, src: Path, out: Path) -> EngineResult | None:
        """Return None when structure matches, else a failed EngineResult."""
        pike = _pike()
        if pike is not None:
            try:
                def boxes(p: Path):
                    pdf = pike.open(p)
                    sizes = []
                    for page in pdf.pages:
                        box = page.get("/MediaBox") or page.get("/CropBox")
                        sizes.append(tuple(round(float(v), 2) for v in box))
                    return len(pdf.pages), sizes
                (np_, sp), (nd_, sd) = boxes(src), boxes(out)
            except Exception as exc:  # noqa: BLE001
                return EngineResult("failed", reason=f"output unreadable: {type(exc).__name__}: {exc}")
            if np_ != nd_:
                return EngineResult("failed", reason=f"page count changed {np_} -> {nd_}")
            if sp != sd:
                return EngineResult("failed", reason="page boxes changed")
            return None
        head = out.read_bytes()[:5] if out.is_file() else b""
        if head != b"%PDF-":
            return EngineResult("failed", reason="output is not a PDF")
        return None  # no pikepdf: pixel hashes still guard pages

    def _page_hashes(self, path: Path, *, deep: bool, dpi: int) -> list[str]:
        pdftoppm = self.engines.path("pdftoppm")
        with tempfile.TemporaryDirectory(prefix="transmute-pdf-", dir=str(path.parent)) as td:
            cmd = [pdftoppm, "-png", "-r", str(dpi)]
            if not deep:
                cmd += ["-f", "1", "-l", "1"]
            cmd += [str(path), str(Path(td) / "pg")]
            res = run_command(cmd, timeout_s=3600)
            if not res.ok:
                raise RuntimeError(_tail(res.stderr_text) or "pdftoppm failed")
            hashes = []
            for f in sorted(Path(td).glob("pg*.png")):
                h = hashlib.sha256()
                with open(f, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                hashes.append(h.hexdigest())
            if not hashes:
                raise RuntimeError("pdftoppm produced no pages")
            return hashes


def _tail(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return (" | ".join(lines[-3:]))[-limit:]
