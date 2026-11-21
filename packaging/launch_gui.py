"""Frozen-app entry point for PyInstaller release binaries (Linux + Windows).

Kept out of the app package so transmute/ stays free of packaging concerns;
`python -m transmute.gui.app` remains the development entry point.

Engines (ffmpeg, ghostscript, qpdf, LibreOffice, pandoc, poppler) stay
EXTERNAL system processes found on PATH at runtime - the frozen bundle
ships zero codec/engine binaries, exactly like the pip and nix installs
(see LICENSE and the probe's honest "engine unavailable" rows).
"""
import sys

from transmute.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
