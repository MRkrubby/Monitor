"""Core monitoring engine for QGIS Monitor Pro.

The rewrite organises the monitoring logic into a cohesive ``MonitorEngine``
class.  The engine is responsible for preparing log destinations, attaching the
various QGIS hooks and keeping track of health indicators such as the
watchdog/heartbeat timers.  Public convenience wrappers are provided at module
level so the rest of the plugin can call :func:`qgismonitor_start` and
:func:`qgismonitor_stop` without having to know about the underlying class.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import MemoryHandler, RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from qgis.PyQt.QtCore import QObject, QTimer
from qgis.core import Qgis, QgsApplication, QgsMapLayer, QgsMessageLog, QgsProject

from .utils import (
    bundle_logs_zip,
    get_log_dir,
    get_setting,
    load_all_settings,
    post_webhook,
    prune_logs_now,
    system_summary,
)


MONITOR_TAG = "QGISMonitorPro"


@dataclass
class LogPaths:
    directory: Path
    full: Path
    errors: Path
    jsonl: Optional[Path]


class ActivityFilter(logging.Filter):
    def __init__(self, callback):
        super().__init__("activity")
        self._callback = callback

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        try:
            self._callback()
        finally:
            return True


class RateLimitFilter(logging.Filter):
    """Simple per-message rate limiter to avoid flooding handlers."""

    def __init__(self, per_sec: int = 20) -> None:
        super().__init__("ratelimit")
        self.per_sec = per_sec
        self._bucket: Dict[str, Deque[float]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        import time

        message = record.getMessage()
        bucket = self._bucket.setdefault(message, deque())
        now = time.time()
        bucket.append(now)
        while bucket and now - bucket[0] > 1.0:
            bucket.popleft()
        return len(bucket) <= self.per_sec


class JsonWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", buffering=1)

    def write(self, record: logging.LogRecord) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass


class Watchdog(QObject):
    def __init__(self, idle_seconds: int, notify, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(max(5, idle_seconds) * 1000)
        self._timer.timeout.connect(self._on_timeout)
        self._idle_seconds = idle_seconds
        self._notify = notify
        self._last_emit = 0.0
        self._warned = False

    def set_idle_seconds(self, seconds: int) -> None:
        self._idle_seconds = max(5, seconds)
        self._timer.setInterval(self._idle_seconds * 1000)

    def note_activity(self) -> None:
        import time

        self._last_emit = time.time()
        self._warned = False

    def start(self) -> None:
        import time

        self._last_emit = time.time()
        self._warned = False
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _on_timeout(self) -> None:
        import time

        if not self._timer.isActive():
            return
        if self._last_emit == 0.0:
            self._last_emit = time.time()
            return
        if time.time() - self._last_emit >= self._idle_seconds and not self._warned:
            self._warned = True
            self._notify()


class Heartbeat(QObject):
    def __init__(self, interval_sec: int, emit, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._emit = emit
        self.set_interval(interval_sec)
        self._timer.timeout.connect(self._emit)

    def set_interval(self, seconds: int) -> None:
        self._timer.setInterval(max(10, seconds) * 1000)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()


class MonitorEngine:
    def __init__(self) -> None:
        self.logger = logging.getLogger(MONITOR_TAG)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        self._paths: Optional[LogPaths] = None
        self._json_writer: Optional[JsonWriter] = None
        self._watchdog: Optional[Watchdog] = None
        self._heartbeat: Optional[Heartbeat] = None
        self._iface = None

        self._breadcrumbs: Deque[str] = deque(maxlen=400)
        self._coalesce_cache: Dict[Tuple[int, str], Tuple[int, float]] = {}

        self._started = False
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, iface) -> None:
        if self._started:
            self.stop()
        self._iface = iface
        if get_setting("prune_on_start", bool):
            prune_logs_now()
        self._paths = self._prepare_paths()
        self._install_handlers()
        self._attach_hooks(iface)
        self._setup_watchdog()
        self._setup_heartbeat()
        self.logger.info("Monitor gestart; sessie %s", self.session_id)
        self._started = True
        QgsApplication.instance().setProperty("qgismonitor_started", True)

    def stop(self) -> None:
        if not self._started:
            return
        self._detach_handlers()
        self._teardown_watchdog()
        self._teardown_heartbeat()
        try:
            QgsApplication.messageLog().messageReceived.disconnect(self._forward_qgis_log)
        except Exception:
            pass
        self.logger.info("Monitor gestopt")
        QgsMessageLog.logMessage(f"{MONITOR_TAG} is gestopt.", MONITOR_TAG, Qgis.Info)
        QgsApplication.instance().setProperty("qgismonitor_started", False)
        self._started = False

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, object]:
        return {
            "started": self._started,
            "session_id": self.session_id,
            "log_dir": str(self._paths.directory) if self._paths else None,
            "log_file": str(self._paths.full) if self._paths else None,
            "error_log": str(self._paths.errors) if self._paths else None,
            "json_log": str(self._paths.jsonl) if self._paths and self._paths.jsonl else None,
            "breadcrumbs": len(self._breadcrumbs),
            "heartbeat_active": bool(self._heartbeat and self._heartbeat._timer.isActive()),
        }

    def breadcrumbs(self, limit: int = 20) -> List[str]:
        limit = max(1, int(limit))
        return list(self._breadcrumbs)[-limit:]

    # ------------------------------------------------------------------
    # Paths & handlers
    # ------------------------------------------------------------------

    def _prepare_paths(self) -> LogPaths:
        directory = Path(get_log_dir())
        directory.mkdir(parents=True, exist_ok=True)
        suffix = self._project_suffix()
        if get_setting("single_file_session", bool):
            cached = QgsApplication.instance().property("qgismonitor_paths")
            if isinstance(cached, dict):
                full = Path(cached.get("full", ""))
                errors = Path(cached.get("errors", ""))
                jsonl = Path(cached["jsonl"]) if cached.get("jsonl") else None
                if full.exists():
                    return LogPaths(directory, full, errors, jsonl)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        full = directory / f"qgis_full_{ts}{suffix}.log"
        errors = directory / f"qgis_errors_{ts}{suffix}.log"
        jsonl = directory / f"qgis_full_{ts}{suffix}.jsonl" if get_setting("json_parallel", bool) else None
        QgsApplication.instance().setProperty(
            "qgismonitor_paths",
            {"full": str(full), "errors": str(errors), "jsonl": str(jsonl) if jsonl else ""},
        )
        (directory / "latest.txt").write_text(
            f"FULL={full}\nERRORS={errors}\nJSON={jsonl or ''}\nSTARTED={datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        return LogPaths(directory, full, errors, jsonl)

    def _install_handlers(self) -> None:
        assert self._paths is not None
        self._detach_handlers()
        level_name = (get_setting("level", str) or "DEBUG").upper()
        level = getattr(logging, level_name, logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt=get_setting("date_format", str) or "%Y-%m-%d %H:%M:%S",
        )

        full_handler = RotatingFileHandler(
            self._paths.full,
            mode="a",
            encoding="utf-8",
            maxBytes=int(get_setting("max_log_mb", int)) * 1024 * 1024,
            backupCount=int(get_setting("rot_backups", int)),
        )
        full_handler.setLevel(level)
        full_handler.setFormatter(formatter)

        error_handler = RotatingFileHandler(
            self._paths.errors,
            mode="a",
            encoding="utf-8",
            maxBytes=int(get_setting("max_log_mb", int)) * 1024 * 1024,
            backupCount=int(get_setting("rot_backups", int)),
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)

        memory_handler = MemoryHandler(256, flushLevel=logging.INFO, target=full_handler)
        memory_handler.setLevel(level)
        memory_handler.setFormatter(formatter)

        qgis_handler = _QgisLogHandler()
        qgis_handler.setFormatter(formatter)

        for handler in (memory_handler, error_handler, qgis_handler):
            handler.addFilter(RateLimitFilter())
            handler.addFilter(self._coalesce_filter())
            handler.addFilter(_QtNoiseFilter())

        activity_filter = ActivityFilter(lambda: self._note_activity())
        self.logger.addFilter(activity_filter)

        self.logger.addHandler(memory_handler)
        self.logger.addHandler(error_handler)
        self.logger.addHandler(qgis_handler)

        if self._paths.jsonl:
            self._json_writer = JsonWriter(self._paths.jsonl)
            self.logger.addHandler(_JsonLogHandler(self._json_writer))
        else:
            self._json_writer = None

        self._note_activity()

        try:
            QgsApplication.messageLog().messageReceived.disconnect(self._forward_qgis_log)
        except Exception:
            pass
        try:
            QgsApplication.messageLog().messageReceived.connect(self._forward_qgis_log)
        except Exception:
            pass

    def _detach_handlers(self) -> None:
        for handler in list(self.logger.handlers):
            try:
                self.logger.removeHandler(handler)
                handler.flush()
                handler.close()
            except Exception:
                pass
        self.logger.filters.clear()
        if self._json_writer:
            self._json_writer.close()
            self._json_writer = None

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _attach_hooks(self, iface) -> None:
        if get_setting("hook_processing", bool):
            self._wrap_processing()
        if get_setting("hook_tasks", bool):
            self._attach_tasks()
        if get_setting("hook_canvas", bool):
            self._attach_canvas(iface)
        if get_setting("hook_project", bool):
            self._attach_project()

    # QGIS hook implementations ------------------------------------------------

    def _wrap_processing(self) -> None:
        try:
            import processing

            if getattr(processing, "_qgm_wrapped", False):
                return
            original_run = processing.run
            run_and_load = getattr(processing, "runAndLoadResults", None)
            depth = int(get_setting("debug_depth", int))

            def _safe(obj):
                try:
                    json.dumps(obj)
                    return obj
                except Exception:
                    if isinstance(obj, dict):
                        return {str(k): _safe(v) for k, v in obj.items()}
                    if isinstance(obj, (list, tuple)):
                        return [_safe(v) for v in obj]
                    return repr(obj)

            def _format_params(params):
                try:
                    return json.dumps(_safe(params), ensure_ascii=False, indent=2)
                except Exception:
                    return repr(params)

            def wrapped_run(algorithm, parameters=None, *args, **kwargs):
                corr = uuid.uuid4().hex[:12]
                start = datetime.now(timezone.utc)
                self.crumb(f"processing:start {algorithm} {corr}")
                self.logger.info("[Processing] START %s corr=%s", algorithm, corr)
                if depth > 0 and parameters is not None:
                    self.logger.debug("[Processing] params corr=%s\n%s", corr, _format_params(parameters))
                try:
                    result = original_run(algorithm, parameters, *args, **kwargs)
                    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                    keys = list(result.keys()) if hasattr(result, "keys") else "?"
                    self.logger.info("[Processing] DONE %s corr=%s in %.3fs | keys=%s", algorithm, corr, elapsed, keys)
                    return result
                except Exception:
                    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                    self.logger.error(
                        "[Processing] FAIL %s corr=%s na %.3fs\n%s",
                        algorithm,
                        corr,
                        elapsed,
                        traceback.format_exc(),
                    )
                    post_webhook(get_setting("webhook_url", str), {"event": "processing_fail", "algorithm": str(algorithm)})
                    raise

            def wrapped_run_load(algorithm, parameters=None, *args, **kwargs):
                if run_and_load is None:
                    return wrapped_run(algorithm, parameters, *args, **kwargs)
                corr = uuid.uuid4().hex[:12]
                self.crumb(f"processing:start(load) {algorithm} {corr}")
                try:
                    return run_and_load(algorithm, parameters, *args, **kwargs)
                finally:
                    self.logger.info("[Processing] runAndLoadResults corr=%s voltooid", corr)

            processing.run = wrapped_run
            if run_and_load is not None:
                processing.runAndLoadResults = wrapped_run_load
            processing._qgm_wrapped = True
            self.logger.debug("Processing hooks actief")
        except Exception as exc:
            self.logger.warning("Kon processing hooks niet installeren: %s", exc)

    def _attach_tasks(self) -> None:
        manager = QgsApplication.taskManager()

        def on_task_added(task):
            try:
                desc = task.description()
            except Exception:
                desc = "(onbekend)"
            self.crumb(f"task:add {desc}")
            self.logger.info("[Task] Toegevoegd: %s", desc)

        def on_all_finished():
            self.logger.info("[Task] Alle QGIS-taken gereed")

        try:
            manager.taskAdded.disconnect(on_task_added)
        except Exception:
            pass
        try:
            manager.allTasksFinished.disconnect(on_all_finished)
        except Exception:
            pass
        manager.taskAdded.connect(on_task_added)
        manager.allTasksFinished.connect(on_all_finished)
        self.logger.debug("Task hooks actief")

    def _attach_canvas(self, iface) -> None:
        canvas = getattr(iface, "mapCanvas", lambda: None)()
        if not canvas:
            return

        try:
            canvas.renderStarting.disconnect(canvas._qgm_render_start)  # type: ignore[attr-defined]
            canvas.renderComplete.disconnect(canvas._qgm_render_complete)  # type: ignore[attr-defined]
        except Exception:
            pass

        def render_start():
            canvas._qgm_render_t0 = datetime.now(timezone.utc)
            self.crumb("canvas:render-start")
            self.logger.debug("[Canvas] render start")

        def render_complete(*_):
            start = getattr(canvas, "_qgm_render_t0", datetime.now(timezone.utc))
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            self.crumb(f"canvas:render-done {elapsed:.3f}s")
            self.logger.info("[Canvas] render klaar in %.3fs", elapsed)

        canvas._qgm_render_start = render_start  # type: ignore[attr-defined]
        canvas._qgm_render_complete = render_complete  # type: ignore[attr-defined]
        canvas.renderStarting.connect(render_start)
        canvas.renderComplete.connect(render_complete)
        self.logger.debug("Canvas hooks actief")

    def _attach_project(self) -> None:
        project = QgsProject.instance()
        try:
            if project.customProperty('qgm_hooks_installed', False):
                return
            project.setCustomProperty('qgm_hooks_installed', True)
        except Exception:
            pass

        def on_opened(*_):
            path = project.fileName() or "(onbekend)"
            self.crumb(f"project:opened {path}")
            self.logger.info("[Project] Geopend: %s", path)

        def on_saved():
            path = project.fileName() or "(onbekend)"
            self.crumb(f"project:saved {path}")
            self.logger.info("[Project] Opgeslagen: %s", path)

        def on_cleared():
            self.crumb("project:cleared")
            self.logger.warning("[Project] Leeggemaakt")

        def on_layer_added(layer: QgsMapLayer):
            try:
                self.logger.info("[Layer+] %s | type=%s | id=%s", layer.name(), layer.type(), layer.id())
            except Exception:
                self.logger.info("[Layer+] onbekend")

        def on_layer_removed(layer_id: str):
            self.logger.info("[Layer-] id=%s", layer_id)

        for signal in ("readProject", "readProjectWithContext", "readProjectFinished"):
            getattr(project, signal).connect(on_opened)
        project.projectSaved.connect(on_saved)
        project.cleared.connect(on_cleared)
        project.layerWasAdded.connect(on_layer_added)
        project.layerRemoved.connect(on_layer_removed)
        try:
            from qgis.core import QgsVectorLayer

            def _selection_changed(layer_id, *_):
                layer = project.mapLayer(layer_id)
                if layer is None:
                    return
                try:
                    count = layer.selectedFeatureCount()
                except Exception:
                    count = '?'
                self.logger.info("[Select] laag=%s selectie=%s", layer.name(), count)

            def _connect_layer(layer: QgsVectorLayer) -> None:
                try:
                    handler = getattr(layer, '_qgm_selection_handler', None)
                    if handler:
                        try:
                            layer.selectionChanged.disconnect(handler)
                        except Exception:
                            pass
                    def handler(*_):
                        _selection_changed(layer.id())
                    layer._qgm_selection_handler = handler
                    layer.selectionChanged.connect(handler)
                except Exception:
                    pass

            for lyr in project.mapLayers().values():
                if isinstance(lyr, QgsVectorLayer):
                    _connect_layer(lyr)

            def on_layer_added_connect(layer):
                if isinstance(layer, QgsVectorLayer):
                    _connect_layer(layer)

            project.layerWasAdded.connect(on_layer_added_connect)
        except Exception as exc:
            self.logger.debug("Select hooks niet beschikbaar: %s", exc)
        self.logger.debug("Project hooks actief")

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _setup_watchdog(self) -> None:
        if not get_setting("watchdog_enabled", bool):
            self._watchdog = None
            return

        def notify():
            self.logger.warning("[Watchdog] Geen logactiviteit sinds %s seconden", get_setting("watchdog_idle_sec", int))
            post_webhook(
                get_setting("webhook_url", str),
                {"event": "watchdog_idle", "seconds": int(get_setting("watchdog_idle_sec", int))},
            )

        self._watchdog = Watchdog(int(get_setting("watchdog_idle_sec", int)), notify)
        self._watchdog.start()

    def _teardown_watchdog(self) -> None:
        if self._watchdog:
            self._watchdog.stop()
            self._watchdog = None

    def _setup_heartbeat(self) -> None:
        interval = int(get_setting("heartbeat_sec", int))
        if interval <= 0:
            self._heartbeat = None
            return

        def emit():
            self.logger.info("[Heartbeat] actief @ %s", datetime.now(timezone.utc).isoformat())

        self._heartbeat = Heartbeat(interval, emit)
        self._heartbeat.start()

    def _teardown_heartbeat(self) -> None:
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def crumb(self, message: str) -> None:
        entry = f"{datetime.now(timezone.utc).isoformat()} {message}"
        self._breadcrumbs.append(entry)

    def _note_activity(self) -> None:
        if self._watchdog:
            self._watchdog.note_activity()

    def _forward_qgis_log(self, message: str, tag: str, level: int) -> None:
        try:
            if tag and str(tag).lower().startswith("qgismonitorpro"):
                return
            mapped = {0: logging.INFO, 1: logging.WARNING, 2: logging.Critical, 3: logging.Critical}.get(level, logging.INFO)
            self.logger.log(mapped, f"[QGIS/{tag}] {message}")
        except Exception:
            pass

    def _project_suffix(self) -> str:
        try:
            name = QgsProject.instance().baseName() or "no_project"
            filtered = [ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name]
            return "_" + "".join(filtered)[:40]
        except Exception:
            return "_no_project"

    def _coalesce_filter(self) -> logging.Filter:
        engine = self

        class _Filter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                if not get_setting("coalesce_enabled", bool):
                    return True
                import time

                key = (record.levelno, record.getMessage().split("\n", 1)[0][:256])
                window = float(get_setting("coalesce_window_sec", float) or 3.0)
                now = time.time()
                count, last = engine._coalesce_cache.get(key, (0, 0.0))
                if now - last <= window:
                    engine._coalesce_cache[key] = (count + 1, last)
                    return False
                else:
                    if count:
                        engine.logger.info("[coalesce] melding %dx onderdrukt: %s", count, key[1])
                    engine._coalesce_cache[key] = (0, now)
                    return True

        return _Filter()


class _QtNoiseFilter(logging.Filter):
    NOISE = (
        "Could not resolve property: #Checkerboard",
        "Could not resolve property: #Cross",
        "libpng warning",
        "Cannot open file ':/images/themes/default",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not get_setting("mute_qt_noise", bool):
            return True
        if record.levelno < logging.WARNING:
            return True
        message = record.getMessage()
        return not any(snippet in message for snippet in self.NOISE)


class _QgisLogHandler(logging.Handler):
    MAP = {
        logging.ERROR: Qgis.Critical,
        logging.WARNING: Qgis.Warning,
        logging.INFO: Qgis.Info,
        logging.DEBUG: Qgis.Info,
    }

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - UI side effect
        try:
            QgsMessageLog.logMessage(record.getMessage(), MONITOR_TAG, self.MAP.get(record.levelno, Qgis.Info))
        except Exception:
            pass


class _JsonLogHandler(logging.Handler):
    def __init__(self, writer: JsonWriter) -> None:
        super().__init__(logging.DEBUG)
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._writer.write(record)
        except Exception:
            pass


ENGINE = MonitorEngine()
LOG_FILE = ERR_PATH = JSON_PATH = None


def qgismonitor_start(iface) -> None:
    ENGINE.start(iface)
    global LOG_FILE, ERR_PATH, JSON_PATH
    status = ENGINE.status()
    LOG_FILE = status.get("log_file")
    ERR_PATH = status.get("error_log")
    JSON_PATH = status.get("json_log")


def qgismonitor_stop() -> None:
    ENGINE.stop()


def monitor_status() -> Dict[str, object]:
    return ENGINE.status()


def get_recent_breadcrumbs(limit: int = 20) -> List[str]:
    return ENGINE.breadcrumbs(limit)


def force_flush() -> None:
    for handler in list(ENGINE.logger.handlers):
        try:
            handler.flush()
        except Exception:
            pass


def make_diagnostics_zip(out_path: str) -> bool:
    status = ENGINE.status()
    files = [status.get("log_file"), status.get("error_log"), status.get("json_log")]
    files = [str(Path(f)) for f in files if f]
    tail_lines = int(get_setting("tail_lines", int))
    extra = {
        "diagnostics.txt": system_summary(),
        "settings.json": json.dumps(load_all_settings(), ensure_ascii=False, indent=2),
    }
    if status.get("log_file") and tail_lines > 0:
        tail = _tail(Path(status["log_file"]), tail_lines)
        extra["tail.log"] = tail
    return bundle_logs_zip(out_path, files, extra)


def _tail(path: Path, lines: int) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:]
        return "\n".join(data)
    except Exception:
        return ""


__all__ = [
    "qgismonitor_start",
    "qgismonitor_stop",
    "monitor_status",
    "get_recent_breadcrumbs",
    "make_diagnostics_zip",
    "LOG_FILE",
    "ERR_PATH",
    "JSON_PATH",
]

