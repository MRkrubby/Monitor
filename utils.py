
# -*- coding: utf-8 -*-
import os, tempfile, platform, sys, json
from datetime import datetime, timezone
from qgis.PyQt.QtCore import QSettings
from qgis.core import Qgis, QgsMessageLog

ORG = "QGISMonitorPro"
APP = "qgis_monitor_pro"

def log(msg, level=Qgis.Info, tag="Monitor"):
    try:
        QgsMessageLog.logMessage(str(msg), tag, level)
    except Exception:
        print(f"[{tag}] {msg}")

def settings() -> QSettings:
    return QSettings(ORG, APP)

DEFAULTS = {
    "single_file_session": True,
    "mute_qt_noise": True,
    "coalesce_enabled": True,
    "coalesce_window_sec": 3.0,

    "log_dir": "",
    "keep_full": 50,
    "keep_errs": 50,
    "keep_snap": 50,
    "compress_old": False,
    "autostart": True,
    "level": "DEBUG",
    "hook_processing": True,
    "hook_tasks": True,
    "hook_canvas": True,
    "hook_project": True,
    "json_parallel": False,
    "debug_depth": 1,
    # Advanced
    "scrub_enabled": True,
    "heartbeat_sec": 120,
    "max_log_mb": 20,
    "rot_backups": 5,
    "webhook_url": "",
    "theme": "auto",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "tail_lines": 800,
    "prune_on_start": True,
    "realtime_view": False,
    "gzip_rotate": False,

}

def get_setting(key, typ=None):
    s = settings()
    if key not in DEFAULTS:
        return s.value(key, None, type=typ)
    default = DEFAULTS[key]
    return s.value(key, default, type=typ if typ is not None else type(default))

def set_setting(key, val):
    s = settings()
    s.setValue(key, val)
def get_log_dir() -> str:
    d = get_setting("log_dir", str)
    if d:
        try: os.makedirs(d, exist_ok=True)
        except Exception: pass
        return d
    d = os.path.join(tempfile.gettempdir(), "qgis_monitor_logs")
    try: os.makedirs(d, exist_ok=True)
    except Exception: pass
    return d

def system_summary() -> str:
    try:
        from qgis.core import Qgis, QgsApplication
        qver = Qgis.QGIS_VERSION
        app = QgsApplication.instance()
    except Exception:
        qver = "unknown"; app = None
    lines = [
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}Z",
        f"OS: {platform.platform()}",
        "Python: " + sys.version.replace("\n", " "),
        f"QGIS Version: {qver}",
        f"App Instance: {'yes' if app else 'no'}",
    ]
    return "\n".join(lines)

def write_diagnostics_txt(out_dir=None) -> str:
    d = out_dir or get_log_dir()
    try: os.makedirs(d, exist_ok=True)
    except Exception: pass
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(d, f"diagnostics_{ts}.txt")
    from .utils import DEFAULTS as _DEF  # local
    info = [
        "== QGIS Monitor Pro Diagnostics ==",
        system_summary(),
        "",
        "Settings:",
        *(f"{k}={get_setting(k)}" for k in _DEF.keys()),
        "",
        "Tip: open QGIS Log Messages panel voor meer details.",
    ]
    try:
        with open(path, "w", encoding="utf-8") as f:

            f.write("\\n".join(info) + "\\n")
        return path
    except Exception as e:
        return f"ERROR: Kon rapport niet schrijven: {e}"

def bundle_logs_zip(zip_path: str, files: list, extra_texts: dict = None):
    try:
        import zipfile
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:

            for p in files:
                if p and os.path.exists(p):
                    z.write(p, os.path.basename(p))
            if extra_texts:
                for name, txt in extra_texts.items():
                    z.writestr(name, txt or "")
        return True
    except Exception:
        return False

def post_webhook(url, payload:dict):
    if not url:
        return
    try:
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type":"application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def export_settings_json(path: str) -> bool:
    try:
        data = {k: get_setting(k) for k in DEFAULTS.keys()}
        with open(path, "w", encoding="utf-8") as f:

            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def import_settings_json(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:

            data = json.load(f)
        for k, v in data.items():
            if k in DEFAULTS:
                set_setting(k, v)
        return True
    except Exception:
        return False

def _prune_pattern(dirpath, pattern, keep=50, compress=False):
    try:
        files = sorted(glob.glob(os.path.join(dirpath, pattern)))
        old = files[:-int(keep)] if keep > 0 else files
        for p in old:
            if compress:
                zp = p + ".zip"
                try:
                    import zipfile
                    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:

                        z.write(p, os.path.basename(p))
                    os.remove(p)
                except Exception:
                    pass
            else:
                try: os.remove(p)
                except Exception: pass
    except Exception:
        pass

def prune_logs_now(dirpath: str = None):
    d = dirpath or get_log_dir()
    try: os.makedirs(d, exist_ok=True)
    except Exception: pass
    keep_full = int(get_setting("keep_full", int))
    keep_errs = int(get_setting("keep_errs", int))
    keep_snap = int(get_setting("keep_snap", int))
    compress = bool(get_setting("compress_old", bool))
    _prune_pattern(d, "qgis_full_*.log*", keep_full, compress)
    _prune_pattern(d, "qgis_errors_*.log*", keep_errs, compress)
    _prune_pattern(os.path.join(d, "crash_snapshots"), "log_tail_*.log", keep_snap, compress)
    # JSONL
    _prune_pattern(d, "qgis_full_*.jsonl*", keep_full, compress)


# ---- QMP 3.3.12: richer diagnostics (overrides earlier definition) ----
from datetime import datetime, timezone
import os

def _tail(path, n=80):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 4096
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                step = block if pos >= block else pos
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
            return data.decode("utf-8", "ignore").splitlines()[-n:]
    except Exception:
        return []

def write_diagnostics_txt():
    base = get_log_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    p = os.path.join(base, f"diagnostics_{ts}.txt")

    lines = [system_summary(), ""]

    try:
        import psutil
        vm = psutil.virtual_memory()
        lines += [
            f"CPU percent: {psutil.cpu_percent(interval=0.2)}%",
            f"RAM used: {vm.used//(1024*1024)} MB / {vm.total//(1024*1024)} MB",
            f"Processes: {len(psutil.pids())}",
        ]
    except Exception:
        lines += ["psutil not available (optional)"]

    try:
        import threading
        lines.append("Active threads: " + str(len(threading.enumerate())))
        for t in threading.enumerate():
            lines.append(f" - {t.name} ({'daemon' if t.daemon else 'user'})")
    except Exception:
        pass

    try:
        latest = os.path.join(base, "latest.txt")
        full = None
        if os.path.exists(latest):
            with open(latest, "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith("FULL="):
                        full = ln.split("=", 1)[1].strip()
        if full and os.path.exists(full):
            lines += ["", f"=== Tail of full log: {full} ==="]
            lines += _tail(full, 80)
    except Exception:
        pass

    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p

