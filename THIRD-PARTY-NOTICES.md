# Third-Party Notices, Licenses & Thanks

Transmute converts files by driving **external programs that run on your own
machine**, each as its own process. Transmute bundles no engine binaries; every
tool below keeps its own license, its own packaging and its own developers —
and this file names them all, with gratitude, as required by FUAL-1.0 §6 and
in the spirit of every license referenced.

## Engines (external programs Transmute drives)

| Project | Used in Transmute for | License | Where to get it |
|---|---|---|---|
| [FFmpeg](https://ffmpeg.org) | video / audio / image conversion, probing (`ffmpeg`, `ffprobe`) | LGPL-2.1-or-later; GPL-2.0-or-later or GPL-3.0-or-later when built with optional GPL components (most official Windows builds are GPL) — see [FFmpeg's legal page](https://www.ffmpeg.org/legal.html) | [ffmpeg.org/download.html](https://ffmpeg.org/download.html) · Windows: `winget install Gyan.FFmpeg` |
| [Ghostscript](https://ghostscript.com) (Artifex Software Inc.) | PDF print-to-PDF, PDF optimization | AGPL-3.0-or-later; commercial licenses offered by [Artifex](https://www.ghostscript.com/copyleft/licensing.html) | [ghostscript.com/releases/gsdnld.html](https://ghostscript.com/releases/gsdnld.html) (Windows 64-bit `.msi`; there is no winget package) |
| [qpdf](https://github.com/qpdf/qpdf) | PDF inspection, repair, linearization | Apache-2.0 | [github.com/qpdf/qpdf/releases](https://github.com/qpdf/qpdf/releases) · Windows: `winget install QPDF.QPDF` |
| [Poppler](https://poppler.freedesktop.org) (`pdftoppm`, `pdftotext`) | PDF → images, PDF → text | GPL-2.0-or-later (utility tools; parts of the library are MIT) — see [cgit license file](https://gitlab.freedesktop.org/poppler/poppler/-/blob/master/COPYING) | [poppler.freedesktop.org](https://poppler.freedesktop.org) · Windows: `winget install oschwartz10612.Poppler` |
| [LibreOffice](https://www.libreoffice.org) (The Document Foundation) | office documents → PDF and other conversions | MPL-2.0 | [libreoffice.org/download](https://www.libreoffice.org/download/) · Windows: `winget install TheDocumentFoundation.LibreOffice` |
| [Pandoc](https://pandoc.org) (John MacFarlane and contributors) | markdown / docx / epub and document-text conversions | GPL-2.0-or-later — [license](https://github.com/jgm/pandoc/blob/master/LICENSE) | [pandoc.org/installing.html](https://pandoc.org/installing.html) · Windows: `winget install JohnMacFarlane.Pandoc` |

All trademarks are the property of their respective owners. Mention of a
project here is credit, not affiliation: none of these projects endorses
Transmute, and Transmute does not modify any of them — it runs them exactly as
their authors distribute them.

## Components shipped inside the Transmute application binary

| Component | Role | License | Note |
|---|---|---|---|
| [Python (CPython)](https://python.org) | application runtime | PSF License | as provided by the build platform's Python |
| [PySide6](https://pyside.org) (Qt for Python) | desktop GUI toolkit | LGPL-3.0 (commercial option available) | linked dynamically |
| [Qt 6](https://qt.io) (Qt Company) | GUI framework behind PySide6 | LGPL-3.0 | shipped as separate, replaceable shared libraries in the unpacked folder — you may swap them for another compatible Qt build, as LGPL requires; sources: [download.qt.io](https://download.qt.io/) |

## Where the full license texts live

Every license named above is published by its owner; the linked pages are the
canonical texts. If you redistribute Transmute, redistribute this file with it
(unmodified), together with the attribution notice required by FUAL-1.0 — see
`README.md` (“License”).

## Thank you

To the FFmpeg, Ghostscript, qpdf, Poppler, LibreOffice, pandoc, CPython, PySide6
and Qt teams and communities: Transmute is a thin layer over *your* decades of
work. Users get lossless-quality conversions, honest engine status screens and
zero proprietary lock-in because you released your work freely. Thank you —
this file exists so your names travel with ours.
