"""PySide6 desktop shell (P2).

Thin on purpose: every decision comes from ConverterController so the whole
surface is testable offscreen. Rows the machine cannot do stay visible but
disabled, with the machine's own reason as tooltip (F3); royalty/compat
targets show ℹ️ (F14); HDR sources get an explicit warning (F15).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .controller import REMUX_CONTAINERS, ConverterController


class MainWindow(QMainWindow):
    # emitted from the worker thread; Qt queues it onto the GUI thread
    # (QTimer.singleShot from a non-GUI thread is a trap - kills timers
    # from the wrong thread at teardown)
    run_done = Signal()

    def __init__(self, controller: ConverterController | None = None) -> None:
        super().__init__()
        self.ctrl = controller or ConverterController()
        self._worker: threading.Thread | None = None
        self.run_done.connect(self._on_worker_done)
        self.setWindowTitle("Transmute")
        self.resize(980, 640)
        self.setAcceptDrops(True)
        self._build()
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self.refresh_queue)
        self._poll.start()
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
        splitter.addWidget(left)

        # right: what this machine can do, chosen from the probed matrix
        right = QGroupBox("Convert to (from this machine's capability report)")
        rv = QFormLayout(right)
        self.cat_box = QComboBox()
        self.cat_box.addItems(self.ctrl.categories())
        self.cat_box.currentTextChanged.connect(self._load_targets)
        rv.addRow("Category", self.cat_box)
        self.tgt_box = QComboBox()
        self.tgt_box.currentIndexChanged.connect(self._load_encoders)
        rv.addRow("Target", self.tgt_box)
        self.enc_box = QComboBox()
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
        self.lbl_remux.setStyleSheet("color:#b3541e;")
        rv.addRow(self.lbl_remux)
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
        names, _ = QFileDialog.getOpenFileNames(
            self, "Add files", str(Path.home()))
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
        d = QFileDialog.getExistingDirectory(self, "Destination",
                                             self.dst_edit.text() or str(Path.home()))
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
            self.lbl_hdr.setText("ℹ ffprobe missing: HDR status unknown.")

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
                # disabled but VISIBLE: the reason must be readable (F3)
                item.setEnabled(False)
                item.setToolTip(t.reason)
            elif t.compat_flag:
                item.setToolTip("Patent/royalty-encumbered codec; offered only "
                                "because this machine can encode it (F14).")
            else:
                item.setToolTip(t.note)
        self._load_encoders(0)

    def _load_encoders(self, _idx: int) -> None:
        cat = self.cat_box.currentText()
        target = self._current_target()
        self.enc_box.clear()
        if not target:
            return
        ladder = self.ctrl.encoders(cat, target)
        if ladder:
            self.enc_box.addItem("auto (fastest verified)")
            self.enc_box.addItems(ladder)
        self.enc_box.setEnabled(bool(ladder))
        tinfo = self._target_info(cat, target) if target else None
        note = ""
        if tinfo is not None:
            note = tinfo.note
        if not ladder and note:
            note = f"Engine chain: {note}"
        self.lbl_note.setText(note)
        self._sync_options_enabled()

    # ------------------------------------------------- P3 option enablement
    def _sync_options_enabled(self) -> None:
        """Grey-with-reason for every P3 control; never hide (F3). The
        machine's facts come from the controller, not from guesses here."""
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
            self.lbl_remux.setText("ℹ add a video file to check copy safety")
            return
        reason = self.ctrl.remux_block_reason(src, self.remux_box.currentText())
        self.lbl_remux.setToolTip(
            "checked against the first video file in the list; every job is "
            "re-checked by the engine when it runs")
        if reason:
            self.lbl_remux.setStyleSheet("color:#b3541e;")
            self.lbl_remux.setText("⚠ " + reason)
        else:
            self.lbl_remux.setStyleSheet("color:#2e7d32;")
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
        enc = self.enc_box.currentText() if self.enc_box.isEnabled() else ""
        enc = None if enc.startswith("auto") or not enc else enc
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
            detail = r.reason or r.encoder
            self.table.setItem(i, 3, QTableWidgetItem(detail))
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
