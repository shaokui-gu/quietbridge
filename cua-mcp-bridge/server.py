#!/usr/bin/env python3
"""MCP stdio bridge for the workbuddy-cua-native macOS helper.

WorkBuddy speaks MCP (JSON-RPC over stdio); the helper speaks a private
JSON-lines protocol over a stdin/stdout proxy. This process is the only new
piece: it translates one into the other, projects oversized AX payloads down
to a compact tree, and serialises requests because the helper allows a single
outstanding operation per connection.

Requires Python 3.9+ (system /usr/bin/python3). No third-party packages.
"""

import collections
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import re
import math
import uuid
import stat
import fcntl
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlsplit
import ipaddress
from browser_events import config_dir, make_tool as make_browser_events_tool

WAIT_DEADLINE = None

# 与扩展 host 共用相同的运行态解析规则，避免任务绑定落到开发树旧副本。
TASK_FILE = str(config_dir() / "task-windows.json")
TASK_BINDINGS = {}  # 仅当前桥接内存中的 appHandle 与连接令牌，不作为持久身份。
TASK_DISPATCH = None
# 任务运行期间的防睡眠保护：锁屏本身不会终止进程，空闲睡眠才会。
# 只阻止空闲系统睡眠，不参与解锁；锁屏时的 GUI 操作仍由原生侧拒绝。
POWER_GUARD_SECONDS = 3600
POWER_GUARD_RENEW_AFTER = 300
POWER_GUARD = {"active": False, "nextRenewAt": 0.0}
TOOL_LOCK = threading.RLock()

DEFAULT_HELPER = (
    "/Users/gushaokui/Documents/AI/computer-use/cua-helper-build/"
    "workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native"
)
HELPER_PATH = os.environ.get("CUA_HELPER_PATH", DEFAULT_HELPER)
SERVER_NAME = "workbuddy-cua"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"

SHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
READ_ONLY = os.environ.get("CUA_READ_ONLY", "").strip() in ("1", "true", "yes")
ALLOWLIST = [x.strip().lower() for x in os.environ.get("CUA_ALLOWLIST", "").split(",") if x.strip()]
MAX_TREE_CHARS = 24000

STATE = {
    "app": None,          # last appHandle
    "apps": {},           # appHandle -> {name, pid, bundleId}
    "window": {},         # appHandle -> last windowId
    "snapshot": None,     # last snapshotId
    "snapshot_app": None,
    "snapshot_window": None,
}


def log(message):
    sys.stderr.write("[cua-bridge] %s\n" % message)
    sys.stderr.flush()


OPERATION_METHODS = {"click", "hover", "scroll", "drag", "getState", "screenshot", "visualState", "listWindows", "preview"}
OPERATION_TOOLS = {"cua_click", "cua_hover", "cua_scroll", "cua_drag", "cua_state", "cua_screenshot", "cua_visual_state", "cua_wait_ready", "cua_windows", "cua_browser_events"}
OPERATION_TRACE_ID = None
OPERATION_LOG_LOCK = threading.Lock()
OPERATION_LOG_MAX_BYTES = 5 * 1024 * 1024


def operation_log_directory():
    if os.environ.get("CUA_DIAGNOSTICS_DIR"):
        return Path(os.environ["CUA_DIAGNOSTICS_DIR"])
    if os.environ.get("CUA_CONFIG_DIR"):
        return Path(os.environ["CUA_CONFIG_DIR"]) / "logs"
    return Path.home() / "Library/Logs/workbuddy-cua"


def diagnostic_fields(params):
    """Allowlist geometry/identifiers; never serialize raw tool parameters or UI contents."""
    if not isinstance(params, dict):
        return {}
    numbers = {"windowId", "elementIndex", "x", "y", "fromX", "fromY", "toX", "toY", "clickCount", "holdMs", "deltaLinesX", "deltaLinesY", "durationMs", "steps"}
    identifiers = {"appHandle", "snapshotId", "mouseButton", "action", "source", "taskId", "operation", "commandId", "documentId", "interactionMode"}
    numbers.update({"browserWindowId", "tabId", "navigationEpoch"})
    return {key: value[:256] if isinstance(value, str) else value for key, value in (params or {}).items()
            if (key in numbers and diagnostic_number(value)) or
               (key in identifiers and isinstance(value, str))}


def diagnostic_geometry(value, fields):
    return {key: value[key] for key in fields if isinstance(value, dict) and
            diagnostic_number(value.get(key))}


def diagnostic_number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def diagnostic_result(result):
    if not isinstance(result, dict):
        return {}
    fields = diagnostic_fields(result)
    for key in ("pid", "pixelWidth", "pixelHeight"):
        if diagnostic_number(result.get(key)):
            fields[key] = result[key]
    for key in ("dispatched", "verified"):
        if type(result.get(key)) is bool:
            fields[key] = result[key]
    for key in ("route", "axAction", "coordinateSpace"):
        if isinstance(result.get(key), str):
            fields[key] = result[key][:128]
    bounds = diagnostic_geometry(result.get("bounds"), ("x", "y", "width", "height"))
    if bounds:
        fields["bounds"] = bounds
        for axis, pixel, size in (("X", "pixelWidth", "width"), ("Y", "pixelHeight", "height")):
            if bounds.get(size, 0) > 0 and fields.get(pixel, 0) > 0:
                fields["pixelsPerPoint" + axis] = fields[pixel] / bounds[size]
    pointer = result.get("pointer")
    if isinstance(pointer, dict):
        safe = diagnostic_geometry(pointer, ("windowId", "targetPid"))
        safe["coordinateSpace"] = "window-points"
        for key, keys in (("windowBounds", ("x", "y", "width", "height")), ("windowPoint", ("x", "y")), ("screenPoint", ("x", "y"))):
            safe[key] = diagnostic_geometry(pointer.get(key), keys)
        fields["pointer"] = safe
    preview = result.get("preview")
    if isinstance(preview, dict):
        safe = diagnostic_geometry(preview, ("trackingWindowId", "hostWindowId", "hostPid", "displayId"))
        for key in ("panelSpaceVerified", "hostOnCurrentSpace", "panelOnCurrentSpace", "visible", "streamReady", "waitingForTargetDesktop"):
            if type(preview.get(key)) is bool:
                safe[key] = preview[key]
        for key in ("hostSpaceIds", "panelSpaceIds"):
            values = preview.get(key)
            if isinstance(values, list) and len(values) <= 64 and all(type(v) is int and v > 0 for v in values):
                safe[key] = values
        for key in ("mode", "captureMode"):
            if preview.get(key) in ("window", "application", "desktop"):
                safe[key] = preview[key]
        fields["preview"] = safe
    return fields


def operation_log(event, **fields):
    if os.environ.get("CUA_OPERATION_LOG") == "0":
        return
    try:
        record = dict(fields, event=event, timestamp=time.time(), pid=os.getpid(), toolTraceId=OPERATION_TRACE_ID)
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(data) > OPERATION_LOG_MAX_BYTES:
            return
        path = operation_log_directory() / "bridge-operations.jsonl"
        with OPERATION_LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lockfd = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(lockfd, "ab") as lease:
                info = os.fstat(lease.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    return
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "ab", buffering=0) as output:
                    info = os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                        return
                    if info.st_size + len(data) > OPERATION_LOG_MAX_BYTES:
                        os.replace(path, str(path) + ".1")
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "ab", buffering=0) as fresh:
                            fresh.write(data)
                    else:
                        output.write(data)
    except (OSError, ValueError, TypeError):
        pass  # Diagnostics must never change input delivery or its original error.


class HelperError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, "%s: %s" % (code, message))
        self.code = code
        self.message = message


