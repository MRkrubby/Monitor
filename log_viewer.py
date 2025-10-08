"""Live log viewer dock widget.

The widget tails the monitor log(s) and provides an inactivity watchdog so the
user can tell at a glance when new data arrives.  The implementation avoids
continuous polling by combining a ``QTimer`` for coarse refreshes with
``QFileSystemWatcher`` notifications when the underlying file changes.
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Tuple

from qgis.PyQt import QtCore, QtGui, QtWidgets


@dataclass
class FileSnapshot:
    path: str
    size: int
    modified: float


class _TailSession:
    def __init__(self, path: str) -> None:
        self.path = path
        self.handle: Optional[io.TextIOBase] = None
        self.position = 0
        self.snapshot: Optional[FileSnapshot] = None

    def open(self, reset: bool = False) -> None:
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
        self.handle = io.open(self.path, "r", encoding="utf-8", errors="ignore")
        if reset:
            self.position = 0
        else:
            self.handle.seek(0, os.SEEK_END)
            self.position = self.handle.tell()
        self.snapshot = self._stat()

    def close(self) -> None:
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
        self.handle = None
        self.snapshot = None

    def _stat(self) -> Optional[FileSnapshot]:
        try:
            stat = os.stat(self.path)
            return FileSnapshot(self.path, stat.st_size, stat.st_mtime)
        except Exception:
            return None

    def read_new(self) -> str:
        if not self.handle:
            return ""
        data = self.handle.read()
        self.position = self.handle.tell()
        self.snapshot = self._stat()
        return data


class LiveLogDock(QtWidgets.QDockWidget):
    def __init__(self, parent=None, get_paths: Optional[Callable[[], Tuple[Optional[str], Optional[str]]]] = None) -> None:
        super().__init__("QGIS Monitor Pro — Live Log", parent)
        self.setObjectName("QGM_LiveLogDock")
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)

        self._resolve_paths = get_paths or (lambda: (None, None))
        self._session: Optional[_TailSession] = None
        self._current_path: Optional[str] = None
        self._last_activity = 0.0
        self._paused = False

        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        controls = QtWidgets.QHBoxLayout()
        layout.addLayout(controls)

        self.level_combo = QtWidgets.QComboBox()
        self.level_combo.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter op tekst of [Tag]")
        self.auto_checkbox = QtWidgets.QCheckBox("Auto-refresh")
        self.auto_checkbox.setChecked(True)
        self.interval_spin = QtWidgets.QDoubleSpinBox()
        self.interval_spin.setRange(0.2, 10.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.2)
        self.interval_spin.setValue(1.0)
        self.interval_spin.setSuffix(" s")
        self.pause_button = QtWidgets.QPushButton("Pauze")
        self.clear_button = QtWidgets.QPushButton("Clear")
        self.open_button = QtWidgets.QPushButton("Open map")
        self.pick_button = QtWidgets.QPushButton("Kies bestand")

        controls.addWidget(QtWidgets.QLabel("Level:"))
        controls.addWidget(self.level_combo)
        controls.addWidget(self.filter_edit, 1)
        controls.addWidget(self.auto_checkbox)
        controls.addWidget(self.interval_spin)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.clear_button)
        controls.addWidget(self.open_button)
        controls.addWidget(self.pick_button)

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        layout.addWidget(self.view, 1)

        self.status_label = QtWidgets.QLabel("—")
        layout.addWidget(self.status_label)

        self.setWidget(container)

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self._tick)
        self._apply_interval()
        self.refresh_timer.start()

        self.watchdog_timer = QtCore.QTimer(self)
        self.watchdog_timer.setInterval(3000)
        self.watchdog_timer.timeout.connect(self._watchdog_tick)
        self.watchdog_timer.start()

        self.fs_watcher = QtCore.QFileSystemWatcher(self)
        self.fs_watcher.fileChanged.connect(self._on_file_changed)
        self.fs_watcher.directoryChanged.connect(self._on_directory_changed)

        self.auto_checkbox.toggled.connect(self._toggle_auto)
        self.interval_spin.valueChanged.connect(self._apply_interval)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.clear_button.clicked.connect(self.view.clear)
        self.open_button.clicked.connect(self._open_folder)
        self.pick_button.clicked.connect(self._pick_file)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _apply_interval(self) -> None:
        self.refresh_timer.setInterval(int(self.interval_spin.value() * 1000))

    def _toggle_auto(self, enabled: bool) -> None:
        if enabled and not self._paused:
            self.refresh_timer.start()
        else:
            self.refresh_timer.stop()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("Hervat" if self._paused else "Pauze")
        self._toggle_auto(self.auto_checkbox.isChecked())

    def _open_folder(self) -> None:
        path, _ = self._resolve_paths()
        if path and os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(path)))

    def _pick_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Kies logbestand",
            "",
            "Log files (*.log *.txt);;Alle bestanden (*.*)",
        )
        if path:
            self._switch_file(path, reset=True)

    # ------------------------------------------------------------------
    # Tail logic
    # ------------------------------------------------------------------

    def _switch_file(self, path: str, reset: bool = False) -> None:
        if not path:
            return
        try:
            session = _TailSession(path)
            session.open(reset)
            self._session = session
            self._current_path = path
            self._last_activity = time.time()
            self._watch_file(path)
            self._update_status()
        except Exception as exc:
            self._session = None
            self.status_label.setText(f"Kon bestand niet openen: {exc}")

    def _tick(self) -> None:
        if self._paused:
            return
        path, _ = self._resolve_paths()
        if path and path != self._current_path:
            if os.path.exists(path):
                self._switch_file(path, reset=False)
            else:
                self._watch_file(path)
        if not self._session or not self._session.handle:
            return
        data = self._session.read_new()
        if data:
            self._append_text(data)
            self._last_activity = time.time()
            self._update_status()

    def _append_text(self, data: str) -> None:
        level_filter = self.level_combo.currentText()
        text_filter = self.filter_edit.text().strip().lower()
        block = []
        for line in data.splitlines():
            if level_filter != "ALL" and f"[{level_filter}]" not in line:
                continue
            if text_filter and text_filter not in line.lower():
                continue
            block.append(line)
        if block:
            cursor = self.view.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            cursor.insertText("\n".join(block) + "\n")
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _watch_file(self, path: str) -> None:
        try:
            self.fs_watcher.removePaths(self.fs_watcher.files())
        except Exception:
            pass
        try:
            self.fs_watcher.removePaths(self.fs_watcher.directories())
        except Exception:
            pass
        if path:
            directory = os.path.dirname(path) or os.getcwd()
            if os.path.isdir(directory):
                self.fs_watcher.addPath(directory)
            if os.path.exists(path):
                self.fs_watcher.addPath(path)

    def _on_file_changed(self, path: str) -> None:
        if self._session and path == self._session.path:
            self._tick()

    def _on_directory_changed(self, directory: str) -> None:
        if self._current_path and os.path.dirname(self._current_path) == directory:
            if os.path.exists(self._current_path):
                self.fs_watcher.addPath(self._current_path)
            self._tick()

    # ------------------------------------------------------------------
    # Status / watchdog
    # ------------------------------------------------------------------

    def _update_status(self) -> None:
        if not self._current_path:
            self.status_label.setText("Geen actief logbestand")
            return
        snapshot = self._session.snapshot if self._session else None
        size = snapshot.size if snapshot else 0
        timestamp = datetime.fromtimestamp(snapshot.modified).strftime("%H:%M:%S") if snapshot else "—"
        idle = time.time() - self._last_activity
        self.status_label.setText(
            f"{os.path.basename(self._current_path)} — {size:,} bytes — laatst gewijzigd {timestamp} — idle {idle:.1f}s"
        )

    def _watchdog_tick(self) -> None:
        if not self._session:
            return
        idle = time.time() - self._last_activity
        if idle > 10:
            self.status_label.setStyleSheet("color: #d9534f;")
        else:
            self.status_label.setStyleSheet("")


__all__ = ["LiveLogDock"]

