# -*- coding: utf-8 -*-
"""Dock widget for the live log viewer with filesystem watchdog support."""

import io
import os
import time
from datetime import datetime

from qgis.PyQt import QtCore, QtGui, QtWidgets


class LiveLogDock(QtWidgets.QDockWidget):
    def __init__(self, parent=None, get_paths=None):
        super().__init__("QGIS Monitor Pro — Live Log", parent)
        self.setObjectName("QGM_LiveLogDock")
        self.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea
        )

        self._get_paths = get_paths or (lambda: (None, None))
        self._f = None
        self._pos = 0
        self._last_size = 0
        self._last_path = None
        self._last_activity = 0.0
        self._idle_warned = False

        container = QtWidgets.QWidget(self)
        self.setWidget(container)
        root = QtWidgets.QVBoxLayout(container)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        controls = QtWidgets.QHBoxLayout()
        root.addLayout(controls)

        self.cbo_level = QtWidgets.QComboBox()
        self.cbo_level.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.txt_filter = QtWidgets.QLineEdit()
        self.txt_filter.setPlaceholderText("Filter op tekst of [Tag]")
        self.chk_auto = QtWidgets.QCheckBox("Auto-refresh")
        self.chk_auto.setChecked(True)
        self.spn_interval = QtWidgets.QDoubleSpinBox()
        self.spn_interval.setRange(0.5, 10.0)
        self.spn_interval.setDecimals(1)
        self.spn_interval.setSingleStep(0.5)
        self.spn_interval.setValue(1.0)
        self.spn_interval.setSuffix(" s")
        self.btn_pause = QtWidgets.QPushButton("Pauze")
        self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_open = QtWidgets.QPushButton("Open map")
        self.btn_pick = QtWidgets.QPushButton("Kies bestand")

        controls.addWidget(QtWidgets.QLabel("Level:"))
        controls.addWidget(self.cbo_level)
        controls.addWidget(self.txt_filter, 1)
        controls.addWidget(self.chk_auto)
        controls.addWidget(self.spn_interval)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.btn_clear)
        controls.addWidget(self.btn_open)
        controls.addWidget(self.btn_pick)

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.view.setFont(mono)
        root.addWidget(self.view, 1)

        self.lbl = QtWidgets.QLabel("—")
        root.addWidget(self.lbl)

        self.tmr = QtCore.QTimer(self)
        self.tmr.timeout.connect(self._tick)
        self._apply_interval()
        self.tmr.start()

        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(3000)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start()

        self._watcher = QtCore.QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)

        self._paused = False

        self.chk_auto.toggled.connect(self._toggle_auto)
        self.spn_interval.valueChanged.connect(self._apply_interval)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear.clicked.connect(self.view.clear)
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_pick.clicked.connect(self._pick_file)

    # ---- UI helpers -----------------------------------------------------
    def _apply_interval(self):
        self.tmr.setInterval(int(self.spn_interval.value() * 1000))

    def _toggle_auto(self, enabled: bool):
        if enabled and not self._paused:
            self.tmr.start()
        else:
            self.tmr.stop()

    def _toggle_pause(self):
        self._paused = not self._paused
        self.btn_pause.setText("Hervat" if self._paused else "Pauze")
        self._toggle_auto(self.chk_auto.isChecked())

    def _open_folder(self):
        log, _ = self._get_paths()
        if log and os.path.exists(log):
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(os.path.dirname(log))
            )

    def _pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Kies logbestand",
            "",
            "Log files (*.log *.txt);;Alle bestanden (*.*)",
        )
        if path:
            self._open_file(path, reset=True)

    # ---- Tail logic -----------------------------------------------------
    def _open_file(self, path: str, reset: bool = False):
        try:
            if self._f:
                try:
                    self._f.close()
                except Exception:
                    pass
            self._f = io.open(path, "r", encoding="utf-8", errors="ignore")
            self._last_path = path
            self._pos = 0 if reset else self._f.seek(0, os.SEEK_END)
            self._last_size = os.path.getsize(path)
            self._last_activity = time.time()
            self._idle_warned = False
            self._watch_file_and_dir(path)
            self._update_status_label()
        except Exception as exc:
            self._f = None
            self.lbl.setText(f"Kon bestand niet openen: {exc}")

    def _tick(self):
        if self._paused:
            return

        log, _ = self._get_paths()
        if log and (
            self._last_path is None
            or os.path.normpath(log) != os.path.normpath(self._last_path)
        ):
            if os.path.exists(log):
                self._open_file(log, reset=False)
            else:
                self._watch_file_and_dir(log)
                self.lbl.setText(f"Wacht op logbestand: {log}")

        if not self._f:
            return

        try:
            size = os.path.getsize(self._last_path)
            if size < self._last_size:
                self._f.seek(0)
                self._pos = 0
            self._last_size = size

            self._f.seek(self._pos)
            chunk = self._f.read()
            self._pos = self._f.tell()
            if not chunk:
                return

            lines = chunk.splitlines()
            level = self.cbo_level.currentText()
            text_filter = self.txt_filter.text().strip().lower()

            filtered = []
            for ln in lines:
                include = True
                if level != "ALL" and f"[{level}]" not in ln:
                    include = False
                if include and text_filter and text_filter not in ln.lower():
                    include = False
                if include:
                    filtered.append(ln)

            if filtered:
                self.view.appendPlainText("\n".join(filtered))
                bar = self.view.verticalScrollBar()
                bar.setValue(bar.maximum())
                self._last_activity = time.time()
                self._idle_warned = False
                self._update_status_label()
        except Exception as exc:
            self._f = None
            self.lbl.setText(f"Leesfout: {exc}")

    # ---- Filesystem watchdog --------------------------------------------
    def _watch_file_and_dir(self, path: str):
        if not path:
            return
        directory = os.path.dirname(path) or os.getcwd()
        try:
            if directory and directory not in self._watcher.directories():
                self._watcher.addPath(directory)
        except Exception:
            pass
        if os.path.exists(path):
            try:
                if path not in self._watcher.files():
                    self._watcher.addPath(path)
            except Exception:
                pass

    def _on_file_changed(self, path: str):
        if self._paused or not path:
            return
        if self._last_path and os.path.normpath(path) == os.path.normpath(self._last_path):
            self._watch_file_and_dir(path)
            if self.chk_auto.isChecked():
                self._tick()

    def _on_directory_changed(self, path: str):
        if self._paused or not path:
            return
        log, _ = self._get_paths()
        if not log:
            return
        directory = os.path.dirname(log)
        if directory and os.path.normpath(directory) == os.path.normpath(path):
            if os.path.exists(log):
                self._open_file(log, reset=False)
            else:
                self.lbl.setText(f"Wacht op logbestand: {log}")

    def _watchdog_tick(self):
        if self._paused or not self.chk_auto.isChecked():
            return
        log, _ = self._get_paths()
        if not log:
            self.lbl.setText("Geen logbestand beschikbaar. Start de monitor om live te kijken.")
            return

        if self._last_path and os.path.normpath(self._last_path) != os.path.normpath(log):
            if os.path.exists(log):
                self._open_file(log, reset=False)
            else:
                self._watch_file_and_dir(log)
                self.lbl.setText(f"Wacht op logbestand: {log}")

        if self._last_path and not os.path.exists(self._last_path):
            self.lbl.setText(f"Logbestand verwijderd: {self._last_path}")
            self._f = None
            self._watch_file_and_dir(self._last_path)
            return

        if self._last_activity and not self._idle_warned:
            idle_for = time.time() - self._last_activity
            threshold = max(3.0 * float(self.spn_interval.value()), 15.0)
            if idle_for >= threshold:
                self.lbl.setText(
                    f"Watchdog: geen nieuwe regels sinds {int(idle_for)}s…"
                )
                self._idle_warned = True

    def _update_status_label(self):
        if not self._last_path or not self._last_activity:
            return
        ts = datetime.fromtimestamp(self._last_activity).strftime("%H:%M:%S")
        self.lbl.setText(f"Bestand: {self._last_path} — laatste update {ts}")