class HelperClient(object):
    """One long-lived helper proxy process, one request at a time."""

    def __init__(self, path):
        self.path = path
        self.proc = None
        self.seq = 0
        self.connection_id = None
        self.lock = threading.Lock()
        self.cv = threading.Condition()
        self.responses = {}
        self.pending_browser_response = None
        self.reader = None
        self.errthread = None
        self.stderr_tail = collections.deque(maxlen=40)

    # -- process lifecycle -------------------------------------------------
    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        if self._alive():
            return
        if not os.path.exists(self.path):
            raise HelperError("HELPER_MISSING", "helper not found at %s" % self.path)
        self.proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.connection_id = str(uuid.uuid4())
        self.reader = threading.Thread(target=self._read_stdout, args=(self.proc,), daemon=True)
        self.reader.start()
        self.errthread = threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True)
        self.errthread.start()

    def _stop(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def _read_stdout(self, proc):
        stream = proc.stdout
        while True:
            line = stream.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            rid = msg.get("id")
            if rid is None:
                continue
            with self.cv:
                self.responses[rid] = msg
                self.cv.notify_all()

    def _read_stderr(self, proc):
        stream = proc.stderr
        while True:
            line = stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            # Screenshot stage traces are per-frame noise; keep only real failures.
            if text.startswith("[cua-screenshot]"):
                self.stderr_tail.append(text)
                continue
            self.stderr_tail.append(text)
            log(text)

    # -- request path ------------------------------------------------------
    def request(self, method, params=None, timeout=60.0, retry_safe=False):
        payload_params = params or {}
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._request_once(method, payload_params, timeout)
            except HelperError:
                raise
            except (BrokenPipeError, OSError, RuntimeError) as exc:
                # Only idempotent reads may be retried; input must never replay.
                if not retry_safe or attempt >= 2:
                    raise HelperError("HELPER_BROKEN", "%s (helper restarted=%s)" % (exc, attempt - 1))
                log("helper pipe failed (%s); restarting for idempotent %s" % (exc, method))
                with self.lock:
                    self._stop()
                time.sleep(0.15)

    def _request_once(self, method, params, timeout):
        deadline = time.monotonic() + timeout
        if WAIT_DEADLINE is not None:
            deadline = min(deadline, WAIT_DEADLINE)
        with self.lock:
            if self.pending_browser_response is not None:
                with self.cv:
                    pending = self.responses.pop(self.pending_browser_response, None)
                if pending is None:
                    raise HelperError("BROWSER_LAUNCH_UNCERTAIN", "此前 start 尚未收到回包，可能已启动；保留连接，不允许再次派发")
                self.pending_browser_response = None
            if TASK_DISPATCH is not None:
                assert_task_connection(TASK_DISPATCH)
            self._start()
            if TASK_DISPATCH is not None:
                assert_task_connection(TASK_DISPATCH)
            if method in INPUT_METHODS and INPUT_CONNECTION is not None and current_connection() != INPUT_CONNECTION:
                raise HelperError("REQUIRES_REVALIDATION", "输入发送前连接已改变，未派发")
            if time.monotonic() >= deadline:
                raise HelperError("HELPER_TIMEOUT", "请求预算已耗尽，未发送新请求")
            self.seq += 1
            rid = "mcp-%d-%d" % (os.getpid(), self.seq)
            started = time.perf_counter()
            if method in OPERATION_METHODS:
                operation_log("helper.request", nativeRequestId=rid, method=method, target=diagnostic_fields(params))
            frame = json.dumps({"id": rid, "method": method, "params": params}, ensure_ascii=False)
            try:
                self.proc.stdin.write((frame + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                if method in OPERATION_METHODS:
                    operation_log("helper.sent", nativeRequestId=rid, method=method)
            except (BrokenPipeError, OSError) as exc:
                if method in OPERATION_METHODS:
                    operation_log("helper.failed", nativeRequestId=rid, method=method, errorCode="HELPER_BROKEN")
                self._stop()
                raise RuntimeError("helper stdin closed: %s" % exc)

            with self.cv:
                while rid not in self.responses:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.cv.wait(min(remaining, 0.25))
                msg = self.responses.pop(rid, None)

            if msg is None:
                if method in OPERATION_METHODS:
                    operation_log("helper.failed", nativeRequestId=rid, method=method, errorCode="HELPER_TIMEOUT",
                                  elapsedMs=(time.perf_counter() - started) * 1000)
                if method == "browserSession" and params.get("action") == "start":
                    self.pending_browser_response = rid
                    raise HelperError("BROWSER_LAUNCH_UNCERTAIN", "start 回包超时；可能已经启动，保留连接接收迟到回包，禁止重试 start")
                self._stop()
                tail = " | ".join(list(self.stderr_tail)[-3:])
                raise HelperError(
                    "HELPER_TIMEOUT",
                    "%s did not answer within %.0fs%s" % (method, timeout, (" [" + tail + "]") if tail else ""),
                )

            if "error" in msg and msg["error"]:
                err = msg["error"]
                if isinstance(err, dict):
                    if method in OPERATION_METHODS:
                        operation_log("helper.failed", nativeRequestId=rid, method=method, errorCode=err.get("code") or "NATIVE_ERROR",
                                      elapsedMs=(time.perf_counter() - started) * 1000)
                    raise HelperError(err.get("code") or "NATIVE_ERROR", err.get("message") or "native error")
                raise HelperError("NATIVE_ERROR", str(err))
            if method in OPERATION_METHODS:
                operation_log("helper.completed", nativeRequestId=rid, method=method,
                              elapsedMs=(time.perf_counter() - started) * 1000, result=diagnostic_result(msg.get("result")))
            return msg.get("result")


HELPER = HelperClient(HELPER_PATH)
TIMEOUTS = {
    "getCapabilities": 15,
    "permissionStatus": 15,
    "listApps": 20,
    "findApps": 30,
    "getApp": 30,
    "listWindows": 30,
    "browserSession": 15,
    "getState": 60,
    "screenshot": 60,
    "visualState": 8,
    "paste": 60,
}
READ_ONLY_METHODS = {
    "getCapabilities",
    "permissionStatus",
    "openPermissionSettings",
    "requestPermission",
    "listApps",
    "findApps",
    "getApp",
    "listWindows",
    "getState",
    "screenshot",
    "visualState",
}


INPUT_METHODS = {"click", "hover", "scroll", "drag", "setValue", "typeText", "paste", "pressKey", "activate", "show"}
CALL_RECORDS = None
INPUT_CONNECTION = None
SHARE_RECORD = None
SHARE_SELECTION = None  # 显式范围仅属于同一连接和目标，不影响其他应用或任务。


def input_window(app, window=None, snapshot=None, allow_unique=False):
    if window is not None:
        if type(window) is not int or not 1 <= window <= 4294967295:
            raise HelperError("TARGET_REQUIRED", "输入需要有效 windowId")
        return window
    if (STATE.get("snapshot") and STATE.get("snapshot_app") == app and
            (STATE.get("snapshot_connection") == current_connection() or TASK_DISPATCH is not None) and
            (snapshot is None or snapshot == STATE["snapshot"]) and STATE.get("snapshot_window")):
        return STATE["snapshot_window"]
    if allow_unique:
        windows = list_windows(app)
        ids = {w.get("windowId") for w in windows if type(w.get("windowId")) is int}
        if len(ids) == 1:
            return ids.pop()
    raise HelperError("TARGET_REQUIRED", "目标不明确：显式指定 windowId 或先观察确切窗口；不会选择第一窗口")


def request_share(app, window, source, duration=600, require_frame=True):
    params = {"action": "hold", "appHandle": app, "windowId": window,
              "source": source, "durationSeconds": duration}
    if not require_frame:
        params["waitForFrame"] = False
    if TASK_DISPATCH is not None:
        assert_task_connection(TASK_DISPATCH)
        validate_task_target("preview", params, TASK_DISPATCH)
    HELPER._start()
    connection = current_connection()
    timeout = 45 if WAIT_DEADLINE is None else WAIT_DEADLINE - time.monotonic()
    if timeout <= 0:
        raise HelperError("HELPER_TIMEOUT", "共享等待总预算已耗尽，未派发")
    result = HELPER.request("preview", params, timeout=timeout, retry_safe=False) or {}
    if WAIT_DEADLINE is not None and time.monotonic() >= WAIT_DEADLINE:
        raise HelperError("HELPER_TIMEOUT", "共享等待已耗尽总预算，未派发")
    preview = result.get("preview") or {}
    if CALL_RECORDS is not None:
        CALL_RECORDS.append({"method": "preview", "target": {"appHandle": app, "windowId": window},
                             "preview": {k: preview[k] for k in ("streamReady", "mode", "trackingWindowId", "scope",
                                          "targetOnCurrentSpace", "displayId", "frameAgeMs", "streamError") if k in preview},
                             "scopeWarning": ("desktop 包含目标显示器当前 Space 的其他应用。" if source == "desktop"
                                              else "仅共享目标应用。" if source == "application" else "仅共享目标窗口。")})
    if current_connection() != connection or not HELPER._alive():
        raise HelperError("REQUIRES_REVALIDATION", "共享期间连接改变，输入未派发")
    if ((require_frame and preview.get("streamReady") is not True) or preview.get("mode") != source or
            preview.get("trackingWindowId") != window):
        raise HelperError("PREVIEW_NOT_READY", "目标%s尚无已确认视频首帧，输入未派发" %
                          {"desktop": "桌面", "application": "应用", "window": "窗口"}[source])
    if TASK_DISPATCH is not None:
        assert_task_connection(TASK_DISPATCH)
        validate_task_target("preview", params, TASK_DISPATCH)
    return connection, preview


def native_preview_matches(task_id):
    """A browser tab lease alone does not prove a WindowServer capture target."""
    from browser_events import status
    try:
        state = status(task_id, task_file=TASK_FILE)
    except (ValueError, OSError):
        return False
    return state.get('mappingVerified') is True and state.get('crossCheck', {}).get('consistent') is True


def ensure_background_preview(app, window):
    """Keep the task window preview leased; CDP never waits for a video frame."""
    connection, preview = request_share(app, window, "window", require_frame=False)
    if preview.get('streamReady') is not True:
        operation_log("browser.preview_waiting", windowId=window, reason="frame_pending")
    return connection


def ensure_share(app, window, source=None, duration=600):
    """应用内默认只共享该应用；显式桌面范围在同一目标的后续动作中保留。"""
    global SHARE_RECORD, SHARE_SELECTION
    if source is not None:
        SHARE_SELECTION = None
    if source is None:
        selection = SHARE_SELECTION or {}
        same_target = (selection.get("appHandle") == app and selection.get("windowId") == window
                       and selection.get("connection") == current_connection() and HELPER._alive())
        source = selection["source"] if same_target else "application"
    try:
        connection, preview = request_share(app, window, source, duration)
    except HelperError:
        SHARE_SELECTION = {"appHandle": app, "windowId": window, "connection": current_connection(), "source": source}
        raise
    if source == "desktop" and preview.get("targetOnCurrentSpace") is not True:
        try:
            call_helper("preview", {"action": "release"})
        finally:
            # 失败只关闭视频流，保留任务要求的桌面范围；重试也不能悄悄收窄。
            SHARE_SELECTION = {"appHandle": app, "windowId": window, "connection": connection, "source": source}
        raise HelperError("PREVIEW_NOT_READY", "无法确认目标所在桌面正在共享；请先手动切换到目标桌面后重试")
    record = {"source": source, "scope": preview.get("scope") or
              {"desktop": "display_current_space", "application": "application_current_space", "window": "window"}[source],
              "targetOnCurrentSpace": preview.get("targetOnCurrentSpace")}
    if source == "application" and preview.get("targetOnCurrentSpace") is False:
        connection, preview = request_share(app, window, "window", duration)
        record = {"source": "window", "scope": preview.get("scope") or "window",
                  "requestedSource": "application",
                  "targetOnCurrentSpace": False,
                  "reason": "target_on_background_space: 应用流不含后台 Space，改为仅共享目标窗口"}
    if CALL_RECORDS is not None:
        CALL_RECORDS.append({"method": "share", "target": {"appHandle": app, "windowId": window},
                             "share": record})
    SHARE_RECORD = record
    SHARE_SELECTION = {"appHandle": app, "windowId": window, "connection": connection, "source": source}
    return connection


def call_helper(method, params=None, **kwargs):
    global INPUT_CONNECTION, SHARE_SELECTION
    timeout = kwargs.pop("timeout", None) or TIMEOUTS.get(method, 45)
    if TASK_DISPATCH is not None:
        assert_task_connection(TASK_DISPATCH)
        validate_task_target(method, params or {}, TASK_DISPATCH)
    if WAIT_DEADLINE is not None:
        remaining = WAIT_DEADLINE - time.monotonic()
        if remaining <= 0:
            raise HelperError("HELPER_TIMEOUT", "等待总预算已耗尽，未发送新请求")
        timeout = min(timeout, remaining)
    params = params or {}
    if method == "preview" and params.get("action") == "release":
        SHARE_SELECTION = None
    target = {k: params[k] for k in ("appHandle", "windowId", "snapshotId", "elementIndex") if k in params}
    if method in INPUT_METHODS:
        window = input_window(params.get("appHandle"), params.get("windowId"), params.get("snapshotId"),
                              allow_unique=method in ("typeText", "paste", "pressKey", "activate", "show"))
        connection = ensure_share(params.get("appHandle"), window)
        target["windowId"] = window
        if current_connection() != connection or not HELPER._alive():
            raise HelperError("REQUIRES_REVALIDATION", "输入前连接改变，未派发")
        if TASK_DISPATCH is not None:
            assert_task_connection(TASK_DISPATCH)
            validate_task_target(method, params, TASK_DISPATCH)
    record = {"method": method, "target": target, "dispatched": "unknown", "verified": "unknown"}
    if CALL_RECORDS is not None:
        CALL_RECORDS.append(record)
    if method in INPUT_METHODS:
        INPUT_CONNECTION = connection
    try:
        result = HELPER.request(method, params, timeout=timeout,
                                retry_safe=WAIT_DEADLINE is None and TASK_DISPATCH is None and method in READ_ONLY_METHODS)
    finally:
        INPUT_CONNECTION = None
    if isinstance(result, dict):
        for key in ("dispatched", "verified", "route"):
            if key in result:
                record[key] = result[key]
        pointer = diagnostic_result(result).get("pointer")
        if pointer is not None:
            record["pointer"] = pointer
        foreground = result.get("foreground")
        if isinstance(foreground, dict):
            record["foreground"] = {k: foreground[k] for k in ("frontPid", "focusedWindowId") if k in foreground}
            for phase in ("before", "after"):
                if isinstance(foreground.get(phase), dict):
                    record["foreground"][phase] = {k: foreground[phase][k] for k in ("frontPid", "focusedWindowId") if k in foreground[phase]}
    if TASK_DISPATCH is not None:
        assert_task_connection(TASK_DISPATCH)
        validate_task_result(method, result, TASK_DISPATCH)
    if method in ("getState", "visualState") and isinstance(result, dict):
        STATE["snapshot_connection"] = current_connection()
    return result


@contextmanager
def task_store():
    # 逐级使用目录描述符，避免检查后符号链接替换；锁文件也拒绝链接。
    parent, name = os.path.split(TASK_FILE)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    lock = None
    try:
        for part in os.path.abspath(parent).split(os.sep)[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        lock = os.open(name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise HelperError("UNSAFE_PATH", "任务锁不是普通文件")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            data = {"version": 1, "activeTaskId": None, "tasks": {}}
        else:
            with os.fdopen(source, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise HelperError("UNSAFE_PATH", "任务记录不是普通文件")
                data = json.load(stream)
            if data.get("version") != 1 or not isinstance(data.get("tasks"), dict):
                raise HelperError("TASK_STORE_INVALID", "任务记录格式不支持")
        original = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != original:
            temporary = ".task-windows-%s.tmp" % uuid.uuid4()
            try:
                target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
                with os.fdopen(target, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise HelperError("UNSAFE_PATH", "拒绝替换符号链接任务记录")
                except FileNotFoundError:
                    pass
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise HelperError("TASK_STORE_ERROR", "任务记录无法安全读写：%s" % exc)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def current_connection():
    return (id(HELPER), HELPER.connection_id)


def assert_task_connection(binding):
    if current_connection() != binding["connection"] or not HELPER._alive():
        raise HelperError("REQUIRES_REVALIDATION", "requires_revalidation：连接已改变，需显式 bind + confirm=true；不会按 PID 自动重绑")


def native_task_check(record, binding):
    assert_task_connection(binding)
    listing = call_helper("browserSession", {"action": "list"}) or {}
    assert_task_connection(binding)
    session = next((s for s in listing.get("sessions", [])
                    if s.get("sessionId") == record["sessionId"]), None)
    if session is None:
        return "requires_revalidation"
    if (session.get("state") != "attached" or session.get("running") is not True or
            session.get("processIdentity") != record["processIdentity"] or
            session.get("pid") != record["pid"] or session.get("windowId") != record["windowId"] or
            not any(w.get("windowId") == record["windowId"] for w in session.get("windows", []))):
        return "invalidated"
    remaining = TIMEOUTS["listWindows"] if WAIT_DEADLINE is None else WAIT_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise HelperError("HELPER_TIMEOUT", "任务核验已耗尽等待预算")
    windows = HELPER.request("listWindows", {"appHandle": binding["appHandle"]},
                             timeout=min(TIMEOUTS["listWindows"], remaining), retry_safe=False) or []
    assert_task_connection(binding)
    return "bound" if any(w.get("windowId") == record["windowId"] and
                          w.get("pid", record["pid"]) == record["pid"] for w in windows) else "invalidated"


def task_status(record):
    previous_state = record["state"]
    checked_native_binding = False
    if record["state"] in ("awaiting_window", "invalidated", "ended"):
        return dict(record)
    binding = TASK_BINDINGS.get(record["taskId"])
    if binding is None or binding["connection"] != current_connection() or not HELPER._alive():
        record["state"] = "requires_revalidation"
    else:
        try:
            record["state"] = native_task_check(record, binding)
            checked_native_binding = True
        except HelperError as exc:
            if exc.code in ("STALE_APP", "WINDOW_UNAVAILABLE", "BROWSER_PROCESS_MISMATCH"):
                record["state"] = "invalidated"
            elif exc.code == "REQUIRES_REVALIDATION":
                record["state"] = "requires_revalidation"
            else:
                raise
    if checked_native_binding and record["state"] == "invalidated" and previous_state != "invalidated":
        # A task that can no longer be verified is no longer executing. Never leave
        # the privacy shield covering the user's desktop after its target vanished.
        release_power_guard()
    return dict(record)


def hold_power_guard():
    """任务租约按住防睡眠断言，并监视首次系统锁屏。

    尽力而为：断言不可用时不给任务本身添麻烦，返回值仅用于回报状态。
    首次锁屏自动解锁一次后显示浮层；用户触碰后交回 macOS 自身锁屏。
    """
    now = time.monotonic()
    # 有界等待期间不续租：既不吃等待预算，也不给轮询额外加一次往返。
    if WAIT_DEADLINE is not None:
        return {}
    if POWER_GUARD["active"] and now < POWER_GUARD["nextRenewAt"]:
        return {}
    try:
        payload = call_helper("powerGuard", {"action": "hold", "durationSeconds": POWER_GUARD_SECONDS},
                              timeout=5) or {}
    except HelperError:
        return {}
    guard = payload.get("guard") or {}
    privacy = payload.get("privacy") or {}
    if guard.get("holding") is True:
        POWER_GUARD["active"] = True
        POWER_GUARD["nextRenewAt"] = now + POWER_GUARD_RENEW_AFTER
    if guard or privacy:
        status = dict(guard)
        if isinstance(privacy, dict):
            status["privacy"] = privacy
        return status
    return {}


def release_power_guard():
    # 即使本进程没记住持有状态也照样请求释放：桥接重启过时不能让断言留到 TTL。
    POWER_GUARD["active"] = False
    POWER_GUARD["nextRenewAt"] = 0.0
    try:
        payload = call_helper("powerGuard", {"action": "release"}, timeout=5) or {}
    except HelperError:
        return {}
    guard = payload.get("guard") or {}
    privacy = payload.get("privacy") or {}
    if isinstance(guard, dict) and isinstance(privacy, dict):
        guard = dict(guard, privacy=privacy)
    return guard


def tool_task(args):
    global SHARE_SELECTION
    if not isinstance(args, dict) or set(args) - {"action", "taskId", "appHandle", "windowId", "confirm", "interactionMode"}:
        raise HelperError("INVALID_ARGUMENT", "仅接受 action、taskId、appHandle、windowId、confirm、interactionMode")
    action = args.get("action")
    if action not in ("begin", "bind", "status", "end"):
        raise HelperError("INVALID_ARGUMENT", "action 必须为 begin、bind、status、end")
    if action == "begin" and (set(args) - {"action", "interactionMode"} or
            args.get("interactionMode", "background_browser") not in ("background_browser", "desktop")):
        raise HelperError("INVALID_ARGUMENT", "begin 的 interactionMode 仅允许 background_browser（默认）或 desktop")
    if action != "begin" and "interactionMode" in args:
        raise HelperError("INVALID_ARGUMENT", "任务执行模式不能中途降级；只能在 begin 时指定")
    if action == "status" and set(args) - {"action", "taskId"}:
        raise HelperError("INVALID_ARGUMENT", "status 仅接受 action 和 taskId")
    if action == "end" and (set(args) - {"action", "taskId", "confirm"} or args.get("confirm") is not True):
        raise HelperError("INVALID_ARGUMENT", "end 需要 taskId（省略时结束当前任务）与 confirm=true")
    if "taskId" in args and (not isinstance(args["taskId"], str) or not args["taskId"]):
        raise HelperError("INVALID_ARGUMENT", "taskId 必须是非空字符串")
    with task_store() as data:
        if action == "begin":
            SHARE_SELECTION = None
            previous = data["tasks"].get(data.get("activeTaskId"))
            if isinstance(previous, dict) and previous.get("state") == "bound":
                try:
                    call_helper("preview", {"action": "release"})
                except HelperError:
                    pass
                release_power_guard()
            task_id = str(uuid.uuid4())
            record = {"taskId": task_id, "state": "awaiting_window", "mode": None,
                      "newWindowCreated": False,
                      "interactionMode": args.get("interactionMode", "background_browser")}
            data["tasks"][task_id] = record
            data["activeTaskId"] = task_id
            return json.dumps(record, ensure_ascii=False)
        task_id = args.get("taskId", data["activeTaskId"])
        record = data["tasks"].get(task_id)
        if record is None:
            raise HelperError("UNKNOWN_TASK", "任务不存在；先 begin")
        if action == "status":
            return json.dumps(task_status(record), ensure_ascii=False)
        if action == "end":
            # 显式结束是唯一回到「无任务」原生模式的入口：只收尾记录，
            # 不清空历史，也不把已绑定目标转交其它窗口。
            TASK_BINDINGS.pop(task_id, None)
            if data.get("activeTaskId") == task_id:
                data["activeTaskId"] = None
            record["state"] = "ended"
            # 共享面板跟随界面操作任务：任务结束就释放租约，不让浮层继续留在屏幕上。
            try:
                call_helper("preview", {"action": "release"})
            except HelperError:
                pass
            # 防睡眠断言同样随任务结束释放，不让机器被一个已结束的任务按住。
            return json.dumps(dict(record, powerGuard=release_power_guard()), ensure_ascii=False)
        if args.get("confirm") is not True or type(args.get("windowId")) is not int or not 1 <= args["windowId"] <= 4294967295 or not args.get("appHandle"):
            raise HelperError("INVALID_ARGUMENT", "bind 需要显式 appHandle、有效 windowId、confirm=true")
        if READ_ONLY:
            raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，bind 已禁用")
        app = resolve_app(args["appHandle"])
        pid = STATE["apps"][app].get("pid")
        if type(pid) is not int or not 1 <= pid <= 2147483647:
            raise HelperError("INVALID_ARGUMENT", "应用必须是已有运行进程")
        if record.get("windowId") is not None and (record["windowId"] != args["windowId"] or record["pid"] != pid):
            raise HelperError("TARGET_MISMATCH", "已记录任务不能改绑其他 PID 或窗口；新任务使用 begin")
        if record["state"] == "invalidated":
            raise HelperError("TASK_INVALIDATED", "任务绑定已失效，不能复用窗口 ID")
        if ALLOWLIST and not any(a in "com.google.chrome google chrome" for a in ALLOWLIST):
            raise HelperError("APP_NOT_ALLOWED", "CUA_ALLOWLIST 未包含 Google Chrome")
        native = call_helper("browserSession", {"action": "attach", "pid": pid,
                             "windowId": args["windowId"], "confirm": True}) or {}
        identity = native.get("processIdentity")
        if (native.get("state") != "attached" or native.get("running") is not True or
                native.get("pid") != pid or native.get("windowId") != args["windowId"] or
                not isinstance(identity, dict) or identity.get("pid") != pid or
                not all(isinstance(identity.get(k), str) and identity[k].isdigit()
                        for k in ("startedSeconds", "startedMicroseconds")) or
                not isinstance(native.get("sessionId"), str) or not native["sessionId"] or
                not any(w.get("windowId") == args["windowId"] for w in native.get("windows", []))):
            raise HelperError("ATTACH_UNVERIFIED", "原生 attach 未返回完整窗口和进程代际身份；任务未绑定")
        if record.get("processIdentity") is not None and record["processIdentity"] != identity:
            record["state"] = "invalidated"
            return json.dumps(record, ensure_ascii=False)
        record.update(state="bound", mode="attached_existing", pid=pid, processIdentity=identity,
                      windowId=args["windowId"], sessionId=native["sessionId"], newWindowCreated=False)
        TASK_BINDINGS[task_id] = {"appHandle": app, "connection": current_connection()}
        data["activeTaskId"] = task_id
        STATE.update(app=app, snapshot=None, snapshot_app=None, snapshot_window=None)
        # 绑定成功即进入任务运行期：按住防睡眠断言，锁屏后任务仍能继续。
        return json.dumps(dict(record, limitation="仅绑定已有窗口，共享 Chrome 数据；未实现新建专用窗口或数据隔离",
                               powerGuard=hold_power_guard() if record.get("interactionMode") == "desktop" else {"active": False}), ensure_ascii=False)


TASK_TARGET_TOOLS = {"cua_situation", "cua_state", "cua_visual_state", "cua_screenshot", "cua_wait_ready",
                     "cua_click", "cua_hover", "cua_scroll", "cua_drag", "cua_type_text",
                     "cua_paste", "cua_key", "cua_set_value"}


def task_guard(name, args):
    if name == "cua_task":
        return args, None
    # 无任务且记录文件不存在时不创建锁文件，保留旧行为。
    if not os.path.lexists(TASK_FILE):
        if TASK_BINDINGS:
            raise HelperError("TASK_STORE_ERROR", "已绑定任务的持久记录丢失，拒绝回退无任务模式")
        return args, None
    with task_store() as data:
        record = data["tasks"].get(data["activeTaskId"])
        if record is None:
            return args, None
        # 等待绑定时允许只读枚举窗口：否则「先建窗再绑定」会被守卫锁死。
        discovery = (name == "cua_windows" and args.get("action", "list") == "list" and
                     record.get("state") == "awaiting_window")
        protected = name in TASK_TARGET_TOOLS or (name == "cua_windows" and not discovery)
        if name == "cua_browser_session" and args.get("action") != "list":
            raise HelperError("TASK_SCOPE_REQUIRED", "任务期间仅允许通过 cua_task bind 登记已有窗口，不允许启动或另行绑定")
        if name == "cua_apps" and args.get("action") == "launch":
            raise HelperError("TASK_SCOPE_REQUIRED", "任务期间不允许启动应用")
        if record.get("interactionMode", "background_browser") == "background_browser":
            native_input = name in {"cua_click", "cua_hover", "cua_scroll", "cua_drag", "cua_type_text",
                                    "cua_paste", "cua_key", "cua_set_value"}
            native_window_write = name == "cua_windows" and args.get("action", "list") not in ("list", "share", "unshare")
            if native_input or native_window_write:
                operation_log("browser.foreground_fallback_blocked", taskId=record["taskId"], tool=name)
                raise HelperError("BACKGROUND_ONLY", "后台浏览器任务仅允许固定标签页扩展输入；通道不可用时等待恢复，不回落 AX、桌面键鼠或激活窗口")
            if name == "cua_windows" and args.get("action") == "share" and not native_preview_matches(record['taskId']):
                raise HelperError("BROWSER_MAPPING_REQUIRED", "先通过扩展 assign、verify_mapping 核验浏览器与系统窗口的对应关系；核验前可用扩展截图，不展示未确认的窗口")
        if not protected:
            return args, None
        binding = TASK_BINDINGS.get(record["taskId"])
        if record.get("windowId") is not None and "windowId" in args and (type(args["windowId"]) is not int or args["windowId"] != record["windowId"]):
            raise HelperError("TARGET_MISMATCH", "显式窗口与任务绑定目标不同")
        if binding and args.get("appHandle", binding["appHandle"]) != binding["appHandle"]:
            raise HelperError("TARGET_MISMATCH", "显式应用与任务绑定目标不同")
        status = task_status(record)
    if status["state"] != "bound":
        raise HelperError("REQUIRES_REVALIDATION" if status["state"] == "requires_revalidation" else "TASK_NOT_BOUND",
                          "%s：任务没有有效目标；不回退焦点或最新窗口" % status["state"])
    # 每个受保护动作顺带续租（节流），长任务不会被租约上限打断。
    if record.get("interactionMode") == "desktop":
        hold_power_guard()
    context = dict(binding, windowId=record["windowId"])
    if name in ("cua_type_text", "cua_paste", "cua_key") or (
            name == "cua_windows" and args.get("action", "list") not in ("list", "share", "unshare")):
        raise HelperError("UNSAFE_WINDOW_DELIVERY", "原生接口仅确认应用，无法安全投递到绑定窗口，已拒绝")
    args = dict(args, appHandle=binding["appHandle"])
    if name in TASK_TARGET_TOOLS:
        element = args.get("elementIndex") is not None
        if "snapshotId" in args or element:
            snapshot = args.get("snapshotId") or STATE["snapshot"]
            if (not snapshot or snapshot != STATE["snapshot"] or
                    STATE["snapshot_app"] != binding["appHandle"] or STATE["snapshot_window"] != record["windowId"]):
                raise HelperError("TARGET_MISMATCH", "snapshot 无法通过 STATE snapshot_window 证明属于绑定窗口")
            args["snapshotId"] = snapshot
        if not element and "windowId" not in args:
            raise HelperError("TARGET_REQUIRED", "任务操作必须显式提供 windowId；元素操作必须使用已核验快照")
        if name == "cua_state" and args.get("includeMenus"):
            raise HelperError("TARGET_MISMATCH", "任务观察不包含应用级共享菜单")
    return args, context


def validate_task_target(method, params, binding):
    if params.get("appHandle", binding["appHandle"]) != binding["appHandle"]:
        raise HelperError("TARGET_MISMATCH", "原生调用应用不匹配")
    if method in ("typeText", "paste", "pressKey", "activate", "launchApp"):
        raise HelperError("UNSAFE_WINDOW_DELIVERY", "原生接口没有窗口级确认")
    if method == "preview" and params.get("action") == "hold":
        if params.get("windowId") != binding["windowId"]:
            raise HelperError("TARGET_MISMATCH", "共享窗口与任务绑定目标不同")
    if method in ("getState", "visualState", "screenshot", "click", "hover", "scroll", "drag", "setValue"):
        if "windowId" in params and params["windowId"] != binding["windowId"]:
            raise HelperError("TARGET_MISMATCH", "原生调用窗口不匹配")
        if "snapshotId" in params and (params["snapshotId"] != STATE["snapshot"] or
                STATE["snapshot_app"] != binding["appHandle"] or STATE["snapshot_window"] != binding["windowId"]):
            raise HelperError("TARGET_MISMATCH", "原生调用快照归属不匹配")
        if "windowId" not in params and "snapshotId" not in params:
            raise HelperError("TARGET_REQUIRED", "原生调用缺少确切目标")


def validate_task_result(method, result, binding):
    if method in ("getState", "visualState", "screenshot") and (
            not isinstance(result, dict) or result.get("windowId") != binding["windowId"] or
            result.get("appHandle", binding["appHandle"]) != binding["appHandle"]):
        STATE.update(snapshot=None, snapshot_app=None, snapshot_window=None)
        raise HelperError("TARGET_MISMATCH", "原生观察返回不同目标，未交付结果或记录快照")


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def fmt_app_ident(entry):
    pid = entry.get("pid")
    bits = [entry.get("name") or "?"]
    if entry.get("bundleId"):
        bits.append("(%s)" % entry["bundleId"])
    bits.append("pid=%s" % (pid if pid is not None else "-"))
    return " ".join(bits)


def fmt_windows(windows):
    if not windows:
        return "（无窗口）"
    lines = []
    for w in windows:
        b = w.get("bounds") or {}
        size = "%sx%s" % (int(b.get("width", 0)), int(b.get("height", 0)))
        pos = "@%s,%s" % (int(b.get("x", 0)), int(b.get("y", 0)))
        lines.append(
            "- windowId=%s \"%s\" %s %s onScreen=%s currentSpace=%s"
            % (
                w.get("windowId"),
                (w.get("title") or "")[:60],
                size,
                pos,
                "yes" if w.get("isOnScreen") else "no",
                "yes" if w.get("onCurrentSpace") is True else ("no" if w.get("onCurrentSpace") is False else "?"),
            )
        )
    return "\n".join(lines)


def resolve_app(app_handle):
    handle = app_handle or STATE["app"]
    if not handle:
        raise HelperError(
            "NO_APP",
            "还没有选择应用：先调用 cua_apps(action=\"get\", selector=\"应用名或 bundleId\")",
        )
    if handle not in STATE["apps"]:
        raise HelperError("UNKNOWN_APP_HANDLE", "appHandle 不在本会话缓存中（可能 helper 重启过）：重新调用 cua_apps 获取")
    return handle


def list_windows(app_handle):
    return call_helper("listWindows", {"appHandle": app_handle}) or []


def resolve_window(app_handle, window_id=None, require=True):
    """Validate a window id against a fresh list; cached ids go stale when a window closes."""
    if window_id:
        return int(window_id)
    windows = list_windows(app_handle)
    ids = [w.get("windowId") for w in windows]
    cached = STATE["window"].get(app_handle)
    if cached is not None and cached in ids:
        return cached
    if cached is not None:
        operation_log("target.lost", appHandle=app_handle, windowId=cached)
        raise HelperError("TARGET_LOST", "原目标窗口已不可用；请显式选择窗口，不按前台或窗口顺序换目标")
    valid_ids = {value for value in ids if type(value) is int}
    if len(valid_ids) == 1:
        wid = valid_ids.pop()
        STATE["window"][app_handle] = wid
        operation_log("target.selected", appHandle=app_handle, windowId=wid, reason="only_window")
        return wid
    if len(valid_ids) > 1:
        raise HelperError("TARGET_REQUIRED", "应用有多个窗口；请显式指定 windowId，不会选择第一个或当前聚焦窗口")
    if require:
        raise HelperError(
            "WINDOW_UNAVAILABLE",
            "该应用当前没有可观察窗口：核对运行状态和窗口列表；不要自动启动或激活",
        )
    return None


def remember_window(app_handle, result):
    if isinstance(result, dict) and result.get("windowId"):
        previous = STATE["window"].get(app_handle)
        STATE["window"][app_handle] = result["windowId"]
        if previous != result["windowId"]:
            operation_log("target.selected", appHandle=app_handle, previousWindowId=previous,
                          windowId=result["windowId"], reason="explicit_observation_result")


def observe_window(app_handle, window_id):
    """Coordinate input requires a prior observation of that exact window."""
    call_helper(
        "getState",
        {"appHandle": app_handle, "windowId": int(window_id), "maxElements": 1, "maxDepth": 1},
    )


def window_note(app_handle, window_id):
    """Warn when the target lives on another Space: the agent pointer is drawn there, not here."""
    try:
        for w in list_windows(app_handle):
            if w.get("windowId") == window_id:
                if not w.get("isOnScreen"):
                    return "；注意：该窗口 isOnScreen=false（不在当前桌面/已最小化），代理光标画在你看不到的 Space 上"
                return ""
    except Exception:
        return ""
    return ""


def call_pointer(method, params, app_handle, window_id, retry=True):
    # 请求已交给原生后不根据错误文本重放输入；观察与下一次显式操作分开。
    return call_helper(method, params) or {}


def resolve_snapshot(app_handle, snapshot_id):
    if snapshot_id:
        return snapshot_id
    if STATE["snapshot"] and STATE["snapshot_app"] == app_handle:
        return STATE["snapshot"]
    raise HelperError("NO_SNAPSHOT", "元素操作需要 snapshotId：先调用 cua_state 读取界面树")


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def tool_browser_session(args):
    if not isinstance(args, dict):
        raise HelperError("INVALID_ARGUMENT", "参数必须是对象")
    action = args.get("action")
    if not isinstance(action, str) or action not in ("prepare", "list", "start", "attach"):
        raise HelperError("INVALID_ARGUMENT", "action 必须是 prepare、list、start 或 attach")
    if action == "attach":
        if set(args) != {"action", "pid", "windowId", "confirm"} or args.get("confirm") is not True or \
                type(args.get("pid")) is not int or not 1 <= args["pid"] <= 2147483647 or \
                type(args.get("windowId")) is not int or not 1 <= args["windowId"] <= 4294967295:
            raise HelperError("INVALID_ARGUMENT", "attach 必须且仅提供有效 pid、windowId 和 confirm=true")
    if action == "start":
        if set(args) != {"action", "sessionId", "confirm"} or args.get("confirm") is not True or \
                not isinstance(args.get("sessionId"), str) or not re.fullmatch(
                    r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", args["sessionId"]):
            raise HelperError("INVALID_ARGUMENT", "start 必须提供 prepare 返回的 sessionId 和 confirm=true")
    if action in ("start", "attach"):
        if READ_ONLY:
            raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，%s 已禁用" % action)
        if ALLOWLIST and not any(a in "com.google.chrome google chrome" for a in ALLOWLIST):
            raise HelperError("APP_NOT_ALLOWED", "CUA_ALLOWLIST 未包含 Google Chrome")
    elif set(args) != {"action"}:
        raise HelperError("INVALID_ARGUMENT", "prepare/list 仅接受 action")
    # 包括 start/attach 在内均不重试，管道超时不表示操作未完成。
    result = call_helper("browserSession", args)
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def tool_permissions(args):
    action = (args.get("action") or "status").lower()
    if action == "open_settings":
        permission = args.get("permission")
        if permission not in ("accessibility", "screenRecording"):
            raise HelperError("INVALID_ARGUMENT", "permission 必须是 accessibility 或 screenRecording")
        call_helper("openPermissionSettings", {"permission": permission})
        return "已请求打开系统设置面板（%s）。若未弹出，请手动进入 系统设置 > 隐私与安全 授权 \"workbuddy-cua\"。" % permission

    status = call_helper("permissionStatus") or {}
    caps = call_helper("getCapabilities") or {}
    launch = status.get("launchApplication") or {}
    launch.pop("icon", None)
    status.pop("launchApplication", None)
    status.pop("parent", None)
    privacy = caps.get("sessionPrivacy")
    if not isinstance(privacy, dict):
        privacy = {}
    locked_use = privacy.get("lockedUse")
    if not isinstance(locked_use, dict):
        locked_use = {}
    payload = {
        "platform": caps.get("platform"),
        "permissions": caps.get("permissions"),
        "sessionLocked": caps.get("sessionLocked"),
        "browserChannel": caps.get("browser"),
        "coordinateSpace": caps.get("coordinateSpace"),
        "responsibleApp": launch.get("name"),
        "responsiblePath": launch.get("path"),
        "helperPid": (status.get("helper") or {}).get("pid"),
        "bridgeReadOnly": READ_ONLY,
        "sessionPrivacy": privacy,
    }
    perms = payload["permissions"] or {}
    lines = [
        "platform=%s accessibility=%s screenRecording=%s sessionLocked=%s"
        % (
            payload["platform"],
            perms.get("accessibility"),
            perms.get("screenRecording"),
            payload["sessionLocked"],
        ),
        "responsibleApp=%s" % payload["responsibleApp"],
        "coordinateSpace=%s browserChannel=%s readOnly=%s"
        % (payload["coordinateSpace"], payload["browserChannel"], READ_ONLY),
        "sessionPrivacy active=%s shieldVisible=%s idleDisplaySleepPrevented=%s systemLocked=%s"
        % (
            privacy.get("active"),
            privacy.get("shieldVisible"),
            privacy.get("idleDisplaySleepPrevented"),
            privacy.get("systemLocked"),
        ),
        "lockedUse available=%s socketStarted=%s consumed=%s rejected=%s"
        % (
            locked_use.get("available"),
            locked_use.get("socketStarted"),
            locked_use.get("consumedCount"),
            locked_use.get("rejectedCount"),
        ),
    ]
    if not perms.get("accessibility") or not perms.get("screenRecording"):
        lines.append(
            "缺失授权：给 \"workbuddy-cua\" 授予辅助功能与屏幕录制后重试；"
            "可调用 cua_permissions(action=\"open_settings\", permission=\"accessibility\")。"
        )
    return "\n".join(lines)


def wait_for_window(app, pid, timeout=8.0, interval=0.25):
    """等待启动的应用出现唯一可核验窗口；多候选时如实放弃，不猜第一窗口。"""
    if type(pid) is not int or pid <= 0:
        return None, "应用未返回有效 pid"
    if WAIT_DEADLINE is not None:
        timeout = max(0.0, min(timeout, WAIT_DEADLINE - time.monotonic()))
    deadline = time.monotonic() + timeout
    last = "窗口尚未出现"
    while True:
        windows = list_windows(app)
        pool = [w for w in windows if isinstance(w.get("windowId"), int) and w.get("pid") in (None, pid)]
        preferred = [w for w in pool if w.get("isOnScreen")] or [w for w in pool if (w.get("title") or "").strip()]
        ids = {w["windowId"] for w in preferred}
        if len(ids) == 1:
            return ids.pop(), "ok"
        if len(ids) > 1:
            return None, "多个候选窗口，未猜测"
        if time.monotonic() >= deadline:
            return None, last
        time.sleep(interval)


def browser_window_snapshot():
    """为未绑定任务的建窗入口取得浏览器进程与现有窗口集合；失败时不建窗。"""
    chrome = call_helper("getApp", {"selector": "Google Chrome"}) or {}
    handle = chrome.get("appHandle")
    if not handle or type(chrome.get("pid")) is not int:
        raise HelperError("TARGET_REQUIRED", "创建前无法解析正在运行的 Google Chrome，不能确认共享目标；未派发创建")
    before = {w["windowId"] for w in list_windows(handle) if isinstance(w.get("windowId"), int)}
    return handle, before


def share_new_window(app, before, timeout=6.0):
    deadline = time.monotonic() + timeout
    while True:
        fresh = [w["windowId"] for w in list_windows(app)
                 if isinstance(w.get("windowId"), int) and w["windowId"] not in before]
        if len(fresh) == 1:
            ensure_share(app, fresh[0])
            return True
        if len(fresh) > 1:
            raise HelperError("TARGET_REQUIRED", "新建后出现多个候选窗口，未猜测共享目标；请显式 cua_windows action=share")
        if time.monotonic() >= deadline:
            raise HelperError("PREVIEW_NOT_READY", "新建窗口未在 6 秒内唯一出现，共享待补；不冒充已覆盖")
        time.sleep(0.25)


def tool_apps(args):
    action = (args.get("action") or "list").lower()
    selector = args.get("selector")
    query = args.get("query")
    # Lenient: selector without an explicit action means "select this app".
    if selector and action == "list":
        action = "get"
    if action in ("launch",) and READ_ONLY:
        raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，launch 已禁用")

    if action == "list":
        apps = call_helper("listApps") or []
        apps = sorted(apps, key=lambda a: (a.get("name") or "").lower())
        return "运行中的普通应用 %d 个：\n%s" % (
            len(apps),
            "\n".join("- " + fmt_app_ident(a) for a in apps),
        )

    if action == "find":
        if not query:
            raise HelperError("INVALID_ARGUMENT", "find 需要 query")
        found = call_helper("findApps", {"query": query, "maxAppMatches": 20}) or []
        if not found:
            return "没有匹配 %r 的已安装应用" % query
        return "匹配 %d 个：\n%s" % (
            len(found),
            "\n".join(
                "- %s [%s] %s%s"
                % (
                    f.get("displayName"),
                    f.get("id"),
                    f.get("path"),
                    " (running pid=%s)" % f["pid"] if f.get("isRunning") else "",
                )
                for f in found
            ),
        )

    if action in ("get", "launch"):
        if not selector and not args.get("appHandle"):
            raise HelperError("INVALID_ARGUMENT", "get/launch 需要 selector（应用名、bundleId、PID 或路径）")
        handle = args.get("appHandle")
        if selector:
            info = call_helper("getApp", {"selector": selector}) or {}
            handle = info.get("appHandle")
            info.pop("authorization", None)
            info.pop("applicationIcon", None)
            STATE["apps"][handle] = {
                "name": info.get("name"),
                "pid": info.get("pid"),
                "bundleId": info.get("bundleId"),
            }
            STATE["app"] = handle
            if ALLOWLIST:
                ident = ((info.get("bundleId") or "") + " " + (info.get("name") or "")).lower()
                if not any(a in ident for a in ALLOWLIST):
                    raise HelperError("APP_NOT_ALLOWED", "CUA_ALLOWLIST 未包含该应用：%s" % ident)
        else:
            handle = resolve_app(handle)
            info = STATE["apps"].get(handle, {})

        if action == "get":
            return "已选择 %s appHandle=%s%s" % (
                fmt_app_ident(info),
                handle,
                "" if info.get("pid") else "（尚未运行，需 action=launch）",
            )

        launched = call_helper("launchApp", {"appHandle": handle}) or {}
        launched.pop("authorization", None)
        launched.pop("applicationIcon", None)
        STATE["apps"][handle] = {
            "name": launched.get("name") or info.get("name"),
            "pid": launched.get("pid"),
            "bundleId": launched.get("bundleId"),
        }
        # 启动本身没有窗口可共享；窗口一出现就补上共享并核验首帧，不把“已启动”当共享成功。
        window, reason = wait_for_window(handle, launched.get("pid"), timeout=8)
        share = "未发现可核验窗口（%s），共享待补" % reason
        if window is not None:
            try:
                ensure_share(handle, window)
                share = "已共享目标应用（锚点窗口 %d）" % window
            except HelperError as exc:
                share = "共享未就绪（%s）" % exc.code
        return "已启动 %s appHandle=%s；共享: %s" % (fmt_app_ident(STATE["apps"][handle]), handle, share)

    raise HelperError("INVALID_ARGUMENT", "action 必须是 list/find/get/launch")


def tool_windows(args):
    action = (args.get("action") or "list").lower()
    if action == "unshare":
        # 释放共享不需要应用句柄：目标应用可能已经退出，但浮层必须能收掉。
        if READ_ONLY:
            raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，unshare 已禁用")
        result = call_helper("preview", {"action": "release"}) or {}
        preview = result.get("preview") or {}
        return "已结束窗口共享（holding=%s，visible=%s）。" % (preview.get("holding"), preview.get("visible"))
    app_handle = resolve_app(args.get("appHandle"))
    if action == "list":
        windows = call_helper("listWindows", {"appHandle": app_handle}) or []
        meta = STATE["apps"].get(app_handle, {})
        return "%s 的窗口 %d 个：\n%s" % (meta.get("name") or "应用", len(windows), fmt_windows(windows))
    if action == "activate":
        if READ_ONLY:
            raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，activate 已禁用")
        result = call_helper("activate", {"appHandle": app_handle}) or {}
        fg = result.get("foreground") or {}
        return "已请求激活 %s（dispatched=%s foreground.frontPid=%s focusedWindowId=%s）" % (
            STATE["apps"].get(app_handle, {}).get("name") or "应用",
            result.get("dispatched"),
            fg.get("frontPid"),
            fg.get("focusedWindowId"),
        )
    if action == "share":
        if READ_ONLY:
            raise HelperError("READ_ONLY", "CUA_READ_ONLY=1，share 已禁用")
        window_id = args.get("windowId")
        if type(window_id) is not int or not 1 <= window_id <= 4294967295:
            raise HelperError("INVALID_ARGUMENT", "share 需要显式有效 windowId")
        duration = args.get("durationSeconds", 1800)
        if type(duration) not in (int, float) or not 60 <= duration <= 3600:
            raise HelperError("INVALID_ARGUMENT", "durationSeconds 必须在 60 到 3600 之间")
        source = args.get("source", "application")
        if source not in ("application", "window", "desktop"):
            raise HelperError("INVALID_ARGUMENT", "source 必须是 application、window 或 desktop")
        ensure_share(app_handle, window_id, source=source, duration=duration)
        scope = {"desktop": "目标应用所在的整个桌面", "application": "目标应用", "window": "单个窗口"}[SHARE_RECORD["source"]]
        return ("已开始共享%s（windowId=%d，租约=%s 秒，holding=True）。"
                "共享面板在任务结束前保持：动作会续租，cua_windows action=unshare 或 cua_task end 会立即关闭。"
                % (scope, window_id, duration))
    raise HelperError("INVALID_ARGUMENT", "action 必须是 list、activate、share 或 unshare")


def tool_situation(args):
    if (not isinstance(args, dict) or set(args) != {"appHandle", "windowId"} or
            not isinstance(args.get("appHandle"), str) or not args["appHandle"].strip() or
            type(args.get("windowId")) is not int or not 1 <= args["windowId"] <= 4294967295):
        raise HelperError("INVALID_ARGUMENT", "现场观察必须且仅提供 appHandle 和有效 windowId")
    app = resolve_app(args["appHandle"])
    window = args["windowId"]
    caps = call_helper("getCapabilities") or {}
    windows = list_windows(app)
    target = next((w for w in windows if w.get("windowId") == window), None)
    if target is None:
        raise HelperError("WINDOW_UNAVAILABLE", "指定现场窗口不存在")
    state = call_helper("getState", {"appHandle": app, "windowId": window,
                                    "maxElements": 50, "maxDepth": 6, "includeMenus": False}) or {}
    if state.get("windowId") != window or state.get("appHandle", app) != app:
        raise HelperError("TARGET_MISMATCH", "现场观察返回不同目标")
    STATE.update(snapshot=state.get("snapshotId"), snapshot_app=app, snapshot_window=window)
    tree = state.get("tree")
    preview_result = call_helper("preview", {"action": "status"}) or {}
    preview = preview_result.get("preview")
    return {"content": [{"type": "text", "text": "只读现场观察；未启动共享或截图。"}],
            "situation": {"target": dict({"appHandle": app, "windowId": window},
                                                     **{k: target[k] for k in ("bounds", "isOnScreen", "onCurrentSpace") if k in target}),
                          "permissions": caps.get("permissions", "unknown"),
                          "sessionLocked": caps.get("sessionLocked", "unknown"),
                          "focus": {"focusedWindowId": state.get("focusedWindowId", "unknown"),
                                    "frontPid": state.get("frontPid", "unknown"),
                                    "targetOnCurrentSpace": target.get("onCurrentSpace", "unknown")},
                          "ax": {"available": bool(tree.strip()) if isinstance(tree, str) else "unknown",
                                 "truncated": state.get("truncated", "unknown"), "tree": tree,
                                 "snapshotId": state.get("snapshotId"), "maxElements": 50, "maxDepth": 6},
                          "preview": preview if isinstance(preview, dict) else {"status": "unknown"},
                          "scopeWarning": "desktop 可能包含其他应用及敏感信息；现场观察不证明共享正在运行。",
                          "suggestedActions": ["按明确目标观察 AX；空树不证明自绘或权限失败。",
                                               "输入会先校验 desktop 视频首帧；未验证时不派发。"]}}


def tool_state(args):
    app_handle = resolve_app(args.get("appHandle"))
    window_id = resolve_window(app_handle, args.get("windowId"))
    params = {
        "appHandle": app_handle,
        "maxElements": int(args.get("maxElements") or 200),
        "maxDepth": int(args.get("maxDepth") or 8),
    }
    if window_id:
        params["windowId"] = int(window_id)
    if args.get("includeMenus"):
        params["includeMenus"] = True
    if args.get("details"):
        params["details"] = True

    try:
        result = call_helper("getState", params) or {}
    except HelperError as exc:
        if exc.code in ("NATIVE_ERROR", "WINDOW_UNAVAILABLE"):
            raise HelperError(
                exc.code,
                "%s；该应用可能没有可观察窗口，先核对 cua_windows 列表，不自动激活"
                % exc.message,
            )
        raise
    STATE["snapshot"] = result.get("snapshotId")
    STATE["snapshot_app"] = app_handle
    STATE["snapshot_window"] = result.get("windowId")
    if result.get("windowId"):
        remember_window(app_handle, result)

    tree = result.get("tree") or ""
    if len(tree) > MAX_TREE_CHARS:
        tree = tree[:MAX_TREE_CHARS] + "\n... (tree truncated by bridge at %d chars)" % MAX_TREE_CHARS

    meta = STATE["apps"].get(app_handle, {})
    header = [
        "app=%s windowId=%s focusedWindowId=%s"
        % (meta.get("name") or app_handle, result.get("windowId"), result.get("focusedWindowId")),
        "snapshotId=%s truncated=%s" % (result.get("snapshotId"), result.get("truncated")),
    ]
    tail = [
        "",
        "提示：元素操作传 snapshotId + elementIndex；坐标操作使用窗口左上角起算的 point"
        "（截图 bounds 给的是屏幕坐标，换算 window 坐标需减去 bounds.x/y）。",
    ]
    if result.get("truncated"):
        tail.append("界面树被截断：可加大 maxElements/maxDepth，或先用 cua_windows 选中具体窗口缩小范围。")
    if not tree.strip():
        target = by_id.get(result.get("windowId") or window_id) or {}
        if target and not target.get("isOnScreen"):
            tail.append(
                "AX 树为空且该窗口 isOnScreen=false，不能据此断定 Space 或自绘状态；先核对目标与可见性，"
                "可用 cua_visual_state 获取视频帧进行视觉分析；不得自动点击或猜测盲点。"
            )
        else:
            tail.append("AX 树为空或缺失AXWebArea不代表权限失败：用 cua_visual_state 获取视频帧供视觉分析，不自动点击，不根据不确定视觉盲点操作。")
    elif result.get("focusedWindowId") and result.get("windowId") and result["focusedWindowId"] != result["windowId"]:
        tail.append(
            "注意：观察的是 windowId=%s，但应用焦点在 windowId=%s；输入会进焦点窗口，必要时显式传 windowId。"
            % (result["windowId"], result["focusedWindowId"])
        )
    return "\n".join(header) + "\n" + (tree or "(空)") + "\n".join(tail)


def tool_visual_state(args):
    if not isinstance(args, dict) or set(args) - {"appHandle", "windowId"}:
        raise HelperError("INVALID_ARGUMENT", "仅接受 appHandle、windowId")
    if "appHandle" in args and (not isinstance(args["appHandle"], str) or not args["appHandle"].strip()):
        raise HelperError("INVALID_ARGUMENT", "appHandle 必须是非空字符串")
    if "windowId" in args and (type(args["windowId"]) is not int or not 1 <= args["windowId"] <= 4294967295):
        raise HelperError("INVALID_ARGUMENT", "windowId 必须是有效窗口整数")
    app_handle = resolve_app(args.get("appHandle"))
    window_id = resolve_window(app_handle, args.get("windowId"))
    result = call_helper("visualState", {"appHandle": app_handle, "windowId": window_id}) or {}
    data = result.get("data")
    if not data or result.get("mimeType") != "image/png" or not result.get("snapshotId"):
        raise HelperError("NATIVE_ERROR", "helper 未返回完整视频帧观察")
    STATE["snapshot"] = result["snapshotId"]
    STATE["snapshot_app"] = app_handle
    STATE["snapshot_window"] = result.get("windowId")
    remember_window(app_handle, result)
    saved = None
    # 仅复用既有用户授权目录，不创建目录、不清理用户文件。
    if os.path.isdir(SHOTS_DIR):
        import base64
        import tempfile
        try:
            raw = base64.b64decode(data, validate=True)
            with tempfile.NamedTemporaryFile(prefix="visual-", suffix=".png", dir=SHOTS_DIR, delete=False) as fh:
                fh.write(raw)
                saved = fh.name
        except (OSError, ValueError) as exc:
            saved = "保存失败: %s" % exc
    metadata = dict((k, v) for k, v in result.items() if k != "data")
    metadata["savedPath"] = saved
    info = json.dumps(metadata, ensure_ascii=False) + "\n像素转窗口point：x=像素x*bounds.width/pixelWidth，y=像素y*bounds.height/pixelHeight；窗口左上角为原点。AXWebArea缺失不代表权限失败。本工具仅观察，不自动点击；禁止根据不确定视觉盲点派发输入。新complete帧不保证对应实际新页面。"
    return {"content": [{"type": "image", "data": data, "mimeType": "image/png"},
                        {"type": "text", "text": info}]}


def normalized_page_url(value):
    if not isinstance(value, str) or not value or re.search(r'[\s\x00-\x20\x7f\\]', value):
        raise ValueError("URL 含空白、控制字符或反斜杠")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or not parts.hostname or \
            parts.username is not None or parts.password is not None:
        raise ValueError("需要无 credentials 的完整 http/https URL")
    host = parts.hostname
    if parts.netloc.startswith("[") and ":" not in host:
        raise ValueError("方括号地址必须是有效 IPv6 地址")
    if ":" in host:
        host = ipaddress.IPv6Address(host).compressed
    else:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                      for label in host.split(".")):
            raise ValueError("URL 主机名无效")
    port = parts.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL 端口无效")
    if parts.netloc.endswith(":"):
        raise ValueError("URL 端口为空")
    scheme = parts.scheme.lower()
    # 路径区分大小写；仅统一空路径、百分号大小写及 unreserved 字符。
    def normalize(component):
        if re.search(r'%(?![0-9a-fA-F]{2})', component):
            raise ValueError("URL 转义无效")
        def escape(match):
            char = chr(int(match[0][1:], 16))
            return char if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" else match[0].upper()
        return re.sub(r'%[0-9a-fA-F]{2}', escape, component)
    path = normalize(parts.path or "/")
    # RFC 3986 点段规范化，不折叠重复斜杠或尾部斜杠。
    segments = []
    for segment in path.split("/")[1:]:
        if segment == "..":
            if segments:
                segments.pop()
        elif segment != ".":
            segments.append(segment)
    if path.endswith(("/.", "/..")):
        segments.append("")
    return (scheme, host, port or (443 if scheme == "https" else 80),
            "/" + "/".join(segments), normalize(parts.query))


def ax_webarea_matches(tree, expected_url, expected_text):
    # AXTextFormatter 每层两个空格；AXAccess 折叠透明容器但保留可见父子缩进。
    # Swift String(reflecting:) 的非 JSON 转义保守拒绝，不猜测正文。
    target = normalized_page_url(expected_url)
    stack, counts, matches = [], {}, set()
    quoted = r'"(?:[^"\\]|\\.)*"'
    for line in tree.splitlines():
        node = re.match(r'^( *)- (?:\[\d+\] )?(AX\w+)(?= |$)', line)
        if not node or len(node[1]) % 2:
            stack = []
            continue
        depth, role = len(node[1]) // 2, node[2]
        if depth > len(stack):
            stack = []
            continue
        stack = stack[:depth]
        parent = stack[-1]["path"] if stack else ()
        ordinal = counts.get(parent, 0)
        counts[parent] = ordinal + 1
        fields, end = {}, node.end()
        for token in re.finditer(quoted, line[node.end():]):
            start, stop = node.end() + token.start(), node.end() + token.end()
            prefix = line[end:start].strip()
            try:
                value = json.loads(token[0])
            except ValueError:
                value = None
            if prefix == "url=" or (end == node.end() and re.fullmatch(
                    r'(?:(?:focused|disabled|selected|\w+=(?:unknown|true|false|AX\w+))\s+)+url=', prefix)):
                fields["url"] = value
            elif prefix in ("", "=") or (end == node.end() and re.fullmatch(
                    r'(?:(?:focused|disabled|selected|\w+=(?:unknown|true|false|AX\w+))\s*)+(?:=\s*)?', prefix)):
                fields.setdefault("text", []).append(value)
            end = stop
        entry = {"role": role, "path": parent + ((role, ordinal),), "url": fields.get("url")}
        stack.append(entry)
        if role != "AXStaticText" or not any(expected_text in text for text in fields.get("text", []) if text):
            continue
        web = next((i for i in range(len(stack) - 2, -1, -1) if stack[i]["role"] == "AXWebArea"), None)
        if web is None or any(e["role"] in ("AXToolbar", "AXMenuBar", "AXMenu", "AXTabGroup") for e in stack):
            continue
        try:
            if normalized_page_url(stack[web]["url"]) == target:
                matches.add((stack[web]["path"], target))
        except (ValueError, UnicodeError):
            pass
    return matches


def wait_ready_options(args):
    fields = {"appHandle", "windowId", "expectedUrl", "expectedText", "timeoutSeconds", "intervalSeconds"}
    if not isinstance(args, dict) or set(args) - fields or not {"appHandle", "windowId", "expectedUrl", "expectedText"} <= set(args):
        raise HelperError("INVALID_ARGUMENT", "必须显式提供 appHandle、windowId、expectedUrl、expectedText，仅接受等待参数")
    if not isinstance(args["appHandle"], str) or not args["appHandle"].strip():
        raise HelperError("INVALID_ARGUMENT", "appHandle 必须是非空字符串")
    if type(args["windowId"]) is not int or not 1 <= args["windowId"] <= 4294967295:
        raise HelperError("INVALID_ARGUMENT", "windowId 必须是有效窗口整数")
    expected = args["expectedText"]
    if not isinstance(expected, str) or not expected.strip() or len(expected) > 200:
        raise HelperError("INVALID_ARGUMENT", "expectedText 必须是非空且最多200字符的字符串")
    try:
        normalized_page_url(args["expectedUrl"])
    except (ValueError, UnicodeError) as exc:
        raise HelperError("INVALID_ARGUMENT", "expectedUrl 无效：%s" % exc)
    timeout, interval = args.get("timeoutSeconds", 30), args.get("intervalSeconds", 2)
    for value, minimum, maximum in ((timeout, 0, 30), (interval, 1, None)):
        if type(value) not in (int, float) or not math.isfinite(value) or value < minimum or \
                value == 0 or (maximum is not None and value > maximum):
            raise HelperError("INVALID_ARGUMENT", "timeoutSeconds 必须在(0,30]，intervalSeconds 必须>=1且有限")
    return timeout, interval


def tool_wait_ready(args):
    timeout, interval = wait_ready_options(args)
    deadline = WAIT_DEADLINE if WAIT_DEADLINE is not None else time.monotonic() + timeout
    app, window = resolve_app(args["appHandle"]), args["windowId"]
    base = {"appHandle": app, "windowId": window}
    connection, pid = current_connection(), STATE["apps"][app].get("pid")
    previous, latest_visual, frames = set(), None, 0

    def read(method, extra=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if TASK_DISPATCH is not None:
            assert_task_connection(TASK_DISPATCH)
            validate_task_target(method, base, TASK_DISPATCH)
        result = HELPER.request(method, dict(base, **(extra or {})),
                                timeout=min(remaining, TIMEOUTS[method]), retry_safe=False)
        if current_connection() != connection:
            raise HelperError("REQUIRES_REVALIDATION", "等待期间 helper 连接变化")
        if TASK_DISPATCH is not None:
            assert_task_connection(TASK_DISPATCH)
            validate_task_result(method, result, TASK_DISPATCH)
        if not isinstance(result, dict) or result.get("windowId") != window:
            raise HelperError("WINDOW_CHANGED", "观察结果不属于指定窗口，已停止等待")
        if result.get("appHandle", app) != app or (pid is not None and result.get("pid", pid) != pid):
            raise HelperError("STALE_APP", "观察结果进程身份变化，已停止等待")
        return result

    def output(status, observation=None):
        meta = dict((k, v) for k, v in (observation or {}).items() if k not in ("data", "tree", "elements"))
        meta.update(base, ready=status == "ready", status=status, expectedUrl=args["expectedUrl"],
                    expectedText=args["expectedText"], completionEvidence="ax_url_and_content" if status == "ready" else "unknown",
                    consecutiveMatches=2 if status == "ready" else (1 if previous else 0), visualFrames=frames,
                    remainingSeconds=max(0, deadline - time.monotonic()))
        content = []
        if status != "ready" and latest_visual:
            content.append({"type": "image", "data": latest_visual["data"], "mimeType": "image/png"})
        content.append({"type": "text", "text": json.dumps(meta, ensure_ascii=False) +
                        "\n单次调用内有界 AX 轮询；同一 AXWebArea 的规范化 URL（忽略 fragment，保留 query）与正文静态文本连续两次匹配。页面身份仅为 AX 树位置与 URL，原生无稳定文档实例 ID；不证明 DOM readyState 或网络完成，同 URL 重载不可区分。视频仅供主代理判断，不自动视觉分类，不操作网页。"})
        return {"content": content}

    while time.monotonic() < deadline:
        state = read("getState", {"maxElements": 2000, "maxDepth": 30})
        if state is None or time.monotonic() >= deadline:
            break
        observed_pid = state.get("pid")
        if type(observed_pid) is not int or observed_pid <= 0 or not state.get("snapshotId"):
            matches = set()
        else:
            if pid is None:
                pid = observed_pid
            matches = ax_webarea_matches(state.get("tree") or "", args["expectedUrl"], args["expectedText"])
        if matches & previous:
            STATE.update(snapshot=state.get("snapshotId"), snapshot_app=app, snapshot_window=window)
            return output("ready", state)
        previous = matches
        # 不把未匹配当成视觉成功；最多读取三帧，仅在内存保留最新帧。
        if not matches and frames < 3:
            visual = read("visualState")
            if visual is not None:
                if not visual.get("data") or visual.get("mimeType") != "image/png" or not visual.get("snapshotId"):
                    raise HelperError("NATIVE_ERROR", "helper 未返回完整视频帧观察")
                latest_visual = visual
                frames += 1
                STATE.update(snapshot=visual["snapshotId"], snapshot_app=app, snapshot_window=window)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            threading.Event().wait(min(interval, remaining))
    return output("needs_visual_review" if latest_visual else "timeout")


def tool_screenshot(args):
    app_handle = resolve_app(args.get("appHandle"))
    window_id = resolve_window(app_handle, args.get("windowId"))
    params = {"appHandle": app_handle}
    if window_id:
        params["windowId"] = int(window_id)
    result = call_helper("screenshot", params) or {}

    data = result.get("data") or ""
    if not data:
        raise HelperError("NATIVE_ERROR", "helper 未返回截图数据")
    os.makedirs(SHOTS_DIR, exist_ok=True)
    name = "shot-%s.png" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(SHOTS_DIR, name)
    raw = data
    try:
        import base64

        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
    except Exception as exc:
        path = "(保存失败: %s)" % exc

    # Keep the newest 100 shots so a long session does not fill the disk.
    try:
        shots = [
            os.path.join(SHOTS_DIR, f) for f in os.listdir(SHOTS_DIR) if f.endswith(".png")
        ]
        shots.sort(key=os.path.getmtime)
        for stale in shots[:-100]:
            os.remove(stale)
    except Exception:
        pass

    bounds = result.get("bounds") or {}
    meta = STATE["apps"].get(app_handle, {})
    info = (
        "%s 截图 windowId=%s %sx%s px bounds=%s,%s %sx%s pt (coordinateSpace=%s)\n保存: %s"
        % (
            meta.get("name") or "应用",
            result.get("windowId"),
            result.get("pixelWidth"),
            result.get("pixelHeight"),
            int(bounds.get("x", 0)),
            int(bounds.get("y", 0)),
            int(bounds.get("width", 0)),
            int(bounds.get("height", 0)),
            result.get("coordinateSpace"),
            path,
        )
    )
    return {
        "content": [
            {"type": "image", "data": raw, "mimeType": result.get("mimeType") or "image/png"},
            {"type": "text", "text": info},
        ]
    }


def build_input_args(args, allowed):
    out = {}
    for key in allowed:
        if key in args and args[key] is not None:
            out[key] = args[key]
    return out


def tool_click(args, method="click"):
    app_handle = resolve_app(args.get("appHandle"))
    element_index = args.get("elementIndex")
    base = {"appHandle": app_handle}
    if args.get("mouseButton"):
        base["mouseButton"] = args["mouseButton"]
    if args.get("modifiers"):
        base["modifiers"] = args["modifiers"]
    if args.get("clickCount"):
        base["clickCount"] = int(args["clickCount"])
    if args.get("holdMs") is not None:
        base["holdMs"] = int(args["holdMs"])

    if element_index is not None:
        base["snapshotId"] = resolve_snapshot(app_handle, args.get("snapshotId"))
        base["elementIndex"] = int(element_index)
        target = "element[%s] snapshot=%s" % (element_index, base["snapshotId"])
    else:
        if args.get("x") is None or args.get("y") is None:
            raise HelperError("INVALID_ARGUMENT", "元素点击传 elementIndex；坐标点击必须同时传 x 和 y")
        window_id = input_window(app_handle, args.get("windowId"), args.get("snapshotId"))
        base["windowId"] = int(window_id)
        base["x"] = float(args["x"])
        base["y"] = float(args["y"])
        if args.get("snapshotId"):
            base["snapshotId"] = args["snapshotId"]
        target = "window=%s point=(%s,%s)" % (window_id, base["x"], base["y"])

    result = call_pointer(method, base, app_handle, base.get("windowId") or 0) if "windowId" in base else (call_helper(method, base) or {})
    remember_window(app_handle, result)
    detail = ""
    if result.get("route"):
        detail = " route=%s axAction=%s focusRestorationAttempted=%s focusRestorationAccepted=%s frontPid=%s->%s" % (
            result["route"], result.get("axAction"), result.get("focusRestorationAttempted"),
            result.get("focusRestorationAccepted"), result.get("frontPidBefore"), result.get("frontPidAfterAction"))
    return "已派发 %s %s dispatched=%s verified=%s（读 cua_state 核实效果）%s%s" % (
        method, target,
        result.get("dispatched"),
        result.get("verified"),
        window_note(app_handle, base.get("windowId")) if base.get("windowId") else "",
        detail,
    )


def tool_type_text(args):
    app_handle = resolve_app(args.get("appHandle"))
    text = args.get("text")
    if text is None:
        raise HelperError("INVALID_ARGUMENT", "type_text 需要 text")
    result = call_helper("typeText", {"appHandle": app_handle, "text": text}) or {}
    return "已派发 typeText %d 字符 dispatched=%s（长文本建议改用 cua_paste）" % (len(text), result.get("dispatched"))


def tool_paste(args):
    app_handle = resolve_app(args.get("appHandle"))
    text = args.get("text")
    if text is None:
        raise HelperError("INVALID_ARGUMENT", "paste 需要 text")
    params = {
        "appHandle": app_handle,
        "text": text,
        "maxClipboardBytes": 4 * 1024 * 1024,
        "pasteReadTimeoutMs": 1500,
        "maxElements": 200,
        "maxDepth": 8,
    }
    if args.get("format"):
        params["format"] = args["format"]
    result = call_helper("paste", params) or {}
    return "已派发 paste %d 字符 dispatched=%s%s" % (
        len(text),
        result.get("dispatched"),
        "（已核对输入框内容）" if result.get("verified") else "（未核实，需读 cua_state）",
    )


def tool_key(args):
    app_handle = resolve_app(args.get("appHandle"))
    key = args.get("key")
    if not key:
        raise HelperError("INVALID_ARGUMENT", "key 需要键名，如 return/tab/escape 或单字符")
    params = {"appHandle": app_handle, "key": key}
    if args.get("modifiers"):
        params["modifiers"] = args["modifiers"]
    result = call_helper("pressKey", params) or {}
    return "已派发 pressKey %s dispatched=%s" % (key, result.get("dispatched"))


def tool_scroll(args):
    app_handle = resolve_app(args.get("appHandle"))
    window_id = input_window(app_handle, args.get("windowId"), args.get("snapshotId"))
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        windows = call_helper("listWindows", {"appHandle": app_handle}) or []
        target = next((w for w in windows if w.get("windowId") == window_id), None)
        if not target:
            raise HelperError("WINDOW_UNAVAILABLE", "找不到 windowId=%s 的窗口" % window_id)
        b = target.get("bounds") or {}
        x = float(b.get("width", 0)) / 2.0
        y = float(b.get("height", 0)) / 2.0
    params = {
        "appHandle": app_handle,
        "windowId": int(window_id),
        "x": float(x),
        "y": float(y),
        "deltaLinesX": int(args.get("deltaLinesX") or 0),
        "deltaLinesY": int(args.get("deltaLinesY") or 0),
    }
    if args.get("snapshotId"):
        params["snapshotId"] = args["snapshotId"]
    result = call_pointer("scroll", params, app_handle, window_id)
    route = result.get("route")
    detail = ""
    if route:
        detail = " route=%s foundScrollBar=%s scrollBarMoved=%s bar=%s->%s" % (
            route, result.get("foundScrollBar"), result.get("scrollBarMoved"),
            result.get("barBefore"), result.get("barAfter"))
    return "已派发 scroll window=%s at(%s,%s) dx=%s dy=%s dispatched=%s%s" % (
        window_id,
        params["x"],
        params["y"],
        params["deltaLinesX"],
        params["deltaLinesY"],
        result.get("dispatched"),
        detail,
    )


def tool_drag(args):
    app_handle = resolve_app(args.get("appHandle"))
    window_id = input_window(app_handle, args.get("windowId"), args.get("snapshotId"))
    for key in ("fromX", "fromY", "toX", "toY"):
        if args.get(key) is None:
            raise HelperError("INVALID_ARGUMENT", "drag 需要 %s" % key)
    params = {
        "appHandle": app_handle,
        "windowId": int(window_id),
        "fromX": float(args["fromX"]),
        "fromY": float(args["fromY"]),
        "toX": float(args["toX"]),
        "toY": float(args["toY"]),
        "durationMs": int(args.get("durationMs") or 400),
        "steps": int(args.get("steps") or 15),
    }
    for key in ("mouseButton", "modifiers", "snapshotId"):
        if args.get(key):
            params[key] = args[key]
    result = call_pointer("drag", params, app_handle, window_id)
    return "已派发 drag window=%s (%s,%s)->(%s,%s) dispatched=%s" % (
        window_id,
        params["fromX"],
        params["fromY"],
        params["toX"],
        params["toY"],
        result.get("dispatched"),
    )


def tool_set_value(args):
    app_handle = resolve_app(args.get("appHandle"))
    element_index = args.get("elementIndex")
    if element_index is None or args.get("value") is None:
        raise HelperError("INVALID_ARGUMENT", "set_value 需要 elementIndex 和 value")
    params = {
        "appHandle": app_handle,
        "snapshotId": resolve_snapshot(app_handle, args.get("snapshotId")),
        "elementIndex": int(element_index),
        "value": args["value"],
    }
    result = call_helper("setValue", params) or {}
    return "已派发 setValue element[%s]=%r dispatched=%s（只对 valueWritable 元素有效）" % (
        element_index,
        args["value"],
        result.get("dispatched"),
    )


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

APP_HANDLE_PROP = {"type": "string", "description": "cua_apps 返回的 appHandle；省略时使用本会话最近选择的应用"}
WINDOW_PROP = {"type": "integer", "description": "窗口 id；输入必须显式指定或使用同应用当前连接可信快照的窗口"}
SNAPSHOT_PROP = {"type": "string", "description": "cua_state 返回的 snapshotId；省略时使用该应用最近快照"}
MODIFIERS_PROP = {
    "type": "array",
    "items": {"type": "string", "enum": ["command", "control", "option", "shift", "fn"]},
    "description": "修饰键",
}

TOOLS = [
    {"name": "cua_situation", "description": "只读现场：必须显式 appHandle/windowId，经任务守卫核验，仅读取能力、窗口与有界AX（50元素/6层），不截图、不启动共享、不重绑。焦点、AX、共享未知值诚实返回，不推断空树为自绘。选路：原生 App 走本工具及其后的 AX 系列；网页内容优先 cua_browser_events（结构化、不靠猜元素）；纯命令行或文件操作不需要 CUA。",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"appHandle": {"type": "string", "minLength": 1},
                                    "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295}},
                     "required": ["appHandle", "windowId"]},
     "handler": tool_situation, "readOnly": True,
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {
        "name": "cua_task",
        "description": "持久任务绑定：begin 默认 interactionMode=background_browser，执行层禁止所有原生输入和窗口激活；扩展未就绪必须等待。只有明确的普通桌面应用任务才在 begin 指定 desktop，模式不能中途更改。begin 生成 UUID，保持 awaiting_window，不创建或启动浏览器。bind 必须显式 appHandle、windowId、confirm=true，仅 attach 已有窗口，mode=attached_existing，不等于新建或数据隔离。status 只读核验进程代际和窗口；跨连接 requires_revalidation，需重新显式确认，绝不按 PID 自动接管。任务期间观察和输入严格限制目标，应用级键盘接口拒绝。"
                       "后台模式不启动桌面接管；共享预览独立维持原目标和宿主位置。结束任务释放绑定和预览租约。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["begin", "bind", "status", "end"]},
                "taskId": {"type": "string", "minLength": 1},
                "appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP,
                "confirm": {"type": "boolean", "const": True},
                "interactionMode": {"type": "string", "enum": ["background_browser", "desktop"], "default": "background_browser"},
            },
            "required": ["action"],
            "oneOf": [
                {"properties": {"action": {"const": "begin"}},
                 "not": {"anyOf": [{"required": [k]} for k in ("taskId", "appHandle", "windowId", "confirm")]}},
                {"properties": {"action": {"const": "bind"}}, "required": ["appHandle", "windowId", "confirm"], "not": {"required": ["interactionMode"]}},
                {"properties": {"action": {"const": "status"}},
                 "not": {"anyOf": [{"required": [k]} for k in ("appHandle", "windowId", "confirm", "interactionMode")]}},
                {"properties": {"action": {"const": "end"}}, "required": ["confirm"],
                 "not": {"anyOf": [{"required": [k]} for k in ("appHandle", "windowId", "interactionMode")]}},
            ],
        },
        "handler": tool_task, "readOnly": True,
    },
    {
        "name": "cua_browser_session",
        "description": "受管 Chrome 会话：prepare 登记固定配置；start 必须 sessionId+confirm=true 并核验安装身份，返回 pending/started；list 核验进程身份与窗口。start 不重试，超时可能已启动；禁止重复启动，不关闭浏览器、不切换 Space。attach(pid,windowId,confirm=true) 共享既有 Chrome 数据，仅绑定指定进程代际和窗口，不启动、不写 profile、不激活、不 raise；关闭或身份改变后 invalidated，不选择其他窗口。READ_ONLY 拦截 start/attach。", 
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["prepare", "list", "start", "attach"]},
                "pid": {"type": "integer", "minimum": 1, "maximum": 2147483647},
                "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295},
                "sessionId": {"type": "string", "pattern": "^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$"},
                "confirm": {"type": "boolean", "const": True},
            },
            "required": ["action"],
            "additionalProperties": False,
            "oneOf": [
                {"properties": {"action": {"enum": ["prepare", "list"]}}, "not": {"anyOf": [{"required": ["sessionId"]}, {"required": ["confirm"]}, {"required": ["pid"]}, {"required": ["windowId"]}]}},
                {"properties": {"action": {"const": "start"}}, "required": ["sessionId", "confirm"], "not": {"anyOf": [{"required": ["pid"]}, {"required": ["windowId"]}]}},
                {"properties": {"action": {"const": "attach"}}, "required": ["pid", "windowId", "confirm"], "not": {"required": ["sessionId"]}},
            ],
        },
        "handler": tool_browser_session,
        "readOnly": True,
    },
    {
        "name": "cua_permissions",
        "description": "查看 macOS 辅助功能/屏幕录制授权、平台能力、会话锁定与任务隐私层状态；action=open_settings 打开对应系统设置面板。任何操作前先确认这里是 true。"
                       "sessionLocked=true 时所有界面操作会被拒绝（SESSION_LOCKED），须先解锁再重新观察。"
                       "任务隐私层会阻止显示器空闲睡眠并覆盖所有窗口，不保存或输入系统密码；合盖、触控 ID 和系统级锁定仍不可拦截。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "open_settings"], "default": "status"},
                "permission": {"type": "string", "enum": ["accessibility", "screenRecording"]},
            },
        },
        "handler": tool_permissions,
        "readOnly": True,
    },
    {
        "name": "cua_apps",
        "description": "应用层入口：action=list 列出运行中的应用；find 搜索已安装应用；get 按名称/bundleId/PID/路径选中应用并拿到 appHandle；launch 启动已选应用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "find", "get", "launch"], "default": "list"},
                "selector": {"type": "string", "description": "精确应用名、bundleId、十进制 PID 或 .app 路径（get/launch 用）"},
                "query": {"type": "string", "description": "模糊搜索关键词（find 用）"},
                "appHandle": APP_HANDLE_PROP,
            },
        },
        "handler": tool_apps,
        "readOnly": True,
    },
    {
        "name": "cua_windows",
        "description": "列出应用窗口（windowId、标题、尺寸、屏幕坐标、是否在屏/当前桌面）、请求激活应用，或开始/结束窗口共享预览。share 会让浮动共享面板在整个界面操作任务期间保持（动作自动续租），直到 unshare 或 cua_task end。"
                       "应用内观察与输入默认只共享目标应用；任务需要跨应用、Dock 或系统界面时，先 share(source=desktop) 共享目标所在桌面，后续动作保留该范围。"
                       "不必为应用内任务手动 share；锁屏时共享立即关闭并清除画面。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "activate", "share", "unshare"], "default": "list"},
                "appHandle": APP_HANDLE_PROP,
                "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295},
                "durationSeconds": {"type": "number", "minimum": 60, "maximum": 3600, "default": 1800},
                "source": {"type": "string", "enum": ["application", "window", "desktop"], "default": "application",
                           "description": "应用内操作用 application，仅包含目标应用的窗口；跨应用或系统界面操作用 desktop，包含目标显示器当前桌面；window 仅包含指定窗口。所选范围随动作续租，直到结束共享或更换目标。"},
            },
        },
        "handler": tool_windows,
        "readOnly": True,
    },
    {
        "name": "cua_state",
        "description": "读取应用的辅助功能界面树（紧凑文本，含 [elementIndex]、role、title/value、可用 actions），返回 snapshotId 供元素级操作。AXWebArea缺失或树不完整不代表权限失败，可调用 cua_visual_state 获取真实视频帧供视觉分析；禁止自动点击或根据不确定视觉盲点操作。"
                       "浏览器里的网页内容优先 cua_browser_events，AX 对网页结构通常不完整。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "windowId": WINDOW_PROP,
                "maxElements": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000},
                "maxDepth": {"type": "integer", "default": 8, "minimum": 1, "maximum": 30},
                "includeMenus": {"type": "boolean", "default": False},
                "details": {"type": "boolean", "default": False, "description": "包含更多属性（bounds/valueWritable 等）"},
            },
        },
        "handler": tool_state,
        "readOnly": True,
    },
    {
        "name": "cua_wait_ready",
        "description": "只读有界 AX 加载观察：显式应用、窗口、expectedUrl 和 expectedText；同一真实 AXWebArea URL与正文静态文本连续两次匹配才 ready，completionEvidence=ax_url_and_content，否则 unknown。单次调用内按 intervalSeconds>=1 轮询，共享 monotonic 总预算默认/最多30秒，包含任务核验，不重试。URL规范化scheme/host/defaultport/path，保留query、忽略fragment；不支持credentials。最多3个视频帧，仅返回最新供主代理审核，结束timeout或needs_visual_review，不自动视觉分类。权限/锁屏/进程/窗口错误立即传播。不启动、导航、点击或刷新，不访问HTTP/JS/调试接口。不证明DOM readyState或网络完成；页面身份仅AX树位置+URL，同URL重载不可区分。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": {"type": "string", "minLength": 1, "pattern": "\\S", "description": "必须显式提供已选择应用的appHandle"},
                "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295},
                "expectedUrl": {"type": "string", "minLength": 1, "description": "完整http/https URL，无credentials；忽略fragment，保留query"},
                "expectedText": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "\\S"},
                "timeoutSeconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30, "default": 30},
                "intervalSeconds": {"type": "number", "minimum": 1, "default": 2},
            },
            "required": ["appHandle", "windowId", "expectedUrl", "expectedText"],
            "additionalProperties": False,
        },
        "handler": tool_wait_ready,
        "readOnly": True,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "cua_visual_state",
        "description": "AXWebArea缺失、AX树不完整或自绘界面的视觉兜底观察：从目标window的SCStream取请求后的首个新complete视频帧并stop，等待最多4秒，返回PNG image、frameId、snapshotId、单调采集时间/age、point bounds及pixel尺寸。保留右上角实时视频预览，不是截图轮播。不绕过进程身份、owner、解锁、屏幕录制与真实坐标几何校验。仅供主agent视觉分析，不做OCR或视觉规划，不自动点击，禁止根据不确定视觉盲点操作；首帧不保证是实际新页面。snapshotId仅用于坐标观察，不含AX元素。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP},
            "additionalProperties": False,
        },
        "handler": tool_visual_state,
        "readOnly": True,
    },
    {
        "name": "cua_screenshot",
        "description": "截取应用窗口的 PNG（ScreenCaptureKit），返回图片、像素尺寸、屏幕 bounds 与落盘路径。坐标换算：窗口坐标 = 屏幕坐标 - bounds.x/y；像素与 point 比例应按实际 pixelWidth/pixelHeight 和 bounds 宽高分别计算，不能假定固定 2x。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP},
        },
        "handler": tool_screenshot,
        "readOnly": True,
    },
    {
        "name": "cua_click",
        "description": "点击。优先元素级：传 snapshotId + elementIndex（走 AXPress 或元素中心事件）；否则传 windowId + x/y（窗口左上角起算的 point）。clickCount=2 双击。"
                       "元素级找不到可点元素时，先用 cua_visual_state 或 cua_screenshot 核对真实位置再改坐标点击，不要凭 AX 树缺失就假定点中了。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "windowId": WINDOW_PROP,
                "snapshotId": SNAPSHOT_PROP,
                "elementIndex": {"type": "integer", "minimum": 0},
                "x": {"type": "number", "description": "窗口内 x（point）"},
                "y": {"type": "number", "description": "窗口内 y（point）"},
                "clickCount": {"type": "integer", "enum": [1, 2], "default": 1},
                "mouseButton": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "modifiers": MODIFIERS_PROP,
                "holdMs": {"type": "integer", "minimum": 0},
            },
        },
        "handler": tool_click,
        "readOnly": False,
    },
    {
        "name": "cua_type_text",
        "description": "向前台应用输入文本（逐字符 Unicode 事件，适合短文本/需触发逐键行为的场景）。长文本用 cua_paste 更快。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_type_text,
        "readOnly": False,
    },
    {
        "name": "cua_paste",
        "description": "通过剪贴板粘贴文本（长文本/中文/带换行首选），会核对输入框内容。要求目标输入框已聚焦。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_paste,
        "readOnly": False,
    },
    {
        "name": "cua_key",
        "description": "按键或组合键，如 key=return / tab / escape / space / f5 / a 配 modifiers=[\"command\"]。用于提交、切换焦点、快捷键。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "key": {"type": "string"},
                "modifiers": MODIFIERS_PROP,
            },
            "required": ["key"],
        },
        "handler": tool_key,
        "readOnly": False,
    },
    {
        "name": "cua_scroll",
        "description": "在窗口内滚动。deltaLinesY>0 显示上方内容、<0 显示下方内容；每轴 -10..10。省略 x/y 时用窗口中心。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "windowId": WINDOW_PROP,
                "snapshotId": SNAPSHOT_PROP,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "deltaLinesX": {"type": "integer", "minimum": -10, "maximum": 10, "default": 0},
                "deltaLinesY": {"type": "integer", "minimum": -10, "maximum": 10, "default": 0},
            },
        },
        "handler": tool_scroll,
        "readOnly": False,
    },
    {
        "name": "cua_drag",
        "description": "窗口内拖拽（窗口坐标）。用于滑块、框选、拖放排序。默认 400ms / 15 步。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "windowId": WINDOW_PROP,
                "fromX": {"type": "number"},
                "fromY": {"type": "number"},
                "toX": {"type": "number"},
                "toY": {"type": "number"},
                "durationMs": {"type": "integer", "default": 400},
                "steps": {"type": "integer", "default": 15},
                "mouseButton": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "modifiers": MODIFIERS_PROP,
                "snapshotId": SNAPSHOT_PROP,
            },
            "required": ["fromX", "fromY", "toX", "toY"],
        },
        "handler": tool_drag,
        "readOnly": False,
    },
    {
        "name": "cua_set_value",
        "description": "直接写入 AX 元素的 value（比逐键输入可靠，但只对 valueWritable 元素生效）。元素来自 cua_state 的树。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "appHandle": APP_HANDLE_PROP,
                "snapshotId": SNAPSHOT_PROP,
                "elementIndex": {"type": "integer", "minimum": 0},
                "value": {"type": "string"},
            },
            "required": ["elementIndex", "value"],
        },
        "handler": tool_set_value,
        "readOnly": False,
    },
]

