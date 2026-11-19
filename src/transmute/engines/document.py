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

# --- A2 exports (transform, NOT the document->PDF path) -----------------
# pandoc readers by source suffix (explicit -f: pandoc cannot guess .txt etc)
PANDOC_READER = {".md": "markdown", ".markdown": "markdown", ".txt": "plain",
                 ".html": "html", ".htm": "html", ".rst": "rst", ".epub": "epub"}
# pandoc writers by target id
PANDOC_WRITER = {"docx": "docx", "odt": "odt", "rtf": "rtf", "html": "html",
                 "markdown": "markdown", "txt": "plain"}
# LibreOffice export filters by target id
LO_OUT = {"docx": "docx", "odt": "odt", "rtf": "rtf", "html": "html",
          "txt": "txt:Text (encoded):UTF8"}
DOC_TARGETS = ("docx", "odt", "rtf", "html", "markdown", "txt")
# format magics checked BEFORE accepting an export (same doctrine as PDF A1)
_OUT_MAGIC = {"docx": (b"PK\x03\x04",), "odt": (b"PK\x03\x04",),
              "rtf": (b"{\\rtf",), "html": (b"<",)}
_TARGET_EXT = {".md": ".md", ".markdown": ".md", ".txt": ".txt", ".text": ".txt",
               ".htm": ".html"}
