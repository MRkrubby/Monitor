\
# -*- coding: utf-8 -*-
"""
Ultra-logger: pakt ALLE kanalen, non-blocking (Queue), rotatie en per-sessie rotate.
Kanalen: Python logging, unhandled exceptions, warnings, stdout/stderr,
QGIS kernel/core (messageLog), Qt message handler, Processing hooks.
"""
import os, sys, io, json, time, logging, threading, warnings, tempfile, atexit
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
try:
    from queue import Queue
except Exception:
    from Queue import Queue

from qgis.core import QgsApplication, QgsMessageLog
try:
    # Gebruik vendor import zodat het in QGIS gegarandeerd werkt
    from qgis.PyQt.QtCore import qInstallMessageHandler, QtMsgType
except Exception:
    qInstallMessageHandler = None
    QtMsgType = None

LOGGER_NAME        = "QGISMonitorPro"
KERNEL_LOGGER_NAME = "QGISKernelCore"

_logger        = logging.getLogger(LOGGER_NAME)
_kernel_logger = logging.getLogger(KERNEL_LOGGER_NAME)
for lg in (_logger, _kernel_logger):
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

_LISTENER = None
_QUEUE    = None
_STARTED  = False
_CFG      = {}

_FAULT_FH = None
_STDOUT_ORIG = None
_STDERR_ORIG = None

_LAST = {}

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts":      time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "module":  record.module,
            "func":    record.funcName,
            "line":    record.lineno,
            "thread":  record.threadName,
            "process": record.process,
        }
        return json.dumps(payload, ensure_ascii=False)

class LineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d} [{record.levelname}] {record.getMessage()}"

class RateLimitFilter(logging.Filter):
    def __init__(self, per_sec=60):
        super().__init__()
        from collections import defaultdict, deque
        self.per_sec = per_sec
        self.bucket = defaultdict(deque)
    def filter(self, record):
        try:
            if record.levelno >= logging.WARNING:
                return True
            key = getattr(record, "msg", record.getMessage())
            dq = self.bucket[key]
            now = time.time()
            dq.append(now)
            while dq and now - dq[0] > 1.0:
                dq.popleft()
            return len(dq) <= self.per_sec
        except Exception:
            return True

class CoalesceFilter(logging.Filter):
    def __init__(self, window_sec=1.0):
        super().__init__()
        self.window = window_sec
    def filter(self, record):
        try:
            msg = record.getMessage()
            now = time.time()
            last = _LAST.get(record.levelno)
            if last and last[0] == msg and (now - last[1]) <= self.window:
                return False
            _LAST[record.levelno] = (msg, now)
            return True
        except Exception:
            return True

def _qt_msg_handler(mode, context, message):
    try:
        if QtMsgType and (mode == QtMsgType.QtCriticalMsg or mode == QtMsgType.QtFatalMsg):
            _kernel_logger.critical(f"[Qt] {message}")
        elif QtMsgType and mode == QtMsgType.QtWarningMsg:
            _kernel_logger.warning(f"[Qt] {message}")
        elif QtMsgType and mode == QtMsgType.QtInfoMsg:
            _kernel_logger.info(f"[Qt] {message}")
        else:
            _kernel_logger.debug(f"[Qt] {message}")
    except Exception:
        pass

def _warnings_to_log(message, category, filename, lineno, file=None, line=None):
    logging.getLogger(LOGGER_NAME).warning(f"[PyWarning] {category.__name__} in {filename}:{lineno} - {message}")

def _sys_excepthook(exctype, value, tb):
    import traceback
    txt = "".join(traceback.format_exception(exctype, value, tb))
    logging.getLogger(LOGGER_NAME).error(f"[Unhandled] {exctype.__name__}: {value}\n{txt}")

def _thread_excepthook(args):
    _sys_excepthook(args.exc_type, args.exc_value, args.exc_traceback)

class _StreamRedirect(io.TextIOBase):
    def __init__(self, level=logging.INFO):
        self.level = level
        self.logger = logging.getLogger(LOGGER_NAME)
    def write(self, s):
        s = s.strip()
        if not s: return
        try:
            self.logger.log(self.level, s)
        except Exception:
            pass
    def flush(self): pass