TOOLS.append(dict(next(t for t in TOOLS if t["name"] == "cua_click"),
                  name="cua_hover", description="悬停指定窗口坐标或快照元素；任务期间执行相同严格目标校验。",
                  handler=lambda args: tool_click(args, method="hover")))
def validate_browser_assignment(task_id):
    with task_store() as data:
        record = data['tasks'].get(task_id) if isinstance(task_id, str) else None
        if record is None or data.get('activeTaskId') != task_id or task_status(record)['state'] != 'bound':
            raise HelperError('TASK_NOT_BOUND', '浏览器分配需要当前连接中已核验的 bound 活动任务')


def _bound_windows(task_id):
    """用原生窗口列表为 verify_mapping 提供证据；调用方不能自报窗口。"""
    binding = TASK_BINDINGS.get(task_id) or {}
    handle = binding.get('appHandle')
    if not handle:
        # TASK_BINDINGS 只在进程内有效；host 重启后按持久记录的 pid 重新解析应用。
        try:
            with task_store() as data:
                record = data['tasks'].get(task_id) or {}
            pid = record.get('pid')
            if isinstance(pid, int):
                app = HELPER.request('getApp', {'selector': str(pid)})
                handle = (app or {}).get('appHandle') if isinstance(app, dict) else None
        except Exception:
            handle = None
    if not handle:
        return []
    try:
        windows = HELPER.request('listWindows', {'appHandle': handle})
    except Exception:
        return []
    rows = windows.get('windows') if isinstance(windows, dict) else windows
    if not isinstance(rows, list):
        return []
    return [{'windowId': row['windowId'], 'title': row.get('title') or ''}
            for row in rows if isinstance(row, dict) and isinstance(row.get('windowId'), int)]


