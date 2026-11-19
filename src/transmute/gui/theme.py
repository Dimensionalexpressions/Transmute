"""Transmute's own look - every pixel from code, no .ui files, no images.

The design target is a professional dark converter UI (the genre the
parent products invented), but implemented entirely in our own QSS: no
VidProc or Movavi file or asset is touched or imitated literally.

Two hard rules learned the hard way:

* the app must not fight the desktop theme - colors are resolved from the
  palette at build time (``is_dark``), and ``TRANSMUTE_THEME=light|dark``
  overrides detection for testing and previews;
* everything the stylesheet colors is chosen so tests stay offscreen-safe:
  no custom widgets, no event filters - only QSS plus objectNames.
"""
from __future__ import annotations

import os

# Every color the app paints lives in exactly one of these two dicts.
DARK = {
    "window": "#1c2127",     # app canvas
    "surface": "#232a32",    # cards, headers, lists
    "surface2": "#272f38",   # alternating rows
    "input": "#2b333d",      # text fields, combos, spins
    "btn": "#333d48",        # ordinary buttons
    "line": "#3a444f",       # hairlines / borders
    "line2": "#4b5866",      # hover borders
    "text": "#e8ebee",
    "muted": "#8f9aa5",
    "accent": "#34c98e",     # the one decision color: primary action
    "accentHi": "#4fe0a6",
    "accentLo": "#25a273",
    "onAccent": "#0d2b20",
    "sel": "#31506b",        # selection wash (kept blue: "picked", not "go")
    "grid": "#2c343d",
    "handle": "#465160",
    "warn": "#e6b45a",
    "warnBg": "#3a3324",
    "info": "#9fd0c8",
    "infoBg": "#24323a",
}

LIGHT = {
    "window": "#f2f4f6",
    "surface": "#ffffff",
    "surface2": "#f6f8fa",
    "input": "#ffffff",
    "btn": "#e8ebef",
    "line": "#d3d9df",
    "line2": "#aab3bc",
    "text": "#22262a",
    "muted": "#667079",
    "accent": "#12996b",
    "accentHi": "#0e855d",
    "accentLo": "#17ae7a",
    "onAccent": "#ffffff",
    "sel": "#cfe6f5",
    "grid": "#e4e8ec",
    "handle": "#c2c9d0",
    "warn": "#8a5a00",
    "warnBg": "#fdf3dd",
    "info": "#1f6d5f",
    "infoBg": "#e8f4f1",
}


def is_dark(widget) -> bool:
    """Follow the SYSTEM theme unless TRANSMUTE_THEME says otherwise."""
    forced = os.environ.get("TRANSMUTE_THEME", "").strip().lower()
    if forced in ("dark", "light"):
        return forced == "dark"
    return widget.palette().color(
        widget.palette().ColorRole.Window).lightness() < 128


def chip(p: dict, kind: str) -> str:
    """Inline style for the rounded info/warning chips (lbl_about,
    lbl_est). The app swaps kinds at runtime, so these stay strings."""
    if kind == "warn":
        return (f"color:{p['warn']};background:{p['warnBg']};"
                "border-radius:6px;padding:6px 8px;")
    return (f"color:{p['info']};background:{p['infoBg']};"
            "border-radius:6px;padding:6px 8px;")