def _build_handlers(cfg, for_kernel=False):
    log_dir   = cfg["log_dir"]
    json_on   = cfg["json"]
    max_mb    = cfg["max_mb"]
    backups   = cfg["backups"]
    rps       = cfg["rate_limit"]
    coalesce  = cfg["coalesce_sec"]

    if for_kernel:
        full = os.path.join(log_dir, "qgis_kernel_core.log")
        errs = os.path.join(log_dir, "qgis_kernel_errors.log")
    else:
        full = cfg["full_path"]
        errs = cfg["err_path"]

    os.makedirs(log_dir, exist_ok=True)
    fmt = JsonFormatter() if json_on else LineFormatter()

    fh = RotatingFileHandler(full, maxBytes=max_mb*1024*1024, backupCount=backups, encoding="utf-8")
    fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)

    eh = RotatingFileHandler(errs, maxBytes=max_mb*1024*1024, backupCount=max(1, backups//2), encoding="utf-8")
    eh.setLevel(logging.ERROR); eh.setFormatter(fmt)

    if rps and rps > 0:
        rlf = RateLimitFilter(per_sec=rps)
        fh.addFilter(rlf); eh.addFilter(rlf)
    if coalesce and coalesce > 0:
        cf = CoalesceFilter(window_sec=coalesce)
        fh.addFilter(cf)
    return fh, eh

def _attach_queue_handlers(logger_obj, fh, eh):
    global _QUEUE, _LISTENER
    if _QUEUE is None:
        _QUEUE = Queue(maxsize=20000)
    qh = QueueHandler(_QUEUE)
    for h in list(logger_obj.handlers):
        try:
            logger_obj.removeHandler(h)
            if hasattr(h, "close"): h.close()
        except Exception:
            pass
    logger_obj.addHandler(qh)
    logger_obj.setLevel(logging.DEBUG)
    logger_obj.propagate = False

    if _LISTENER:
        try: _LISTENER.stop()
        except Exception: pass
    _LISTENER = QueueListener(_QUEUE, fh, eh, respect_handler_level=True)
    _LISTENER.start()

def _rotate_session(log_dir, keep=50):
    ts = time.strftime("%Y%m%d_%H%M%S")
    names = ["qgis_full_ultra.log","qgis_errors_ultra.log","qgis_kernel_core.log","qgis_kernel_errors.log"]
    for n in names:
        p = os.path.join(log_dir, n)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            dst = os.path.join(log_dir, f"{n}.prev-{ts}")
            try: os.replace(p, dst)
            except Exception: pass
    # prune
    import glob
    for n in names:
        prevs = sorted(glob.glob(os.path.join(log_dir, f"{n}.prev-*")), reverse=True)
        for old in prevs[keep:]:
            try: os.remove(old)
            except Exception: pass

def start(config=None):
    """Start alle handlers. Idempotent."""
    global _STARTED, _CFG, _FAULT_FH, _STDOUT_ORIG, _STDERR_ORIG
    if _STARTED:
        return

    cfg = dict(
        log_dir       = os.path.join(os.path.expanduser("~"), "Desktop", "logs"),
        full_path     = None,
        err_path      = None,
        json          = False,
        max_mb        = 20,
        backups       = 5,
        rate_limit    = 60,
        coalesce_sec  = 1.0,
        capture_warnings = True,
        capture_qt    = True,
        capture_stdout= True,
        faulthandler  = True,
        kernel_enable = True,
        keep_prev     = 50,
    )
    if config:
        cfg.update({k:v for k,v in config.items() if v is not None})

    os.makedirs(cfg["log_dir"], exist_ok=True)
    _rotate_session(cfg["log_dir"], keep=int(cfg.get("keep_prev", 50)))

    if not cfg["full_path"]: cfg["full_path"] = os.path.join(cfg["log_dir"], "qgis_full_ultra.log")
    if not cfg["err_path"]:  cfg["err_path"]  = os.path.join(cfg["log_dir"], "qgis_errors_ultra.log")

    fh, eh = _build_handlers(cfg, for_kernel=False)
    _attach_queue_handlers(_logger, fh, eh)

    if cfg["kernel_enable"]:
        kfh, keh = _build_handlers(cfg, for_kernel=True)
        _attach_queue_handlers(_kernel_logger, kfh, keh)
        try:
            QgsApplication.messageLog().messageReceived.connect(_kernel_qgis_hook)
        except Exception:
            pass

    if cfg["capture_warnings"]:
        warnings.showwarning = _warnings_to_log

    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook

    if cfg["capture_qt"] and qInstallMessageHandler and QtMsgType:
        try: qInstallMessageHandler(_qt_msg_handler)
        except Exception: pass

    if cfg["capture_stdout"]:
        _STDOUT_ORIG, _STDERR_ORIG = sys.stdout, sys.stderr
        sys.stdout = _StreamRedirect(logging.INFO)
        sys.stderr = _StreamRedirect(logging.ERROR)

    if cfg["faulthandler"]:
        try:
            import faulthandler
            if not faulthandler.is_enabled():
                crash_path = os.path.join(tempfile.gettempdir(), "qgis_faulthandler.log")
                _FAULT_FH = open(crash_path, "a", encoding="utf-8")
                faulthandler.enable(file=_FAULT_FH, all_threads=True)
                _logger.info("[CrashLog] Faulthandler enabled → %s", crash_path)
        except Exception as e:
            _logger.warning("[CrashLog] kon faulthandler niet activeren: %s", e)

    _CFG = cfg; _STARTED = True
    _logger.info("[Probe] Logging actief (full)")
    _logger.error("[Probe] Error-kanaal actief (errors)")
    atexit.register(stop)

def stop():
    global _STARTED, _LISTENER, _QUEUE, _FAULT_FH, _STDOUT_ORIG, _STDERR_ORIG
    try:
        try: QgsApplication.messageLog().messageReceived.disconnect(_kernel_qgis_hook)
        except Exception: pass
        if _STDOUT_ORIG:
            sys.stdout = _STDOUT_ORIG; _STDOUT_ORIG = None
        if _STDERR_ORIG:
            sys.stderr = _STDERR_ORIG; _STDERR_ORIG = None
        if _LISTENER:
            try: _LISTENER.stop()
            except Exception: pass
            _LISTENER = None
        _QUEUE = None
        for lg in (_logger, _kernel_logger):
            for h in list(lg.handlers):
                try:
                    lg.removeHandler(h)
                    if hasattr(h, "close"): h.close()
                except Exception: pass
        try:
            import faulthandler
            if faulthandler.is_enabled():
                faulthandler.disable()
            if _FAULT_FH:
                try: _FAULT_FH.close()
                except Exception: pass
            _FAULT_FH = None
        except Exception: pass
        _STARTED = False
        _logger.info("[UltraLog] gestopt")
    except Exception:
        pass

def _kernel_qgis_hook(msg, tag, level):
    try:
        if level in (QgsMessageLog.CRITICAL, QgsMessageLog.FATAL):
            _kernel_logger.critical("[QGIS/%s] %s", tag, msg)
        elif level == QgsMessageLog.WARNING:
            _kernel_logger.warning("[QGIS/%s] %s", tag, msg)
        elif level == QgsMessageLog.INFO:
            _kernel_logger.info("[QGIS/%s] %s", tag, msg)
        elif level == QgsMessageLog.DEBUG:
            _kernel_logger.debug("[QGIS/%s] %s", tag, msg)
        else:
            _kernel_logger.info("[QGIS/%s] %s", tag, msg)
    except Exception:
        pass

def hook_processing():
    """Log elke processing.run-aanroep."""
    try:
        import processing
        _orig = processing.run
        def _wrap(alg_id, params=None, *args, **kwargs):
            lg = logging.getLogger(LOGGER_NAME)
            try:
                lg.info("[Processing] run %s params=%s", alg_id, params)
                res = _orig(alg_id, params or {}, *args, **kwargs)
                lg.info("[Processing] done %s keys=%s", alg_id, list(res.keys()) if isinstance(res, dict) else type(res))
                return res
            except Exception as e:
                lg.error("[Processing] ERROR %s: %s", alg_id, e, exc_info=True)
                raise
        processing.run = _wrap
        logging.getLogger(LOGGER_NAME).info("[Processing] hook actief")
    except Exception:
        pass
