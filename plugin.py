# -*- coding: utf-8 -*-
import glob
import logging
import os

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QToolBar
from qgis.core import QgsApplication

from .log_viewer import LiveLogDock
from .qgis_monitor import (
    ERR_PATH,
    LOG_FILE,
    get_recent_breadcrumbs,
    make_diagnostics_zip,
    monitor_status,
    qgismonitor_start,
    qgismonitor_stop,
)
from .settings_ui import SettingsDialog
from .utils import (
    export_settings_json,
    get_log_dir,
    get_setting,
    import_settings_json,
    prune_logs_now,
    system_summary,
    write_diagnostics_txt,
)


ICON_DIR = os.path.dirname(__file__)


def ico(name):
    return QIcon(os.path.join(ICON_DIR, name))


class QgisMonitorProPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._live_dock = None
        self._menu_name = "&QGIS Monitor Pro"
        self.actions = []
        self.toolbar = None
        self._act_live = None
        self.log = logging.getLogger("QGISMonitorPro.UI")
        self.log.setLevel(logging.DEBUG)

    def initGui(self):
        # Menu actions (met iconen)
        self.action_settings = QAction(ico("icon_settings.png"), "Instellingen…", self.iface.mainWindow())
        self.action_settings.triggered.connect(self._open_settings)
        self.iface.addPluginToMenu(self._menu_name, self.action_settings)
        self.actions.append(self.action_settings)

        self.action_diag = QAction(ico("icon_diag.png"), "Diagnose uitvoeren → TXT", self.iface.mainWindow())
        self.action_diag.triggered.connect(self._run_diag)
        self.iface.addPluginToMenu(self._menu_name, self.action_diag)
        self.actions.append(self.action_diag)

        self.action_bundle = QAction(ico("icon_diag.png"), "Bundel logs → ZIP", self.iface.mainWindow())
        self.action_bundle.triggered.connect(self._bundle_logs)
        self.iface.addPluginToMenu(self._menu_name, self.action_bundle)
        self.actions.append(self.action_bundle)

        self.action_open = QAction(ico("icon_folder.png"), "Open logmap", self.iface.mainWindow())
        self.action_open.triggered.connect(self._open_folder)
        self.iface.addPluginToMenu(self._menu_name, self.action_open)
        self.actions.append(self.action_open)

        self.action_open_latest = QAction(ico("tab_logging.png"), "Laatste log openen", self.iface.mainWindow())
        self.action_open_latest.triggered.connect(self._open_latest_log)
        self.iface.addPluginToMenu(self._menu_name, self.action_open_latest)
        self.actions.append(self.action_open_latest)

        self.action_toggle = QAction(ico("icon_start.png"), "Start Monitor", self.iface.mainWindow())
        self.action_toggle.setCheckable(True)
        self.action_toggle.triggered.connect(self._toggle_monitor)
        self.iface.addPluginToMenu(self._menu_name, self.action_toggle)
        self.actions.append(self.action_toggle)

        # Toolbar (slank): alleen hoofdicoon + start/stop
        mw = self.iface.mainWindow()
        existing = mw.findChild(QToolBar, "QGISMonitorProToolbar")
        if existing:
            self.toolbar = existing
        else:
            self.toolbar = self.iface.addToolBar("QGIS Monitor Pro")
            self.toolbar.setObjectName("QGISMonitorProToolbar")

        try:
            for act in list(self.toolbar.actions()):
                self.toolbar.removeAction(act)
        except Exception:
            pass

        main_btn = QAction(QIcon(os.path.join(ICON_DIR, "icon.png")), "QGIS Monitor Pro — Instellingen", self.iface.mainWindow())
        main_btn.triggered.connect(self._open_settings)
        self.toolbar.addAction(main_btn)
        self.toolbar.addAction(self.action_toggle)

        # Live Log Viewer action (additive)
        try:
            self._act_live = QAction(QgsApplication.getThemeIcon("mActionOpenTable.svg"), "Live Log Viewer", self.iface.mainWindow())
            self._act_live.triggered.connect(self._open_live)
            if self.toolbar:
                self.toolbar.addAction(self._act_live)
            try:
                self.iface.addPluginToMenu(self._menu_name, self._act_live)
                self.actions.append(self._act_live)
            except Exception:
                pass
        except Exception:
            self._act_live = None

        # Testlogs (menu only)
        self._act_test = QAction("Genereer testlogs", self.iface.mainWindow())
        self._act_test.triggered.connect(self._emit_test)
        self.iface.addPluginToMenu(self._menu_name, self._act_test)
        self.actions.append(self._act_test)

        self._act_status = QAction(ico("tab_view.png"), "Statusoverzicht", self.iface.mainWindow())
        self._act_status.triggered.connect(self._show_status)
        self.iface.addPluginToMenu(self._menu_name, self._act_status)
        self.actions.append(self._act_status)

        self._act_breadcrumbs = QAction(ico("tab_adv.png"), "Recente gebeurtenissen", self.iface.mainWindow())
        self._act_breadcrumbs.triggered.connect(self._show_breadcrumbs)
        self.iface.addPluginToMenu(self._menu_name, self._act_breadcrumbs)
        self.actions.append(self._act_breadcrumbs)

        self._act_prune = QAction("Opschonen logmap", self.iface.mainWindow())
        self._act_prune.triggered.connect(self._prune_logs)
        self.iface.addPluginToMenu(self._menu_name, self._act_prune)
        self.actions.append(self._act_prune)

        self._act_export = QAction("Exporteer instellingen…", self.iface.mainWindow())
        self._act_export.triggered.connect(self._export_settings)
        self.iface.addPluginToMenu(self._menu_name, self._act_export)
        self.actions.append(self._act_export)

        self._act_import = QAction("Importeer instellingen…", self.iface.mainWindow())
        self._act_import.triggered.connect(self._import_settings)
        self.iface.addPluginToMenu(self._menu_name, self._act_import)
        self.actions.append(self._act_import)

        if bool(get_setting("autostart", bool)):
            QTimer.singleShot(1000, self._auto_start)

    def unload(self):
        try:
            qgismonitor_stop()
        except Exception:
            pass
        for a in self.actions:
            try:
                self.iface.removePluginMenu(self._menu_name, a)
            except Exception:
                pass
        self.actions.clear()
        if self.toolbar:
            try:
                for act in list(self.toolbar.actions()):
                    self.toolbar.removeAction(act)
            except Exception:
                pass
            self.toolbar = None

    # ---- UI callbacks ----
    def _log_action(self, action: str, **kwargs):
        if kwargs:
            self.log.info("%s %s", action, kwargs)
        else:
            self.log.info(action)

    def _open_settings(self):
        self._log_action("open_settings")
        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec_():
            dlg.apply()
            QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "Instellingen opgeslagen.")
            if self.action_toggle.isChecked():
                qgismonitor_stop()
                QTimer.singleShot(300, lambda: qgismonitor_start(self.iface))

    def _run_diag(self):
        self._log_action("run_diagnostics")
        p = write_diagnostics_txt()
        if p.startswith("ERROR"):
            QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", p)
        else:
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Diagnose geschreven: {p}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))

    def _bundle_logs(self):
        self._log_action("bundle_logs")
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
        self._log_action("open_folder", path=get_log_dir())
        d = get_log_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))
        self.iface.messageBar().pushInfo("QGIS Monitor Pro", f"Logmap geopend: {d}")

    def _open_latest_log(self):
        self._log_action("open_latest_log")
        candidates = []
        if LOG_FILE and os.path.exists(LOG_FILE):
            candidates.append(LOG_FILE)
        if ERR_PATH and os.path.exists(ERR_PATH):
            candidates.append(ERR_PATH)
        if not candidates:
            try:
                log_dir = get_log_dir()
                for pattern in ("qgis_full_*.log", "qgis_errors_*.log"):
                    candidates.extend(sorted(glob.glob(os.path.join(log_dir, pattern)), key=os.path.getmtime, reverse=True))
            except Exception as exc:
                self.log.warning("Kon logbestanden niet ophalen: %s", exc)
        target = candidates[0] if candidates else None
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Log geopend: {target}")
        else:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", "Geen logbestand gevonden.")

    def _toggle_monitor(self, checked):
        self._log_action("toggle_monitor", state=checked)
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
        if self.action_toggle.isChecked():
            return
        try:
            self._log_action("auto_start")
            qgismonitor_start(self.iface)
            self.action_toggle.setChecked(True)
            self.action_toggle.setIcon(ico("icon_stop.png"))
            self.action_toggle.setText("Stop Monitor")
            self.iface.messageBar().pushInfo("QGIS Monitor Pro", "Autostart actief.")
        except Exception as e:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Autostart faalde: {e}")

    def _open_live(self):
        self._log_action("open_live_view")
        try:
            if self._live_dock is None:
                self._live_dock = LiveLogDock(self.iface.mainWindow(), get_paths=lambda: (LOG_FILE, ERR_PATH))
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._live_dock)
            self._live_dock.show()
            self._live_dock.raise_()
        except Exception as e:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Kon Live Log niet openen: {e}")

    def _emit_test(self):
        self._log_action("emit_test_logs")
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

    def _show_status(self):
        status = monitor_status()
        self._log_action("show_status", **status)
        lines = [
            "Monitor actief" if status.get("started") else "Monitor staat uit",
            f"Sessiesleutel: {status.get('session_id')}",
            f"Logmap: {status.get('log_dir') or '-'}",
            f"Laatste log: {status.get('log_file') or '-'}",
            f"Errors log: {status.get('error_log') or '-'}",
            f"JSON log: {status.get('json_log') or '-'}",
            f"Heartbeat actief: {'ja' if status.get('heartbeat_active') else 'nee'}",
            f"Aantal breadcrumbs: {status.get('breadcrumbs')}",
            "",
            "Systeem:",
            system_summary(),
        ]
        QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "\n".join(lines))

    def _show_breadcrumbs(self):
        crumbs = get_recent_breadcrumbs(20)
        self._log_action("show_breadcrumbs", aantal=len(crumbs))
        if not crumbs:
            QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "Geen gebeurtenissen beschikbaar.")
            return
        text = "\n".join(crumbs)
        QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro — gebeurtenissen", text)

    def _prune_logs(self):
        self._log_action("prune_logs")
        try:
            prune_logs_now()
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Logs opgeschoond.")
        except Exception as exc:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Opschonen mislukt: {exc}")

    def _export_settings(self):
        self._log_action("export_settings")
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporteer instellingen",
            os.path.join(get_log_dir(), "qgis_monitor_settings.json"),
            "JSON (*.json)",
        )
        if not path:
            return
        if export_settings_json(path):
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Instellingen geëxporteerd: {path}")
        else:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", "Export mislukt.")

    def _import_settings(self):
        self._log_action("import_settings")
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Importeer instellingen",
            get_log_dir(),
            "JSON (*.json)",
        )
        if not path:
            return
        if import_settings_json(path):
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Instellingen geïmporteerd. Herstart de monitor voor effect.")
        else:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", "Import mislukt. Controleer het bestand.")
