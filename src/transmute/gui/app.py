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

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .controller import REMUX_CONTAINERS, ConverterController
from ..watch import make_queue_callback


class MainWindow(QMainWindow):
    # emitted from the worker thread; Qt queues it onto the GUI thread
    # (QTimer.singleShot from a non-GUI thread is a trap - kills timers
    # from the wrong thread at teardown)
    run_done = Signal()
    reprobe_done = Signal()

    def __init__(self, controller: ConverterController | None = None) -> None:
        super().__init__()
        self.ctrl = controller or ConverterController()
        # persisted GPU pref must be in place BEFORE any probe runs,
        # so the report never claims GPU support the user switched off
        if self.ctrl.prefs().get("use_gpu", True) is False:
            os.environ["TRANSMUTE_DISABLE_GPU"] = "1"
        self._worker: threading.Thread | None = None
        # watch-folders state (F24): the watcher thread only posts text lines
        # into the SimpleQueue; everything else happens on the GUI thread.
        self._watcher = None                     # FolderWatcher, lazy
        self._watch_rules: list[tuple[Path, Path]] = []
        self._watch_events: pyqueue.SimpleQueue[str] = pyqueue.SimpleQueue()
        self._watch_more = False                 # watch jobs await a worker
        self.run_done.connect(self._on_worker_done)
        self.reprobe_done.connect(self._on_reprobe_done)
        self.setWindowTitle("Transmute")
        self.resize(980, 640)
        self.setAcceptDrops(True)
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
        splitter.addWidget(left)

        # right: what this machine can do, chosen from the probed matrix
        right = QGroupBox("Convert to (from this machine's capability report)")
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
        self.btn_bench = QPushButton("Re-benchmark this PC")
        self.btn_bench.setToolTip(
            "Test-encodes a 720p sample on every encoder found here, then "
            "updates the speed words above. Takes about a minute; safe to "
            "run any time.")
        self.btn_bench.clicked.connect(self._start_reprobe)
        rv.addRow("", self.btn_bench)
        self.enc_box = QComboBox()
        self.enc_box.currentIndexChanged.connect(
            lambda *_: (self._update_enc_note(),
                        self._apply_option_visibility()))
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
        self.chk_audio_replace = QCheckBox("Force audio re-encode (AAC)")
        self.chk_audio_replace.setToolTip(
            "Default only replaces audio when the target container rejects "
            "the existing codec; this forces it always.")
        rv.addRow("", self.chk_audio_replace)

        # More… options (user directive): the graphics-card switch changes
        # what the capability report may claim - off means CPU-only truth.
        more = QGroupBox("More…")
        mform = QFormLayout(more)
        self.chk_gpu = QCheckBox("Use graphics card if it is faster (recommended)")
        self.chk_gpu.setChecked(os.environ.get(
            "TRANSMUTE_DISABLE_GPU", "").lower() not in ("1", "true", "yes"))
        self.chk_gpu.setToolTip(
            "Off: the app will not use or recommend GPU encoders on this\n"
            "machine - the capability report is re-measured without them.\n"
            "Graphics encoding keeps your CPU free but files can be larger.")
        self.chk_gpu.toggled.connect(self._on_gpu_toggle)
        mform.addRow(self.chk_gpu)
        rv.addRow(more)
        qlrow = QHBoxLayout()
        self.rad_crf = QRadioButton("Quality (CRF)")
        self.rad_crf.setChecked(True)
        self.crf_box = QSpinBox()
        self.crf_box.setRange(0, 51)
        self.crf_box.setValue(23)
        self.rad_size = QRadioButton("Size target (MB)")
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(0.5, 100000.0)
        self.size_box.setValue(700.0)
        self.size_box.setToolTip(
            "Approximate for codecs outside x264/x265 (no reliable rate "
            "control); the report says whether the result is within budget.")
        for w in (self.rad_crf, self.rad_size):
            w.toggled.connect(self._sync_options_enabled)
        qlrow.addWidget(self.rad_crf)
        qlrow.addWidget(self.crf_box)
        qlrow.addWidget(self.rad_size)
        qlrow.addWidget(self.size_box)
        qlrow.addStretch(1)
        rv.addRow("Video rate", qlrow)
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
            "audio": self._row_of(self.chk_audio_replace),
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
        # NON-native by deliberate choice: Qt's own dialog enumerates mounts
        # when it opens, so a drive plugged in since launch appears (the KDE
        # native dialog served a stale cached bookmark - real user, 2026-11).
        names, _ = QFileDialog.getOpenFileNames(
            self, "Add files", str(Path.home()),
            options=QFileDialog.Option.DontUseNativeDialog)
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

    def _remove_selected(self) -> None:
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))
        self._update_remux_status()

    def _choose_dst(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Destination", self.dst_edit.text() or str(Path.home()),
            options=QFileDialog.Option.DontUseNativeDialog)
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

    def _on_gpu_toggle(self, on: bool) -> None:
        self.ctrl.set_pref("use_gpu", bool(on))
        if on:
            os.environ.pop("TRANSMUTE_DISABLE_GPU", None)
        else:
            os.environ["TRANSMUTE_DISABLE_GPU"] = "1"
        # the report must match the switch: re-measure this machine now
        self._start_reprobe()

    def _start_reprobe(self) -> None:
        if (self._worker is not None and self._worker.is_alive()) or \
                not self.btn_bench.isEnabled():
            return  # a conversion is hogging the CPUs - measure later
        self.btn_bench.setEnabled(False)
        self.btn_bench.setText("Measuring this PC… about a minute")
        self.lbl_about.setText("")
        self._toggle_row(self._row["about"], False)
        threading.Thread(target=self._reprobe_body, daemon=True).start()

    def _reprobe_body(self) -> None:
        # OFF the GUI thread: touch no widgets; signal only.
        try:
            self.ctrl.reprobe()
        finally:
            self.reprobe_done.emit()

    def _on_reprobe_done(self) -> None:
        self.btn_bench.setEnabled(True)
        self.btn_bench.setText("Re-benchmark this PC")
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
        self.lbl_about.setVisible(bool(lines))
        self._toggle_row(self._row["about"], bool(lines))

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
            # non-codec targets: engine picks internally; chain label is the
            # only honest statement, raw ids stay in the tooltip (F5)
            self.lbl_note.setText("Engine chain: " + raw if raw else "")
            self.lbl_note.setToolTip(raw)
            return
        enc = self.enc_box.currentData()
        if enc:
            self.lbl_note.setText(
                f"Using {self.ctrl.human_encoder(enc)} - overrides the auto "
                "pick below.")
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
        rate = is_v and not remux
        self.rad_crf.setEnabled(rate)
        self.rad_size.setEnabled(rate)
        self.crf_box.setEnabled(rate and self.rad_crf.isChecked())
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
        self._toggle_row(self._row["audio"], video)
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
        enc = (self.enc_box.currentData() or None) if self.enc_box.isEnabled() else None
        # P3 controls map 1:1 onto controller kwargs; the controller (not the
        # widgets) decides what they mean, incl. CRF-vs-size exclusivity.
        kw: dict = dict(encoder=enc, quality=self.quality.value(),
                        deep_pdf=self.deep_pdf.isChecked())
        remux = cat == "video" and self.chk_remux.isChecked()
        if remux:
            # remux is an operation, not a codec choice: nothing else rides
            # along (the controller mirrors this contract, F2)
            kw = {"remux": True}
        elif cat == "video":
            kw["tonemap"] = self.chk_tonemap.isChecked()
            kw["audio_replace"] = self.chk_audio_replace.isChecked()
            if self.rad_size.isChecked():
                kw["size_mb"] = float(self.size_box.value())
            else:
                kw["crf"] = int(self.crf_box.value())
        ext = self.remux_box.currentText() if remux else None
        added = 0
        for i in range(self.files.count()):
            it = self.files.item(i)
            src = it.data(Qt.ItemDataRole.UserRole)
            if not src:
                continue
            # the picker applies to its category only: never silently
            # enqueue a file the row wasn't chosen for
            if self.ctrl.category_of(src) != cat:
                continue
            self.ctrl.enqueue(Path(src), Path(dst), cat, target,
                              ext=ext, **kw)
            added += 1
        if not added:
            # non-modal: a blocking dialog would also stall the offscreen
            # test tier; the status line is where users look after a click
            self.lbl_status.setText(
                "Nothing to do - no listed file matches this category.")
            return
        self._start_worker()

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
        opts = QFileDialog.Option.DontUseNativeDialog  # fresh mounts (SPEC F.3)
        src = QFileDialog.getExistingDirectory(
            self, "Folder to watch (new files get converted)",
            str(Path.home()), options=opts)
        if not src:
            return
        dst = QFileDialog.getExistingDirectory(
            self, "Where converted files go (must be a different tree)",
            src, options=opts)
        if not dst:
            return
        self._watch_add(Path(src), Path(dst))

    def _watch_add(self, src: Path, dst: Path) -> None:
        try:
            if self._watcher is None:
                w = self._new_watcher()
                w.add_folder(src, dst)  # schedule BEFORE start (live API)
                w.start()
                self._watcher = w  # assign only once fully valid (F3: a
            else:                  # rejected rule must not leave a dead watcher)
                self._watcher.add_folder(src, dst)  # live-scheduled
            self._watch_rules.append((src, dst))
        except (ValueError, RuntimeError) as exc:
            # honest refusal (dst==src, nested trees, not a dir): say it, keep
            # the existing watcher running (F3)
            self.lbl_watch.setText("⚠ " + str(exc))
            return
        self._sync_watch_list()
        self.lbl_watch.setText(
            f"watching {len(self._watch_rules)} folder(s) — drops are queued "
            "and converted automatically with default quality")

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
        self._watch_rules.pop(row)
        old, self._watcher = self._watcher, None
        if old is not None:
            old.stop()
        # watchdog gives no un-schedule; rebuild the watcher from remaining
        # rules (cheap: folders only, no state to lose - jobs are journalled)
        if self._watch_rules:
            w = self._new_watcher()
            for src, dst in self._watch_rules:
                w.add_folder(src, dst)  # same rules already validated once
            w.start()
            self._watcher = w
        self._sync_watch_list()
        self.lbl_watch.setText(
            f"watching {len(self._watch_rules)} folder(s)" if self._watch_rules
            else "no watch folders")

    def _sync_watch_list(self) -> None:
        self.watch_list.clear()
        for src, dst in self._watch_rules:
            self.watch_list.addItem(f"{src}  →  {dst}")

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

    def closeEvent(self, event):  # noqa: N802
        if self._watcher is not None:
            self._watcher.stop()  # joins observer + poll threads (daemons too)
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
