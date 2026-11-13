"""Document engine: office/markup -> PDF via LibreOffice headless (F1 category).

- LibreOffice runs headless with an ISOLATED -env:UserInstallation profile so
  concurrent conversions never fight over a user's real LO profile.
- pandoc is a fallback for text/markup sources and only counts as available
  when a PDF writer (pdflatex/xelatex/…) is actually present - no pretending.
- Verification: output must be a real %PDF that pikepdf can open with >=1 page.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import Availability, EngineResult, run_command
from ..probe.engines import EngineManager

LO_FORMAT = {".docx": "docx", ".doc": "doc", ".odt": "odt", ".rtf": "rtf",
             ".pptx": "pptx", ".ppt": "ppt", ".odp": "odp", ".xlsx": "xlsx",
             ".xls": "xls", ".ods": "ods", ".csv": "csv", ".epub": "epub"}
LATEX_WRITERS = ("pdflatex", "xelatex", "lualatex", "tectonic", "typst")


class DocumentEngine:
    category = "document"
    name = "document-libreoffice"

    def __init__(self, engines: EngineManager | None = None):
        self.engines = engines or EngineManager()

    def _latex_writer(self) -> str | None:
        return next((w for w in LATEX_WRITERS if shutil.which(w)), None)

    def availability(self) -> Availability:
        info = self.engines.detect()
        lo_ok = info["libreoffice"].status == "ok"
        pandoc_ok = info["pandoc"].status == "ok" and self._latex_writer() is not None
        if not lo_ok and not pandoc_ok:
            hints = {o: info["libreoffice"].install_hint for o in ("linux", "windows")}
            return Availability(False, "document->PDF needs LibreOffice (or pandoc + a LaTeX "
                                       "PDF writer, none found)", hints)
        why = []
        if not lo_ok:
            why.append("LibreOffice missing: using pandoc for text sources only")
        elif not pandoc_ok:
            why.append("pandoc/LaTeX missing: text markup converts via LibreOffice formats only")
        return Availability(True, reason="; ".join(why),
                            info={"libreoffice": lo_ok, "pandoc_latex": pandoc_ok})

    # ------------------------------------------------------------- convert
    def convert(self, src: Path, dst: Path, *, timeout_s: float = 600.0,
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

        ext = src.suffix.lower()
        if ext in LO_FORMAT and self.engines.is_available("libreoffice"):
            return self._via_libreoffice(src, dst, timeout_s=timeout_s,
                                         governor=governor, log_path=log_path, t0=t0)
        writer = self._latex_writer()
        if self.engines.is_available("pandoc") and writer:
            return self._via_pandoc(src, dst, writer, timeout_s=timeout_s,
                                    governor=governor, log_path=log_path, t0=t0)
        if ext in LO_FORMAT:
            return EngineResult("failed", reason=f"{ext} conversion needs LibreOffice "
                                                 f"(missing)", data={"engine_unavailable": True})
        return EngineResult("failed",
                            reason=f"no converter for '{ext}' on this machine "
                                   f"(LibreOffice formats and pandoc+LaTeX only)")

    def _via_libreoffice(self, src: Path, dst: Path, *, timeout_s: float,
                         governor, log_path, t0: float) -> EngineResult:
        lo = self.engines.path("libreoffice")
        assert lo is not None
        with tempfile.TemporaryDirectory(prefix="transmute-lo-", dir=str(dst.parent)) as td:
            outdir = Path(td) / "out"
            profile = Path(td) / "profile"
            res = run_command([lo, "--headless", "--norestore", "--nologo",
                               f"-env:UserInstallation=file://{profile}",
                               "--convert-to", "pdf", "--outdir", outdir, src],
                              timeout_s=timeout_s, log_path=log_path, governor=governor)
            produced = next(iter(sorted(outdir.glob("*.pdf"))), None)
            if produced is None or not res.ok:
                dst.unlink(missing_ok=True)
                reason = ("timed out" if res.timed_out else
                          _tail(res.stderr_text) or "LibreOffice produced no PDF")
                return EngineResult("failed", reason=f"LibreOffice: {reason}",
                                    seconds=time.monotonic() - t0, log_path=log_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), dst)
        return EngineResult("converted", output=dst, encoder="libreoffice",
                            seconds=time.monotonic() - t0, log_path=log_path,
                            data={"out_size": dst.stat().st_size})

    def _via_pandoc(self, src: Path, dst: Path, writer: str, *, timeout_s: float,
                    governor, log_path, t0: float) -> EngineResult:
        pandoc = self.engines.path("pandoc")
        res = run_command([pandoc, str(src), "-o", dst, f"--pdf-engine={writer}"],
                          timeout_s=timeout_s, log_path=log_path, governor=governor)
        if not res.ok or not dst.is_file() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            reason = ("timed out" if res.timed_out else
                      _tail(res.stderr_text) or "pandoc produced no PDF")
            return EngineResult("failed", reason=f"pandoc: {reason}",
                                seconds=time.monotonic() - t0, log_path=log_path)
        return EngineResult("converted", output=dst, encoder=f"pandoc+{writer}",
                            seconds=time.monotonic() - t0, log_path=log_path)

    # ------------------------------------------------------------- verify (F24)
    def verify(self, src: Path, out: Path) -> EngineResult:
        out = Path(out)
        if not out.is_file() or out.read_bytes()[:5] != b"%PDF-":
            return EngineResult("failed", reason="output is not a PDF")
        try:
            import pikepdf
            with pikepdf.open(out) as pdf:
                pages = len(pdf.pages)
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"output PDF unreadable: {type(exc).__name__}: {exc}")
        if pages < 1:
            return EngineResult("failed", reason="output PDF has zero pages")
        return EngineResult("converted", output=out,
                            data={"verified": True, "pages": pages})


def _tail(text: str, limit: int = 300) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return (" | ".join(lines[-3:]))[-limit:]
