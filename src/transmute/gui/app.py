"""PySide6 desktop shell (P2).

Thin on purpose: every decision comes from ConverterController so the whole
surface is testable offscreen. Rows the machine cannot do stay visible but
disabled, with the machine's own reason as tooltip (F3); royalty/compat
targets show ℹ️ (F14); HDR sources get an explicit warning (F15).
"""
from __future__ import annotations

import queue as pyqueue
import sys
import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QFrame, QDialog, QProgressBar, QPushButton, QRadioButton, QSlider,
    QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .controller import REMUX_CONTAINERS, ConverterController
from ..watch import make_queue_callback



def _fmt_secs(s: float) -> str:
    if s < 45:
        return f"{max(1, int(round(s)))} s"
    m = s / 60.0
    if m < 2:
        return "about a minute"
    if m < 90:
        return f"{round(m)} min"
    h = m / 60.0
    return (f"{h:.1f} hours" if h < 10 else f"{round(h)} hours")


def _fmt_bytes(n: float) -> str:
    if n < 1e6:
        return f"{int(n // 1024)} KB"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.2f} GB"


def _sidebar_urls_with_fresh_mounts() -> "list[QUrl]":
    """Qt sidebar URLs with any currently-mounted volumes injected.

    The Qt non-native file dialog's sidebar is populated from QSettings
    bookmarks (saved by the desktop environment).  Drives that are mounted
    after those bookmarks were written -- or that were never bookmarked --
    never appear.  We add every live /run/media/<user>/* and /media/<user>/*
    volume path at dialog-open time so the sidebar is always current."""
    urls: list[QUrl] = []
    # standard Qt sidebar (Home, Desktop, Documents …)
    try:
        dlg_probe = QFileDialog()
        urls = list(dlg_probe.sidebarUrls())
        dlg_probe.deleteLater()
    except Exception:  # noqa: BLE001
        pass
    # live removable / external volumes - /proc/mounts is ground truth and
    # never cached (QStorageInfo can serve a per-process snapshot, which is
    # part of why mounts made after launch stayed invisible)
    for root in _live_volume_roots():
        u = QUrl.fromLocalFile(root)
        if u not in urls:
            urls.append(u)
    return urls


def _live_volume_roots() -> list[str]:
    """User-visible volume mount points live from /proc/mounts."""
    try:
        raw = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [p for p in _mount_roots_from_table(raw) if Path(p).is_dir()]


def _mount_roots_from_table(raw: str) -> list[str]:
    """Decode a /proc/mounts table (pure: no I/O) to volume roots only:
    /run/media/[<user>/]<vol>, /media/[<user>/]<vol>, /mnt/<vol>. Auxiliary
    binds (/tmp/.veracrypt_aux_mnt1…) and nested submounts stay excluded."""
    out: list[str] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = (parts[1].replace("\\040", " ")
               .replace("\\011", "\t").replace("\\134", "\\"))
        depth = mnt.strip("/").split("/")
        # /run/media/<vol> (VeraCrypt) AND /run/media/<user>/<vol> (KDE/udisks)
        # are both real volume roots; anything nested deeper is a submount.
        if mnt.startswith("/run/media/") and len(depth) in (3, 4):
            out.append(mnt)
        elif mnt.startswith("/media/") and len(depth) in (2, 3):
            out.append(mnt)
        elif mnt.startswith("/mnt/") and len(depth) == 2:
            out.append(mnt)
    return list(dict.fromkeys(out))


def _show_open_files_dialog(parent: QWidget, caption: str, directory: str,
                             ) -> "list[str]":
    """Non-native multi-file picker with an up-to-date sidebar."""
    dlg = QFileDialog(parent, caption, directory)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # one dialog per
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)  # browse,
    dlg.setFileMode(QFileDialog.FileMode.ExistingFiles)         # not one
    dlg.setSidebarUrls(_sidebar_urls_with_fresh_mounts())
    return dlg.selectedFiles() if dlg.exec() == QDialog.DialogCode.Accepted else []


def _show_existing_dir_dialog(parent: QWidget, caption: str, directory: str,
                               ) -> str:
    """Non-native single-directory picker with an up-to-date sidebar."""
    dlg = QFileDialog(parent, caption, directory)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # see above
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setSidebarUrls(_sidebar_urls_with_fresh_mounts())
    sel = dlg.selectedFiles() if dlg.exec() == QDialog.DialogCode.Accepted else []
    return sel[0] if sel else ""