# what each export target's own extension is (same-format detection)
_TARGET_AS_EXT = {"markdown": ".md", "txt": ".txt", "html": ".html"}
# sources that MUST be ZIP containers - LibreOffice would happily "convert"
# garbage bytes via its text-import fallback, so the engine checks first.
_ZIP_SOURCES = {".docx", ".odt", ".odp", ".xlsx", ".pptx", ".epub"}


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
    def convert(self, src: Path, dst: Path, *, target: str | None = None,
                timeout_s: float = 600.0,
                governor=None, log_id: str | None = None,
                log_root: Path | None = None) -> EngineResult:
        t0 = time.monotonic()
        src, dst = Path(src), Path(dst)
        if not src.is_file():
            return EngineResult("failed", reason="source file missing")
        # A2 exports are transforms: they never touch the document->PDF path,
        # and they self-verify (clean exit + format magic) inside convert().
        if target and target != "pdf":
            return self._export(src, dst, target, timeout_s=timeout_s,
                                governor=governor, log_id=log_id,
                                log_root=log_root, t0=t0)
        avail = self.availability()
        if not avail.ok:
            return EngineResult("failed", reason=avail.reason,
                                data={"engine_unavailable": True})
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
            # P6: as_uri() is byte-identical to the old f"file://{profile}" on
            # Linux and produces file:///C:/... on Windows (a raw backslash
            # path after file:// parses as host "C" there - LO then ignores
            # the isolated profile and fights the user's real one).
            res = run_command([lo, "--headless", "--norestore", "--nologo",
                               f"-env:UserInstallation={profile.as_uri()}",
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

    # ------------------------------------------------------------- exports (A2)
    def _export_log(self, log_id, log_root):
        if log_root is not None and log_id:
            from ..jsonl import JobLogStore
            return JobLogStore(log_root).open_capture(log_id)
        return None

    def _export(self, src: Path, dst: Path, target: str, *, timeout_s: float,
                governor, log_id, log_root, t0: float) -> EngineResult:
        if target not in DOC_TARGETS:
            return EngineResult("failed",
                                reason=f"unknown document target '{target}' "
                                       f"(supported: {', '.join(DOC_TARGETS)}, pdf)")
        ext = src.suffix.lower()
        ext_norm = _TARGET_EXT.get(ext, ext)
        if ext_norm == _TARGET_AS_EXT.get(target, "." + target):
            return EngineResult("skipped",
                                reason=f"this file is already {target.upper()}")
        if ext in _ZIP_SOURCES:
            # LibreOffice would "convert" garbage bytes through its text-import
            # fallback and call it success. A container that isn't a container
            # is the file's own problem - say so before spending a process.
            head = src.open("rb").read(4)
            if not head.startswith(b"PK\x03\x04"):
                return EngineResult("failed",
                                    reason=f"this {ext} file is not a valid "
                                           f"document container (corrupted or "
                                           f"renamed?)")
        log_path = self._export_log(log_id, log_root)
        lo_ok = self.engines.is_available("libreoffice")
        pan_ok = self.engines.is_available("pandoc")
        # Markup sources go to pandoc (clean writers); office sources go to
        # LibreOffice; everything else tries pandoc first (it reads Word/ODT/
        # RTF/epub natively) then LibreOffice. A tool's own error IS the
        # reason shown.
        if ext in PANDOC_READER and pan_ok:
            return self._pandoc_export(src, dst, target, PANDOC_READER[ext],
                                       timeout_s=timeout_s, governor=governor,
                                       log_path=log_path, t0=t0)
        if target == "markdown":
            # pandoc only; its native readers cover Word/ODT/RTF/epub.
            if not pan_ok:
                return EngineResult("failed", reason="Markdown output needs "
                                                     "pandoc (not installed)",
                                    data={"engine_unavailable": True})
            return self._pandoc_export(src, dst, target, None,
                                       timeout_s=timeout_s, governor=governor,
                                       log_path=log_path, t0=t0)
        if ext in LO_FORMAT and lo_ok:
            return self._lo_export(src, dst, target, timeout_s=timeout_s,
                                   governor=governor, log_path=log_path, t0=t0)
        if pan_ok:
            return self._pandoc_export(src, dst, target, None,
                                       timeout_s=timeout_s, governor=governor,
                                       log_path=log_path, t0=t0)
        if lo_ok:
            return self._lo_export(src, dst, target, timeout_s=timeout_s,
                                   governor=governor, log_path=log_path, t0=t0)
        return EngineResult("failed", reason="document conversion needs "
                                             "LibreOffice or pandoc (neither found)",
                            data={"engine_unavailable": True})

    @staticmethod
    def _invalid_output(path: Path, target: str) -> str | None:
        """Reason string when the produced file is not what it claims (None = fine)."""
        if not path.is_file() or path.stat().st_size == 0:
            return "produced no content"
        magic = _OUT_MAGIC.get(target)
        if magic and not path.read_bytes()[:8].startswith(magic):
            return f"output is not a valid {target.upper()} file"
        mags = tuple(m for m in (magic if isinstance(magic, tuple) else (magic,))
                     if m)  # txt etc.: magic None or a prefix set, guard both
        if any(m.startswith(b"PK") for m in mags):
            # A zip container passing the magic sniff can still be a
            # truncated half-write: open it and test every entry, or a
            # corrupt file is reported as a successful conversion.
            import zipfile
            try:
                with zipfile.ZipFile(path) as zf:
                    if zf.testzip() is not None:
                        return f"{target.upper()} archive is corrupt"
            except (zipfile.BadZipFile, OSError):
                return f"output is not a valid {target.upper()} file"
        return None

    def _pandoc_export(self, src: Path, dst: Path, target: str,
                       reader: str | None, *, timeout_s: float, governor,
                       log_path, t0: float) -> EngineResult:
        pandoc = self.engines.path("pandoc")
        cmd = [pandoc, str(src)]
        if reader:
            cmd += ["-f", reader]
        writer = PANDOC_WRITER[target]
        cmd += ["-t", writer]
        if target in ("rtf", "html"):
            cmd += ["-s"]            # document targets need the full wrapper:
        if target == "txt":          # the bare writers emit fragments
            cmd += ["--wrap=none"]      # text means text: no artificial margins
        cmd += ["-o", str(dst)]
        res = run_command(cmd, timeout_s=timeout_s, log_path=log_path,
                          governor=governor)
        # Exit status is authority: a killed or failed pandoc can leave an
        # output file that sniffs valid (truncated .txt passes any header
        # check), and declaring success from the sniff alone would certify
        # garbage - verified_by_engine then skips the queue's own verify.
        if res.timed_out:
            bad = "timed out"
        elif not res.ok:
            bad = _tail(res.stderr_text) or f"exit {res.returncode}"
        else:
            bad = self._invalid_output(dst, target)
        if bad:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason=f"pandoc: {bad}",
                                seconds=time.monotonic() - t0, log_path=log_path)
        return EngineResult("converted", output=dst, encoder="pandoc",
                            seconds=time.monotonic() - t0, log_path=log_path,
                            data={"verified_by_engine": True,
                                  "out_size": dst.stat().st_size})

    def _lo_export(self, src: Path, dst: Path, target: str, *, timeout_s: float,
                   governor, log_path, t0: float) -> EngineResult:
        lo = self.engines.path("libreoffice")
        assert lo is not None
        with tempfile.TemporaryDirectory(prefix="transmute-lo-",
                                         dir=str(dst.parent)) as td:
            outdir = Path(td) / "out"
            profile = Path(td) / "profile"
            res = run_command([lo, "--headless", "--norestore", "--nologo",
                               f"-env:UserInstallation={profile.as_uri()}",
                               "--convert-to", LO_OUT[target],
                               "--outdir", str(outdir), str(src)],
                              timeout_s=timeout_s, log_path=log_path,
                              governor=governor)
            produced = next(iter(sorted(outdir.glob("*.*"))), None)
            if produced is not None and res.timed_out:
                # LibreOffice can be killed while writing its output: a
                # half-written file that sniffs valid is still not a
                # conversion (exit code is unreliable for LO, so timeout -
                # not exit status - is what we refuse on; a finished LO run
                # that exited nonzero but produced a sound container per
                # _invalid_output, zip entries included, stays accepted)
                bad = "timed out mid-write: a half-written file is not a conversion"
            elif produced is not None:
                bad = self._invalid_output(produced, target)
            else:
                bad = ("timed out" if res.timed_out
                       else _tail(res.stderr_text) or "LibreOffice produced no file")
            if bad:
                return EngineResult("failed", reason=f"LibreOffice: {bad}",
                                    seconds=time.monotonic() - t0,
                                    log_path=log_path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), dst)
        return EngineResult("converted", output=dst, encoder="libreoffice",
                            seconds=time.monotonic() - t0, log_path=log_path,
                            data={"verified_by_engine": True,
                                  "out_size": dst.stat().st_size})

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