_events_tool = make_browser_events_tool(lambda: TASK_FILE, validate_browser_assignment)
_events_handler = _events_tool['handler']


def _events_dispatch(args):
    global TASK_DISPATCH
    page_input = (isinstance(args, dict) and (args.get('action') == 'create_target' or
                  (args.get('action') == 'command' and
                   args.get('operation') in ('click', 'fill', 'select', 'scroll', 'navigate', 'keypress'))))
    unbound_create = False
    if page_input:
        task_id = args.get('taskId')
        if args.get('action') == 'create_target':
            with task_store() as data:
                record = data['tasks'].get(task_id)
                unbound_create = not record or record.get('windowId') is None
                if not unbound_create:
                    operation_log("browser.create_refused", taskId=task_id, windowId=record.get('windowId'), reason="target_already_bound")
                    raise HelperError('TARGET_ALREADY_BOUND', '任务已有固定窗口；复用既有标签页并查询 status/assign，不另开窗口。新建窗口需要新的显式任务。')
        if unbound_create:
            # 没有已绑定窗口：先确认浏览器身份与既有窗口集合，创建后只对唯一新窗口补共享。
            if record and record.get('interactionMode', 'background_browser') == 'background_browser':
                handle, before = None, None
            else:
                handle, before = browser_window_snapshot()
        else:
            binding = TASK_BINDINGS.get(task_id)
            if binding is None:
                raise HelperError('REQUIRES_REVALIDATION', '页面输入需要当前连接中的任务绑定')
            with task_store() as data:
                record = data['tasks'].get(task_id)
                if record is None or data.get('activeTaskId') != task_id:
                    raise HelperError('TASK_NOT_BOUND', '页面输入需要活动任务')
                window = record.get('windowId')
            validate_browser_assignment(task_id)
            context = dict(binding, windowId=window)
            TASK_DISPATCH = context
            assert_task_connection(context)
            # Preview is observational. An unavailable capture must not turn a
            # working background browser channel into foreground input.
            try:
                if record.get('interactionMode', 'background_browser') == 'desktop' or native_preview_matches(task_id):
                    ensure_background_preview(binding['appHandle'], window)
                else:
                    operation_log("browser.preview_waiting", taskId=task_id, reason="browser_mapping_required")
            except HelperError as exc:
                operation_log("browser.preview_waiting", taskId=task_id, reason=exc.code)
    if isinstance(args, dict) and args.get('action') == 'verify_mapping':
        args = dict(args, windows=_bound_windows(args.get('taskId')))
    if page_input and not unbound_create and CALL_RECORDS is not None:
        CALL_RECORDS.append({'method': 'pageCommand', 'target': {'appHandle': binding['appHandle'], 'windowId': window},
                             'dispatched': 'unknown', 'verified': 'unknown'})
    if unbound_create:
        operation_log("browser.create_requested", taskId=args.get('taskId'), reason="explicit_create_target")
        output = _events_handler(args)
        payload = json.loads(output) if isinstance(output, str) else output
        if isinstance(payload, dict) and payload.get('reused'):
            operation_log("browser.target_reused", taskId=args.get('taskId'), requestId=payload.get('requestId'))
            return output
        if handle is None:
            return json.dumps(dict(payload, sharePending="browser_mapping_required"), ensure_ascii=False)
        try:
            share_new_window(handle, before)
        except HelperError as exc:
            if isinstance(output, dict):
                output = dict(output, sharePending=exc.code)
            else:
                output = str(output) + '\n共享待补: %s: %s' % (exc.code, exc.message)
        return output
    output = _events_handler(args)
    if isinstance(args, dict) and args.get('action') == 'assign':
        # Assignment invalidates native mapping evidence. Never leave a previous
        # capture showing a different window while the new association is unknown.
        try:
            call_helper('preview', {'action': 'release'})
        except HelperError:
            pass
    if isinstance(args, dict) and args.get('action') in ('command', 'select_tab'):
        payload = json.loads(output) if isinstance(output, str) else output
        operation_log("browser.command_queued" if args['action'] == 'command' else "browser.logical_tab_selected",
                      taskId=args.get('taskId'), operation=args.get('operation'),
                      commandId=payload.get('commandId'), tabId=payload.get('tabId'),
                      target=diagnostic_fields(payload.get("target", {})), route="background_browser")
    if isinstance(args, dict) and args.get('action') == 'result':
        payload = json.loads(output) if isinstance(output, str) else output
        result = payload.get('result', {}) if isinstance(payload, dict) else {}
        if result.get('mimeType') == 'image/jpeg' and result.get('data'):
            metadata = dict(payload, result={k: v for k, v in result.items() if k != 'data'})
            return {"content": [{"type": "image", "mimeType": "image/jpeg", "data": result['data']},
                                {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}]}
    return output


