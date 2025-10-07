
# -*- coding: utf-8 -*-
"""
QGIS Monitor Pro — engine (v3.3.7 clean)
"""
import os, sys, time, json, traceback, logging, tempfile, uuid, re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, MemoryHandler
from collections import deque
from typing import Dict, List

from qgis.core import Qgis, QgsApplication, QgsProject, QgsMapLayer, QgsMessageLog
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtCore import QSettings, QTimer

from .utils import get_setting, set_setting, get_log_dir, bundle_logs_zip, post_webhook

MONITOR_TAG = "QGISMonitorPro"
ORG = "QGISMonitorPro"
APP = "qgis_monitor_pro"

logger = logging.getLogger(MONITOR_TAG)
logger.setLevel(logging.DEBUG)
logger.propagate = False

_started = False
fh = eh = qh = jh = memh = None
LOG_DIR = LOG_FILE = ERR_PATH = JSON_PATH = None
session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
_breadcrumbs = deque(maxlen=400)
_heartbeat_timer = None
_watchdog_timer = None
_last_log_emit = 0.0
_watchdog_warned = False
_WATCHDOG_FILTER = None

try:
    import psutil
    _ps = psutil.Process(os.getpid())
except Exception:
    psutil = None; _ps = None

# ------------- helpers -------------
def _normpath(p:str) -> str:
    try: return os.path.normpath(os.path.abspath(p))
    except Exception: return p

def _sample_label(prefix=""):
    if not _ps: return prefix.strip()
    try:
        mem = _ps.memory_info().rss / (1024 * 1024)
        cpu = int(_ps.cpu_percent(interval=None))
        return f"{prefix}RAM={mem:.1f}MB CPU≈{cpu}%".strip()
    except Exception:
        return prefix.strip()

def _fmt():
    df = get_setting("date_format", str) or "%Y-%m-%d %H:%M:%S"
    return logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt=df)

def _level():
    lvl = get_setting("level", str) or "DEBUG"
    return getattr(logging, lvl.upper(), logging.DEBUG)

def crumb(evt:str):
    try:
        entry = f"{datetime.now(timezone.utc).isoformat()} {evt}"
        _breadcrumbs.append(entry)
        logger.debug("Breadcrumb toegevoegd: %s", entry)
    except Exception:
        pass


def monitor_status() -> Dict[str, object]:
    """Return a snapshot of the monitor engine state for UI diagnostics."""
    try:
        hb_active = bool(_heartbeat_timer and _heartbeat_timer.isActive())
    except Exception:
        hb_active = False
    return {
        "started": bool(_started),
        "session_id": session_id,
        "log_dir": LOG_DIR,
        "log_file": LOG_FILE,
        "error_log": ERR_PATH,
        "json_log": JSON_PATH,
        "breadcrumbs": len(_breadcrumbs),
        "heartbeat_active": hb_active,
    }


def get_recent_breadcrumbs(limit: int = 20) -> List[str]:
    try:
        lim = max(1, int(limit))
    except Exception:
        lim = 20
    if not _breadcrumbs:
        return []
    return list(_breadcrumbs)[-lim:]

