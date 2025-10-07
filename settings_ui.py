import json

# -*- coding: utf-8 -*-
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox, QPushButton,
    QFileDialog, QSpinBox, QDoubleSpinBox, QDialogButtonBox, QGroupBox, QLineEdit, QFormLayout, QTabWidget, QWidget
)
from qgis.PyQt.QtCore import Qt, QFile, QTextStream
from qgis.PyQt.QtGui import QIcon, QPalette
from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtCore import QUrl
from .utils import get_setting, set_setting, DEFAULTS, export_settings_json, import_settings_json, prune_logs_now, get_log_dir, post_webhook

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QGIS Monitor Pro • Instellingen")
        self.setMinimumWidth(620)
        self._load_style()

        main = QVBoxLayout(self)

        tabs = QTabWidget(self)
        tabs.setTabPosition(QTabWidget.North)

        # ----- Tab: Logbestanden -----
        tab_log = QWidget(); v_log = QVBoxLayout(tab_log)
        gb_path = QGroupBox("Loglocatie & retentie"); form_path = QFormLayout(gb_path)
        self.dir_label = QLineEdit(get_setting("log_dir", str) or "(auto)"); self.dir_label.setReadOnly(True)
        btn_browse = QPushButton(QIcon(self.style().standardIcon(self.style().SP_DirIcon)), "Kies map…")
        btn_browse.clicked.connect(self._browse)
        row_loc = QHBoxLayout(); row_loc.addWidget(self.dir_label,1); row_loc.addWidget(btn_browse)
        form_path.addRow(QLabel("Map:"), row_loc)

        self.ret_full = QSpinBox(); self.ret_full.setRange(5, 5000); self.ret_full.setValue(int(get_setting("keep_full", int)))
        self.ret_err  = QSpinBox(); self.ret_err.setRange(5, 5000); self.ret_err.setValue(int(get_setting("keep_errs", int)))
        self.ret_snap = QSpinBox(); self.ret_snap.setRange(5, 5000); self.ret_snap.setValue(int(get_setting("keep_snap", int)))
        form_path.addRow("Bewaar (full):", self.ret_full)
        form_path.addRow("Bewaar (errors):", self.ret_err)
        form_path.addRow("Bewaar (snapshots):", self.ret_snap)

        self.compress_old = QCheckBox("Oude logs comprimeren (.zip) bij opruimen")
        self.compress_old.setChecked(bool(get_setting("compress_old", bool)))
        v_log.addWidget(gb_path); v_log.addWidget(self.compress_old); v_log.addStretch(1)
        tabs.addTab(tab_log, QIcon(self._icon_path("tab_logging.png")), "Logbestanden")

        # ----- Tab: Hooks & gedrag -----
        tab_hooks = QWidget(); v_hooks = QVBoxLayout(tab_hooks)
        gb_hooks = QGroupBox("Gedrag & hooks"); form_hooks = QFormLayout(gb_hooks)
        self.autostart = QCheckBox("Automatisch starten bij QGIS (profiel)"); self.autostart.setChecked(bool(get_setting("autostart", bool)))
        form_hooks.addRow("", self.autostart)

        self.level = QComboBox(); self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"]); self.level.setCurrentText(get_setting("level", str))
        form_hooks.addRow("Log level:", self.level)

        self.hook_processing = QCheckBox("Processing hooks"); self.hook_processing.setChecked(bool(get_setting("hook_processing", bool)))
        self.hook_tasks      = QCheckBox("Task hooks");       self.hook_tasks.setChecked(bool(get_setting("hook_tasks", bool)))
        self.hook_canvas     = QCheckBox("Canvas render hooks"); self.hook_canvas.setChecked(bool(get_setting("hook_canvas", bool)))
        self.hook_project    = QCheckBox("Project & layer hooks"); self.hook_project.setChecked(bool(get_setting("hook_project", bool)))
        self.json_parallel   = QCheckBox("Parallel JSON-log (.jsonl)"); self.json_parallel.setChecked(bool(get_setting("json_parallel", bool)))
        self.debug_depth     = QSpinBox(); self.debug_depth.setRange(0,5); self.debug_depth.setValue(int(get_setting("debug_depth", int)))

        form_hooks.addRow("", self.hook_processing)
        form_hooks.addRow("", self.hook_tasks)
        form_hooks.addRow("", self.hook_canvas)
        form_hooks.addRow("", self.hook_project)
        form_hooks.addRow("", self.json_parallel)
        form_hooks.addRow("Debug diepte:", self.debug_depth)

        v_hooks.addWidget(gb_hooks); v_hooks.addStretch(1)
        tabs.addTab(tab_hooks, QIcon(self._icon_path("tab_hooks.png")), "Hooks & gedrag")

        # ----- Tab: Advanced -----
        tab_adv = QWidget(); v_adv = QVBoxLayout(tab_adv)
        gb_adv = QGroupBox("Advanced logging"); form_adv = QFormLayout(gb_adv)
        self.scrub = QCheckBox("PII/paden scrubben in logs"); self.scrub.setChecked(bool(get_setting("scrub_enabled", bool)))
        form_adv.addRow("", self.scrub)

        self.heartbeat = QSpinBox(); self.heartbeat.setRange(10, 3600); self.heartbeat.setValue(int(get_setting("heartbeat_sec", int)))
        form_adv.addRow("Heartbeat (sec):", self.heartbeat)

        self.watchdog = QCheckBox("Watchdog logactiviteit bewaken")
        self.watchdog.setChecked(bool(get_setting("watchdog_enabled", bool)))
        self.watchdog_idle = QSpinBox(); self.watchdog_idle.setRange(10, 3600)
        self.watchdog_idle.setValue(int(get_setting("watchdog_idle_sec", int)))
        row_watch = QWidget(); row_watch_lay = QHBoxLayout(row_watch); row_watch_lay.setContentsMargins(0,0,0,0)
        row_watch_lay.addWidget(self.watchdog); row_watch_lay.addWidget(QLabel("Tijd (s):"))
        row_watch_lay.addWidget(self.watchdog_idle); row_watch_lay.addStretch(1)
        form_adv.addRow("", row_watch)

        self.max_mb = QSpinBox(); self.max_mb.setRange(1, 1024); self.max_mb.setValue(int(get_setting("max_log_mb", int)))
        self.rot_bk = QSpinBox(); self.rot_bk.setRange(1, 50); self.rot_bk.setValue(int(get_setting("rot_backups", int)))
        row_rot = QWidget(); row_rot_lay = QHBoxLayout(row_rot); row_rot_lay.setContentsMargins(0,0,0,0)
        row_rot_lay.addWidget(self.max_mb); row_rot_lay.addWidget(QLabel("MB   Backups:")); row_rot_lay.addWidget(self.rot_bk); row_rot_lay.addStretch(1)
        form_adv.addRow("Rotatie:", row_rot)

        self.webhook = QLineEdit(get_setting("webhook_url", str)); self.webhook.setPlaceholderText("https://…")
        form_adv.addRow("Webhook URL (errors):", self.webhook)
        self.single_file = QCheckBox("Eén logbestand per QGIS-sessie"); self.single_file.setChecked(bool(get_setting("single_file_session", bool)))
        form_adv.addRow("", self.single_file)
        self.mute_qt = QCheckBox("Qt-ruis dempen (bekende icon/pattern warnings)"); self.mute_qt.setChecked(bool(get_setting("mute_qt_noise", bool)))
        form_adv.addRow("", self.mute_qt)
        self.coalesce = QCheckBox("Herhaalde meldingen samenvoegen (coalesce)"); self.coalesce.setChecked(bool(get_setting("coalesce_enabled", bool)))
        self.coalesce_win = QDoubleSpinBox(); self.coalesce_win.setDecimals(1); self.coalesce_win.setRange(0.5, 30.0); self.coalesce_win.setSingleStep(0.5); self.coalesce_win.setValue(float(get_setting("coalesce_window_sec", int)))
        rowc = QWidget(); from qgis.PyQt.QtWidgets import QHBoxLayout as _QHB; _rc = _QHB(rowc); _rc.setContentsMargins(0,0,0,0); _rc.addWidget(self.coalesce); _rc.addWidget(QLabel("Venster (s):")); _rc.addWidget(self.coalesce_win); _rc.addStretch(1)
        form_adv.addRow("", rowc)

        v_adv.addWidget(gb_adv); v_adv.addStretch(1)
        tabs.addTab(tab_adv, QIcon(self._icon_path("tab_adv.png")), "Advanced")

        # ----- Tab: Weergave -----
        tab_view = QWidget(); v_view = QVBoxLayout(tab_view)
        gb_view = QGroupBox("Weergave"); form_view = QFormLayout(gb_view)
        self.theme = QComboBox(); self.theme.addItems(["auto","light","dark"]); self.theme.setCurrentText(get_setting("theme", str))
        self.date_fmt = QLineEdit(get_setting("date_format", str)); self.date_fmt.setPlaceholderText("%Y-%m-%d %H:%M:%S")
        self.realtime = QCheckBox("Realtime logging viewer inschakelen (experimenteel)")
        self.realtime.setChecked(bool(get_setting("realtime_view", bool)))
        form_view.addRow("Thema:", self.theme)
        form_view.addRow("Datum/tijd-format:", self.date_fmt)
        form_view.addRow("", self.realtime)
        v_view.addWidget(gb_view); v_view.addStretch(1)
        tabs.addTab(tab_view, QIcon(self._icon_path("tab_view.png")), "Weergave")

        # ----- Tab: Tools -----
        tab_tools = QWidget(); v_tools = QVBoxLayout(tab_tools)
        gb_tools = QGroupBox("Beheer & tools"); form_tools = QFormLayout(gb_tools)

        self.tail_lines = QSpinBox(); self.tail_lines.setRange(50, 5000); self.tail_lines.setValue(int(get_setting("tail_lines", int)))
        form_tools.addRow("Tail-regels bij bundel:", self.tail_lines)

        self.prune_start = QCheckBox("Logs opschonen bij start (retentie toepassen)")
        self.prune_start.setChecked(bool(get_setting("prune_on_start", bool)))
        form_tools.addRow("", self.prune_start)

        self.gzip_rotate = QCheckBox("Gecodeerde backups (zip) voor oude logs")
        self.gzip_rotate.setChecked(bool(get_setting("gzip_rotate", bool)))
        form_tools.addRow("", self.gzip_rotate)

        btns_row = QWidget(); br = QHBoxLayout(btns_row); br.setContentsMargins(0,0,0,0)
        self.btn_export = QPushButton(QgsApplication.getThemeIcon("mActionSaveAs.svg"), "Export settings → JSON")
        self.btn_import = QPushButton(QgsApplication.getThemeIcon("mActionFileOpen.svg"), "Import settings ← JSON")
        self.btn_prune  = QPushButton(QgsApplication.getThemeIcon("mActionDeleteSelected.svg"), "Opschonen nu")
        self.btn_open   = QPushButton(QgsApplication.getThemeIcon("mActionOpenTable.svg"), "Open logmap")
        self.btn_testwh = QPushButton(QgsApplication.getThemeIcon("mIconInfo.svg"), "Test webhook")
        for b in (self.btn_export, self.btn_import, self.btn_prune, self.btn_open, self.btn_testwh):
            br.addWidget(b)
        br.addStretch(1)
        v_tools.addWidget(gb_tools); v_tools.addWidget(btns_row); v_tools.addStretch(1)
        tabs.addTab(tab_tools, QIcon(self._icon_path("tab_tools.png")), "Tools")

        main.addWidget(tabs)

        # Tooltips (snelle uitleg)
        self.json_parallel.setToolTip("Schrijf parallel een JSONL-bestand met rijke context (project, user, sessie).")
        self.scrub.setToolTip("Verberg privé-paden/IP in logregels.")
        self.heartbeat.setToolTip("Schrijf periodiek een 'heartbeat' met RAM/CPU-schatting.")
        self.watchdog.setToolTip("Waarschuw wanneer er langere tijd geen nieuwe logregels zijn.")
        self.watchdog_idle.setToolTip("Aantal seconden zonder activiteit voordat de watchdog logt.")
        self.max_mb.setToolTip("Maximale grootte per logbestand (rotatie).")
        self.rot_bk.setToolTip("Aantal bewaarde rotatie-bestanden per logtype.")
        self.prune_start.setToolTip("Voer bij starten automatisch retentie/opschonen uit.")
        self.tail_lines.setToolTip("Aantal regels uit het einde van het log dat in de bundel-zip komt.")

        # Wire tools
        self.btn_export.clicked.connect(self._export_json)
        self.btn_import.clicked.connect(self._import_json)
        self.btn_prune.clicked.connect(self._prune_now)
        self.btn_open.clicked.connect(self._open_dir)
        self.btn_testwh.clicked.connect(self._test_webhook)

        # Dialog buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        main.addWidget(btns)

    # ---- helpers ----
    def _icon_path(self, name): 
        import os
        return os.path.join(os.path.dirname(__file__), name)

    def _load_style(self):
        pref = (get_setting("theme", str) or "auto").lower()
        if pref not in {"auto", "light", "dark"}:
            pref = "auto"

        theme = pref
        if pref == "auto":
            app = QApplication.instance()
            palette = app.palette() if app else self.palette()
            try:
                # QPalette.Window best represents the platform background colour.
                window_colour = palette.color(QPalette.Window)
                theme = "dark" if window_colour.lightness() < 128 else "light"
            except Exception:
                theme = "light"

        sheet = "style_dark.qss" if theme == "dark" else "style.qss"
        self._apply_stylesheet(sheet)

    def _apply_stylesheet(self, name: str) -> None:
        try:
            f = QFile(self._icon_path(name))
            if f.open(QFile.ReadOnly | QFile.Text):
                ts = QTextStream(f)
                self.setStyleSheet(ts.readAll())
        except Exception:
            pass

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Kies logmap", self.dir_label.text() or "")
        if d: self.dir_label.setText(d)

    def _open_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_log_dir()))

    def _export_json(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Export settings", "", "JSON (*.json)")
        if not fn: return
        ok = export_settings_json(fn)
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "QGIS Monitor Pro", "Export gelukt." if ok else "Export mislukt.")

    def _import_json(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Import settings", "", "JSON (*.json)")
        if not fn: return
        ok = import_settings_json(fn)
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "QGIS Monitor Pro", "Import gelukt. Heropen Instellingen voor actuele waarden." if ok else "Import mislukt.")

    def _prune_now(self):
        prune_logs_now()
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "QGIS Monitor Pro", "Opschonen voltooid.")

    def _test_webhook(self):
        url = get_setting("webhook_url", str)
        if not url:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.warning(self, "QGIS Monitor Pro", "Geen Webhook URL ingesteld.")
            return
        post_webhook(url, {"type":"test","ok":True})

    def apply(self):
        set_setting("log_dir", "" if self.dir_label.text() == "(auto)" else self.dir_label.text())
        set_setting("keep_full", int(self.ret_full.value()))
        set_setting("keep_errs", int(self.ret_err.value()))
        set_setting("keep_snap", int(self.ret_snap.value()))
        set_setting("compress_old", self.compress_old.isChecked())
        set_setting("autostart", self.autostart.isChecked())
        set_setting("level", self.level.currentText())
        set_setting("hook_processing", self.hook_processing.isChecked())
        set_setting("hook_tasks", self.hook_tasks.isChecked())
        set_setting("hook_canvas", self.hook_canvas.isChecked())
        set_setting("hook_project", self.hook_project.isChecked())
        set_setting("json_parallel", self.json_parallel.isChecked())
        set_setting("debug_depth", int(self.debug_depth.value()))
        # advanced
        set_setting("scrub_enabled", self.scrub.isChecked())
        set_setting("heartbeat_sec", int(self.heartbeat.value()))
        set_setting("watchdog_enabled", self.watchdog.isChecked())
        set_setting("watchdog_idle_sec", int(self.watchdog_idle.value()))
        set_setting("max_log_mb", int(self.max_mb.value()))
        set_setting("rot_backups", int(self.rot_bk.value()))
        set_setting("webhook_url", self.webhook.text().strip())
        set_setting("single_file_session", self.single_file.isChecked())
        set_setting("mute_qt_noise", self.mute_qt.isChecked())
        set_setting("coalesce_enabled", self.coalesce.isChecked())
        set_setting("coalesce_window_sec", float(self.coalesce_win.value()))
        # weergave/tools
        set_setting("theme", self.theme.currentText())
        set_setting("date_format", self.date_fmt.text().strip())
        set_setting("tail_lines", int(self.tail_lines.value()))
        set_setting("prune_on_start", self.prune_start.isChecked())
        set_setting("realtime_view", self.realtime.isChecked())
        set_setting("gzip_rotate", self.gzip_rotate.isChecked())