TOOLS.append(dict(_events_tool, handler=_events_dispatch))
TOOL_MAP = dict((t["name"], t) for t in TOOLS)
HINTS = {
    "BACKGROUND_ONLY": "继续使用 cua_browser_events 的原任务、原标签；先检查 status，不能改用前台输入或新建窗口。",
    "TARGET_LOST": "原窗口不可用时保留任务状态，显式重新选择；不自动新建 Chrome 窗口。",
    "PERMISSION_DENIED": "给 \"workbuddy-cua\" 授予辅助功能/屏幕录制后再试。",
    "FOREGROUND_REQUIRED": "先观察确切目标与焦点；仅在明确需要且允许时请求激活，不自动切换窗口。",
    "STALE_SNAPSHOT": "快照已失效：重新 cua_state 后再操作。",
    "STALE_APP": "应用进程已变化：重新 cua_apps(action=\"get\")。",
    "WINDOW_UNAVAILABLE": "窗口不存在或已关闭：重新 cua_windows。",
    "CAPTURE_BUSY": "上一次屏幕捕获未结束：稍后重试截图，AX/输入仍可用。",
    "APP_NOT_ALLOWED": "CUA_ALLOWLIST 拦截：修改环境变量或换目标应用。",
    "SESSION_LOCKED": "屏幕已锁定：解锁后再操作。",
    "READ_ONLY": "当前 CUA_READ_ONLY=1，只允许观察类操作。",
}


