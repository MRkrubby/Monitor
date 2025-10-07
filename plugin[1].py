
# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolBar
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.core import Qgis, QgsApplication
from .utils import get_setting, write_diagnostics_txt, get_log_dir, bundle_logs_zip
from .settings_ui import SettingsDialog
from .log_viewer import LiveLogDock
import logging
from .qgis_monitor import qgismonitor_start, qgismonitor_stop, LOG_DIR, LOG_FILE, ERR_PATH, JSON_PATH, make_diagnostics_zip

ICON_DIR = os.path.dirname(__file__)
def ico(name): return QIcon(os.path.join(ICON_DIR, name))

class QgisMonitorProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._live_dock = None
        self._menu_name = "&QGIS Monitor Pro"
        self.app = QgsApplication.instance()
        self.actions = []
        self.toolbar = None

    def initGui(self):
        # Menu actions (met iconen)
        self.action_settings = QAction(ico("icon_settings.png"), "Instellingen…", self.iface.mainWindow())
        self.action_settings.triggered.connect(self._open_settings)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self.action_settings); self.actions.append(self.action_settings)

        self.action_diag = QAction(ico("icon_diag.png"), "Diagnose uitvoeren → TXT", self.iface.mainWindow())
        self.action_diag.triggered.connect(self._run_diag)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self.action_diag); self.actions.append(self.action_diag)

        self.action_bundle = QAction(ico("icon_diag.png"), "Bundel logs → ZIP", self.iface.mainWindow())
        self.action_bundle.triggered.connect(self._bundle_logs)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self.action_bundle); self.actions.append(self.action_bundle)

        self.action_open = QAction(ico("icon_folder.png"), "Open logmap", self.iface.mainWindow())
        self.action_open.triggered.connect(self._open_folder)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self.action_open); self.actions.append(self.action_open)

        self.action_toggle = QAction(ico("icon_start.png"), "Start Monitor", self.iface.mainWindow())
        self.action_toggle.setCheckable(True); self.action_toggle.triggered.connect(self._toggle_monitor)
        self.iface.addPluginToMenu("&QGIS Monitor Pro", self.action_toggle); self.actions.append(self.action_toggle)
        # Live Log Viewer action (additive)
        # Live Log Viewer action (additive)
        try:
            self._act_live = QAction(QgsApplication.getThemeIcon('mActionOpenTable.svg'), 'Live Log Viewer', self.iface.mainWindow())
            self._act_live.triggered.connect(self._open_live)
            try:
                self._toolbar.addAction(self._act_live)
            except Exception:
                pass
            try:
                self.iface.addPluginToMenu(self._menu_name, self._act_live)
            except Exception:
                pass
        except Exception:
            pass

        # Testlogs (menu only)
        self._act_test = QAction('Genereer testlogs', self.iface.mainWindow())
        self._act_test.triggered.connect(self._emit_test)
        self.iface.addPluginToMenu(self._menu_name, self._act_test)

# Toolbar (slank): alleen hoofdicoon + start/stop
        mw = self.iface.mainWindow()
        existing = mw.findChild(QToolBar, "QGISMonitorProToolbar")
        if existing: self.toolbar = existing
        else:
            self.toolbar = self.iface.addToolBar("QGIS Monitor Pro"); self.toolbar.setObjectName("QGISMonitorProToolbar")

        try:
            for act in list(self.toolbar.actions()):
                self.toolbar.removeAction(act)
        except Exception: pass

        main_btn = QAction(QIcon(os.path.join(ICON_DIR, "icon.png")), "QGIS Monitor Pro — Instellingen", self.iface.mainWindow())
        main_btn.triggered.connect(self._open_settings); self.toolbar.addAction(main_btn)
        self.toolbar.addAction(self.action_toggle)

        if bool(get_setting("autostart", bool)):
            QTimer.singleShot(1000, self._auto_start)

    def unload(self):
        try: qgismonitor_stop()
        except Exception: pass
        for a in self.actions:
            try: self.iface.removePluginMenu("&QGIS Monitor Pro", a)
            except Exception: pass
        self.actions.clear()
        if self.toolbar:
            try:
                for act in list(self.toolbar.actions()):
                    self.toolbar.removeAction(act)
            except Exception: pass
            self.toolbar = None

    # ---- UI callbacks ----
    def _open_settings(self):
        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec_():
            dlg.apply()
            QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "Instellingen opgeslagen.")
            if self.action_toggle.isChecked():
                qgismonitor_stop(); QTimer.singleShot(300, lambda: qgismonitor_start(self.iface))

    def _run_diag(self):
        p = write_diagnostics_txt()
        if p.startswith("ERROR"):
            QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", p)
        else:
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Diagnose geschreven: {p}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))

    def _bundle_logs(self):
        try:
            out = os.path.join(get_log_dir(), f"logs_bundle_{QgsApplication.instance().applicationPid()}.zip")
            ok = make_diagnostics_zip(out)
            if ok:
                self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Bundle geschreven: {out}")
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out)))
            else:
                QMessageBox.warning(self.iface.mainWindow(), "QGIS Monitor Pro", "Kon de bundel niet schrijven.")
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", str(e))

    def _open_folder(self):
        d = get_log_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))
        self.iface.messageBar().pushInfo("QGIS Monitor Pro", f"Logmap geopend: {d}")

    def _toggle_monitor(self, checked):
        if checked:
            try:
                qgismonitor_start(self.iface)
                self.action_toggle.setIcon(ico("icon_stop.png"))
                self.action_toggle.setText("Stop Monitor")
                self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Monitor gestart.")
            except Exception as e:
                self.action_toggle.setChecked(False)
                QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", f"Kon de monitor niet starten:\n{e}")
        else:
            qgismonitor_stop()
            self.action_toggle.setIcon(ico("icon_start.png"))
            self.action_toggle.setText("Start Monitor")
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Monitor gestopt.")

    def _auto_start(self):
        if self.action_toggle.isChecked(): return
        try:
            qgismonitor_start(self.iface)
            self.action_toggle.setChecked(True)
            self.action_toggle.setIcon(ico("icon_stop.png"))
            self.action_toggle.setText("Stop Monitor")
            self.iface.messageBar().pushInfo("QGIS Monitor Pro", "Autostart actief.")
        except Exception as e:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Autostart faalde: {e}")


    def _open_live(self):
        try:
            if self._live_dock is None:
                from .qgis_monitor import LOG_FILE, ERR_PATH
                self._live_dock = LiveLogDock(self.iface.mainWindow(), get_paths=lambda: (LOG_FILE, ERR_PATH))
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._live_dock)
            self._live_dock.show(); self._live_dock.raise_()
        except Exception as e:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Kon Live Log niet openen: {e}")


    def _emit_test(self):
        try:
            log = logging.getLogger('QGISMonitorPro')
            log.info('[Test] INFO melding')
            log.warning('[Test] WARNING melding')
            log.error('[Test] ERROR melding')
            try:
                raise RuntimeError('Test exception voor errors.log')
            except Exception as e:
                import traceback
                log.error('[Test] Exception: ' + ''.join(traceback.format_exception_only(type(e), e)).strip())
            self.iface.messageBar().pushInfo('QGIS Monitor Pro', 'Testlogs geschreven (full/errors/json).')
        except Exception as e:
            self.iface.messageBar().pushWarning('QGIS Monitor Pro', f'Testlog mislukt: {e}')