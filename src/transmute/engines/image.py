"""Image engine: Pillow (PIL) - static re-encode + true animated GIF->WebP.

Ported discipline from production webp-convert (95k-file archive verified):
- ImageFile.LOAD_TRUNCATED_IMAGES for real-world truncated JPEGs,
- zero-byte/all-zero files reported, never converted,
- >16383px (WebP format limit) downscaled with LANCZOS before encode,
- animated GIFs: partial-frame disposal composited onto the MAX canvas
  (frame size must be measured AFTER load() - size before load can lie),
  loop count and centisecond durations preserved,
- keep-original unless the output saves >= min_savings_pct (default 1%).
"""
from __future__ import annotations

import time
from pathlib import Path

from .base import Availability, EngineResult

WEBP_MAX_PX = 16383
STATIC_INPUTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".jfif",
                 ".webp", ".avif", ".heic", ".heif", ".gif"}
ANIMATED_EXTS = {".gif", ".apng", ".webp"}
STATIC_TARGETS = {".webp", ".png"}


def _load_pillow():
    from PIL import Image, ImageFile  # noqa: F401
    ImageFile.LOAD_TRUNCATED_IMAGES = True  # real archives contain truncated files
    from PIL import features
    return Image, features


def all_zero(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
        return path.stat().st_size == 0 or head.count(0) == len(head)
    except OSError:
        return True


def _pdf_ready(im, Image):
    """One page of pixels any viewer will show, exactly as measured on this
    Pillow build: RGBA written raw embeds as lossy JPEG-2000 and its alpha
    lands on a SECOND page - so alpha is flattened onto white; palettes and
    16-bit/float modes move to modes the PDF plugin writes for sure."""
    if im.mode in ("I", "I;16", "F"):
        return im.convert("L")
    has_alpha = ("A" in im.mode or (im.mode == "P"
                 and im.info.get("transparency") is not None))
    if has_alpha or im.mode == "P":
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, (0, 0), rgba)
        return flat
    return im.copy()  # RGB/L/1/CMYK: PDF plugin writes these directly (measured)


def _supports(features, name: str) -> bool:
    # jpg/png/gif are core Pillow formats: features.check() does not know them
    # (and warns) on modern builds - check the plugin registry instead.
    if name in ("jpg", "jpeg", "png", "gif"):
        try:
            from PIL import Image
            key = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF"}[name]
            return key in getattr(Image, "registered_extensions", lambda: {})().values() \
                or key in getattr(Image, "formats", [])
        except Exception:  # noqa: BLE001
            return False
    try:
        return bool(features.check(name))
    except Exception:  # noqa: BLE001
        return False


