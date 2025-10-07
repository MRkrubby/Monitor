
# -*- coding: utf-8 -*-
from qgis.PyQt import QtWidgets, QtCore, QtGui
import os, io

class LiveLogDock(QtWidgets.QDockWidget):
    def __init__(self, parent=None, get_paths=None):
        super().__init__("QGIS Monitor Pro — Live Log", parent)
        self.setObjectName("QGM_LiveLogDock")
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self._get_paths = get_paths or (lambda: (None, None))
        self._f = None; self._pos = 0; self._last_size = 0; self._last_path = None

        w = QtWidgets.QWidget(self); self.setWidget(w)
        lay = QtWidgets.QVBoxLayout(w); lay.setContentsMargins(6,6,6,6); lay.setSpacing(6)

        top = QtWidgets.QHBoxLayout(); lay.addLayout(top)
        self.cbo_level = QtWidgets.QComboBox(); self.cbo_level.addItems(["ALL","DEBUG","INFO","WARNING","ERROR"])
        self.txt_filter = QtWidgets.QLineEdit(); self.txt_filter.setPlaceholderText("Filter op tekst of [Tag]")
        self.chk_auto = QtWidgets.QCheckBox("Auto-refresh"); self.chk_auto.setChecked(True)
        self.spn_interval = QtWidgets.QDoubleSpinBox(); self.spn_interval.setRange(0.5,10.0); self.spn_interval.setDecimals(1); self.spn_interval.setValue(1.0); self.spn_interval.setSuffix(" s")
        self.btn_pause = QtWidgets.QPushButton("Pauze"); self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_open = QtWidgets.QPushButton("Open map"); self.btn_pick = QtWidgets.QPushButton("Kies bestand")
        top.addWidget(QtWidgets.QLabel("Level:")); top.addWidget(self.cbo_level)
        top.addWidget(self.txt_filter, 1); top.addWidget(self.chk_auto); top.addWidget(self.spn_interval)
        top.addWidget(self.btn_pause); top.addWidget(self.btn_clear); top.addWidget(self.btn_open); top.addWidget(self.btn_pick)

        self.view = QtWidgets.QPlainTextEdit(); self.view.setReadOnly(True)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.view.setFont(mono); lay.addWidget(self.view, 1)

        self.lbl = QtWidgets.QLabel("—"); lay.addWidget(self.lbl)

        self.tmr = QtCore.QTimer(self); self.tmr.timeout.connect(self._tick)
        self._apply_interval(); self.tmr.start()
        self._paused = False

        self.chk_auto.toggled.connect(self._toggle_auto)
        self.spn_interval.valueChanged.connect(self._apply_interval)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear.clicked.connect(self.view.clear)
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_pick.clicked.connect(self._pick_file)

    def _apply_interval(self): self.tmr.setInterval(int(self.spn_interval.value()*1000))
    def _toggle_auto(self, on): self.tmr.start() if on and not self._paused else self.tmr.stop()
    def _toggle_pause(self): self._paused = not self._paused; self.btn_pause.setText("Hervat" if self._paused else "Pauze"); self._toggle_auto(self.chk_auto.isChecked())
    def _open_folder(self):
        log, _ = self._get_paths()
        if log and os.path.exists(log): QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(log)))
    def _pick_file(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Kies logbestand", "", "Log files (*.log *.txt);;Alle bestanden (*.*)")
        if p: self._open_file(p, reset=True)
    def _open_file(self, path, reset=False):
        try:
            if self._f: 
                try: self._f.close()
                except Exception: pass
            self._f = io.open(path, "r", encoding="utf-8", errors="ignore")
            self._last_path = path; self._pos = 0 if reset else self._f.seek(0, os.SEEK_END)
            self._last_size = os.path.getsize(path); self.lbl.setText(f"Bestand: {path}")
        except Exception as e:
            self.lbl.setText(f"Kon bestand niet openen: {e}")
    def _tick(self):
        if self._paused: return
        log, _ = self._get_paths()
        if log and (self._last_path is None or os.path.normpath(log) != os.path.normpath(self._last_path)): self._open_file(log, reset=False)
        if not self._f and self._last_path: self._open_file(self._last_path, reset=False)
        if not self._f: return
        try:
            size = os.path.getsize(self._last_path)
            if size < self._last_size: self._f.seek(0); self._pos = 0
            self._last_size = size
            self._f.seek(self._pos); chunk = self._f.read(); self._pos = self._f.tell()
            if not chunk: return
            lines = chunk.splitlines(); lvl = self.cbo_level.currentText(); text_filter = self.txt_filter.text().strip().lower()
            out=[]; 
            for ln in lines:
                ok=True
                if lvl!="ALL" and f"[{lvl}]" not in ln: ok=False
                if ok and text_filter and text_filter not in ln.lower(): ok=False
                if ok: out.append(ln)
            if out:
                self.view.appendPlainText("\\n".join(out))
                self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        except Exception as e:
            self.lbl.setText(f"Leesfout: {e}")
