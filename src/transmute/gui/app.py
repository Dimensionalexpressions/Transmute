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
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .controller import ConverterController


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
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(80)
        rv.addRow("Quality (image)", self.quality)
        self.deep_pdf = QCheckBox("Deep PDF pass (LOSSY recompress)")
        rv.addRow("PDF", self.deep_pdf)
        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        rv.addRow(self.lbl_note)
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
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)
        self.lbl_status = QLabel("")
        ctl.addWidget(self.btn_run)
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
        if is_ignored(path):
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

    def _remove_selected(self) -> None:
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

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
            self.lbl_hdr.setText(
                "⚠ HDR source: converting to SDR unless you choose an HDR "
                "10-bit target; tone-mapping quality varies.")
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
        self.quality.setEnabled(cat == "image")
        self.deep_pdf.setEnabled(cat == "pdf")

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
            QMessageBox.warning(self, "Transmute", "Pick a destination folder.")
            return
        cat = self.cat_box.currentText()
        target = self._current_target()
        if not target:
            QMessageBox.warning(self, "Transmute",
                                "No target selected (none available?).")
            return
        enc = self.enc_box.currentText() if self.enc_box.isEnabled() else ""
        enc = None if enc.startswith("auto") or not enc else enc
        added = 0
        for i in range(self.files.count()):
            it = self.files.item(i)
            src = it.data(Qt.ItemDataRole.UserRole)
            if not src:
                continue
            if self.ctrl.category_of(src) is None:
                continue
            self.ctrl.enqueue(Path(src), Path(dst), cat, target,
                              encoder=enc, quality=self.quality.value(),
                              deep_pdf=self.deep_pdf.isChecked())
            added += 1
        if not added:
            QMessageBox.information(self, "Transmute", "Nothing to do.")
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