class MainWindow(QMainWindow):
    # emitted from the worker thread; Qt queues it onto the GUI thread
    # (QTimer.singleShot from a non-GUI thread is a trap - kills timers
    # from the wrong thread at teardown)
    run_done = Signal()
    reprobe_done = Signal()
    bench_note = Signal(int, int, str)   # done, total, what (worker -> GUI)
    bench_failed = Signal(str)           # "" ok; otherwise the machine's reason
    est_done = Signal(int, object)       # generation, measured estimate dict

    def __init__(self, controller: ConverterController | None = None) -> None:
        super().__init__()
        self.ctrl = controller or ConverterController()
        # the persisted device pick must be in force BEFORE any probe runs,
        # so the report never claims GPU paths the user switched off. One
        # migration step: the old "GPU off" switch was the user's intent to
        # run CPU-only - honour it as a CPU pin, never lose it silently.
        if (self.ctrl.prefs().get("device") is None
                and self.ctrl.prefs().get("use_gpu", True) is False):
            self.ctrl.set_device("cpu")
        if self.ctrl.device() == "cpu":
            os.environ["TRANSMUTE_DISABLE_GPU"] = "1"
        self._worker: threading.Thread | None = None
        # watch-folders state (F24): the watcher thread only posts text lines
        # into the SimpleQueue; everything else happens on the GUI thread.
        self._watcher = None                     # FolderWatcher, lazy
        self._watch_rules: list[tuple[Path, Path]] = []
        self._watch_events: pyqueue.SimpleQueue[str] = pyqueue.SimpleQueue()
        self._watch_more = False                 # watch jobs await a worker
        self._watch_missing: set[tuple[str, str]] = set()  # saved but not here
        # The throttle is a per-machine judgement call, not a constant:
        # ResourceGovernor keeps conversions from owning the machine (CPU
        # cap, low IO priority, capped GPU sessions) but it costs speed.
        # The checkbox below decides; this object is the thing toggled -
        # detached from the queue when off, started and attached when on.
        from ..governor import make_governor
        self._governor = make_governor()
        self.run_done.connect(self._on_worker_done)
        self.reprobe_done.connect(self._on_reprobe_done)
        self.bench_note.connect(self._on_bench_note)
        self.bench_failed.connect(self._on_bench_failed)
        self.est_done.connect(self._on_est_done)
        self.setWindowTitle("Transmute")
        self.resize(980, 640)
        self.setAcceptDrops(True)
        # serve mode: HTTP front door onto this same controller/queue,
        # started on demand from its dialog (serve.launcher owns it)
        from ..serve.launcher import ServeLauncher
        self.serve = ServeLauncher()
        self._serve_dlg = None
        self._build()
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self.refresh_queue)
        self._poll.start()
        self._wpoll = QTimer(self)
        self._wpoll.setInterval(500)
        self._wpoll.timeout.connect(self._drain_watch)
        self._wpoll.start()
        self.refresh_queue()
        # watch folders are a standing arrangement, not a per-session wish:
        # saved rules re-arm their watcher now that every widget exists
        self._restore_watch_folders()
        # bring the governor to the state the persisted checkbox asks for
        # (off by default only if the user ever said so; new machines
        # get the considerate behaviour, matching the README promise)
        self._apply_throttle(self.chk_throttle.isChecked())

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(splitter)

        # left: files in, destination out
        left = QGroupBox("Files")
        lv = QVBoxLayout(left)
        self.files = QListWidget()
        self.files.setAcceptDrops(True)
        lv.addWidget(self.files, stretch=1)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add files…")
        self.btn_add.clicked.connect(self._add_dialog)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_selected)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_remove)
        lv.addLayout(row)
        dst = QHBoxLayout()
        self.dst_edit = QLineEdit()
        self.dst_edit.setPlaceholderText("Destination folder")
        self.dst_edit.setText(str(Path.home() / "Transmute"))
        self.btn_dst = QPushButton("Choose…")
        self.btn_dst.clicked.connect(self._choose_dst)
        dst.addWidget(self.dst_edit, stretch=1)
        dst.addWidget(self.btn_dst)
        lv.addLayout(dst)
        self.lbl_hdr = QLabel("")
        self.lbl_hdr.setWordWrap(True)
        self.lbl_hdr.setStyleSheet("color:#b3541e;")
        lv.addWidget(self.lbl_hdr)
        prow = QHBoxLayout()
        self.btn_prev = QPushButton("Preview first frame")
        self.btn_prev.setEnabled(False)
        self.btn_prev.clicked.connect(self._preview)
        prow.addWidget(self.btn_prev)
        prow.addStretch(1)
        lv.addLayout(prow)
        self.lbl_prev = QLabel("")
        self.lbl_prev.setWordWrap(True)
        lv.addWidget(self.lbl_prev)
        self.files.currentItemChanged.connect(
            lambda *_: self.btn_prev.setEnabled(self._current_file_is_video()))

        # ---- watch folders (F24 in the GUI): the watcher decides NOTHING -
        # it hands settled files over; controller/queue policy stays central.
        wbox = QGroupBox("Watch folders (auto-convert on drop)")
        wv = QVBoxLayout(wbox)
        self.watch_list = QListWidget()
        self.watch_list.setMaximumHeight(64)
        wv.addWidget(self.watch_list)
        self.watch_log = QListWidget()
        self.watch_log.setMaximumHeight(56)
        self.watch_log.setEnabled(False)  # history view; clicks do nothing
        wv.addWidget(self.watch_log)
        wrow = QHBoxLayout()
        self.btn_watch = QPushButton("Watch folder…")
        self.btn_watch.clicked.connect(self._watch_add_dialog)
        self.btn_unwatch = QPushButton("Remove selected")
        self.btn_unwatch.clicked.connect(self._watch_remove_selected)
        wrow.addWidget(self.btn_watch)
        wrow.addWidget(self.btn_unwatch)
        wrow.addStretch(1)
        wv.addLayout(wrow)
        self.lbl_watch = QLabel("no watch folders")
        self.lbl_watch.setWordWrap(True)
        wv.addWidget(self.lbl_watch)
        lv.addWidget(wbox)

        # ---- serve mode: the same core, reached over HTTP (off by default)
        sbox = QGroupBox("Serve (convert from other devices)")
        sh = QHBoxLayout(sbox)
        self.lbl_serve = QLabel("not serving")
        self.lbl_serve.setWordWrap(True)
        self.btn_serve = QPushButton("Serving…")
        self.btn_serve.clicked.connect(self._serve_dialog)
        sh.addWidget(self.lbl_serve, stretch=1)
        sh.addWidget(self.btn_serve)
        lv.addWidget(sbox)
        splitter.addWidget(left)

        # right: what this machine can do, chosen from the probed matrix
        right = QGroupBox("Convert to")
        rv = QFormLayout(right)
        self._form = rv
        self.cat_box = QComboBox()
        self.cat_box.addItems(self.ctrl.categories())
        self.cat_box.currentTextChanged.connect(self._load_targets)
        rv.addRow("Category", self.cat_box)
        self.tgt_box = QComboBox()
        self.tgt_box.currentIndexChanged.connect(self._load_encoders)
        rv.addRow("Target", self.tgt_box)
        # plain-language details for the chosen target (what you get, measured
        # speed per device on THIS machine). Colours follow the SYSTEM theme
        # (user feedback: hard-coded light box glared on the dark theme).
        _dark = self.palette().color(QPalette.ColorRole.Window
                                     ).lightness() < 128
        self.lbl_about = QLabel("")
        self.lbl_about.setWordWrap(True)
        self.lbl_about.setStyleSheet(
            "color:#a3ccc6;background:#26332f;" if _dark else
            "color:#2f5f59;background:#eef6f5;")
        self.lbl_about.setStyleSheet(self.lbl_about.styleSheet() +
                                     "border-radius:6px;padding:6px 8px;")
        self._warn_c = ("color:#e6b45a;" if _dark else "color:#b3541e;")
        self._ok_c = ("color:#7cc47f;" if _dark else "color:#2e7d32;")
        rv.addRow(self.lbl_about)

        # user directive: every number shown must come from THIS machine's
        # hardware - measured at startup, or re-measured here on demand.
        self.lbl_est = QLabel("")
        self.lbl_est.setWordWrap(True)
        self.lbl_est.hide()
        if _dark:
            self.lbl_est.setStyleSheet(
                "color:#a3ccc6;background:#26332f;border-radius:6px;"
                "padding:6px;")
        else:
            self.lbl_est.setStyleSheet(
                "color:#2f5f59;background:#eef6f5;border-radius:6px;"
                "padding:6px;")
        rv.addRow(self.lbl_est)
        self._est_ok_style = self.lbl_est.styleSheet()
        self._est_warn_style = (
            "color:#d9a441;background:#3a3324;border-radius:6px;padding:6px;"
            if _dark else
            "color:#8a6d1a;background:#fdf6e3;border-radius:6px;padding:6px;")
        self.btn_bench = QPushButton("Re-benchmark this PC")
        self.btn_bench.setToolTip(
            "Test-encodes a 720p sample on every encoder found here, then "
            "updates the speed words above. Takes about a minute; safe to "
            "run any time.")
        self.btn_bench.clicked.connect(self._start_reprobe)
        rv.addRow("", self.btn_bench)
        self.chk_throttle = QCheckBox("Throttle while converting")
        self.chk_throttle.setToolTip(
            "Checked (default): conversions stay under this machine's\n"
            "measured CPU headroom, disk work runs at low priority, and\n"
            "GPU encodes run as capped sessions - the PC stays usable.\n"
            "Unchecked: engines run flat out - fastest, but ffmpeg\n"
            "takes every core it can reach.\n"
            "Applies to new conversions immediately; remembered.")
        self.chk_throttle.setChecked(
            bool(self.ctrl.prefs().get("throttle",
                                       self._governor.enabled)))
        self.chk_throttle.toggled.connect(self._apply_throttle)
        rv.addRow("", self.chk_throttle)
        # benchmark progress: the measurement takes minutes on slow machines
        # and the user must SEE which encoder it is busy with right now.
        self.bench_bar = QProgressBar()
        self.bench_bar.setRange(0, 0)
        self.bench_bar.setTextVisible(False)
        self.lbl_bench = QLabel("")
        self.lbl_bench.setWordWrap(True)
        self.lbl_bench.setStyleSheet(
            "color:" + ("#8f979e" if _dark else "#616161") + ";")
        rv.addRow(self.bench_bar)
        rv.addRow("", self.lbl_bench)
        self.bench_bar.hide()
        self.lbl_bench.hide()
        self._bench_live = False
        self._bench_cancel = False
        self.enc_box = QComboBox()
        self.enc_box.currentIndexChanged.connect(
            lambda *_: (self._update_enc_note(),
                        # manual encoder picks change what the quality knob
                        # means and which rows apply - resync everything
                        self._sync_options_enabled()))
        rv.addRow("Encoder", self.enc_box)
        # ---- P3 video options: every one of these maps 1:1 to a controller
        # kwarg; nothing here decides anything itself (F2/F15: remux is a
        # separate operation, tone-map is never silent).
        self.chk_remux = QCheckBox("Remux only - lossless stream copy")
        self.chk_remux.setToolTip(
            "Change container without touching the encoded streams. Never "
            "re-encodes; refuses when a stream cannot be copied (F2).")
        self.chk_remux.toggled.connect(self._sync_options_enabled)
        rv.addRow("Video", self.chk_remux)
        self.remux_box = QComboBox()
        self.remux_box.addItems(list(REMUX_CONTAINERS))
        self.remux_box.currentTextChanged.connect(
            lambda *_: self._update_remux_status())
        rv.addRow("Remux into", self.remux_box)
        self.chk_tonemap = QCheckBox("Tone-map HDR to SDR")
        rv.addRow("", self.chk_tonemap)

        # More… options (user directive): which hardware does the work. One
        # named entry per device THIS PC actually has; the pick re-measures,
        # so every speed number and estimate that follows belongs to it.
        more = QGroupBox("More…")
        mform = QFormLayout(more)
        # Performance wish (user-approved Option 2): ONE control for what
        # the machine optimises for. It never invents capability - it
        # filters the verified ladder; a wish this PC cannot honour falls
        # back to the fastest verified path and says so on the line below.
        self.perf_box = QComboBox()
        for _lbl, _val, _tip in (
            ("Auto - this PC decides (recommended)", "auto",
             "Encode with whatever this machine MEASURED fastest for this\n"
             "format, and decode on the GPU whenever the probe's trial\n"
             "proved GPU decoding here. The measured speeds are listed\n"
             "under the target details."),
            ("Quality-first - CPU encoder, best picture", "quality",
             "Keep compression on the CPU encoders (e.g. x264/x265): they\n"
             "give the best quality per byte, at CPU speed. Video\n"
             "decoding still uses the GPU when proven. Falls back, with\n"
             "an explanation, if no CPU encoder works for this format."),
            ("Speed-first - graphics card when it works", "speed",
             "Encode on the graphics-card encoder when this PC has one\n"
             "that passed a real trial encode; otherwise the fastest CPU\n"
             "encoder does it, and the line below says why."),
        ):
            self.perf_box.addItem(_lbl, _val)
            self.perf_box.setItemData(
                self.perf_box.count() - 1, _tip, Qt.ItemDataRole.ToolTipRole)
        _i = self.perf_box.findData(self.ctrl.perf_mode())
        self.perf_box.setCurrentIndex(_i if _i >= 0 else 0)
        self.perf_box.currentIndexChanged.connect(self._on_perf_pick)
        mform.addRow("Performance", self.perf_box)
        self.device_box = QComboBox()
        self.device_box.setToolTip(
            "Choose which hardware encodes and decodes.\n"
            "Auto: whatever this PC measured fastest for this file.\n"
            "Devices listed here were read from this machine at startup; one\n"
            "that cannot be used stays listed with its own measured reason.")
        self._load_devices()
        self.device_box.currentIndexChanged.connect(self._on_device_pick)
        mform.addRow("CPU / GPU", self.device_box)
        # Decode honesty line: the picker above speaks about ENCODING; video
        # DECODING also runs on the GPU only when the probe proved this
        # machine really decodes there. One short truth, no new knob.
        self.lbl_dec = QLabel("")
        self.lbl_dec.setWordWrap(True)
        mform.addRow(self.lbl_dec)
        self.lbl_dec.hide()
        # the first _load_devices() above ran before this row existed (its
        # end calls _update_decode_line); now that the row does exist, show
        # what the boot probe measured - the line must never boot invisible
        self._update_decode_line()
        rv.addRow(more)
        qlrow = QHBoxLayout()
        self.rad_crf = QRadioButton("Quality (CRF)")
        self.rad_crf.setChecked(True)
        self.rad_size = QRadioButton("Size target (MB)")
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(0.5, 100000.0)
        self.size_box.setValue(700.0)
        self.size_box.setToolTip(
            "Approximate for codecs outside x264/x265 (no reliable rate "
            "control); the report says whether the result is within budget.")
        for w in (self.rad_crf, self.rad_size):
            w.toggled.connect(self._sync_options_enabled)
        self.size_box.valueChanged.connect(lambda *_: self._est_dirty())
        qlrow.addWidget(self.rad_crf)
        qlrow.addWidget(self.rad_size)
        qlrow.addWidget(self.size_box)
        qlrow.addStretch(1)
        rv.addRow("Video rate", qlrow)

        # ------------------------------------------- advanced fine-tuning
        # User directive: an Advanced dropdown for video & audio quality, and
        # the estimated file size updates with EVERY movement of the selector,
        # like the parent products. The difference: those numbers stay
        # MEASURED on this PC (a real sample re-encoded at each new setting) -
        # between measurements the label honestly says "measuring…".
        # Not a click-to-open dropdown: hidden controls are invisible controls
        # (user feedback), so this header sits above always-visible rows.
        self.adv_header = QLabel("Advanced (fine-tune quality & audio)")
        self.adv_header.setStyleSheet("font-weight:bold;")
        rv.addRow(self.adv_header)
        self.adv_frame = QFrame()
        af = QFormLayout(self.adv_frame)
        af.setContentsMargins(12, 0, 0, 0)
        self.crf_box = QSpinBox()
        self.crf_box.setRange(0, 51)
        self.crf_box.setValue(23)
        self.qslider = QSlider(Qt.Orientation.Horizontal)
        self.qslider.setRange(0, 51)
        self.qslider.setValue(23)
        # drag RIGHT = better quality = lower CRF, like every editor slider
        self.qslider.setInvertedAppearance(True)
        self.qslider.valueChanged.connect(
            lambda v: self._on_crf_choice(v, "slider"))
        self.crf_box.valueChanged.connect(
            lambda v: self._on_crf_choice(v, "spin"))
        self.lbl_crf_word = QLabel("")
        # which way is which (user feedback: an unlabeled slider is a guess)
        muted = "#8b949e" if _dark else "#6e7781"
        self.lbl_crf_worse = QLabel("worse")
        self.lbl_crf_better = QLabel("better")
        for _lbl in (self.lbl_crf_worse, self.lbl_crf_better):
            _lbl.setStyleSheet(f"color:{muted};")
        self.lbl_size = QLabel("")
        self.lbl_size.setMinimumWidth(90)
        if _dark:
            self.lbl_size.setStyleSheet("color:#79b8ff;")
        else:
            self.lbl_size.setStyleSheet("color:#0a5bc0;")
        qrow = QHBoxLayout()
        qrow.addWidget(self.lbl_crf_worse)
        qrow.addWidget(self.qslider, stretch=1)
        qrow.addWidget(self.lbl_crf_better)
        qrow.addWidget(self.crf_box)
        qrow.addWidget(self.lbl_crf_word)
        qrow.addWidget(self.lbl_size)
        af.addRow("Quality", qrow)
        self.preset_box = QComboBox()
        self.preset_box.addItems(
            ["ultrafast", "superfast", "veryfast", "faster", "fast",
             "medium", "slow", "slower", "veryslow", "placebo"])
        self.preset_box.setCurrentText("medium")
        self.preset_box.setToolTip(
            "CPU encoders (x264/x265): slower presets squeeze more quality\n"
            "out of the same file size. Graphics-card encoders run their own\n"
            "speed ladder - this row does not apply to them.\n"
            "Estimates are re-MEASURED with whatever is chosen here.")
        self.preset_box.currentTextChanged.connect(lambda *_: self._est_dirty())
        af.addRow("Encoder speed", self.preset_box)
        self.chk_audio_replace = QCheckBox("Re-encode audio (AAC)")
        self.chk_audio_replace.setToolTip(
            "Default leaves audio untouched. This re-encodes it at the\n"
            "bitrate chosen beside it: smaller files, and the estimate\n"
            "counts the real audio bytes for exactly this plan.")
        self.audio_kbps = QComboBox()
        self.audio_kbps.addItems(
            ["64k", "96k", "128k", "160k", "192k", "256k", "320k"])
        self.audio_kbps.setCurrentText("160k")
        self.audio_kbps.setEnabled(False)
        self.chk_audio_replace.toggled.connect(
            lambda on: (self.audio_kbps.setEnabled(
                bool(on) and self.chk_audio_replace.isEnabled()),
                self._est_dirty()))
        self.audio_kbps.currentTextChanged.connect(lambda *_: self._est_dirty())
        arow = QHBoxLayout()
        arow.addWidget(self.chk_audio_replace)
        arow.addWidget(self.audio_kbps)
        arow.addStretch(1)
        af.addRow("Audio", arow)
        rv.addRow(self.adv_frame)  # always open; category visibility below
        self._update_crf_word()
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(80)
        rv.addRow("Quality (image)", self.quality)
        self.deep_pdf = QCheckBox("Deep PDF pass (LOSSY recompress)")
        rv.addRow("PDF", self.deep_pdf)
        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        rv.addRow(self.lbl_note)
        self.lbl_remux = QLabel("")
        self.lbl_remux.setWordWrap(True)
        self.lbl_remux.setStyleSheet(self._warn_c)
        rv.addRow(self.lbl_remux)
        # QFormLayout.addRow returns no row index in PySide6 - derive them
        # by scanning the form once (cheap, fixed set).
        self._row = {
            "about": self._row_of(self.lbl_about),
            "enc": self._row_of(self.enc_box),
            "remux": self._row_of(self.chk_remux),
            "remux_into": self._row_of(self.remux_box),
            "tone": self._row_of(self.chk_tonemap),
            "adv_btn": self._row_of(self.adv_header),
            "adv": self._row_of(self.adv_frame),
            "rate": self._row_of(self.rad_crf),
            "quality": self._row_of(self.quality),
            "pdf": self._row_of(self.deep_pdf),
            "note": self._row_of(self.lbl_note),
            "remuxmsg": self._row_of(self.lbl_remux),
        }
        splitter.addWidget(right)

        # bottom: queue table + run control
        bottom = QGroupBox("Queue")
        bv = QVBoxLayout(bottom)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Target", "State", "Detail"])
        self.table.horizontalHeader().setStretchLastSection(True)
        bv.addWidget(self.table, stretch=1)
        ctl = QHBoxLayout()
        self.btn_run = QPushButton("Run queue")
        self.btn_run.clicked.connect(self._run)
        # P5: recoverable pause vs terminal cancel are DIFFERENT buttons —
        # never let one word mean both. In-flight jobs finish either way.
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._pause)
        self.btn_resume = QPushButton("Resume")
        self.btn_resume.clicked.connect(self._resume)
        self.btn_resume.setEnabled(False)
        self.btn_cancel = QPushButton("Cancel pending")
        self.btn_cancel.setToolTip(
            "Abandon everything not yet started (visible in the table as\n"
            "'cancelled by user'). Jobs already running finish normally.")
        self.btn_cancel.clicked.connect(self._cancel_pending)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)
        self.lbl_status = QLabel("")
        ctl.addWidget(self.btn_run)
        ctl.addWidget(self.btn_pause)
        ctl.addWidget(self.btn_resume)
        ctl.addWidget(self.btn_cancel)
        ctl.addWidget(self.bar)
        ctl.addWidget(self.lbl_status, stretch=1)
        bv.addLayout(ctl)
        lay.addWidget(bottom, stretch=2)

        self._load_targets(self.cat_box.currentText())

    # -------------------------------------------------------------- slots
    def _add_dialog(self) -> None:
        # NON-native and sidebar rebuilt from the LIVE mount table on every
        # open. The old assumption - "Qt enumerates mounts when the dialog
        # opens, so a newly attached drive shows up" - was false: Qt caches
        # the mount table per process (and VeraCrypt-style mounts emit no
        # notification at all), so a volume mounted after launch stayed
        # invisible no matter how often the dialog was reopened (real user,
        # 2026-11; see _sidebar_urls_with_fresh_mounts).
        names = _show_open_files_dialog(self, "Add files", str(Path.home()))
        for n in names:
            self._add_one(Path(n))

    def _add_one(self, path: Path) -> None:
        from ..sources import category_of, is_ignored
        # the user picked these files explicitly: judge hidden/partial rules on
        # the NAME only — ancestors above their folder are the user's business
        if is_ignored(path.name, real_path=path):
            return
        if self.files.findItems(str(path), Qt.MatchFlag.MatchExactly):
            return
        cat = category_of(path)
        label = str(path) if cat else f"{path}  (unsupported type)"
        it = QListWidgetItem(label)
        it.setData(Qt.ItemDataRole.UserRole, str(path))
        if cat is None:
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.files.addItem(it)
        self._update_hdr(path)
        self._update_remux_status()
        self._est_dirty()

    def _remove_selected(self) -> None:
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))
        self._update_remux_status()
        self._est_dirty()

    def _choose_dst(self) -> None:
        d = _show_existing_dir_dialog(
            self, "Destination", self.dst_edit.text() or str(Path.home()))
        if d:
            self.dst_edit.setText(d)

    def _update_hdr(self, last: Path) -> None:
        self.lbl_hdr.setText("")
        if self.ctrl.category_of(last) != "video":
            return
        hdr = self.ctrl.hdr_source(last)
        if hdr:
            # F15: never silently tone-map; the choice is the user's, and the
            # machine's real tone-map ability is stated either way.
            if self.ctrl.tonemap_available:
                self.lbl_hdr.setText(
                    "⚠ HDR source: a plain convert keeps the HDR tags. Tick "
                    "\"Tone-map HDR to SDR\" for output viewable on SDR screens.")
            else:
                self.lbl_hdr.setText(
                    "⚠ HDR source: this ffmpeg lacks the tone-map chain "
                    "(probed); output will keep HDR tags.")
        elif hdr is None:
            self.lbl_hdr.setText("HDR status unknown - ffprobe missing.")

    def _load_targets(self, category: str) -> None:
        self.tgt_box.clear()
        for t in self.ctrl.targets(category):
            self.tgt_box.addItem(t.label)
            idx = self.tgt_box.count() - 1
            item = self.tgt_box.model().item(idx)
            # NB: QStandardItem.setData(value, role) - opposite argument order
            # from QListWidgetItem.setData(role, value) above.
            item.setData(t.target, Qt.ItemDataRole.UserRole)
            if not t.available:
                # disabled but VISIBLE: the reason is IN the label (real-user
                # feedback: tooltip-only reasons go unnoticed) - F3 honoured,
                # full machine words in the tooltip.
                item.setEnabled(False)
                item.setToolTip((t.reason + "\n\n" + t.detail).strip())
            elif t.compat_flag:
                item.setToolTip("Patent/royalty-encumbered codec; offered only "
                                "because this machine can encode it (F14).\n"
                                + t.detail)
            else:
                item.setToolTip(t.detail or t.note)
        self._load_encoders(0)

    def _load_encoders(self, _idx: int) -> None:
        cat = self.cat_box.currentText()
        target = self._current_target()
        self.enc_box.clear()
        if not target:
            return
        ladder = self.ctrl.encoders(cat, target)
        if ladder:
            # raw encoder ids become item DATA; users see human words only
            self.enc_box.addItem("auto (fastest verified)", "")
            for enc in ladder:
                self.enc_box.addItem(self.ctrl.human_encoder(enc), enc)
        self.enc_box.setEnabled(bool(ladder))
        self._update_enc_note()
        self._sync_options_enabled()

    def _load_devices(self) -> None:
        """Fill the CPU/GPU picker from what THIS machine reports right now
        (controller reads /proc and /sys live - nothing assumed at build
        time). A device that cannot encode is still selectable: doctrine is
        visible with its own reason, never hidden."""
        self.device_box.blockSignals(True)
        self.device_box.clear()
        self.device_box.addItem("Auto - whichever is fastest here", "auto")
        for d in self.ctrl.devices():
            label = ("CPU - " if d["kind"] == "cpu" else "GPU - ") + d["name"]
            tip = d["name"]
            if not d["works"]:
                # three DIFFERENT truths, three wordings (user, 2026-11):
                # idle-by-choice, mid-benchmark, or genuinely measured broken
                kind = d.get("disabled_kind")
                if d.get("disabled"):
                    label += ("  (not in use - CPU is chosen)"
                              if kind == "cpu-selected"
                              else "  (not in use right now)")
                    tip = f"{d['name']} - {d['why']}"
                elif kind == "measuring":
                    label += "  (measuring now...)"
                    tip = f"{d['name']} - {d['why']}"
                else:
                    label += "  (not working here)"
                    tip = f"{d['name']} - why: {d['why']}"
            self.device_box.addItem(label, d["id"])
            self.device_box.setItemData(self.device_box.count() - 1, tip,
                                        Qt.ItemDataRole.ToolTipRole)
        i = self.device_box.findData(self.ctrl.device())
        self.device_box.setCurrentIndex(i if i >= 0 else 0)
        self.device_box.blockSignals(False)
        self._update_decode_line()

    def _update_decode_line(self) -> None:
        """One honest line about the DECODE path, from the probe's own trial:
        on means ffmpeg on this machine decoded our GPU-frame-checked H.264
        sample on the GPU; off carries its reason. Silent when nothing was
        attempted (GPU held out at launch, no ffmpeg) - nothing to claim."""
        lbl = getattr(self, "lbl_dec", None)
        if lbl is None:  # called during construction, before the row exists
            return
        gd = self.ctrl.gpu_decode_status()
        pretty = {"cuda": "NVIDIA GPU (NVDEC)",
                  "vaapi": "GPU via VA-API"}.get(gd.get("api", ""),
                                                 gd.get("api", ""))
        base = ""
        if gd.get("available"):
            base = f"Video decoding: on the GPU ({pretty})"
        elif gd.get("attempted"):
            why = (gd.get("reason") or "").strip() or "this GPU declined to decode"
            if len(why) > 160:
                why = why[:157] + "..."
            base = f"Video decoding: on the CPU - {why}"
        # when the Quality/Speed wish could not be honoured on this PC, the
        # same line carries the machine's own reason - never silent
        wish = self.ctrl.perf_note(self.cat_box.currentText(),
                                   self._current_target() or "")
        if wish:
            wish = wish[0].upper() + wish[1:] + "."
        text = " · ".join(t for t in (base, wish) if t)
        if not text:
            lbl.hide()
            return
        lbl.setText(text)
        lbl.setStyleSheet(self._warn_c if wish else self._ok_c)
        lbl.show()

    def _on_perf_pick(self, _idx: int) -> None:
        """Mode changed: the encoder ladder changes under the Encoder box,
        the quality knob's scale belongs to a different encoder, and every
        estimate measured against the old one is stale. No re-probe - the
        measurements don't depend on the wish."""
        self.ctrl.set_perf_mode(self.perf_box.currentData() or "auto")
        self._load_encoders(0)
        self._sync_quality_scale()
        self._update_about()
        self._update_decode_line()
        self._est_dirty()

    def _on_device_pick(self, _idx: int) -> None:
        dev = self.device_box.currentData() or "auto"
        self.ctrl.set_device(dev)
        # the report must match the pick: a CPU pin drops GPU rows, Auto or
        # a GPU pin measures them again
        if dev == "cpu":
            os.environ["TRANSMUTE_DISABLE_GPU"] = "1"
        else:
            os.environ.pop("TRANSMUTE_DISABLE_GPU", None)
        # The SCREEN matches the pick the same instant, not a minute later.
        # (user's screenshot, 2026-11: they pinned the RTX 5090, chose
        # "GPU - NVIDIA av1", then picked CPU - yet the Encoder box kept
        # holding av1_nvenc while the re-benchmark ran, so a job would have
        # encoded on the very GPU they had just deselected.) Rebuild both
        # lists from the NEW device's rows; the fresh measurement then
        # refines them again when it lands.
        self._load_encoders(0)
        self._load_devices()
        # the numbers must belong to the chosen device: re-measure this
        # machine now (same contract the old GPU switch had)
        self._start_reprobe()

    def _effective_encoder(self) -> str:
        """What actually runs for this pick. A device pin means 'auto' must
        resolve HERE (fastest verified ON THAT DEVICE) - the engine's own
        auto knows no devices, and estimates measure the same plan."""
        if self.enc_box.isEnabled():
            enc = self.enc_box.currentData()
            if enc and self._enc_on_device(str(enc)):
                return str(enc)
        if self.ctrl.device() != "auto":
            cat = self.cat_box.currentText()
            target = self._current_target()
            if target:
                return self.ctrl.auto_encoder(cat, target) or ""
        return ""

    def _enc_on_device(self, enc: str) -> bool:
        """True when enc belongs to the device the user pinned (Auto covers
        the whole ladder). Guards the one window a stale widget could still
        offer another vendor's encoder: the pin's allowed ladder is checked
        AGAIN at every use, so a deselected GPU can never run a job."""
        backends = self.ctrl.device_backends()
        if backends is None:
            return True
        cat = self.cat_box.currentText()
        target = self._current_target()
        if not target:
            return True
        return enc in self.ctrl.encoders(cat, target)

    # ------------------------------------------------ estimate (measured here)
    def _on_crf_choice(self, val: int, source: str) -> None:
        """Every notch of the slider (or spin) re-labels immediately and
        schedules a fresh MEASURED size - parent-product behaviour, honest
        numbers."""
        if not getattr(self, "_crf_sync", False):
            self._crf_sync = True
            if source == "slider":
                self.crf_box.setValue(val)
            else:
                self.qslider.setValue(val)
            self._crf_sync = False
        self._update_crf_word()
        self.lbl_size.setText("measuring…")
        self._est_dirty()

    def _update_crf_word(self) -> None:
        crf = self.crf_box.value()
        lo, hi = self.crf_box.minimum(), self.crf_box.maximum()
        knob = "QP" if hi >= 255 else "CRF"  # librav1e speaks -qp, not CRF
        if (lo, hi) == (0, 51):
            # the x264/x265 scale - the words users already know
            if crf >= 32:
                word = "small file"
            elif crf >= 24:
                word = "balanced"
            elif crf >= 18:
                word = "high quality"
            else:
                word = "near-lossless"
        else:
            # other encoders carry their own scale: judge POSITION, never the
            # raw number - CRF 30 is "balanced" for SVT-AV1 but enormous
            # for x264 (live bug: absolute words lied across scales)
            frac = (hi - crf) / (hi - lo) if hi > lo else 0.0
            if frac >= 0.82:
                word = "near-lossless"
            elif frac >= 0.70:
                word = "high quality"
            elif frac >= 0.35:
                word = "balanced"
            else:
                word = "small file"
        self.lbl_crf_word.setText(f"{knob} {crf} · {word}")

    def _sync_quality_scale(self) -> None:
        """Each encoder carries its OWN quality scale and the slider must
        follow the encoder that will run (live bug: AV1/SVT-AV1 shown on
        x264's 0..51 made CRF 0 the "better" end, where SVT collapses and
        a sample measured SMALLER than CRF 10 - better got smaller).
        When the scale changes the selector restarts at the default the
        new encoder itself measured; the value never silently re-maps."""
        cat = self.cat_box.currentText()
        target = self._current_target()
        rng = None
        if cat == "video" and target:
            try:
                rng = self.ctrl.quality_range(cat, target)
            except Exception:
                rng = None
        if rng is None:
            return
        lo, hi, dflt = rng
        if (self.crf_box.minimum(), self.crf_box.maximum()) == (lo, hi):
            return
        for w in (self.crf_box, self.qslider):
            w.blockSignals(True)
        self.crf_box.setRange(lo, hi)
        self.qslider.setRange(lo, hi)
        self.crf_box.setValue(dflt)
        self.qslider.setValue(dflt)
        for w in (self.crf_box, self.qslider):
            w.blockSignals(False)
        self._update_crf_word()
        self._est_dirty()

    def _est_dirty(self) -> None:
        # ONE coalescing timer, restarted per movement: a slider drag causes
        # a burst of ticks but exactly one measuring pass when it settles.
        # (Lockup post-mortem: per-tick singleShots each launched their own
        # full-machine sample encode and none could be cancelled.)
        t = getattr(self, "_est_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(600)
            t.timeout.connect(self._est_run)
            self._est_timer = t
        t.start()  # restart == latest movement wins

    def _est_run(self) -> None:
        if not hasattr(self, "lbl_est"):
            return  # construction order
        cat = self.cat_box.currentText()
        target = self._current_target()
        src = ""
        if cat == "video" and target and self.files.count():
            src = self.files.item(0).data(Qt.ItemDataRole.UserRole) or ""
        if not src or (self._worker is not None and self._worker.is_alive()) \
                or self._bench_live:
            # a full-machine benchmark sample is running: measuring an
            # estimate through it would describe neither pass honestly
            self.lbl_est.hide()
            return
        tinfo = self._target_info(cat, target)
        if tinfo is not None and not tinfo.available:
            self.lbl_est.hide()
            return
        self._est_gen = getattr(self, "_est_gen", 0) + 1
        g = self._est_gen
        # supersede: a measurement only ever replaces the one before it, so
        # kill the old sample encode BEFORE starting the new one - moving a
        # selector must never stack work on the user's machine
        old = getattr(self, "_est_stop", None)
        if old is not None:
            old.set()
        stop = threading.Event()
        self._est_stop = stop
        self.lbl_size.setText("measuring…")
        self.lbl_est.setText("Measuring an estimate on this PC…")
        self.lbl_est.show()
        encoder = self._effective_encoder()
        if (not encoder and self.ctrl.device() != "auto"
                and cat in ("video", "audio")
                and not (cat == "video" and self.chk_remux.isChecked())):
            d = self.ctrl.device_choice() or {}
            self.lbl_size.setText("this device can't do it here")
            self.lbl_est.setText(
                f"{d.get('name', 'That device')} has no verified encoder for"
                f" this on this PC ({d.get('why', 'probe found none')}).")
            self.lbl_est.show()
            return
        crf = self.crf_box.value() if self.rad_crf.isChecked() else None
        size_mb = self.size_box.value() if self.rad_size.isChecked() else None
        audio_mode = ("replace" if self.chk_audio_replace.isChecked()
                      else "copy")
        threading.Thread(target=self._est_body, daemon=True,
                         args=(g, cat, target, src),
                         kwargs=dict(encoder=encoder, crf=crf,
                                     size_mb=size_mb,
                                     preset=self.preset_box.currentText(),
                                     audio_mode=audio_mode,
                                     audio_bitrate=(
                                         self.audio_kbps.currentText()
                                         if self.chk_audio_replace.isChecked()
                                         else None),
                                     stop=stop)).start()

    def _est_body(self, g, cat, target, src, **kw) -> None:
        # OFF the GUI thread: touch no widgets; signal only.
        try:
            d = self.ctrl.conversion_estimate(cat, target, src, **kw)
        except Exception:
            d = {"ok": False, "note": ""}
        self.est_done.emit(g, d)

    def _on_est_done(self, g: int, d: object) -> None:
        if not hasattr(self, "lbl_est") or g != getattr(self, "_est_gen", 0):
            return  # stale: the user changed their mind mid-sample
        if isinstance(d, dict) and d.get("ok"):
            sz = _fmt_bytes(float(d.get("bytes", 0.0)))
            out = (f"your {sz} target" if d.get("size_is_target")
                   else f"about {sz}")
            self.lbl_est.setText(
                f"Estimate for this file: about "
                f"{_fmt_secs(float(d.get('est_s', 0.0)))} of work, output "
                f"{out}. Measured from a {float(d.get('sec', 0.0)):.0f} s "
                "piece of it on this PC.")
            self.lbl_est.setStyleSheet(self._est_ok_style)
            self.lbl_est.show()
            self.lbl_size.setText(f"about {sz}")
            self.lbl_size.show()
            return
        self.lbl_size.setText("")
        note = d.get("note", "") if isinstance(d, dict) else ""
        if note:
            self.lbl_est.setText(f"Estimate: {note}.")
            self.lbl_est.setStyleSheet(self._est_warn_style)
            self.lbl_est.show()
        else:
            self.lbl_est.hide()

    def _start_reprobe(self) -> None:
        if self._bench_live:
            # button doubles as Cancel: probe raises ProbeCancelled, the
            # previous report stays in force (never half-results).
            self._bench_cancel = True
            self.btn_bench.setEnabled(False)
            self.lbl_bench.setText("Cancelling…")
            return
        if self._worker is not None and self._worker.is_alive():
            return  # a conversion is hogging the CPUs - measure later
        # one measurement on this machine at a time: an in-flight estimate
        # sample would run against the benchmark and poison both numbers
        est = getattr(self, "_est_stop", None)
        if est is not None:
            est.set()
            self._est_stop = None
        self.lbl_est.hide()
        self._bench_live = True
        self._bench_cancel = False
        self.btn_bench.setText("Measuring this PC… click to cancel")
        self.bench_bar.setRange(0, 0)
        self.bench_bar.show()
        self.lbl_bench.setText("Looking up installed engines…")
        self.lbl_bench.show()
        self.lbl_about.setText("")
        self.lbl_est.hide()
        self._toggle_row(self._row["about"], False)
        threading.Thread(target=self._reprobe_body, daemon=True).start()

    def _reprobe_body(self) -> None:
        # OFF the GUI thread: touch no widgets; signal only.
        err = ""
        try:
            self.ctrl.reprobe(
                progress=lambda d, t, w: self.bench_note.emit(d, t, w),
                cancel=lambda: self._bench_cancel)
        except Exception as exc:            # incl. ProbeCancelled
            err = str(exc) or exc.__class__.__name__
        finally:
            if err:
                self.bench_failed.emit(err)
            self.reprobe_done.emit()

    def _on_bench_note(self, done: int, total: int, what: str) -> None:
        if total > 0:
            self.bench_bar.setRange(0, total)
            self.bench_bar.setValue(done)
            self.lbl_bench.setText(f"Measuring {what}… ({done} of {total})")
        else:
            self.lbl_bench.setText(what + "…")

    def _on_bench_failed(self, err: str) -> None:
        if "cancel" in err.lower():
            return  # deliberate: _on_reprobe_done just restores the facts
        self.lbl_bench.setText("Benchmark could not finish here: " + err
                               + " - previous results are still in force.")
        self.lbl_bench.setStyleSheet(
            "color:" + getattr(self, "_warn_c", "#e6b45a") + ";")
        self.lbl_bench.show()

    def _on_reprobe_done(self) -> None:
        self._bench_live = False
        self.btn_bench.setEnabled(True)
        self.btn_bench.setText("Re-benchmark this PC")
        self.bench_bar.hide()
        if not self.lbl_bench.text().startswith("Benchmark could not"):
            self.lbl_bench.hide()
        cat = self.cat_box.currentText()
        prev = self._current_target()
        self._load_targets(cat)
        if prev:  # keep the user's pick when this machine can still do it
            for i in range(self.tgt_box.count()):
                item = self.tgt_box.model().item(i)
                if item.data(Qt.ItemDataRole.UserRole) == prev and \
                        item.isEnabled():
                    self.tgt_box.setCurrentIndex(i)
                    break
        # Same target selected fires no currentIndexChanged - without this
        # the details box stays EMPTY after benchmarking (user-reported).
        self._load_encoders(0)
        self._load_devices()  # names/reasons re-sync to the NEW report

    def _update_about(self) -> None:
        """Details block: what the chosen target means in plain words + the
        machine's MEASURED speeds (from the probe's trial encodes)."""
        cat = self.cat_box.currentText()
        if not hasattr(self, "lbl_about") or not hasattr(self, "_row"):
            return  # construction order: first addItems fires before panels exist
        target = self._current_target()
        tinfo = self._target_info(cat, target) if target else None
        lines = []
        if tinfo is not None and tinfo.available:
            lines = self.ctrl.about_lines(
                cat, target, self.enc_box.currentData() or "")
        self.lbl_about.setText("\n".join(lines))
        self._est_dirty()
        self.lbl_about.setVisible(bool(lines))
        self._toggle_row(self._row["about"], bool(lines))

    def _sync_preset_enabled(self) -> None:
        """x264/x265 speak the speed-word ladder; SVT-AV1/aom/hardware
        encoders do not - a word fed to them makes ffmpeg fail, so the row
        goes grey with the machine's own reason (never a silent lie)."""
        cat = self.cat_box.currentText()
        target = self._current_target()
        rate = cat == "video" and not self.chk_remux.isChecked()
        enc = self.enc_box.currentData()
        if not enc and target:  # "auto": the ladder head is what runs
            enc = (self.ctrl.encoders(cat, target) or [""])[0]
        ok = bool(rate) and enc in ("libx264", "libx265")
        self.preset_box.setEnabled(ok)
        self.preset_box.setToolTip(
            "Slower presets squeeze more quality out of the same file size.\n"
            "Estimates are re-MEASURED with whatever is chosen here."
            if ok else
            "This encoder keeps its own speed settings - the row applies\n"
            "to x264/x265 only. The estimate still measures what THIS\n"
            "machine really does with your other choices.")

    def _update_enc_note(self) -> None:
        """Human words for what the encoder choice MEANS; raw engine ids go
        to the tooltip. (Live-test finding: 'ffmpeg/libaom-av1' shown next
        to 'auto' meant nothing AND implied a false promise - auto follows
        the verified ladder, not the static matrix row.)"""
        self._update_about()
        cat = self.cat_box.currentText()
        target = self._current_target()
        tinfo = self._target_info(cat, target) if target else None
        raw = tinfo.note if tinfo is not None else ""
        ladder = self.ctrl.encoders(cat, target) if target else []
        if not ladder:
            dev = (self.ctrl.device_choice() if target else None)
            if dev is not None and cat in ("video", "audio"):
                # device pinned: the empty ladder is the DEVICE's fact, say
                # so with the probe's own words (doctrine: visible + reason)
                if dev.get("disabled_kind") == "measuring":
                    # mid-benchmark: 'no verified encoder' would be a
                    # verdict from a measurement still running (2026-11)
                    self.lbl_note.setText(f"{dev['name']} - {dev['why']}")
                    self.lbl_note.setToolTip(dev["why"])
                    return
                if not dev["why"]:
                    # device WORKS (other codecs verified) but none for this
                    # Target: naming the device's own success as a "why"
                    # would print empty parentheses (Radeon + AV1, 2026-11)
                    self.lbl_note.setText(
                        f"{dev['name']} works on this PC, but has no "
                        "encoder for the Target you chose that passed the "
                        "benchmark - pick another Target, or another device "
                        "under CPU / GPU.")
                    self.lbl_note.setToolTip(
                        f"{dev['name']} verified encoders do not cover this "
                        "Target. Re-benchmark this PC to measure again.")
                    return
                self.lbl_note.setText(
                    f"{dev['name']} has no verified encoder for this here"
                    f" ({dev['why']}).")
                self.lbl_note.setToolTip(dev["why"])
                return
            # non-codec targets: engine picks internally; chain label is the
            # only honest statement, raw ids stay in the tooltip (F5)
            self.lbl_note.setText("Engine chain: " + raw if raw else "")
            self.lbl_note.setToolTip(raw)
            return
        enc = self.enc_box.currentData()
        if enc:
            # never "the auto pick below": the CPU/GPU picker has its OWN
            # "Auto" row underneath, and users read this line pointing AT IT
            # (screenshot 2026-11). Name the widget the choice lives in.
            self.lbl_note.setText(
                f"Using {self.ctrl.human_encoder(enc)} - chosen under "
                "Encoder, so auto (fastest verified) is not used.")
            self.lbl_note.setToolTip(f"{raw}\nselected: {enc}".strip())
            return
        self.lbl_note.setText(
            "auto uses this machine's fastest verified encoder: "
            f"{self.ctrl.human_encoder(ladder[0])}.")
        self.lbl_note.setToolTip(raw)

    # ------------------------------------------------- P3 option enablement
    def _sync_options_enabled(self) -> None:
        """Grey-with-reason for every P3 control. The machine's facts come
        from the controller, not from guesses here. (F3's "never hide" is
        about TARGETS the machine cannot reach; controls that MEAN nothing
        for the selected category are hidden - live-user feedback: audio
        must not show PDF rows.)"""
        self._apply_option_visibility()
        cat = self.cat_box.currentText()
        is_v = cat == "video"
        remux = is_v and self.chk_remux.isChecked()
        self.quality.setEnabled(cat == "image")
        self.deep_pdf.setEnabled(cat == "pdf")
        self.chk_remux.setEnabled(is_v)
        self.remux_box.setEnabled(remux)
        tonemap_ok = self.ctrl.tonemap_available
        self.chk_tonemap.setEnabled(is_v and not remux and tonemap_ok)
        self.chk_tonemap.setToolTip("" if tonemap_ok else
                                    "this ffmpeg lacks the zscale+tonemap "
                                    "chain (probed)")
        self.chk_audio_replace.setEnabled(is_v and not remux)
        self.audio_kbps.setEnabled(is_v and not remux
                                   and self.chk_audio_replace.isChecked())
        rate = is_v and not remux
        self.rad_crf.setEnabled(rate)
        self.rad_size.setEnabled(rate)
        self._sync_preset_enabled()
        self._sync_quality_scale()
        self.crf_box.setEnabled(rate and self.rad_crf.isChecked())
        self.qslider.setEnabled(rate and self.rad_crf.isChecked())
        self.size_box.setEnabled(rate and self.rad_size.isChecked())
        if remux:
            # codec/encoder are meaningless for a stream copy: disabled but
            # visible; the container comes from the picker above (F2)
            self.enc_box.setEnabled(False)
            self.tgt_box.setEnabled(False)
            self.tgt_box.setToolTip(
                "remux keeps the existing streams; the container is chosen "
                "in 'Remux into'")
        else:
            self.tgt_box.setEnabled(True)
            self.tgt_box.setToolTip("")
            self.enc_box.setEnabled(self.enc_box.count() > 0)
        self._update_remux_status()

    # ---------------------------------------------- adaptive panel (per category)
    @staticmethod
    def _layout_has(layout, widget) -> bool:
        i = 0
        while (li := layout.itemAt(i)) is not None:
            if li.widget() is widget:
                return True
            if li.layout() and MainWindow._layout_has(li.layout(), widget):
                return True
            i += 1
        return False

    def _row_of(self, widget) -> int:
        """Form row index that contains WIDGET (fields, labels, and widgets
        nested inside a row's sub-layout all count)."""
        for r in range(self._form.count()):
            for role in (QFormLayout.ItemRole.LabelRole,
                         QFormLayout.ItemRole.FieldRole):
                item = self._form.itemAt(r, role)
                if item is None:
                    continue
                if item.widget() is widget:
                    return r
                if item.layout() and self._layout_has(item.layout(), widget):
                    return r
        raise LookupError(f"widget not in form: {widget!r}")

    @staticmethod
    def _set_layout_visible(layout, visible: bool) -> None:
        i = 0
        while (li := layout.itemAt(i)) is not None:
            if li.widget():
                li.widget().setVisible(visible)
            if li.layout():
                MainWindow._set_layout_visible(li.layout(), visible)
            i += 1

    def _toggle_row(self, row: int, visible: bool) -> None:
        """Hide/show BOTH cells of a form row - a QFormLayout row is not a
        widget, so toggling only the field would strand its label."""
        for role in (QFormLayout.ItemRole.LabelRole,
                     QFormLayout.ItemRole.FieldRole):
            item = self._form.itemAt(row, role)
            if item is None:
                continue
            if item.widget():
                item.widget().setVisible(visible)
            elif item.layout():
                self._set_layout_visible(item.layout(), visible)

    def _apply_option_visibility(self) -> None:
        """The panel adapts to the CURRENT selection: only options that mean
        something for the chosen category/target are shown (live-user
        feedback: audio must not show PDF rows). Unsupported TARGETS stay
        visible and greyed (F3) - that rule is untouched; this hides
        CONTROLS, which is a different thing."""
        if not hasattr(self, "_row"):
            return          # signals fire while _build is still assembling
        cat = self.cat_box.currentText()
        video = cat == "video"
        # encoder row matters only where codecs exist; its note row only with text
        self._toggle_row(self._row["enc"], self.enc_box.count() > 0)
        self._toggle_row(self._row["remux"], video)
        self._toggle_row(self._row["remux_into"], video and self.chk_remux.isChecked())
        self._toggle_row(self._row["tone"], video)
        # advanced rows are always open (user feedback: collapsed = invisible);
        # they only hide for categories that have no quality/audio knobs
        self._toggle_row(self._row["adv_btn"], video)
        self._toggle_row(self._row["adv"], video)
        self._toggle_row(self._row["rate"], video)
        self._toggle_row(self._row["quality"], cat == "image")
        # per-page/text exports bypass the lossless ladder - a deep pass is
        # only meaningful when the pdf target IS the pdf
        deep_ok = (cat == "pdf" and bool(self._current_target())
                   and self.ctrl.options_for("pdf", self._current_target())
                   .get("target") is None)
        self._toggle_row(self._row["pdf"], deep_ok)
        self._toggle_row(self._row["note"], bool(self.lbl_note.text()))
        self._toggle_row(self._row["remuxmsg"], video and bool(self.lbl_remux.text()))

    def _update_remux_status(self) -> None:
        """Show the machine's own copy-safety verdict (F3). Advisory: it is
        evaluated against the first video file; the engine re-checks every
        single job at run time and fails fast with its own named reason."""
        self.lbl_remux.setText("")
        if self.cat_box.currentText() != "video" or not self.chk_remux.isChecked():
            return
        src = self._first_video_file()
        if src is None:
            self.lbl_remux.setStyleSheet("color:#555;")
            self.lbl_remux.setText("Add a video file to check copy safety")
            return
        reason = self.ctrl.remux_block_reason(src, self.remux_box.currentText())
        self.lbl_remux.setToolTip(
            "checked against the first video file in the list; every job is "
            "re-checked by the engine when it runs")
        if reason:
            self.lbl_remux.setStyleSheet(self._warn_c)
            self.lbl_remux.setText("⚠ " + reason)
        else:
            self.lbl_remux.setStyleSheet(self._ok_c)
            self.lbl_remux.setText(
                f"✓ {src.name}: streams copy losslessly into "
                f"{self.remux_box.currentText()}")

    # ---------------------------------------------------------------- preview
    def _current_file(self) -> Path | None:
        it = self.files.currentItem()
        if it is None:
            return None
        p = it.data(Qt.ItemDataRole.UserRole)
        return Path(p) if p else None

    def _current_file_is_video(self) -> bool:
        src = self._current_file()
        return bool(src) and self.ctrl.category_of(src) == "video"

    def _first_video_file(self) -> Path | None:
        for i in range(self.files.count()):
            p = self.files.item(i).data(Qt.ItemDataRole.UserRole)
            if p and self.ctrl.category_of(p) == "video":
                return Path(p)
        return None

    def _preview(self) -> None:
        src = self._current_file()
        self.lbl_prev.setText("")
        if src is None:
            return
        out = self.ctrl.first_frame(src)
        pm = QPixmap(str(out)) if out is not None else QPixmap()
        if pm.isNull():
            # measured as unmeasurable: say so, never invent a picture (F3)
            self.lbl_prev.setText(
                "preview unavailable (no decodable first frame - reported "
                "as measured)")
            return
        self.lbl_prev.setPixmap(pm.scaledToHeight(
            120, Qt.TransformationMode.SmoothTransformation))

    def _current_target(self) -> str | None:
        idx = self.tgt_box.currentIndex()
        if idx < 0:
            return None
        item = self.tgt_box.model().item(idx)
        return item.data(Qt.ItemDataRole.UserRole)

    def _target_info(self, cat: str, target: str):
        for t in self.ctrl.targets(cat):
            if t.target == target:
                return t
        return None

    def _run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        dst = self.dst_edit.text().strip()
        if not dst:
            self.lbl_status.setText("Pick a destination folder.")
            return
        cat = self.cat_box.currentText()
        target = self._current_target()
        if not target:
            self.lbl_status.setText("No target selected (none available?).")
            return
        remux = cat == "video" and self.chk_remux.isChecked()
        enc = (self._effective_encoder() or None) if not remux else None
        if (not remux and not enc and self.ctrl.device() != "auto"
                and cat in ("video", "audio")):
            # device pinned, nothing verified ON IT for this target: refuse
            # with the machine's own reason - never silently run on other
            # hardware the user did not pick
            d = self.ctrl.device_choice() or {}
            self.lbl_status.setText(
                f"{d.get('name', 'That device')} cannot do this on this PC"
                f" ({d.get('why', 'no verified encoder for it')}).")
            return
        # P3 controls map 1:1 onto controller kwargs; the controller (not the
        # widgets) decides what they mean, incl. CRF-vs-size exclusivity.
        kw: dict = dict(encoder=enc, quality=self.quality.value(),
                        deep_pdf=self.deep_pdf.isChecked())
        if remux:
            # remux is an operation, not a codec choice: nothing else rides
            # along (the controller mirrors this contract, F2)
            kw = {"remux": True}
        elif cat == "video":
            kw["tonemap"] = self.chk_tonemap.isChecked()
            kw["audio_replace"] = self.chk_audio_replace.isChecked()
            if self.chk_audio_replace.isChecked():
                kw["audio_bitrate"] = self.audio_kbps.currentText()
            kw["preset"] = self.preset_box.currentText()
            if self.rad_size.isChecked():
                kw["size_mb"] = float(self.size_box.value())
            else:
                kw["crf"] = int(self.crf_box.value())
        ext = self.remux_box.currentText() if remux else None
        added = 0
        refused: list[str] = []
        for i in range(self.files.count()):
            it = self.files.item(i)
            src = it.data(Qt.ItemDataRole.UserRole)
            if not src:
                continue
            # the picker applies to its category only: never silently
            # enqueue a file the row wasn't chosen for
            if self.ctrl.category_of(src) != cat:
                continue
            # One unreadable file (encrypted PDF, corrupt header) must not
            # kill this loop mid-way: files already queued would sit
            # "queued" forever because the worker line below never ran,
            # with no message anywhere. Each file gets its own verdict, in
            # this machine's own words - nothing is silently dropped.
            try:
                self.ctrl.enqueue(Path(src), Path(dst), cat, target,
                                  ext=ext, **kw)
                added += 1
            except Exception as exc:  # noqa: BLE001
                refused.append(f"{Path(src).name}: {exc}")
        if refused:
            detail = "\n".join(refused)
            self.lbl_status.setToolTip(detail)
            shown = "; ".join(refused[:3])
            if len(refused) > 3:
                shown += f" (+{len(refused) - 3} more, hover for all)"
        if not added:
            # non-modal: a blocking dialog would also stall the offscreen
            # test tier; the status line is where users look after a click
            self.lbl_status.setText(
                f"Refused {len(refused)}: {shown}" if refused else
                "Nothing to do - no listed file matches this category.")
            return
        self._start_worker()
        if refused:
            self.lbl_status.setText(f"Refused {len(refused)}: {shown}")

    def _start_worker(self) -> None:
        self.btn_run.setEnabled(False)
        self.bar.setVisible(True)
        self._worker = threading.Thread(target=self._worker_body, daemon=True)
        self._worker.start()

    def _worker_body(self) -> None:
        # runs OFF the GUI thread: touch no widgets here. The only safe
        # cross-thread call is emitting a signal, which Qt queues to us.
        try:
            self.ctrl.process()
        finally:
            self.run_done.emit()

    def _on_worker_done(self) -> None:
        self.btn_run.setEnabled(True)
        self.bar.setVisible(False)
        # jobs that landed from a watch folder DURING this run need a fresh
        # pass (queue.run snapshots eligible once) - pump now, not in 500ms
        self._drain_watch()

    def refresh_queue(self) -> None:
        rows = self.ctrl.snapshot()
        self.table.setRowCount(len(rows))
        done = sum(1 for r in rows if not r.live)
        for i, r in enumerate(rows):
            src = Path(r.src).name
            self.table.setItem(i, 0, QTableWidgetItem(src))
            self.table.setItem(i, 1, QTableWidgetItem(Path(r.dst).name))
            st = QTableWidgetItem(r.state)
            self.table.setItem(i, 2, st)
            # human words in the table, raw tool ids in the tooltip (SPEC F.1)
            detail = r.reason or (self.ctrl.human_encoder(r.encoder)
                                  if r.encoder else "")
            item = QTableWidgetItem(detail)
            if r.reason and r.encoder:
                item.setToolTip(f"encoder: {r.encoder}")
            self.table.setItem(i, 3, item)
        self.lbl_status.setText(
            f"{done}/{len(rows)} settled" if rows else "queue empty")

    # ------------------------------------------------- pause/resume/cancel
    def _pause(self) -> None:
        n = self.ctrl.pause_queue()
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(True)
        self.refresh_queue()
        self.lbl_status.setText(
            f"Paused {n} pending job(s) — any job already running will finish.")

    def _resume(self) -> None:
        n = self.ctrl.resume_queue()
        self.btn_pause.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.refresh_queue()
        self.lbl_status.setText(f"Resumed {n} job(s).")

    def _cancel_pending(self) -> None:
        n = self.ctrl.cancel_pending()
        self.refresh_queue()
        self.lbl_status.setText(
            f"Cancelled {n} job(s) (shown as cancelled by user) — in-flight "
            f"jobs still finish.")

    # --------------------------------------------------------- watch folders
    def _watch_add_dialog(self) -> None:
        # sidebar rebuilt from the live mount table on every open
        # (see _sidebar_urls_with_fresh_mounts)
        src = _show_existing_dir_dialog(
            self, "Folder to watch (new files get converted)",
            str(Path.home()))
        if not src:
            return
        dst = _show_existing_dir_dialog(
            self, "Where converted files go (must be a different tree)",
            src)
        if not dst:
            return
        self._watch_add(Path(src), Path(dst))

    def _watch_add(self, src: Path, dst: Path) -> None:
        key = (str(src), str(dst))
        if (src, dst) in self._watch_rules:
            if key not in self._watch_missing:
                self.lbl_watch.setText("⚠ already watching that folder")
                return
            # the drive this saved rule lives on is back: re-arming the SAME
            # rule (never a silent edit) resumes watching it
            err = self._watch_attach(src, dst)
            if err:
                self.lbl_watch.setText("⚠ " + err)
                return
            self._watch_missing.discard(key)
            self._sync_watch_list()
            self.lbl_watch.setText(
                f"resumed {src} — watching "
                f"{len(self._watch_rules) - len(self._watch_missing)} "
                "folder(s)")
            return
        err = self._watch_attach(src, dst)
        if err:
            # honest refusal (dst==src, nested trees, not a dir): say it, keep
            # the existing watcher running (F3)
            self.lbl_watch.setText("⚠ " + err)
            return
        self._watch_rules.append((src, dst))
        self._persist_watch_rules()
        self._sync_watch_list()
        self.lbl_watch.setText(
            f"watching {len(self._watch_rules)} folder(s) — drops are queued "
            "and converted automatically with default quality")

    def _watch_attach(self, src: Path, dst: Path) -> str | None:
        """Arm one rule on the live watcher, starting it if none runs yet.
        Returns the refusal text, or None on success. F3: a rejected rule
        must not leave a dead watcher - the instance is assigned only once
        fully valid. OSError too: a failed inotify arm must never kill the
        app at startup."""
        try:
            if self._watcher is None:
                w = self._new_watcher()
                w.add_folder(src, dst)  # schedule BEFORE start (live API)
                w.start()
                self._watcher = w
            else:
                self._watcher.add_folder(src, dst)  # live-scheduled
        except (ValueError, RuntimeError, OSError) as exc:
            return str(exc)
        return None

    def _persist_watch_rules(self) -> None:
        # prefs schema: list of [src, dst] string pairs (JSON-safe, shared
        # with any future headless reader of the same file)
        self.ctrl.set_pref("watch_folders",
                           [[str(s), str(d)] for s, d in self._watch_rules])

    def _restore_watch_folders(self) -> None:
        """Re-arm saved watch rules at startup. A folder that is not here
        right now (unplugged drive) stays LISTED with a marker and stays in
        prefs — nothing is silently dropped; picking the same folder again
        after remounting resumes exactly that rule."""
        saved: list[tuple[str, str]] = []
        raw = self.ctrl.prefs().get("watch_folders", [])
        for item in raw if isinstance(raw, list) else []:
            if (isinstance(item, (list, tuple)) and len(item) == 2
                    and all(isinstance(x, str) and x.strip() for x in item)):
                pair = (str(Path(item[0])), str(Path(item[1])))
                if pair not in saved:
                    saved.append(pair)
        missing = 0
        for src_s, dst_s in saved:
            self._watch_rules.append((Path(src_s), Path(dst_s)))
            if self._watch_attach(Path(src_s), Path(dst_s)):
                self._watch_missing.add((src_s, dst_s))
                missing += 1
        if not saved:
            return
        self._sync_watch_list()
        if missing:
            self.lbl_watch.setText(
                f"⚠ {missing} saved folder(s) are not here right now "
                "(unplugged drive?) — the rest are watching; pick the same "
                "folder again to resume one")
        else:
            self.lbl_watch.setText(
                f"watching {len(self._watch_rules)} folder(s) — resumed from "
                "last session")

    def _new_watcher(self):
        from ..watch import FolderWatcher
        return FolderWatcher(on_settled=make_queue_callback(
            self.ctrl, note=self._watch_note))

    def _watch_note(self, line: str) -> None:
        # watcher-thread door: ONLY touch the SimpleQueue here
        self._watch_events.put(line)

    def _watch_remove_selected(self) -> None:
        row = self.watch_list.currentRow()
        if row < 0 or row >= len(self._watch_rules):
            return
        src, dst = self._watch_rules.pop(row)
        self._watch_missing.discard((str(src), str(dst)))
        self._persist_watch_rules()
        old, self._watcher = self._watcher, None
        if old is not None:
            old.stop()
        # watchdog gives no un-schedule; rebuild the watcher from remaining
        # rules (cheap: folders only, no state to lose - jobs are journalled)
        if self._watch_rules:
            w = self._new_watcher()
            armed = 0
            for src, dst in self._watch_rules:
                try:
                    w.add_folder(src, dst)
                    armed += 1
                except (ValueError, RuntimeError, OSError):
                    # a rule that was already missing stays missing; removing
                    # another folder must not drop it or kill the rebuild
                    self._watch_missing.add((str(src), str(dst)))
            if armed:
                w.start()
                self._watcher = w
        self._sync_watch_list()
        self.lbl_watch.setText(
            f"watching {len(self._watch_rules)} folder(s)" if self._watch_rules
            else "no watch folders")

    def _sync_watch_list(self) -> None:
        self.watch_list.clear()
        for src, dst in self._watch_rules:
            line = f"{src}  →  {dst}"
            if (str(src), str(dst)) in self._watch_missing:
                line += "   (folder missing)"
            self.watch_list.addItem(line)

    def _drain_watch(self) -> None:
        lines = []
        while not self._watch_events.empty():
            lines.append(self._watch_events.get())
        for line in lines[-25:]:  # bounded history; the log has it all (F26)
            self.watch_log.addItem(line)
            self.watch_log.scrollToBottom()
            if line.startswith("queued"):
                self._watch_more = True
        if self._watch_more:
            self._pump_watch_jobs()

    def _pump_watch_jobs(self) -> None:
        """Watch-queued jobs need their own worker pass: JobQueue.run()
        snapshots eligible jobs once, so anything enqueued mid-run must be
        picked up by a fresh run when the worker is idle again."""
        if not self._watch_more or (self._worker and self._worker.is_alive()):
            return
        self._watch_more = False
        self._start_worker()

    def _serve_dialog(self) -> None:
        from .serve_dialog import ServeDialog
        if self._serve_dlg is None:
            self._serve_dlg = ServeDialog(self.serve, self)
            self._serve_dlg.finished.connect(self._serve_status)
        self._serve_dlg.show()
        self._serve_dlg.raise_()

    def _serve_status(self) -> None:
        st = self.serve.status()
        if st["mode"] == "lan" and st["running"]:
            self.lbl_serve.setText("serving on your home network - "
                                   + st["url"])
        elif st["mode"] == "local" and st["running"]:
            self.lbl_serve.setText("serving on this computer only")
        else:
            self.lbl_serve.setText("not serving")

    def _apply_throttle(self, on: bool) -> None:
        """Checkbox is the only switch. Off: governor detached from the
        queue and stopped - children released, cap thread gone. On: started
        and attached. Already-running engines keep the priority class they
        were born with; everything launched after the click obeys it."""
        gov = self._governor
        if on:
            gov.enabled = True
            gov.start()
        else:
            gov.enabled = False
            gov.stop()
        self.ctrl.queue.governor = gov if on else None
        self.ctrl.set_pref("throttle", bool(on))

    def closeEvent(self, event):  # noqa: N802
        # Signal every background measurer FIRST: the joins below are
        # bounded but can still take seconds, and an unsignalled
        # benchmark/estimate pass keeps encoding full-machine while the
        # window is already gone. A running conversion queue is told to
        # stop taking further jobs; whatever ffmpeg is mid-file when the
        # process exits replays as interrupted on next start (journal
        # recovery), never as a silent success.
        self._bench_cancel = True
        if self._governor is not None:
            self._governor.stop()  # release stopped children BEFORE exit:
            self._governor = None  # nothing may linger throttled (or killed
                                   # by a job-object close) after we go
        est = getattr(self, "_est_stop", None)
        if est is not None:
            est.set()
        try:
            self.ctrl.queue.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise here
            pass
        if self._watcher is not None:
            self._watcher.stop()  # joins observer + poll threads (daemons too)
        self.serve.stop()  # closing the app must close the HTTP door
        super().closeEvent(event)

    # ------------------------------------------------------------- drops
    def dropEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file():
                self._add_one(p)
        event.acceptProposedAction()

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication(argv if argv is not None else sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