_SCRUB = [
    (re.compile(r"C:\\\\Users\\[^\\]+", re.I), r"C:\\Users\\<redacted>"),
    (re.compile(r"/home/[^/]+", re.I), r"/home/<redacted>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
]
def _scrub(text: str) -> str:
    if not get_setting("scrub_enabled", bool): return text
    try:
        for rx, rep in _SCRUB: text = rx.sub(rep, text)
    except Exception: pass
    return text

class QGISLogHandler(logging.Handler):
    MAP = {logging.ERROR: Qgis.Critical, logging.WARNING: Qgis.Warning, logging.INFO: Qgis.Info, logging.DEBUG: Qgis.Info}
    def emit(self, record):
        try:
            msg = _scrub(record.getMessage())
            QgsMessageLog.logMessage(str(msg), MONITOR_TAG, self.MAP.get(record.levelno, Qgis.Info))
        except Exception:
            pass

class JsonWriter:
    def __init__(self, path):
        self.path = path; self.f = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.f = open(path, "a", encoding="utf-8", buffering=1)
        except Exception as e:
            QgsMessageLog.logMessage(f"{MONITOR_TAG} JSONL open fail: {e}", MONITOR_TAG, Qgis.Warning); self.f = None
    def write(self, msg:dict):
        if not self.f: return
        try:
            self.f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.f.flush()
            try: os.fsync(self.f.fileno())
            except Exception: pass
        except Exception: pass
    def close(self):
        try:
            if self.f: self.f.close()
        except Exception: pass

# ---- Noise reduction ----
_COALESCE = {}
_COALESCE_TIMER = None
_NOISE_PATTERNS = [
    "Could not resolve property: #Checkerboard",
    "Could not resolve property: #Cross",
    "Could not resolve property: #Dense",
    "Cannot open file ':/images/themes/default/",
    "libpng warning:",
]

def _coalesce_key(record: logging.LogRecord):
    msg = record.getMessage()
    if "http" in msg: msg = re.sub(r"(\?|&).*", "", msg)
    return (record.levelno, msg[:512])

class CoalesceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not get_setting("coalesce_enabled", bool):
            return True
        win = float(get_setting("coalesce_window_sec", float) or 3.0)
        now = time.time()
        key = _coalesce_key(record)
        cnt, last = _COALESCE.get(key, (0, 0.0))
        if now - last <= win:
            _COALESCE[key] = (cnt+1, last)
            return False
        else:
            if cnt > 0:
                logger.log(logging.INFO, "[coalesce] vorige melding %dx herhaald: %s", cnt, key[1])
            _COALESCE[key] = (0, now)
            return True

def _flush_coalesce_summary():
    if not _COALESCE: return
    items = list(_COALESCE.items()); _COALESCE.clear()
    for (lvl, msg), (cnt, last) in items:
        if cnt > 0:
            logger.log(logging.INFO, "[coalesce] melding %dx herhaald: %s", cnt, msg)

class QtNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not get_setting("mute_qt_noise", bool):
            return True
        if record.levelno < logging.WARNING:
            return True
        msg = record.getMessage()
        for pat in _NOISE_PATTERNS:
            if pat in msg:
                return False
        return True

# ------------- setup logger -------------
def _project_suffix():
    try:
        name = QgsProject.instance().baseName() or "no_project"
        return "_" + "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)[:40]
    except Exception:
        return "_no_project"

def _setup_paths():
    global LOG_DIR, LOG_FILE, ERR_PATH, JSON_PATH
    LOG_DIR = _normpath(get_log_dir()); os.makedirs(LOG_DIR, exist_ok=True)
    suffix = _project_suffix()
    app = QgsApplication.instance()
    reuse = bool(get_setting("single_file_session", bool))
    paths = app.property("qgismonitor_paths") if reuse else None
    if reuse and paths:
        LOG_FILE, ERR_PATH, JSON_PATH = paths.get("LOG_FILE"), paths.get("ERR_PATH"), paths.get("JSON_PATH")
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        LOG_FILE = _normpath(os.path.join(LOG_DIR, f"qgis_full_{ts}{suffix}.log"))
        ERR_PATH = _normpath(os.path.join(LOG_DIR, f"qgis_errors_{ts}{suffix}.log"))
        JSON_PATH = _normpath(os.path.join(LOG_DIR, f"qgis_full_{ts}{suffix}.jsonl")) if get_setting("json_parallel", bool) else None
        if reuse: app.setProperty("qgismonitor_paths", {"LOG_FILE": LOG_FILE, "ERR_PATH": ERR_PATH, "JSON_PATH": JSON_PATH})
    try:
        with open(os.path.join(LOG_DIR, "latest.txt"), "w", encoding="utf-8") as mf:
            mf.write(f"FULL={LOG_FILE}\nERRORS={ERR_PATH}\nJSON={JSON_PATH or ''}\nSTARTED={datetime.now(timezone.utc).isoformat()}\n")
    except Exception:
        pass
    logger.info(
        "Logpaden ingesteld: full=%s errors=%s json=%s dir=%s",
        LOG_FILE,
        ERR_PATH,
        JSON_PATH,
        LOG_DIR,
    )

def _open_rotating(path, level):
    fmt = _fmt()
    try:
        h = RotatingFileHandler(path, mode="a", encoding="utf-8",
                                maxBytes=int(get_setting("max_log_mb", int))*1024*1024,
                                backupCount=int(get_setting("rot_backups", int)))
        h.setLevel(level); h.setFormatter(fmt); return h
    except Exception as e:
        try:
            from logging import FileHandler
            h = FileHandler(path, mode="a", encoding="utf-8", delay=False)
            h.setLevel(level); h.setFormatter(fmt)
            QgsMessageLog.logMessage(f"{MONITOR_TAG}: rotating fallback -> {e}", MONITOR_TAG, Qgis.Warning)
            return h
        except Exception as e2:
            QgsMessageLog.logMessage(f"{MONITOR_TAG}: file handler fail -> {e2}", MONITOR_TAG, Qgis.Critical)
            return None

def _install_handlers():
    global fh, eh, qh, jh, memh, _WATCHDOG_FILTER, _last_log_emit, _watchdog_warned
    for h in list(logger.handlers):
        try: logger.removeHandler(h); h.close()
        except Exception: pass

    lvl = _level()
    fh = _open_rotating(LOG_FILE, lvl)
    eh = _open_rotating(ERR_PATH, logging.ERROR)
    class _ErrOnly(logging.Filter):
        def filter(self, rec): return rec.levelno >= logging.ERROR
    if eh: eh.addFilter(_ErrOnly())

    memh = MemoryHandler(256, flushLevel=logging.INFO, target=fh)
    memh.setLevel(lvl); memh.setFormatter(_fmt())
    # filters
    try:
        memh.addFilter(CoalesceFilter()); memh.addFilter(QtNoiseFilter())
    except Exception: pass
    logger.addHandler(memh)

    if eh:
        try: eh.addFilter(CoalesceFilter()); eh.addFilter(QtNoiseFilter())
        except Exception: pass
        logger.addHandler(eh)

    qh = QGISLogHandler(); qh.setLevel(logging.DEBUG); qh.setFormatter(_fmt())
    try: qh.addFilter(QtNoiseFilter())
    except Exception: pass
    logger.addHandler(qh)

    if JSON_PATH:
        try: globals()['jh'] = JsonWriter(JSON_PATH)
        except Exception: globals()['jh'] = None
    else:
        globals()['jh'] = None

    class _WatchdogTap(logging.Filter):
        def filter(self, record):
            globals()['_last_log_emit'] = time.time()
            return True

    if _WATCHDOG_FILTER is not None:
        try: logger.removeFilter(_WATCHDOG_FILTER)
        except Exception: pass
    _WATCHDOG_FILTER = _WatchdogTap()
    logger.addFilter(_WATCHDOG_FILTER)
    _last_log_emit = time.time()
    _watchdog_warned = False

def force_flush():
    for h in list(logger.handlers):
        try: h.flush()
        except Exception: pass
    try:
        if hasattr(memh, "flush"): memh.flush()
    except Exception: pass

def _write_healthcheck():
    logger.info("===== %s gestart @ %s =====", MONITOR_TAG, datetime.now(timezone.utc).isoformat())
    logger.info("Logmap: %s", LOG_DIR)
    logger.info("Huidig full-log: %s", LOG_FILE)
    logger.info("Huidig errors-log: %s", ERR_PATH)
    if JSON_PATH: logger.info("Huidig JSON-log: %s", JSON_PATH)

# ------------- hooks -------------
def _install_processing_hooks():
    if not get_setting("hook_processing", bool): return
    try: import processing as _p
    except Exception as e:
        logger.error("[Processing] import mislukt: %s", e); return
    if getattr(_p, "_qgm_wrapped", False): return
    _orig_run = _p.run
    _orig_runLoad = getattr(_p, "runAndLoadResults", None)
    debug_depth = int(get_setting("debug_depth", int))

    def _safe(o):
        try: json.dumps(o); return o
        except Exception:
            try:
                if isinstance(o, dict): return {str(k): _safe(v) for k,v in o.items()}
                if isinstance(o, (list, tuple)): return [_safe(v) for v in o]
                return repr(o)
            except Exception: return "<unserializable>"

    def _pp(p):
        try: return json.dumps(_safe(p), ensure_ascii=False, indent=2)
        except Exception: return repr(p)

    def run(alg, parameters=None, *a, **kw):
        corr = uuid.uuid4().hex[:12]
        t0 = time.time(); crumb(f"processing:start {alg} {corr}")
        try:
            logger.info("[Processing] START %s corr=%s %s", alg, corr, _sample_label(" | "))
            if debug_depth > 0 and parameters is not None:
                logger.debug("[Processing] params %s (corr=%s):\n%s", alg, corr, _pp(parameters))
            res = _orig_run(alg, parameters, *a, **kw)
            dt = time.time() - t0
            keys = list(res.keys()) if hasattr(res, "keys") else "?"
            logger.info("[Processing] DONE  %s corr=%s in %.3fs | keys=%s %s", alg, corr, dt, keys, _sample_label("| "))
            return res
        except Exception:
            dt = time.time() - t0
            tail = "\n".join(list(_breadcrumbs)[-100:])
            logger.error("[Processing] FAIL %s corr=%s na %.3fs %s\n-- breadcrumbs --\n%s\n%s", alg, corr, dt, _sample_label("| "), tail, traceback.format_exc())
            _notify_webhook("processing_fail", {"alg": str(alg), "corr": corr, "dt": dt})
            raise

    def runLoad(alg, parameters=None, *a, **kw):
        if _orig_runLoad is None: return run(alg, parameters, *a, **kw)
        corr = uuid.uuid4().hex[:12]
        t0 = time.time(); crumb(f"processing:start(load) {alg} {corr}")
        try:
            logger.info("[Processing] START(load) %s corr=%s %s", alg, corr, _sample_label(" | "))
            if debug_depth > 0 and parameters is not None:
                logger.debug("[Processing] params %s (corr=%s):\n%s", alg, corr, _pp(parameters))
            res = _orig_runLoad(alg, parameters, *a, **kw)
            dt = time.time() - t0
            logger.info("[Processing] DONE (load) %s corr=%s in %.3fs %s", alg, corr, dt, _sample_label("| "))
            return res
        except Exception:
            dt = time.time() - t0
            tail = "\n".join(list(_breadcrumbs)[-100:])
            logger.error("[Processing] FAIL(load) %s corr=%s na %.3fs %s\n-- breadcrumbs --\n%s\n%s", alg, corr, dt, _sample_label("| "), tail, traceback.format_exc())
            _notify_webhook("processing_fail", {"alg": str(alg), "corr": corr, "dt": dt})
            raise

    _p.run = run
    if _orig_runLoad is not None: _p.runAndLoadResults = runLoad
    _p._qgm_wrapped = True
    logger.info("[Processing] hooks actief (run%s)", " + runAndLoadResults" if _orig_runLoad else "")

def _connect_canvas(iface):
    if not get_setting("hook_canvas", bool): return
    c = getattr(iface, "mapCanvas", lambda: None)()
    if not c: return

    # Disconnect older handlers
    for attr, sig in [("_qgm_rs", "renderStarting"), ("_qgm_rc", "renderComplete"),
                      ("_qgm_ext", "extentsChanged"), ("_qgm_scale", "scaleChanged"),
                      ("_qgm_ref", "mapCanvasRefreshed")]:
        try:
            fn = getattr(c, attr, None)
            if fn: getattr(c, sig).disconnect(fn)
        except Exception: pass

    def _rs():
        try: c._t0 = time.time()
        except Exception: pass
        crumb("canvas:render-start"); logger.debug("[Canvas] render start %s", _sample_label(" | "))

    def _rc(_img=None):
        t0 = getattr(c, "_t0", None); dt = (time.time() - t0) if t0 else 0.0
        crumb(f"canvas:render-done {dt:.3f}s"); logger.info("[Canvas] render done in %.3fs %s", dt, _sample_label("| "))

    def _ext():
        crumb("canvas:extentsChanged"); logger.debug("[Canvas] extentsChanged %s", _sample_label(" | "))

    def _scale(s):
        crumb(f"canvas:scaleChanged {s}"); logger.info("[Canvas] scaleChanged → %.2f", s)

    def _ref():
        crumb("canvas:refreshed"); logger.debug("[Canvas] refreshed %s", _sample_label(" | "))

    c._qgm_rs = _rs; c._qgm_rc = _rc; c._qgm_ext = _ext; c._qgm_scale = _scale; c._qgm_ref = _ref
    try: c.renderStarting.connect(c._qgm_rs)
    except Exception: pass
    try: c.renderComplete.connect(c._qgm_rc)
    except Exception: pass
    try: c.extentsChanged.connect(c._qgm_ext)
    except Exception: pass
    try: c.scaleChanged.connect(c._qgm_scale)
    except Exception: pass
    try: c.mapCanvasRefreshed.connect(c._qgm_ref)
    except Exception: pass

    logger.debug("[Canvas] hooks verbonden")

def _connect_tasks():
    tm = QgsApplication.taskManager()
    if not get_setting("hook_tasks", bool): return

    def _status_name(task):
        try: return task.statusAsString()
        except Exception: return str(getattr(task, "status", "?"))

    def _desc(task):
        try: return task.description()
        except Exception: return "?"

    def on_added(task):
        try: desc = _desc(task)
        except Exception: desc = "(onbekend)"
        crumb(f"task:add {desc}"); logger.info("[Task] Toegevoegd: %s %s", desc or "(onbekend)", _sample_label(" | "))

    def on_all_finished():
        logger.info("[TaskManager] Alle taken gereed")

    try: tm.taskAdded.disconnect(on_added)
    except Exception: pass
    try: tm.allTasksFinished.disconnect(on_all_finished)
    except Exception: pass
    try:
        tm.taskAdded.connect(on_added)
        tm.allTasksFinished.connect(on_all_finished)
    except Exception:
        logger.warning("[TaskManager] kon signalen niet verbinden")
    logger.debug("[TaskManager] hooks verbonden")

def _connect_project_layer():
    if not get_setting("hook_project", bool): return
    prj = QgsProject.instance()

    def on_opened(*_):
        fn = prj.fileName() or "(onbekend pad)"
        crumb(f"project:opened {fn}"); logger.info("[Project] Geopend: %s", fn)

    def on_saved():
        fn = prj.fileName() or "(onbekend pad)"
        crumb(f"project:saved {fn}"); logger.info("[Project] Opgeslagen: %s", fn)

    def on_cleared():
        crumb("project:cleared"); logger.warning("[Project] Leeggemaakt")

    def on_layer_added(layer: QgsMapLayer):
        try: logger.info("[Layer+] %s | type=%s | id=%s", layer.name(), layer.type(), layer.id())
        except Exception: logger.info("[Layer+] (onbekend)")

    def on_layer_removed(layer_id: str):
        logger.info("[Layer-] id=%s", layer_id)

    for sig in ("readProject", "readProjectWithContext", "readProjectFinished"):
        try: getattr(prj, sig).disconnect(on_opened)
        except Exception: pass
        try: getattr(prj, sig).connect(on_opened)
        except Exception: pass

    for sig, fn in (("projectSaved", on_saved), ("cleared", on_cleared), ("layerWasAdded", on_layer_added), ("layerRemoved", on_layer_removed)):
        try: getattr(prj, sig).disconnect(fn)
        except Exception: pass
        try: getattr(prj, sig).connect(fn)
        except Exception: pass

    # Selection hook
    try:
        from qgis.core import QgsVectorLayer
        def _sel_changed(ids, *a):
            try: logger.info("[Select] selectie %d features in laag", len(ids))
            except Exception: logger.info("[Select] selectie gewijzigd")
        # existing
        for lyr in prj.mapLayers().values():
            try:
                if isinstance(lyr, QgsVectorLayer):
                    try: lyr.selectionChanged.disconnect(_sel_changed)
                    except Exception: pass
                    lyr.selectionChanged.connect(_sel_changed)
            except Exception: pass
        # on add
        def on_layer_added_connect(layer):
            try:
                if isinstance(layer, QgsVectorLayer):
                    try: layer.selectionChanged.disconnect(_sel_changed)
                    except Exception: pass
                    layer.selectionChanged.connect(_sel_changed)
            except Exception: pass
        try: prj.layerWasAdded.disconnect(on_layer_added_connect)
        except Exception: pass
        try: prj.layerWasAdded.connect(on_layer_added_connect)
        except Exception: pass
    except Exception as e:
        logger.warning("[Select] hooks niet verbonden: %s", e)

    logger.debug("[Project/Layers] hooks verbonden")

def _install_qt_gdal_handlers():
    try:
        from osgeo import gdal
        def gdal_err(err_class, err_num, err_msg):
            sev = logging.ERROR if err_class >= 2 else logging.WARNING
            logger.log(sev, "[GDAL %s] %s", err_num, err_msg)
        gdal.PushErrorHandler(gdal_err)
    except Exception:
        pass

def _notify_webhook(event_type, extra=None):
    try:
        url = QSettings(ORG, APP).value("webhook_url","", type=str)
        if url:
            payload = {"type": event_type, "extra": extra or {}, "ts": datetime.now(timezone.utc).isoformat()}
            post_webhook(url, payload)
    except Exception:
        pass

def start_heartbeat():
    global _heartbeat_timer
    sec = int(get_setting("heartbeat_sec", int) or 0)
    if sec <= 0: return
    if _heartbeat_timer is None:
        _heartbeat_timer = QTimer()
        _heartbeat_timer.setInterval(sec*1000)
        _heartbeat_timer.timeout.connect(lambda: logger.info("[HB] %s", _sample_label()))
        _heartbeat_timer.start()

def stop_heartbeat():
    global _heartbeat_timer
    if _heartbeat_timer:
        _heartbeat_timer.stop(); _heartbeat_timer = None

def start_watchdog():
    global _watchdog_timer, _watchdog_warned
    if not get_setting("watchdog_enabled", bool):
        return
    idle_sec = max(10, int(get_setting("watchdog_idle_sec", int) or 60))
    interval = max(2000, min(10000, int(idle_sec * 1000 / 3)))
    if _watchdog_timer is None:
        _watchdog_timer = QTimer()
        _watchdog_timer.timeout.connect(_watchdog_check)
    _watchdog_timer.setInterval(interval)
    _watchdog_timer.start()
    _watchdog_warned = False

def stop_watchdog():
    global _watchdog_timer, _WATCHDOG_FILTER
    if _watchdog_timer:
        _watchdog_timer.stop(); _watchdog_timer = None
    if _WATCHDOG_FILTER is not None:
        try: logger.removeFilter(_WATCHDOG_FILTER)
        except Exception: pass
        _WATCHDOG_FILTER = None

def _watchdog_check():
    global _watchdog_warned
    if not get_setting("watchdog_enabled", bool):
        return
    idle_sec = max(10, int(get_setting("watchdog_idle_sec", int) or 60))
    delta = time.time() - _last_log_emit
    if delta >= idle_sec:
        if not _watchdog_warned:
            logger.warning(
                "[Watchdog] geen nieuwe logregels in %ss %s",
                int(delta),
                _sample_label("| "),
            )
            crumb(f"watchdog:idle {int(delta)}s")
            _notify_webhook("watchdog_idle", {"idle_seconds": int(delta)})
            _watchdog_warned = True
    else:
        if _watchdog_warned:
            logger.info("[Watchdog] activiteit hervat na %.1fs", delta)
        _watchdog_warned = False

def _log_project_summary():
    try:
        prj = QgsProject.instance()
        layers = prj.mapLayers()
        logger.info("[Project summary] layers=%d file=%s", len(layers), prj.fileName() or "(geen)")
    except Exception:
        pass

def make_diagnostics_zip(out_path):
    files = []
    for p in [LOG_FILE, ERR_PATH, JSON_PATH, os.path.join(LOG_DIR,"latest.txt")]:
        if p and os.path.exists(p): files.append(p)
    tail = None
    try:
        if LOG_FILE and os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[- int(get_setting("tail_lines", int) or 300):]
            snap_dir = os.path.join(LOG_DIR, "crash_snapshots"); os.makedirs(snap_dir, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            tail = os.path.join(snap_dir, f"log_tail_{ts}.log")
            with open(tail, "w", encoding="utf-8") as g: g.write("".join(lines))
    except Exception:
        pass
    extra = {"env.txt": "QGIS: " + getattr(Qgis, "QGIS_VERSION", "?")}
    return bundle_logs_zip(out_path, files + ([tail] if tail else []), extra)

# ------------- lifecycle -------------
def qgismonitor_start(iface=None):
    try:
        _qmp_enhance_bootstrap()
    except Exception:
        pass
    global _started, _COALESCE_TIMER
    app = QgsApplication.instance()
    if _started or app.property("qgismonitor_started") is True:
        return

    try:
        import faulthandler
        crash_path = os.path.join(tempfile.gettempdir(), "qgis_faulthandler.log")
        fh_ = open(crash_path, "a"); faulthandler.enable(file=fh_, all_threads=True)
    except Exception:
        pass

    _setup_paths()
    _install_handlers()
    _write_healthcheck()

    try: QApplication.instance().aboutToQuit.connect(force_flush)
    except Exception: pass

    _install_qt_gdal_handlers()
    if iface is not None: _connect_canvas(iface)
    _install_processing_hooks()
    _connect_project_layer()
    _connect_tasks()

    # coalesce summary timer
    try:
        if _COALESCE_TIMER is None:
            _COALESCE_TIMER = QTimer()
            win = max(2.0, float(get_setting("coalesce_window_sec", float) or 3.0))
            _COALESCE_TIMER.setInterval(int(win*1000))
            _COALESCE_TIMER.timeout.connect(_flush_coalesce_summary)
            _COALESCE_TIMER.start()
    except Exception:
        pass

    start_heartbeat()
    start_watchdog()
    _log_project_summary()
    logger.info("Monitor gestart (iface=%s)", bool(iface))
    QgsMessageLog.logMessage(f"{MONITOR_TAG} actief.", MONITOR_TAG, Qgis.Info)
    _started = True; app.setProperty("qgismonitor_started", True)

def _on_qgis_message(message, tag, level):
    if tag == MONITOR_TAG: return
    py = {Qgis.Critical: logging.ERROR, Qgis.Warning: logging.WARNING, Qgis.Info: logging.INFO, Qgis.Success: logging.INFO}.get(level, logging.DEBUG)
    try: logger.log(py, f"[{tag}] {_scrub(message)}")
    except Exception: pass

def qgismonitor_stop():
    global _started, jh, _COALESCE_TIMER
    if not _started: return
    try: QgsApplication.messageLog().messageReceived.disconnect(_on_qgis_message)
    except Exception: pass
    stop_heartbeat()
    stop_watchdog()
    try:
        if _COALESCE_TIMER: _COALESCE_TIMER.stop(); _COALESCE_TIMER = None
    except Exception: pass
    for h in list(logger.handlers):
        try: logger.removeHandler(h); h.close()
        except Exception: pass
    try:
        if jh: jh.close()
    except Exception: pass
    try: QgsApplication.instance().setProperty("qgismonitor_started", False)
    except Exception: pass
    logger.info("Monitor gestopt")
    QgsMessageLog.logMessage(f"{MONITOR_TAG} is gestopt.", MONITOR_TAG, Qgis.Info)
    _started = False


# === QMP 3.3.12 enhanced logging bootstrap ================================
def _qmp_enhance_bootstrap():
    """Install JSONL logging, capture unhandled errors, hook QGIS MessageLog (LogSafe)."""
    import os, re, sys, json, logging, traceback, time
    from collections import defaultdict, deque
    from datetime import datetime, timezone
    try:
        from qgis.core import QgsApplication
    except Exception:
        QgsApplication = None

    log = globals().get("logger") or logging.getLogger("QGISMonitorPro")
    log_dir = globals().get("LOG_DIR") or os.getcwd()
    log_file = globals().get("LOG_FILE")
    json_path = globals().get("JSON_PATH")

    if not json_path:
        ts = None; suffix = ""
        if isinstance(log_file, str):
            m = re.search(r"qgis_full_(\d{8}_\d{6})([^\\/]*?)\.log$", os.path.basename(log_file))
            if m: ts, suffix = m.group(1), (m.group(2) or "")
        if ts is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(log_dir, f"qgis_json_{ts}{suffix}.jsonl")
        globals()["JSON_PATH"] = json_path

    class _JsonlHandler(logging.Handler):
        def __init__(self, path):
            super().__init__(logging.DEBUG)
            self._path = path
            try: os.makedirs(os.path.dirname(path), exist_ok=True)
            except Exception: pass
        def emit(self, record):
            try:
                obj = { "ts": datetime.now(timezone.utc).isoformat(),
                        "level": record.levelname,
                        "msg": record.getMessage() }
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            except Exception:
                pass

    try:
        if not any(h.__class__.__name__.lower().startswith("jsonl") for h in list(log.handlers)):
            log.addHandler(_JsonlHandler(json_path))
    except Exception:
        pass

    def _hook(exc_type, exc_value, exc_tb):
        try:
            msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            log.error("[Python exception] " + msg)
        except Exception:
            pass
    try:
        sys.excepthook = _hook
    except Exception:
        pass

    class _ErrStream:
        def write(self, s):
            s = (s or "").strip()
            if not s:
                return
            if s.startswith("[stderr]") or "QGISMonitorPro" in s or "Traceback" in s:
                return
            try:
                log.error("[stderr] " + s)
            except Exception:
                pass
        def flush(self, *args, **kwargs):
            pass

    try:
        sys.stderr = _ErrStream()
    except Exception:
        pass

    try:
        if QgsApplication is not None:
            def on_msg(message, tag, level):
                try:
                    if tag and str(tag).lower().startswith("qgismonitorpro"):
                        return
                    if "[stderr]" in str(message) or "Traceback" in str(message):
                        return
                    if level >= 3: log.error(f"[QGIS/{tag}] {message}")
                    elif level == 2: log.error(f"[QGIS/{tag}] {message}")
                    elif level == 1: log.warning(f"[QGIS/{tag}] {message}")
                    else: log.info(f"[QGIS/{tag}] {message}")
                except Exception:
                    pass
            QgsApplication.messageLog().messageReceived.connect(on_msg)
    except Exception:
        pass

    try:
        latest = os.path.join(log_dir, "latest.txt")
        cur = {}
        if os.path.exists(latest):
            with open(latest, "r", encoding="utf-8") as f:
                for ln in f:
                    if "=" in ln:
                        k, v = ln.strip().split("=", 1); cur[k] = v
        cur["JSON"] = json_path
        with open(latest, "w", encoding="utf-8") as f:
            for k in ("FULL", "ERRORS", "JSON", "STARTED"):
                if k in cur:
                    f.write(f"{k}={cur[k]}\n")
    except Exception:
        pass

    # LogSafe: rate limiter on all handlers + stop propagation
    try:
        class RateLimitFilter(logging.Filter):
            def __init__(self, per_sec=20):
                super().__init__()
                self.per_sec = per_sec
                self._bucket = defaultdict(deque)
            def filter(self, record):
                key = getattr(record, "msg", str(record))
                dq = self._bucket[key]
                now = time.time()
                dq.append(now)
                while dq and now - dq[0] > 1.0:
                    dq.popleft()
                return len(dq) <= self.per_sec

        rlf = RateLimitFilter(per_sec=20)
        for _h in log.handlers:
            try:
                _h.addFilter(rlf)
            except Exception:
                pass
        log.propagate = False
    except Exception:
        pass

    try:
        latest = os.path.join(log_dir, "latest.txt")
        cur = {}
        if os.path.exists(latest):
            with open(latest, "r", encoding="utf-8") as f:
                for ln in f:
                    if "=" in ln:
                        k, v = ln.strip().split("=", 1); cur[k] = v
        cur["JSON"] = json_path
        with open(latest, "w", encoding="utf-8") as f:
            for k in ("FULL", "ERRORS", "JSON", "STARTED"):
                if k in cur:
                    f.write(f"{k}={cur[k]}\n")
    except Exception: pass
# ==========================================================================



# --- LogSafe: simple rate-limiter to avoid log storms ---------------------
import time
from collections import defaultdict, deque

class RateLimitFilter(logging.Filter):
    def __init__(self, per_sec=20):
        super().__init__()
        self.per_sec = per_sec
        self._bucket = defaultdict(deque)

    def filter(self, record):
        try:
            key = record.getMessage()
        except Exception:
            key = record.msg if hasattr(record, "msg") else "<?>"
        dq = self._bucket[key]
        now = time.time()
        dq.append(now)
        while dq and now - dq[0] > 1.0:
            dq.popleft()
        return len(dq) <= self.per_sec