"""Utility helpers shared across the QGIS Monitor Pro plugin.

The repository rewrite consolidates settings, diagnostics and filesystem
helpers so the rest of the codebase can focus on the monitoring logic and
user interface concerns.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, MutableMapping, Optional

from qgis.PyQt.QtCore import QSettings
from qgis.core import Qgis, QgsMessageLog


ORG = "QGISMonitorPro"
APP = "qgis_monitor_pro"


# ---------------------------------------------------------------------------
# Settings management
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: Any
    qt_type: Optional[type] = None

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default
        if self.qt_type:
            try:
                return self.qt_type(value)
            except Exception:
                return self.default
        try:
            if self.default is None:
                return value
            return type(self.default)(value)
        except Exception:
            return self.default


SETTING_SPECS: Dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec("single_file_session", True, bool),
        SettingSpec("mute_qt_noise", True, bool),
        SettingSpec("coalesce_enabled", True, bool),
        SettingSpec("coalesce_window_sec", 3.0, float),
        SettingSpec("log_dir", "", str),
        SettingSpec("keep_full", 50, int),
        SettingSpec("keep_errs", 50, int),
        SettingSpec("keep_snap", 50, int),
        SettingSpec("compress_old", False, bool),
        SettingSpec("autostart", True, bool),
        SettingSpec("level", "DEBUG", str),
        SettingSpec("hook_processing", True, bool),
        SettingSpec("hook_tasks", True, bool),
        SettingSpec("hook_canvas", True, bool),
        SettingSpec("hook_project", True, bool),
        SettingSpec("json_parallel", False, bool),
        SettingSpec("debug_depth", 1, int),
        SettingSpec("scrub_enabled", True, bool),
        SettingSpec("heartbeat_sec", 120, int),
        SettingSpec("watchdog_enabled", True, bool),
        SettingSpec("watchdog_idle_sec", 90, int),
        SettingSpec("max_log_mb", 20, int),
        SettingSpec("rot_backups", 5, int),
        SettingSpec("webhook_url", "", str),
        SettingSpec("theme", "auto", str),
        SettingSpec("date_format", "%Y-%m-%d %H:%M:%S", str),
        SettingSpec("tail_lines", 800, int),
        SettingSpec("prune_on_start", True, bool),
        SettingSpec("realtime_view", False, bool),
        SettingSpec("gzip_rotate", False, bool),
    )
}


DEFAULTS: Dict[str, Any] = {key: spec.default for key, spec in SETTING_SPECS.items()}


def settings() -> QSettings:
    """Return the plugin-wide :class:`QSettings` instance."""

    return QSettings(ORG, APP)


def get_setting(key: str, typ: Optional[type] = None) -> Any:
    spec = SETTING_SPECS.get(key)
    store = settings()
    if spec is None:
        return store.value(key, None, type=typ)
    value = store.value(key, spec.default, type=spec.qt_type or type(spec.default))
    return spec.coerce(value)


def set_setting(key: str, value: Any) -> None:
    spec = SETTING_SPECS.get(key)
    store = settings()
    if spec is None:
        store.setValue(key, value)
    else:
        store.setValue(key, spec.coerce(value))


def load_all_settings() -> Dict[str, Any]:
    return {key: get_setting(key) for key in SETTING_SPECS}


def apply_settings(update: MutableMapping[str, Any]) -> None:
    for key, value in update.items():
        if key in SETTING_SPECS:
            set_setting(key, value)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def log(message: Any, level: Qgis.MessageLevel = Qgis.Info, tag: str = "Monitor") -> None:
    try:
        QgsMessageLog.logMessage(str(message), tag, level)
    except Exception:
        print(f"[{tag}] {message}")


def get_log_dir() -> str:
    override = str(get_setting("log_dir", str) or "").strip()
    if override:
        resolved = Path(override).expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return str(resolved.resolve())
    auto = Path(tempfile.gettempdir()) / "qgis_monitor_logs"
    auto.mkdir(parents=True, exist_ok=True)
    return str(auto)


# ---------------------------------------------------------------------------
# Diagnostics utilities
# ---------------------------------------------------------------------------


def system_summary() -> str:
    try:
        from qgis.core import QgsApplication

        qver = Qgis.QGIS_VERSION
        app = QgsApplication.instance()
    except Exception:
        qver = "unknown"
        app = None

    lines = [
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}Z",
        f"OS: {platform.platform()}",
        "Python: " + sys.version.replace("\n", " "),
        f"QGIS Version: {qver}",
        f"App Instance: {'yes' if app else 'no'}",
    ]
    return "\n".join(lines)


def write_diagnostics_txt(out_dir: Optional[str] = None) -> str:
    target_dir = Path(out_dir or get_log_dir())
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = target_dir / f"diagnostics_{ts}.txt"
    lines = [
        "== QGIS Monitor Pro Diagnostics ==",
        system_summary(),
        "",
        "Settings:",
        *(f"{key}={get_setting(key)}" for key in sorted(SETTING_SPECS)),
        "",
        "Tip: open QGIS Log Messages panel voor meer details.",
    ]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:  # pragma: no cover - defensive for QGIS runtime
        return f"ERROR: Kon rapport niet schrijven: {exc}"


def bundle_logs_zip(zip_path: str, files: Iterable[str], extra_texts: Optional[Dict[str, str]] = None) -> bool:
    try:
        import zipfile

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                if path and os.path.exists(path):
                    zf.write(path, os.path.basename(path))
            if extra_texts:
                for name, content in extra_texts.items():
                    zf.writestr(name, content or "")
        return True
    except Exception:
        return False


def post_webhook(url: str, payload: Dict[str, Any]) -> None:
    if not url:
        return
    try:
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=2)
    except Exception:
        pass


def export_settings_json(path: str) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(load_all_settings(), handle, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def import_settings_json(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        apply_settings(data)
        return True
    except Exception:
        return False


def _prune_pattern(directory: Path, pattern: str, keep: int, compress: bool) -> None:
    try:
        import glob

        files = sorted(Path(p) for p in glob.glob(str(directory / pattern)))
        if keep > 0:
            old = files[:-keep]
        else:
            old = files
        for path in old:
            if compress:
                target = path.with_suffix(path.suffix + ".zip")
                try:
                    import zipfile

                    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.write(str(path), path.name)
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass


def prune_logs_now(dirpath: Optional[str] = None) -> None:
    directory = Path(dirpath or get_log_dir())
    directory.mkdir(parents=True, exist_ok=True)
    keep_full = int(get_setting("keep_full", int))
    keep_errs = int(get_setting("keep_errs", int))
    keep_snap = int(get_setting("keep_snap", int))
    compress = bool(get_setting("compress_old", bool))
    _prune_pattern(directory, "qgis_full_*.log*", keep_full, compress)
    _prune_pattern(directory, "qgis_errors_*.log*", keep_errs, compress)
    _prune_pattern(directory / "crash_snapshots", "log_tail_*.log", keep_snap, compress)
    _prune_pattern(directory, "qgis_full_*.jsonl*", keep_full, compress)