def call_tool(name, args):
    global CALL_RECORDS, SHARE_RECORD, OPERATION_TRACE_ID
    with TOOL_LOCK:
        CALL_RECORDS = []
        SHARE_RECORD = None
        OPERATION_TRACE_ID = str(uuid.uuid4()) if name in OPERATION_TOOLS else None
        started = time.perf_counter()
        if OPERATION_TRACE_ID:
            operation_log("tool.started", tool=name, target=diagnostic_fields(args))
        try:
            result = _call_tool(name, args)
            records = CALL_RECORDS
            inputs = [r for r in records if r['method'] in INPUT_METHODS or r['method'] == 'pageCommand']
            last = inputs[-1] if inputs else None
            execution = ('unknown' if last and result.get('isError') else
                         ('dispatched' if last and last.get('dispatched') is True else
                          'not_dispatched' if last and last.get('dispatched') is False else
                          'unknown' if last else 'not_requested'))
            payload = None
            if name == _events_tool['name']:
                try:
                    payload = json.loads(result['content'][0]['text'])
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                if isinstance(payload, dict):
                    if payload.get('status') == 'queued':
                        execution = 'queued'
                    elif isinstance(args, dict) and args.get('action') == 'result':
                        execution = 'unknown'
                    elif not inputs:
                        execution = 'not_requested'
            if result.get('isError') and not inputs:
                execution = 'not_dispatched'
            structured = {"tool": name, "ok": not bool(result.get('isError')),
                          "execution": {"status": execution, "requests": records},
                          "verification": {"status": 'verified' if last and last.get('verified') is True and not result.get('isError') else
                                           'not_verified' if last and last.get('verified') is False else 'unknown'},
                          "retry": {"replaySideEffects": False, "guidance": "排队不等于执行；状态未知时先观察或查询既有命令，勿重放副作用。"}}
            error_code = result.pop('errorCode', None)
            if result.get('isError') and error_code is None:
                error_code = 'READ_ONLY' if READ_ONLY else 'UNKNOWN_TOOL' if name not in TOOL_MAP else 'BRIDGE_ERROR'
            structured['errorCode'] = error_code
            structured['suggestedNext'] = ('query_existing_result' if execution in ('queued', 'unknown') else
                                           'observe_target' if error_code else 'inspect_verification')
            if 'situation' in result:
                structured['situation'] = result.pop('situation')
            if SHARE_RECORD is not None:
                structured['share'] = SHARE_RECORD
            if isinstance(payload, dict):
                structured['page'] = {k: payload[k] for k in ('status', 'commandId', 'performed', 'verified', 'mayHaveExecuted') if k in payload}
            if OPERATION_TRACE_ID:
                directory = operation_log_directory()
                structured['diagnostics'] = {"enabled": os.environ.get("CUA_OPERATION_LOG") != "0", "toolTraceId": OPERATION_TRACE_ID,
                                             "bridgeLogPath": str(directory / "bridge-operations.jsonl"),
                                             "nativeLogPath": str(directory / "native-operations.jsonl")}
                operation_log("tool.completed", tool=name, ok=structured['ok'], errorCode=error_code,
                              execution=execution, elapsedMs=(time.perf_counter() - started) * 1000,
                              verification=structured['verification']['status'],
                              pointer=last.get('pointer') if last else None)
            result['structuredContent'] = structured
            return result
        finally:
            CALL_RECORDS = None
            SHARE_RECORD = None
            OPERATION_TRACE_ID = None


