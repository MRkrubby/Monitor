\
# -*- coding: utf-8 -*-
import os, sys, subprocess, platform, datetime
from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtGui import QIcon, QDesktopServices
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import Qgis, QgsMessageLog, QgsApplication

from .ultra_logger import start as ultralog_start, stop as ultralog_stop, hook_processing as ultralog_hook_processing

TAG = "QGISMonitorPro"

def _default_log_dir():
    return os.path.join(os.path.expanduser("~"), "Desktop", "logs")

def _ensure_dir(p):
    try: os.makedirs(p, exist_ok=True)
    except Exception: pass

def _write_diag(log_dir):
    _ensure_dir(log_dir)
    path = os.path.join(log_dir, f"diagnostics_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).isoformat()}Z\n")
            f.write(f"OS: {platform.platform()}\n")
            f.write(f"Python: {sys.version}\n")
            try:
                from qgis.core import Qgis
                f.write(f"QGIS Version: {Qgis.QGIS_VERSION}\n")
            except Exception:
                pass
            f.write("\nLogdir: " + log_dir + "\n")
        return path
    except Exception:
        return None

class QgisMonitorProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._toggle = None
        self._started = False
        self._log_dir = _default_log_dir()

    def initGui(self):
        ico = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self._toggle = QAction(ico, "QGIS Monitor Pro: Start/Stop", self.iface.mainWindow())
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.toggled.connect(self._on_toggle)
        self.iface.addToolBarIcon(self._toggle)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self._toggle)

        act_open = QAction("Open logmap", self.iface.mainWindow())
        act_open.triggered.connect(self._open_log_dir)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", act_open)

        act_diag = QAction("Diagnose → txt", self.iface.mainWindow())
        act_diag.triggered.connect(self._diagnose)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", act_diag)

        act_test = QAction("Schrijf testlogs", self.iface.mainWindow())
        act_test.triggered.connect(self._test_logs)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", act_test)

        # Autostart ultra logger + processing hook
        QTimer.singleShot(600, self._autostart)

    def unload(self):
        try: self.iface.removeToolBarIcon(self._toggle)
        except Exception: pass
        try: self.iface.removePluginMenu("&QGIS Monitor Pro", self._toggle)
        except Exception: pass
        if self._started:
            try: ultralog_stop()
            except Exception: pass
            self._started = False

    def _autostart(self):
        try:
            self._start_logging()
            self._toggle.blockSignals(True)
            self._toggle.setChecked(True)
        finally:
            try: self._toggle.blockSignals(False)
            except Exception: pass

    def _on_toggle(self, checked):
        if checked: self._start_logging()
        else: self._stop_logging()

    def _start_logging(self):
        if self._started: return
        cfg = dict(
            log_dir=self._log_dir,
            json=False,
            max_mb=20, backups=5,
            rate_limit=60, coalesce_sec=1.0,
            capture_warnings=True, capture_qt=True, capture_stdout=True,
            faulthandler=True, kernel_enable=True,
            keep_prev=50,
        )
        try:
            _ensure_dir(self._log_dir)
            ultralog_start(cfg)
            ultralog_hook_processing()
            self._started = True
            self.iface.messageBar().pushInfo(TAG, "Logging gestart (ultra).")
        except Exception as e:
            QMessageBox.warning(self.iface.mainWindow(), "QGIS Monitor Pro", f"Kon logging niet starten:\\n{e}")

    def _stop_logging(self):
        if not self._started: return
        try:
            ultralog_stop()
            self._started = False
            self.iface.messageBar().pushInfo(TAG, "Logging gestopt.")
        except Exception as e:
            QMessageBox.warning(self.iface.mainWindow(), "QGIS Monitor Pro", f"Kon logging niet stoppen:\\n{e}")

    def _open_log_dir(self):
        try:
            _ensure_dir(self._log_dir)
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._log_dir))
        except Exception as e:
            QMessageBox.warning(self.iface.mainWindow(), "QGIS Monitor Pro", f"Kon logmap niet openen:\\n{e}")

    def _diagnose(self):
        p = _write_diag(self._log_dir)
        if p: self.iface.messageBar().pushSuccess(TAG, f"Diagnose geschreven: {p}")
        else: self.iface.messageBar().pushWarning(TAG, "Diagnose schrijven is mislukt.")

    def _test_logs(self):
        import logging
        lg = logging.getLogger("QGISMonitorPro")
        lg.info("[Test] INFO melding (probe)")
        lg.warning("[Test] WARNING melding (probe)")
        try:
            raise RuntimeError("Test exception voor errors.log (probe)")
        except Exception:
            lg.exception("[Test] Exception (probe)")
