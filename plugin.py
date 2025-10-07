"""QGIS Monitor Pro plugin entry point.

The plugin coordinates three primary responsibilities:

* presenting a toolbar/menu to the user,
* orchestrating the monitoring engine exposed via :mod:`qgis_monitor`, and
* providing utilities such as diagnostics, log bundling and settings import.

The previous revisions grew a large amount of imperative code inside
``initGui``.  The rewrite introduces small helper classes to keep concerns
isolated and easier to maintain while preserving backwards compatibility for
QGIS' ``classFactory`` loader.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

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


def _icon(name: str) -> QIcon:
    return QIcon(os.path.join(ICON_DIR, name))


@dataclass
class ActionHandle:
    action: QAction
    add_to_menu: bool = True
    add_to_toolbar: bool = False


class ActionRegistry:
    """Tracks created actions so they can be cleaned up reliably."""

    def __init__(self, iface, menu_name: str) -> None:
        self.iface = iface
        self.menu_name = menu_name
        self.actions: List[ActionHandle] = []
        self.toolbar: Optional[QToolBar] = None

    def ensure_toolbar(self) -> QToolBar:
        if self.toolbar is not None:
            return self.toolbar
        existing = self.iface.mainWindow().findChild(QToolBar, "QGISMonitorProToolbar")
        if existing:
            self.toolbar = existing
            return existing
        toolbar = self.iface.addToolBar("QGIS Monitor Pro")
        toolbar.setObjectName("QGISMonitorProToolbar")
        self.toolbar = toolbar
        return toolbar

    def add(self, action: QAction, *, menu: bool = True, toolbar: bool = False) -> QAction:
        handle = ActionHandle(action, menu, toolbar)
        self.actions.append(handle)
        if menu:
            self.iface.addPluginToMenu(self.menu_name, action)
        if toolbar:
            self.ensure_toolbar().addAction(action)
        return action

    def clear(self) -> None:
        for handle in self.actions:
            if handle.add_to_menu:
                try:
                    self.iface.removePluginMenu(self.menu_name, handle.action)
                except Exception:
                    pass
            if handle.add_to_toolbar and self.toolbar:
                try:
                    self.toolbar.removeAction(handle.action)
                except Exception:
                    pass
        self.actions.clear()
        if self.toolbar:
            try:
                for action in list(self.toolbar.actions()):
                    self.toolbar.removeAction(action)
            except Exception:
                pass
            self.toolbar = None


class QgisMonitorProPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self._registry = ActionRegistry(iface, "&QGIS Monitor Pro")
        self._log = logging.getLogger("QGISMonitorPro.UI")
        self._log.setLevel(logging.DEBUG)
        self._live_dock: Optional[LiveLogDock] = None
        self._toggle_action: Optional[QAction] = None

    # ------------------------------------------------------------------
    # QGIS entry points
    # ------------------------------------------------------------------

    def initGui(self) -> None:
        self._registry.ensure_toolbar()
        main_button = self._make_action("QGIS Monitor Pro — Instellingen", self._open_settings, "icon.png")
        self._registry.add(main_button, menu=False, toolbar=True)

        self._toggle_action = QAction(_icon("icon_start.png"), "Start Monitor", self.iface.mainWindow())
        self._toggle_action.setCheckable(True)
        self._toggle_action.triggered.connect(self._toggle_monitor)
        self._registry.add(self._toggle_action, menu=True, toolbar=True)

        self._registry.add(self._make_action("Instellingen…", self._open_settings, "icon_settings.png"))
        self._registry.add(self._make_action("Diagnose uitvoeren → TXT", self._run_diagnostics, "icon_diag.png"))
        self._registry.add(self._make_action("Bundel logs → ZIP", self._bundle_logs, "icon_diag.png"))
        self._registry.add(self._make_action("Open logmap", self._open_folder, "icon_folder.png"))
        self._registry.add(self._make_action("Laatste log openen", self._open_latest_log, "tab_logging.png"))
        self._registry.add(self._make_action("Live Log Viewer", self._open_live_view, None), toolbar=True)
        self._registry.add(self._make_action("Genereer testlogs", self._emit_test_logs, None))
        self._registry.add(self._make_action("Statusoverzicht", self._show_status, "tab_view.png"))
        self._registry.add(self._make_action("Recente gebeurtenissen", self._show_breadcrumbs, "tab_adv.png"))
        self._registry.add(self._make_action("Opschonen logmap", self._prune_logs, None))
        self._registry.add(self._make_action("Exporteer instellingen…", self._export_settings, None))
        self._registry.add(self._make_action("Importeer instellingen…", self._import_settings, None))

        if bool(get_setting("autostart", bool)):
            QTimer.singleShot(1000, self._auto_start)

    def unload(self) -> None:
        try:
            qgismonitor_stop()
        except Exception:
            pass
        self._registry.clear()
        self._live_dock = None

    # ------------------------------------------------------------------
    # Action helpers
    # ------------------------------------------------------------------

    def _make_action(self, text: str, slot: Callable, icon_name: Optional[str], toolbar: bool = False) -> QAction:
        icon = _icon(icon_name) if icon_name else QIcon()
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(slot)
        return action

    def _log_action(self, name: str, **payload) -> None:
        if payload:
            self._log.info("%s %s", name, payload)
        else:
            self._log.info(name)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        self._log_action("open_settings")
        dialog = SettingsDialog(self.iface.mainWindow())
        if dialog.exec_():
            dialog.apply()
            QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "Instellingen opgeslagen.")
            if self._toggle_action and self._toggle_action.isChecked():
                qgismonitor_stop()
                QTimer.singleShot(300, lambda: qgismonitor_start(self.iface))

    def _run_diagnostics(self) -> None:
        self._log_action("run_diagnostics")
        path = write_diagnostics_txt()
        if path.startswith("ERROR"):
            QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", path)
            return
        self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Diagnose geschreven: {path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def _bundle_logs(self) -> None:
        self._log_action("bundle_logs")
        try:
            out_path = os.path.join(get_log_dir(), f"logs_bundle_{QgsApplication.instance().applicationPid()}.zip")
            if make_diagnostics_zip(out_path):
                self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Bundle geschreven: {out_path}")
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out_path)))
            else:
                QMessageBox.warning(self.iface.mainWindow(), "QGIS Monitor Pro", "Kon de bundel niet schrijven.")
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", str(exc))

    def _open_folder(self) -> None:
        path = get_log_dir()
        self._log_action("open_folder", path=path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.iface.messageBar().pushInfo("QGIS Monitor Pro", f"Logmap geopend: {path}")

    def _open_latest_log(self) -> None:
        self._log_action("open_latest_log")
        candidates = [LOG_FILE, ERR_PATH]
        candidates = [c for c in candidates if c and os.path.exists(c)]
        if not candidates:
            try:
                import glob

                log_dir = get_log_dir()
                for pattern in ("qgis_full_*.log", "qgis_errors_*.log"):
                    found = sorted(glob.glob(os.path.join(log_dir, pattern)), key=os.path.getmtime, reverse=True)
                    candidates.extend(found)
            except Exception as exc:
                self._log.warning("Kon logbestanden niet ophalen: %s", exc)
        target = candidates[0] if candidates else None
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", f"Log geopend: {target}")
        else:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", "Geen logbestand gevonden.")

    def _toggle_monitor(self, checked: bool) -> None:
        self._log_action("toggle_monitor", state=checked)
        if checked:
            try:
                qgismonitor_start(self.iface)
                self._update_toggle_icon(True)
                self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Monitor gestart.")
            except Exception as exc:
                if self._toggle_action:
                    self._toggle_action.setChecked(False)
                QMessageBox.critical(self.iface.mainWindow(), "QGIS Monitor Pro", f"Kon de monitor niet starten:\n{exc}")
        else:
            qgismonitor_stop()
            self._update_toggle_icon(False)
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Monitor gestopt.")

    def _auto_start(self) -> None:
        if self._toggle_action and self._toggle_action.isChecked():
            return
        try:
            qgismonitor_start(self.iface)
            if self._toggle_action:
                self._toggle_action.setChecked(True)
            self._update_toggle_icon(True)
            self.iface.messageBar().pushInfo("QGIS Monitor Pro", "Autostart actief.")
        except Exception as exc:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Autostart faalde: {exc}")

    def _open_live_view(self) -> None:
        self._log_action("open_live_view")
        try:
            if self._live_dock is None:
                self._live_dock = LiveLogDock(self.iface.mainWindow(), lambda: (LOG_FILE, ERR_PATH))
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._live_dock)
            self._live_dock.show()
            self._live_dock.raise_()
        except Exception as exc:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Kon Live Log niet openen: {exc}")

    def _emit_test_logs(self) -> None:
        self._log_action("emit_test_logs")
        try:
            log = logging.getLogger("QGISMonitorPro")
            log.info("[Test] INFO melding")
            log.warning("[Test] WARNING melding")
            log.error("[Test] ERROR melding")
            try:
                raise RuntimeError("Test exception voor errors.log")
            except Exception as exc:
                import traceback

                log.error("[Test] Exception: %s", "".join(traceback.format_exception_only(type(exc), exc)).strip())
            self.iface.messageBar().pushInfo("QGIS Monitor Pro", "Testlogs geschreven (full/errors/json).")
        except Exception as exc:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Testlog mislukt: {exc}")

    def _show_status(self) -> None:
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

    def _show_breadcrumbs(self) -> None:
        crumbs = get_recent_breadcrumbs(20)
        self._log_action("show_breadcrumbs", aantal=len(crumbs))
        if not crumbs:
            QMessageBox.information(self.iface.mainWindow(), "QGIS Monitor Pro", "Geen gebeurtenissen beschikbaar.")
            return
        QMessageBox.information(
            self.iface.mainWindow(), "QGIS Monitor Pro — gebeurtenissen", "\n".join(crumbs)
        )

    def _prune_logs(self) -> None:
        self._log_action("prune_logs")
        try:
            prune_logs_now()
            self.iface.messageBar().pushSuccess("QGIS Monitor Pro", "Logs opgeschoond.")
        except Exception as exc:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", f"Opschonen mislukt: {exc}")

    def _export_settings(self) -> None:
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

    def _import_settings(self) -> None:
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
            self.iface.messageBar().pushSuccess(
                "QGIS Monitor Pro", "Instellingen geïmporteerd. Herstart de monitor voor effect."
            )
        else:
            self.iface.messageBar().pushWarning("QGIS Monitor Pro", "Import mislukt. Controleer het bestand.")

    def _update_toggle_icon(self, running: bool) -> None:
        if not self._toggle_action:
            return
        if running:
            self._toggle_action.setIcon(_icon("icon_stop.png"))
            self._toggle_action.setText("Stop Monitor")
        else:
            self._toggle_action.setIcon(_icon("icon_start.png"))
            self._toggle_action.setText("Start Monitor")


__all__ = ["QgisMonitorProPlugin"]