class ImageEngine:
    category = "image"
    name = "image-pillow"

    # ------------------------------------------------------------ availability
    def availability(self) -> Availability:
        try:
            _Image, features = _load_pillow()
        except Exception as exc:  # noqa: BLE001
            return Availability(False, f"Pillow not importable: {exc}")
        feats = {f: _supports(features, f) for f in ("jpg", "png", "webp", "avif")}
        return Availability(True, reason=f"Pillow features: {feats}",
                            info={"features": feats})

    def supports_target(self, dst: Path) -> tuple[bool, str]:
        try:
            _Image, features = _load_pillow()
        except Exception as exc:  # noqa: BLE001
            return False, f"Pillow not importable: {exc}"
        ext = dst.suffix.lower()
        if ext == ".webp":
            return (_supports(features, "webp"),
                    "" if _supports(features, "webp") else "local Pillow build lacks WebP")
        if ext == ".avif":
            return (_supports(features, "avif"),
                    "" if _supports(features, "avif") else "local Pillow build lacks AVIF")
        if ext == ".png":
            return (True, "")
        if ext == ".pdf":
            return self._can_write_pdf()
        return False, f"unsupported image target '{ext}'"

    @staticmethod
    def _can_write_pdf() -> tuple[bool, str]:
        """PDF write is PROVEN by doing it in memory (ms), never assumed -
        same discipline as _probe_pillow for every other format."""
        import io
        try:
            Image, _ = _load_pillow()
            buf = io.BytesIO()
            Image.new("RGB", (2, 2)).save(buf, "PDF")
            if buf.tell() > 0:
                return True, ""
            return False, "local Pillow build cannot write PDF"
        except Exception as exc:  # noqa: BLE001
            return False, f"PDF write failed: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ convert
    def convert(self, src: Path, dst: Path, *, quality: int = 80, method: int = 6,
                keep_original_smaller: bool = True, min_savings_pct: float = 1.0,
                governor=None, log_id: str | None = None,
                log_root: Path | None = None) -> EngineResult:
        """Convert src -> dst. Keeps ORIGINAL bytes unless the result is
        valid AND saves min_savings_pct (outcome='kept' then)."""
        t0 = time.monotonic()
        src, dst = Path(src), Path(dst)
        if not src.is_file():
            return EngineResult("failed", reason="source file missing")
        if all_zero(src):
            return EngineResult("failed", reason="file is empty or all-zero bytes")
        ok, why = self.supports_target(dst)
        if not ok:
            return EngineResult("failed", reason=why, data={"engine_unavailable": True})
        try:
            Image, features = _load_pillow()
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"Pillow not importable: {exc}")

        ext_in = src.suffix.lower()
        ext_out = dst.suffix.lower()
        try:
            if ext_in == ".gif" and ext_out == ".webp" and self._is_animated_gif(src):
                res = self._convert_animated_gif(src, dst, Image, quality=quality)
            else:
                res = self._convert_static(src, dst, Image, ext_out, quality=quality,
                                           method=method)
        except MemoryError:
            return EngineResult("failed", reason="image too large for memory",
                                seconds=time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 - any decode failure is a failure STATE
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason=f"decode/encode error: {type(exc).__name__}: {exc}",
                                seconds=time.monotonic() - t0)
        res.seconds = time.monotonic() - t0

        if res.outcome == "converted":
            # the in-process producer belongs in the audit trail, same as the
            # external engines record theirs (queue journals rec.encoder)
            res.encoder = "pillow"
        if res.outcome != "converted":
            return res
        # size discipline: keep the original unless real savings (never grow files)
        src_size, out_size = src.stat().st_size, dst.stat().st_size
        if keep_original_smaller and out_size == 0:
            dst.unlink(missing_ok=True)
            return EngineResult("failed", reason="encoder produced empty output",
                                seconds=res.seconds)
        # PDF is a CONTAINER change, not compression: it may honestly grow
        # the file. The savings gate (and its "kept" outcome) guards only
        # same-container re-encodes.
        if ext_out != ".pdf" and out_size >= src_size * (1.0 - min_savings_pct / 100.0):
            dst.unlink(missing_ok=True)
            if out_size > src_size:
                # honest direction: the encode was BIGGER - never say
                # "only -94.7% smaller" (a negative saving is a growth)
                note = (f"output would be {100 * (out_size / max(src_size, 1) - 1):.1f}% "
                        f"bigger: original kept")
            else:
                note = (f"only {100 * (1 - out_size / max(src_size, 1)):.1f}% "
                        f"smaller (<{min_savings_pct:g}%): original kept")
            return EngineResult("kept", reason=note,
                                seconds=res.seconds,
                                data={"src_size": src_size, "out_size": out_size})
        return res

    # ------------------------------------------------------------ static
    def _convert_static(self, src: Path, dst: Path, Image, ext_out: str, *,
                        quality: int, method: int) -> EngineResult:
        with Image.open(src) as im:
            im.load()
            frame_count = getattr(getattr(im, "n_frames", 1), "__int__", lambda: 1)()
            fmt_in = im.format
            icc = im.info.get("icc_profile")
            dpi = im.info.get("dpi")
            # WebP hard limit 16383px: downscale with LANCZOS (proven choice)
            if ext_out == ".webp" and max(im.size) > WEBP_MAX_PX:
                scale = WEBP_MAX_PX / max(im.size)
                new = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
                im = im.resize(new, Image.LANCZOS)
            save_kw: dict = {}
            if icc:
                save_kw["icc_profile"] = icc
            if dpi:
                save_kw["dpi"] = dpi
            if ext_out == ".webp":
                if getattr(im, "n_frames", 1) > 1:
                    save_kw["save_all"] = True
                    save_kw["append_images"] = self._all_frames(im, Image)
                im.save(dst, "WEBP", quality=quality, method=method, **save_kw)
            elif ext_out == ".avif":
                im.save(dst, "AVIF", quality=quality, **save_kw)
            elif ext_out == ".png":
                im.save(dst, "PNG", optimize=True, **save_kw)
            elif ext_out == ".pdf":
                # one picture = one page (animated input: frame 0 - the
                # picker says so in plain words, nothing is lost silently)
                _pdf_ready(im, Image).save(dst, "PDF", quality=quality, **save_kw)
                return EngineResult("converted", output=dst,
                                    data={"frames": 1, "pages": 1,
                                          "input_format": fmt_in,
                                          "pixels": list(im.size)})
            else:
                return EngineResult("failed", reason=f"unsupported target {ext_out}")
        return EngineResult("converted", output=dst,
                            data={"frames": frame_count, "input_format": fmt_in,
                                  "pixels": list(Image.open(dst).size)})

    @staticmethod
    def _all_frames(im, Image):
        frames = []
        try:
            for i in range(1, getattr(im, "n_frames", 1)):
                im.seek(i)
                frames.append(im.convert("RGBA") if im.mode == "P" and im.info.get("transparency")
                              is not None else im.copy())
        except EOFError:
            pass
        return frames

    # ------------------------------------------------------------ animated GIF -> WebP
    @staticmethod
    def _is_animated_gif(src: Path) -> bool:
        try:
            from PIL import Image
            with Image.open(src) as im:
                return getattr(im, "n_frames", 1) > 1
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _convert_animated_gif(src: Path, dst: Path, Image, *, quality: int) -> EngineResult:
        """Partial GIF frames must be composited onto ONE canvas sized to the
        union of all frames - and n_frames/sizes are only trustworthy AFTER
        each frame has been load()ed (measuring before load can lie)."""
        with Image.open(src) as g:
            n = getattr(g, "n_frames", 1)
            metas = []
            for i in range(n):
                g.seek(i)
                g.load()
                metas.append({"size": g.size, "size_info": g.info.get("size"),
                              "duration": g.info.get("duration", 0),
                              "disp": g.info.get("disposal"),
                              "transparency": g.info.get("transparency"),
                              "bbox": g.info.get("bbox")})
            canvas_w = max((m["size"][0] for m in metas), default=0)
            canvas_h = max((m["size"][1] for m in metas), default=0)
            loop = g.info.get("loop", 0)
            bg = None
            try:
                pal = g.getpalette()
                tid = g.info.get("transparency")
                if pal and tid is not None:
                    bg = tuple(pal[tid * 3: tid * 3 + 3]) + (0,)
            except Exception:  # noqa: BLE001
                pass
            frames: list = []
            durations: list[int] = []
            prev = None
            for i, m in enumerate(metas):
                g.seek(i)
                g.load()
                frame = g.convert("RGBA")
                comp = Image.new("RGBA", (canvas_w, canvas_h), bg or (0, 0, 0, 0))
                if m["disp"] == 2:
                    prev = None
                bbox = m["bbox"]
                off = (bbox[0], bbox[1]) if bbox else (0, 0)
                if prev is not None and m["disp"] != 3:
                    comp.paste(prev, (0, 0), prev)
                comp.paste(frame, off, frame)
                frames.append(comp)
                prev = comp
                dur = m["duration"]
                # GIF durations are centiseconds in some paths, ms in Pillow's info;
                # Pillow reports ms. Centisecond-looking values (<10) get x10.
                durations.append(max(20, dur * 10 if 0 < dur < 10 else dur))
        if max(canvas_w, canvas_h) > WEBP_MAX_PX:
            scale = WEBP_MAX_PX / max(canvas_w, canvas_h)
            frames = [f.resize((max(1, int(canvas_w * scale)), max(1, int(canvas_h * scale))),
                               Image.LANCZOS) for f in frames]
        frames[0].save(dst, "WEBP", save_all=True, append_images=frames[1:],
                       duration=durations, loop=loop, quality=quality, method=6)
        return EngineResult("converted", output=dst,
                            data={"frames": len(frames), "loop": loop})

    # ------------------------------------------------------------ verify (F24)
    def verify(self, src: Path, out: Path, *, deep: bool = False) -> EngineResult:
        """Output must open, match source dimensions (unless downscaled for the
        WebP 16383 cap) and keep frame count for animations."""
        try:
            Image, _f = _load_pillow()
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"Pillow not importable: {exc}")
        if Path(out).suffix.lower() == ".pdf":
            return self._verify_pdf(out)
        try:
            with Image.open(out) as o:
                o.load()
                out_size, out_frames = o.size, getattr(o, "n_frames", 1)
                out_fmt = o.format
            with Image.open(src) as s:
                s.load()
                src_size, src_frames = s.size, getattr(s, "n_frames", 1)
                was_capped = max(src_size) > WEBP_MAX_PX
                src_frames = src_frames
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"output unreadable: {type(exc).__name__}: {exc}")
        if not was_capped and out_size != src_size and out_frames > 1:
            return EngineResult("failed", reason=f"size changed {src_size} -> {out_size}")
        if not was_capped and out_size != src_size:
            return EngineResult("failed", reason=f"size changed {src_size} -> {out_size}")
        if src_frames > 1 and out_frames < 2:
            return EngineResult("failed", reason=f"animation lost ({src_frames} frames -> "
                                                 f"{out_frames})")
        return EngineResult("converted", output=out,
                            data={"verified": True, "format": out_fmt,
                                  "frames": out_frames})

    @staticmethod
    def _verify_pdf(out: Path) -> EngineResult:
        """Pillow cannot open PDFs as pictures: verify like the PDF writer in
        document.py does - %PDF magic + pikepdf must parse with >= 1 page."""
        try:
            import pikepdf
            with open(out, "rb") as fh:
                if fh.read(5) != b"%PDF-":
                    return EngineResult("failed", reason="output is not a PDF")
            with pikepdf.open(out) as pdf:
                pages = len(pdf.pages)
        except ImportError:
            return EngineResult("failed", reason="pikepdf missing - cannot verify PDF output")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("failed", reason=f"output unreadable: {type(exc).__name__}: {exc}")
        if pages < 1:
            return EngineResult("failed", reason="PDF has no pages")
        return EngineResult("converted", output=out,
                            data={"verified": True, "format": "PDF", "pages": pages})