def qss(p: dict) -> str:
    """The whole look in one stylesheet, colored from palette dict p."""
    return f"""
/* ---------- canvas -------------------------------------------------- */
QMainWindow, QDialog {{ background: {p['window']}; }}

/* ---------- cards (every group box) --------------------------------- */
QGroupBox {{
    background: {p['surface']};
    border: 1px solid {p['line']};
    border-radius: 10px;
    margin-top: 12px;
    padding: 10px 8px 8px 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {p['muted']};
    background: transparent;
}}
QFormLayout {{ vertical-spacing: 7px; horizontal-spacing: 12px; }}
QVBoxLayout, QHBoxLayout {{ spacing: 8px; }}

/* ---------- text ---------------------------------------------------- */
QLabel {{ background: transparent; color: {p['text']}; }}
QLabel#muted, QLabel#lbl_bench {{ color: {p['muted']}; }}
QLabel#accent {{ color: {p['accent']}; }}
QLabel#warnText {{ color: {p['warn']}; }}

/* ---------- inputs --------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p['input']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    padding: 5px 8px;
    color: {p['text']};
    selection-background-color: {p['sel']};
    selection-color: {p['text']};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {p['line2']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p['accent']};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled {{ color: {p['muted']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p['muted']};
    margin-right: 8px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    width: 18px; border: none; background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 18px; border: none; background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {p['muted']};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {p['muted']};
}}

/* ---------- buttons --------------------------------------------------- */
QPushButton {{
    background: {p['btn']};
    color: {p['text']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {p['line2']}; }}
QPushButton:pressed {{ background: {p['sel']}; }}
QPushButton:disabled {{ color: {p['muted']}; }}
QPushButton:checked {{ background: {p['sel']}; border-color: {p['accent']}; }}
/* the one call to action: bigger, filled, unmistakable */
QPushButton#primary {{
    background: {p['accent']};
    color: {p['onAccent']};
    border: none;
    border-radius: 8px;
    padding: 10px 26px;
    font-weight: bold;
    font-size: 14px;
}}
QPushButton#primary:hover {{ background: {p['accentHi']}; }}
QPushButton#primary:pressed {{ background: {p['accentLo']}; }}
QPushButton#primary:disabled {{
    background: {p['line']};
    color: {p['muted']};
}}

/* ---------- checkboxes / radios --------------------------------------- */
QCheckBox, QRadioButton {{ background: transparent; color: {p['text']};
                          spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    background: {p['input']};
    border: 1px solid {p['line2']};
    border-radius: 4px;
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p['accent']};
}}
QCheckBox::indicator:checked {{
    background: {p['accent']}; border-color: {p['accent']};
}}
QRadioButton::indicator:checked {{
    border: 1px solid {p['accent']};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.55, fx:0.5, fy:0.5,
        stop:0 {p['accent']}, stop:0.42 {p['accent']},
        stop:0.55 {p['input']}, stop:1 {p['input']});
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {p['line']}; background: {p['surface2']};
}}

/* ---------- slider ------------------------------------------------------ */
QSlider::groove:horizontal {{
    height: 4px; background: {p['line']}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {p['accent']}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -7px 0;
    border-radius: 8px; background: {p['accent']};
}}
QSlider::handle:horizontal:hover {{ background: {p['accentHi']}; }}

/* ---------- progress ---------------------------------------------------- */
QProgressBar {{
    background: {p['input']};
    border: 1px solid {p['line']};
    border-radius: 6px;
    min-height: 10px; max-height: 10px;
    text-align: center; color: {p['text']};
}}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 5px; }}

/* ---------- lists & the queue table ------------------------------------- */
QListWidget, QTableWidget {{
    background: {p['surface']};
    alternate-background-color: {p['surface2']};
    border: 1px solid {p['line']};
    border-radius: 8px;
    color: {p['text']};
    gridline-color: {p['grid']};
}}
QListWidget::item {{ padding: 4px 6px; border: none; }}
QTableWidget::item {{ padding: 4px 6px; border: none; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {p['sel']}; color: {p['text']};
}}
QListWidget::item:disabled {{ color: {p['muted']}; }}
QHeaderView::section {{
    background: {p['surface']};
    color: {p['muted']};
    border: none;
    border-bottom: 1px solid {p['line']};
    padding: 6px 8px;
    font-weight: bold;
}}

/* ---------- splitter / scrollbars / tooltip ------------------------------ */
QSplitter::handle {{ background: {p['window']}; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle {{
    background: {p['handle']}; border-radius: 4px; min-height: 28px;
}}
QScrollBar::handle:hover {{ background: {p['line2']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{
    background: {p['surface']};
    color: {p['text']};
    border: 1px solid {p['line2']};
    border-radius: 6px;
    padding: 6px;
}}
QToolTip:inactive {{ color: {p['text']}; }}
"""


def apply(target) -> dict:
    """Style the whole application from our own stylesheet; returns the
    palette dict the caller uses for the handful of styles that must stay
    inline (chips that swap kind at runtime)."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    p = DARK if is_dark(target) else LIGHT
    app = QApplication.instance()
    if app is not None:
        pal = QPalette()
        set = pal.setColor  # readability
        for role, key in (
            (QPalette.ColorRole.Window, "window"),
            (QPalette.ColorRole.WindowText, "text"),
            (QPalette.ColorRole.Base, "surface"),
            (QPalette.ColorRole.AlternateBase, "surface2"),
            (QPalette.ColorRole.Text, "text"),
            (QPalette.ColorRole.Button, "btn"),
            (QPalette.ColorRole.ButtonText, "text"),
            (QPalette.ColorRole.Highlight, "sel"),
            (QPalette.ColorRole.HighlightedText, "text"),
            (QPalette.ColorRole.ToolTipBase, "surface"),
            (QPalette.ColorRole.ToolTipText, "text"),
            (QPalette.ColorRole.PlaceholderText, "muted"),
            (QPalette.ColorRole.Link, "accent"),
        ):
            set(role, QColor(p[key]))
        app.setPalette(pal)
        app.setStyleSheet(qss(p))
    return p