def _call_tool(name, args):
    global TASK_DISPATCH, WAIT_DEADLINE
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}
    if READ_ONLY and not tool["readOnly"]:
        return {"content": [{"type": "text", "text": "CUA_READ_ONLY=1，输入类工具已禁用"}], "isError": True}
    try:
        if name == "cua_wait_ready":
            timeout, _ = wait_ready_options(args)
            WAIT_DEADLINE = time.monotonic() + timeout
        guarded, context = task_guard(name, args or {})
        TASK_DISPATCH = context
        if name == "cua_situation" and (not isinstance(args, dict) or set(args) != {"appHandle", "windowId"}):
            raise HelperError("INVALID_ARGUMENT", "现场观察必须显式提供 appHandle 和 windowId")
        if name in ("cua_state", "cua_visual_state", "cua_screenshot", "cua_wait_ready"):
            if name == "cua_visual_state" and (set(guarded) - {"appHandle", "windowId"} or
                    ("appHandle" in guarded and (not isinstance(guarded["appHandle"], str) or not guarded["appHandle"].strip())) or
                    ("windowId" in guarded and (type(guarded["windowId"]) is not int or not 1 <= guarded["windowId"] <= 4294967295))):
                raise HelperError("INVALID_ARGUMENT", "视觉观察仅接受有效 appHandle/windowId")
            if READ_ONLY:
                raise HelperError("SHARE_REQUIRED", "界面观察需要共享目标应用；只读模式不允许启动共享，请使用 cua_situation")
            app = resolve_app(guarded.get("appHandle"))
            window = resolve_window(app, guarded.get("windowId"))
            ensure_share(app, window)
            guarded = dict(guarded, windowId=window)
        out = tool["handler"](guarded)
        if isinstance(out, dict):
            return out
        return {"content": [{"type": "text", "text": out}]}
    except HelperError as exc:
        text = "%s: %s" % (exc.code, exc.message)
        hint = HINTS.get(exc.code)
        if not hint and "absent from the selected snapshot" in exc.message:
            hint = "快照里没有该 index：重新观察 AX 或视频帧；空树不证明自绘或可安全点击，不能猜测坐标。"
        if not hint and "resolve window ID failed" in exc.message:
            hint = "重新 cua_windows 核对确切窗口、Space 与可见性；不要自动激活或换到其他窗口。"
        if hint:
            text += "\n建议: " + hint
        return {"content": [{"type": "text", "text": text}], "isError": True, "errorCode": exc.code}
    except ValueError as exc:
        if name != "cua_browser_events":
            return {"content": [{"type": "text", "text": "INVALID_ARGUMENT: %s" % exc}], "isError": True, "errorCode": "INVALID_ARGUMENT"}
        message = str(exc)
        code = next((c for c in ("BACKGROUND_WAITING", "STALE_SNAPSHOT") if message.startswith(c)), "BROWSER_TARGET_WAITING")
        return {"content": [{"type": "text", "text": message + "\n保持原任务与标签；等待扩展恢复或重新观察，禁止回落桌面输入。"}],
                "isError": True, "errorCode": code}
    except Exception as exc:  # noqa: BLE001 - surface anything as a tool error
        log("tool %s crashed: %s\n%s" % (name, exc, traceback.format_exc()))
        return {"content": [{"type": "text", "text": "BRIDGE_ERROR: %s: %s" % (type(exc).__name__, exc)}], "isError": True}
    finally:
        TASK_DISPATCH = None
        WAIT_DEADLINE = None


# ---------------------------------------------------------------------------
# JSON-RPC loop
# ---------------------------------------------------------------------------

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_version = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method and method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        public = [
            dict({"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]},
                 **({"annotations": t["annotations"]} if "annotations" in t else {}))
            for t in TOOLS
            if not (READ_ONLY and not t["readOnly"])
        ]
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": public}}

    if method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method in ("resources/list", "prompts/list"):
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []} if method.startswith("resources") else {"prompts": []}}

    if rid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def main():
    log("starting %s v%s helper=%s readOnly=%s" % (SERVER_NAME, SERVER_VERSION, HELPER_PATH, READ_ONLY))
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: %s" % exc}})
            continue
        if isinstance(msg, list):
            for item in msg:
                reply = handle(item)
                if reply:
                    send(reply)
            continue
        reply = handle(msg)
        if reply:
            send(reply)
    HELPER._stop()


if __name__ == "__main__":
    main()
