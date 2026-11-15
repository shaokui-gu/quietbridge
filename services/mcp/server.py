#!/usr/bin/env python3
"""MCP stdio bridge for the quietbridge-native macOS helper.

The host speaks MCP (JSON-RPC over stdio); the helper speaks a private
JSON-lines protocol over a stdin/stdout proxy. This process is the only new
piece: it translates one into the other, projects oversized AX payloads down
to a compact tree, and serialises requests because the helper allows a single
outstanding operation per connection.

Requires Python 3.9+ (system /usr/bin/python3). No third-party packages.
"""

import collections
import copy
import base64
import json
import os
import urllib.request
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
import runpy
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlsplit
import ipaddress
for _dependency in ('compatibility', 'lifecycle_store'):
    _loaded = sys.modules.get(_dependency)
    if _loaded is not None and Path(getattr(_loaded, '__file__', '')).parent != Path(__file__).parent:
        sys.modules.pop(_dependency, None)

from browser_events import application_session, config_dir, read_file, make_tool as make_browser_events_tool, TASK_FILE as SESSION_TASK_FILE, validate_render_probe, RENDER_PROBE_STAGES
from browser_events import resume_assignment as resume_browser_assignment
from lifecycle_store import stop_reason as host_stop_reason, host_binding
from compatibility import canonical_tool, environment, runtime_root
from recovery import GUIDANCE as RECOVERY_GUIDANCE, policy as recovery_policy
from preview_metadata import frame_progress, capture_lifecycle
from task_lifecycle import TaskOwner, ProtectionSession, SESSION_IDLE_SECONDS, lease_live
_adapter_root = Path(__file__).resolve().parents[2]
if (_adapter_root / 'adapters').is_dir():
    sys.path.insert(0, str(_adapter_root))
# Installed bridge adapters live beside server.py; direct script execution
# already includes that directory in sys.path.
# A stable connector retains imported packages across upgrades. Read this release's
# discovery text directly so an older adapters package cannot shadow the new copy.
_guidance_path = Path(__file__).parent / 'adapters/workbuddy/guidance.py'
if not _guidance_path.is_file():
    _guidance_path = _adapter_root / 'adapters/workbuddy/guidance.py'
_guidance = runpy.run_path(str(_guidance_path))
SELECTION_GUIDANCE, TOOL_SUMMARIES = _guidance['SELECTION_GUIDANCE'], _guidance['TOOL_SUMMARIES']

WAIT_DEADLINE = None

# 与扩展 host 共用相同的运行态解析规则，避免任务绑定落到开发树旧副本。
TASK_FILE = str(SESSION_TASK_FILE)
SESSION_KEY = os.environ.get('QUIETBRIDGE_SESSION_KEY', '')
SESSION_ROUTER = None


def startup_host_routing(session_key=None, environ=None):
    """Resolve the trusted DSH host identity once, before serving any call.

    A partial or invalid host environment fails connector startup (the host's
    failOnStartupError) instead of silently degrading to token routing. A
    per-session worker is spawned with QUIETBRIDGE_SESSION_KEY and never routes,
    so it keeps executing tools directly.
    """
    session_key = SESSION_KEY if session_key is None else session_key
    if session_key:
        return None
    from session_routing import host_context
    return host_context(environ)


HOST_ROUTING = startup_host_routing()
# Host mode (DSH) and token mode both enter SessionRouter; only a per-session
# worker executes a tool directly.
ROUTING_ENABLED = not bool(SESSION_KEY) or HOST_ROUTING is not None
CONNECTOR_REVALIDATE = os.environ.pop('QUIETBRIDGE_CONNECTOR_REEXEC', '') == '1'

def task_root():
    parent = Path(TASK_FILE).parent
    return parent.parent.parent if SESSION_KEY else parent
TASK_BINDINGS = {}  # 仅当前桥接内存中的 appHandle 与连接令牌，不作为持久身份。
TASK_DISPATCH = None
# 任务运行期间的防睡眠保护：锁屏本身不会终止进程，空闲睡眠才会。
# 只阻止空闲系统睡眠，不参与解锁；锁屏时的 GUI 操作仍由原生侧拒绝。
POWER_GUARD_SECONDS = 3600
POWER_GUARD_RENEW_AFTER = 30
POWER_GUARD = {"active": False, "nextRenewAt": 0.0}
SESSION_PROTECTION = ProtectionSession()
TOOL_LOCK = threading.RLock()
TASK_OWNER = TaskOwner()
REQUEST_CANCELLED = threading.Event()
TASK_WATCH_STOP = threading.Event()
TASK_WATCH_THREAD = None

DEFAULT_HELPER = str(Path(__file__).resolve().parents[2] / 'apps/macos/QuietBridge.app/Contents/MacOS/quietbridge-native')
HELPER_PATH = environment("QUIETBRIDGE_HELPER_PATH", DEFAULT_HELPER)
SERVER_NAME = "quietbridge"
SERVER_VERSION = "1.3.0"
DEFAULT_PROTOCOL = "2024-11-05"
LOGO_PATH = Path(__file__).with_name('assets') / 'logo-64.png'
TOOL_ICONS = ([{'src': 'data:image/png;base64,' + base64.b64encode(LOGO_PATH.read_bytes()).decode('ascii'),
                'mimeType': 'image/png', 'sizes': ['64x64']}]
              if LOGO_PATH.is_file() else [])

BROWSER_ROUTING_GUIDANCE = (
    "浏览器按操作对象选路：网页正文、链接、表单、页面滚动、导航和页面按键用 Chrome 扩展，"
    "task begin 指定 targetSurface=web_content（自动选 background_browser）。"
    "地址栏、浏览器工具栏等非网页控件用 targetSurface=browser_ui；真实打开/保存等原生对话框用 native_dialog，"
    "这两者自动选 desktop，仅在已核实窗口、共享及后台投递可用时操作。导航网址仍优先扩展 navigate，不为导航切到地址栏。"
    "网页中的弹窗、登录框、跨域 frame、空 AX 树以及 chrome:// 等受限页面不因此变成原生对话框。"
    "原生路由不保证浏览器菜单或系统弹窗都可后台操作；需要前台、应用级快捷键或无法核实目标时通知用户处理。"
    "按真实操作阶段选路，不能因扩展断开、权限拒绝、超时或快照缺元素而降级为原生网页输入。"
    "飞书等 Electron 桌面应用的内嵌网页不是 Chrome 扩展标签；按 desktop_app 核实其实际支持的 AX 或窗口定向能力，"
    "不能直接 assign Chrome 标签或用扩展 fill；后台控件不响应时按能力阻塞处理，不以增加系统权限或切到当前桌面保证恢复。"
    "合法跨界面阶段先核实上一动作结果，保存原标签身份并 end 自己的阶段，再 begin/bind 真实新目标；"
    "返回网页时重新核实原标签、assign 和 mapping，不复用原生快照或重放未知输入，不结束宿主会话保护。"
)
TARGET_SURFACE_MODES = {
    "web_content": "background_browser", "browser_ui": "desktop",
    "native_dialog": "desktop", "desktop_app": "desktop",
}


def task_interaction_mode(args):
    surface = args.get('targetSurface')
    if 'targetSurface' in args and (not isinstance(surface, str) or surface not in TARGET_SURFACE_MODES):
        raise HelperError('INVALID_ARGUMENT', 'targetSurface 必须为 web_content、browser_ui、native_dialog 或 desktop_app')
    mode = args.get('interactionMode', TARGET_SURFACE_MODES.get(surface, 'background_browser'))
    if mode not in ('background_browser', 'desktop'):
        raise HelperError('INVALID_ARGUMENT', 'interactionMode 仅允许 background_browser 或 desktop')
    if surface is not None and mode != TARGET_SURFACE_MODES[surface]:
        raise HelperError('INVALID_ARGUMENT', 'targetSurface 与 interactionMode 冲突；网页不能声明为 desktop，原生控件不能声明为 background_browser')
    return mode


def task_reply(record, **details):
    """Expose the chosen channel without adding runtime handles to task storage."""
    extension = record.get('interactionMode', 'background_browser') == 'background_browser'
    routing = {
        'targetSurface': record.get('targetSurface', 'web_content' if extension else 'desktop_app'),
        'scopeSource': 'explicit_surface' if 'targetSurface' in record else 'interaction_mode',
        'channel': 'chrome_extension' if extension else 'native_window',
        'focusPolicy': 'preserve', 'automaticFallbackAllowed': False,
        'requiresWindowShare': True, 'requiresTargetEvidence': True,
        'extensionTabRequired': extension,
    }
    if record.get('state') == 'suspended' and IDLE_RESUME and TASK_OWNER.owns(record):
        details['resumption'] = {'automatic': True, 'requiresObservation': True,
            'nextSteps': [{'tool': 'quietbridge_windows', 'arguments': {
                'action': 'share', 'appHandle': IDLE_RESUME['binding']['appHandle'],
                'windowId': record['windowId'], 'source': (IDLE_RESUME['share'] or {}).get('source', 'window')}}]}
    return json.dumps(dict(record, routing=routing, **details), ensure_ascii=False)


AGENT_INSTRUCTIONS = SELECTION_GUIDANCE + "\n\n" + """QuietBridge 用于保持用户正常使用电脑的后台界面操作。所有界面操作必须遵守：
1. 不抢焦点：保持用户的前台应用、键盘窗口、物理鼠标和当前 Space。禁止调用 activate、AXRaise、置前窗口、切换 Space 或全局 HID 输入来促成操作；也禁止先抢焦点再自动恢复。无法在后台完成时报告阻塞并等待，不更换自动化通道绕过。
2. 先按操作对象选择 targetSurface，再核实权限和真实目标、创建并绑定 quietbridge_task；普通桌面应用用 desktop_app，网页内容用 web_content，浏览器自身控件用 browser_ui，真实原生对话框用 native_dialog。自动选择对应的 desktop 或 background_browser；始终复用已确认的 appHandle/windowId 或 browserWindowId/tabId，不按前台焦点换目标，不把失败的网页任务改为桌面任务。
3. 使用共享窗口能力：原生界面操作前调用 quietbridge_windows(action=share, appHandle=..., windowId=..., source=window)，确认目标窗口及 streamReady=true。后续观察和输入会自动核验并续租，复用同一捕获流，不要为每次点击重复 unshare/share。共享预览只负责展示，不提供独立桌面或焦点隔离；仍须使用窗口定向输入。
4. 网页只走 quietbridge_browser_events：先 bind、用真实候选 assign，再 verify_mapping 核验系统窗口与标签的对应关系，随后为该窗口建立共享预览。映射或共享未就绪时报告并等待，不展示未经核验的窗口，不激活浏览器或回落原生键鼠；标签 screenshot 是观察证据，不能代替实时共享窗口。
5. 原生输入前必须有同一目标的最新快照或图像证据；坐标使用窗口内 point。后台点击走窗口定向事件，AX 点击仅用于已经处于前台键盘焦点的同一窗口。桌面任务拒绝没有窗口级确认的 type_text/paste/key，使用已确认且可写的 AX 元素；网页填写用扩展 fill。
6. 串行派发并读回核实。dispatched/performed 不代表成功；点击回执中的 focusPreserved=false 或 null 时暂停后续输入并核实原因，不能直接重复点击或自动恢复焦点。FOCUS_STATE_UNKNOWN、FOREGROUND_REQUIRED、共享或窗口投递错误均不得通过激活窗口绕过。超时或执行状态未知时先观察或查询既有命令，不重放副作用。结束时 quietbridge_task(action=end, confirm=true) 释放共享租约。"""
AGENT_INSTRUCTIONS += "\n准备检查无需先 begin：permissions 的 browserChannel 表示扩展全局心跳与授权；无活动任务的 candidates 返回 connectionScope=extension_preflight、clientVersion 和候选元数据，taskChannelReady=false 不表示扩展离线，也不授权输入或共享。begin 后必须使用自己的会话通道；channel_pending/BROWSER_CHANNEL_STARTING 时保留原任务，只读复查 candidates 最多两次，不要求用户重载扩展、不重建目标。全局候选不能替代会话内 assign 的候选证据。"
AGENT_INSTRUCTIONS += "\n7. " + RECOVERY_GUIDANCE
AGENT_INSTRUCTIONS += "\n8. quietbridge_task end 仅结束当前界面操作阶段并释放绑定和共享预览；阶段切换和短暂停顿不结束会话锁屏保护。end 返回 previewCleanup；released 仅确认面板/租约释放，捕获停止需看 captureStop.stopState=completed、stopConfirmed=true 或对应 captureStopCompleted 日志；requested/failed/缺失不能称捕获已确认退出。failed/unconfirmed 时通知用户清理未确认，不把 state=ended 当作共享已关闭。同一宿主本轮可 begin/bind 多个界面阶段。120 秒无实际界面操作时释放共享并将原绑定暂停为 suspended；同一存活 worker 的下次原目标调用自动核验恢复，TASK_SUSPENDED/TASK_RESUMED 要立即按 resumption.nextSteps 重建共享与新快照，继续已授权工作，不等待用户提醒重连。恢复调用不派发输入，旧引用失效，未知结果不重放；恢复失败只读复查最多两次，再说明具体阻塞并按 recovery.userDecision 弹框等待选择。ping、权限/任务状态查询和扩展心跳不续期也不唤起共享。宿主 FinalStop/SessionEnd、取消、断连或退出结束会话保护及自动恢复意图；已核实宿主归属的会话保护允许最长 30 分钟无实际工具活动，未核实归属时保留 120 秒兜底。TASK_CANCELLED 或 interruption.explicitStop=true 是明确停止，不弹继续选择、不自动重建。其他 TASK_INTERRUPTED 仅结束旧界面阶段，保留业务进度并用 Agent 交互工具让用户选择处理后继续/保持暂停/结束；不能复活旧任务。TARGET_MISMATCH 只说明目标不一致；先查看状态，不自动结束或接管其他任务。"
AGENT_INSTRUCTIONS += "\n9. QuietBridge 最多同时运行三个独立会话。宿主 hook 自动提供可信逐调用身份，Agent 不填写会话 ID 或 _quietbridgeRoute。每项复用自己的 taskId、appHandle、窗口/标签、快照和命令结果；卡片切换只是用户查看画面，不能据此变更操作目标。SESSION_CAPACITY_FULL/TARGET_IN_USE 时通知用户并等待，不能结束或接管其他会话。同一个原生应用进程的不同窗口暂按进程互斥；浏览器使用不同专用窗口。某项停止、超时或执行进程退出后只处理本项，禁止重启整个连接器影响其他项。"
AGENT_INSTRUCTIONS += "\n10. " + BROWSER_ROUTING_GUIDANCE

def shots_directory():
    """Screenshots must land somewhere the signed app bundle never owns."""
    override = environment("QUIETBRIDGE_DIAGNOSTICS_DIR")
    if override:
        return os.path.join(override, "shots")
    return os.path.join(str(runtime_root()), "shots")


SHOTS_DIR = shots_directory()
READ_ONLY = environment("QUIETBRIDGE_READ_ONLY", "").strip() in ("1", "true", "yes")
CONTROL_URL = environment("QUIETBRIDGE_CONTROL_URL", "http://127.0.0.1:47631/")
CAPABILITIES = {
    "quietbridge_situation": "native_observe", "quietbridge_state": "native_observe", "quietbridge_visual_state": "native_observe",
    "quietbridge_screenshot": "native_observe", "quietbridge_wait_ready": "native_observe",
    "quietbridge_click": "native_input", "quietbridge_hover": "native_input", "quietbridge_scroll": "native_input", "quietbridge_drag": "native_input",
    "quietbridge_type_text": "native_input", "quietbridge_paste": "native_input", "quietbridge_key": "native_input", "quietbridge_set_value": "native_input",
}
ALLOWLIST = [x.strip().lower() for x in environment("QUIETBRIDGE_ALLOWLIST", "").split(",") if x.strip()]
MAX_TREE_CHARS = 24000

STATE = {
    "app": None,          # last appHandle
    "apps": {},           # appHandle -> {name, pid, bundleId}
    "window": {},         # appHandle -> last windowId
    "snapshot": None,     # last snapshotId
    "snapshot_app": None,
    "snapshot_window": None,
}


def control_snapshot():
    """Only the live application's current session can enable capabilities."""
    session = application_session()
    if session is None:
        return None
    try:
        with urllib.request.urlopen(CONTROL_URL, timeout=0.15) as response:
            payload = json.loads(response.read(4096).decode())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if (isinstance(payload, dict) and isinstance(payload.get('_service'), dict) and
                       payload['_service'].get('applicationSession') == session) else None


def capability_block(name, args, switches):
    if not isinstance(switches, dict) or type(switches.get('master')) is not bool:
        return 'application'
    if switches.get("master") is False and name != "quietbridge_permissions":
        return "master"
    key = CAPABILITIES.get(name)
    if name == "quietbridge_apps" and isinstance(args, dict) and args.get("action") == "launch":
        key = "app_launch"
    if name == "quietbridge_windows" and isinstance(args, dict) and args.get("action") in ("activate", "share", "unshare"):
        key = "native_input"
    if name == "quietbridge_browser_session" and isinstance(args, dict) and args.get("action") in ("start", "attach"):
        key = "app_launch"
    if name == "quietbridge_browser_events" and isinstance(args, dict) and args.get("action") in ("command", "assign", "create_target", "select_tab"):
        key = "browser"
    if key and switches.get(key) is False:
        return key
    return None


def log(message):
    sys.stderr.write("[quietbridge-bridge] %s\n" % message)
    sys.stderr.flush()


OPERATION_METHODS = {"click", "hover", "scroll", "drag", "getState", "screenshot", "visualState", "listWindows", "windowIdentity", "browserSession", "preview"}
OPERATION_TOOLS = {"quietbridge_click", "quietbridge_hover", "quietbridge_scroll", "quietbridge_drag", "quietbridge_state", "quietbridge_screenshot", "quietbridge_visual_state", "quietbridge_wait_ready", "quietbridge_windows", "quietbridge_browser_events", "quietbridge_task", "quietbridge_browser_session"}
OPERATION_TRACE_ID = None
OPERATION_LOG_LOCK = threading.Lock()
OPERATION_LOG_MAX_BYTES = 5 * 1024 * 1024


def operation_log_directory():
    if environment("QUIETBRIDGE_DIAGNOSTICS_DIR"):
        return Path(os.environ["QUIETBRIDGE_DIAGNOSTICS_DIR"])
    if environment("QUIETBRIDGE_CONFIG_DIR"):
        return Path(os.environ["QUIETBRIDGE_CONFIG_DIR"]) / "logs"
    return Path.home() / "Library/Logs/quietbridge"


def diagnostic_fields(params):
    """Allowlist geometry/identifiers; never serialize raw tool parameters or UI contents."""
    if not isinstance(params, dict):
        return {}
    numbers = {"windowId", "elementIndex", "x", "y", "fromX", "fromY", "toX", "toY", "clickCount", "holdMs", "deltaLinesX", "deltaLinesY", "durationMs", "steps"}
    identifiers = {"appHandle", "snapshotId", "mouseButton", "action", "source", "taskId", "operation", "commandId", "documentId", "interactionMode", "targetSurface"}
    numbers.update({"browserWindowId", "tabId", "navigationEpoch", "deltaY", "lines"})
    fields = {key: value[:256] if isinstance(value, str) else value for key, value in (params or {}).items()
            if (key in numbers and diagnostic_number(value)) or
               (key in identifiers and isinstance(value, str))}
    if params.get('direction') in ('up', 'down'):
        fields['direction'] = params['direction']
    if params.get('stage') in RENDER_PROBE_STAGES:
        fields['stage'] = params['stage']
    return fields


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
        for key in ("panelSpaceVerified", "panelOrderVerified", "panelArtworkVisible", "hostOnCurrentSpace", "panelOnCurrentSpace", "targetOnCurrentSpace",
                    "visible", "streamReady", "waitingForTargetDesktop", "videoLayerReady", "videoLayerFailed"):
            if type(preview.get(key)) is bool:
                safe[key] = preview[key]
        if preview.get('panelOrderState') in ('verified', 'repairing', 'unavailable'):
            safe['panelOrderState'] = preview['panelOrderState']
        for key in ("hostSpaceIds", "panelSpaceIds"):
            values = preview.get(key)
            if isinstance(values, list) and len(values) <= 64 and all(type(v) is int and v > 0 for v in values):
                safe[key] = values
        for key in ("mode", "captureMode"):
            if preview.get(key) in ("window", "application", "desktop", "browser_content"):
                safe[key] = preview[key]
        for key in ("frameAgeMs", "videoFrames"):
            if diagnostic_number(preview.get(key)) and preview[key] >= 0:
                safe[key] = preview[key]
        progress = preview.get('frameProgress')
        if isinstance(progress, dict):
            safe['frameProgress'] = frame_progress(progress)
        for key in ('captureLifecycle', 'captureStop'):
            if isinstance(preview.get(key), dict):
                safe[key] = capture_lifecycle(preview[key])
        fields["preview"] = safe
    return fields


def operation_log(event, **fields):
    if environment("QUIETBRIDGE_OPERATION_LOG") == "0":
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
    def __init__(self, code, message, *, target_conflict=None, interruption=None):
        Exception.__init__(self, "%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.target_conflict = target_conflict
        self.interruption = interruption


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
            if text.startswith(("[quietbridge-screenshot]", "[cua-screenshot]")):
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
                cancellable_wait(0.15)

    def _request_once(self, method, params, timeout):
        check_request_cancelled()
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
                    if REQUEST_CANCELLED.is_set():
                        self._stop()
                        raise HelperError('TASK_CANCELLED', '宿主取消或断开连接，已停止原生请求；可能已执行的输入不重放')
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
    "windowIdentity": 15,
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
IDLE_RESUME = None  # Only this live worker may resume its own idle-suspended target.


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


def browser_channel_state():
    """Extension presence, not a task binding: fresh heartbeat plus consent."""
    import browser_events
    try:
        return browser_events.extension_presence(Path(task_root()) / 'browser-events.json')
    except (ValueError, OSError, TypeError):
        return False


def native_preview_matches(task_id):
    """A browser tab lease alone does not prove a WindowServer capture target."""
    from browser_events import status
    try:
        state = status(task_id, task_file=TASK_FILE)
    except (ValueError, OSError):
        return False
    return state.get('mappingVerified') is True and state.get('crossCheck', {}).get('consistent') is True


def stop_browser_preview():
    """Remove legacy experimental preview metadata without native I/O."""
    import browser_events as events
    path = Path(TASK_FILE).with_name('browser-events.json')
    if path.exists():
        try:
            with events.event_store(path) as data:
                for key in ('windowPreview', 'navigationCapture', 'previewLease', 'previewFrame'):
                    data.setdefault('peer', {}).pop(key, None)
        except (ValueError, OSError):
            pass


def selected_share_source(app, window):
    selection = SHARE_SELECTION or {}
    if (selection.get('appHandle') == app and selection.get('windowId') == window and
            selection.get('connection') == current_connection() and HELPER._alive()):
        return selection.get('source')
    return None


def ensure_background_preview(app, window):
    """Reuse the continuous native window stream for Chrome page commands."""
    return ensure_share(app, window, require_frame=False)


def ensure_share(app, window, source=None, duration=600, require_frame=True):
    """应用内默认只共享该应用；显式桌面范围在同一目标的后续动作中保留。"""
    global SHARE_RECORD, SHARE_SELECTION
    if source is None:
        source = selected_share_source(app, window)
    # Screenshot-mailbox previews are retained only for protocol compatibility.
    # Product sharing always uses ScreenCaptureKit window/application streams.
    stop_browser_preview()
    if source is not None:
        SHARE_SELECTION = None
    if source is None:
        selection = SHARE_SELECTION or {}
        same_target = (selection.get("appHandle") == app and selection.get("windowId") == window
                       and selection.get("connection") == current_connection() and HELPER._alive())
        source = selection["source"] if same_target else "application"
    try:
        connection, preview = request_share(app, window, source, duration, require_frame=require_frame)
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
    if source == "application" and preview.get("scope") == "window":
        # SpacePreview already routes an application on a background/unknown Space
        # to window capture. Keep the requested mode: another window hold would
        # tear down that ready stream, then switch it back on the next action.
        record = {"source": "window", "scope": "window",
                  "requestedSource": "application",
                  "targetOnCurrentSpace": preview.get("targetOnCurrentSpace"),
                  "reason": ("target_on_background_space: 应用流不含后台 Space，复用目标窗口共享"
                             if preview.get("targetOnCurrentSpace") is False else
                             "application_window_capture: Space 归属未确认，复用原生端的目标窗口共享")}
    if CALL_RECORDS is not None:
        CALL_RECORDS.append({"method": "share", "target": {"appHandle": app, "windowId": window},
                             "share": record})
    SHARE_RECORD = record
    SHARE_SELECTION = {"appHandle": app, "windowId": window, "connection": connection, "source": source}
    return connection


def call_helper(method, params=None, **kwargs):
    check_request_cancelled()
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
        if method == "setValue" and result.get("valueReadback") in ("match", "mismatch", "unavailable"):
            record["valueReadback"] = result["valueReadback"]
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


def check_request_cancelled():
    if REQUEST_CANCELLED.is_set():
        raise HelperError('TASK_CANCELLED', '当前调用已取消，未派发后续操作')


def cancellable_wait(seconds):
    REQUEST_CANCELLED.wait(seconds)
    check_request_cancelled()


def interrupt_task(reason):
    """Only this bridge's task/socket is revoked; never start a helper to clean up."""
    global SHARE_SELECTION, IDLE_RESUME
    with TOOL_LOCK:
        IDLE_RESUME = None
        stopping_id = None
        try:
            if TASK_OWNER.fd is not None and os.path.lexists(TASK_FILE):
                with task_store() as data:
                    record = data['tasks'].get(data.get('activeTaskId'))
                    if record and TASK_OWNER.owns(record) and record['state'] not in ('ended', 'interrupted'):
                        stopping_id = record['taskId']
                        record.update(state='stopping', interruptionReason=reason, interruptedAt=time.time())
                        TASK_BINDINGS.pop(record['taskId'], None)
        finally:
            SHARE_SELECTION = None
            stop_browser_preview()
            STATE.update(snapshot=None, snapshot_app=None, snapshot_window=None)
            POWER_GUARD.update(active=False, nextRenewAt=0.0)
            SESSION_PROTECTION.clear()
            HELPER._stop()
            if stopping_id is not None:
                with task_store() as data:
                    record = data['tasks'].get(stopping_id)
                    if record and TASK_OWNER.owns(record) and record['state'] == 'stopping':
                        record['state'] = 'interrupted'
                        operation_log('task.interrupted', taskId=stopping_id, reason=reason)


def suspend_idle_task(reason='idle_timeout', retain_protection=False):
    """Release idle resources while retaining only this worker's target consent."""
    global IDLE_RESUME, SHARE_SELECTION
    with task_store() as data:
        record = data['tasks'].get(data.get('activeTaskId'))
        if not record or not TASK_OWNER.owns(record):
            return
        binding = TASK_BINDINGS.get(record['taskId'])
        if record['state'] == 'bound' and binding:
            browser = read_file(Path(TASK_FILE).with_name('browser-events.json'), {}).get('tasks', {}).get(record['taskId'])
            IDLE_RESUME = {'record': copy.deepcopy(record), 'binding': copy.deepcopy(binding),
                           'share': copy.deepcopy(SHARE_SELECTION),
                           'browser': copy.deepcopy(browser or binding.get('browserTarget')),
                           'applicationSession': application_session(), 'nativeRetained': retain_protection}
            record.update(state='suspended', suspensionReason=reason, suspendedAt=time.time())
            TASK_BINDINGS.pop(record['taskId'], None)
            operation_log('task.suspended', taskId=record['taskId'], reason=reason,
                          automaticResume=True, protectionRetained=retain_protection)
        elif record['state'] != 'suspended' or IDLE_RESUME is None:
            return
    STATE.update(snapshot=None, snapshot_app=None, snapshot_window=None)
    if retain_protection:
        release_task_preview()
    else:
        SHARE_SELECTION = None
        stop_browser_preview()
        POWER_GUARD.update(active=False, nextRenewAt=0.0)
        SESSION_PROTECTION.clear()
        HELPER._stop()


def resume_idle_task(name, args):
    """Revalidate on new work, never from a timer or by replaying the triggering call."""
    global IDLE_RESUME, SHARE_SELECTION
    eligible = (name in TASK_TARGET_TOOLS - {'quietbridge_situation'} or
                name == 'quietbridge_windows' and args.get('action') == 'share' or
                name == 'quietbridge_browser_events' and args.get('action') in ('command', 'result', 'assign', 'verify_mapping'))
    saved = IDLE_RESUME
    if not saved or not eligible:
        return None
    if READ_ONLY:
        return None
    original = saved['record']
    task_id, window = original['taskId'], original['windowId']
    with task_store() as data:
        record = data['tasks'].get(data.get('activeTaskId'))
        if not record or record.get('taskId') != task_id or record.get('state') != 'suspended' or not TASK_OWNER.owns(record):
            IDLE_RESUME = None
            return None
        if (args.get('taskId', task_id) != task_id or args.get('windowId', window) != window or
                args.get('appHandle', saved['binding']['appHandle']) != saved['binding']['appHandle']):
            raise HelperError('TARGET_MISMATCH', '等待恢复仅允许原任务和原窗口，未连接其他目标')
        if saved['applicationSession'] != application_session():
            IDLE_RESUME = None
            raise HelperError('REQUIRES_REVALIDATION', 'QuietBridge 应用运行会话已改变，不能自动恢复旧任务')
        stopped = host_stop_reason(record, task_root())
        if stopped:
            raise HelperError('TASK_INTERRUPTED', '宿主已经停止，未恢复共享或派发输入',
                              interruption={'explicitStop': True, 'reason': stopped})
    check_request_cancelled()
    if saved.get('nativeRetained') and HELPER._alive():
        try:
            # Native and bridge timers can expire in either order. A revoked
            # idle socket cannot reacquire preview; rotate just that socket.
            HELPER.request('preview', {'action': 'renew'}, timeout=3, retry_safe=False)
        except HelperError as error:
            if error.code != 'TASK_INTERRUPTED':
                raise
            HELPER._stop()
            POWER_GUARD.update(active=False, nextRenewAt=0.0)
        saved['nativeRetained'] = False
    # Resolve a fresh handle; an idle native socket may already be revoked.
    app = call_helper('getApp', {'selector': str(original['pid'])}) or {}
    handle = app.get('appHandle')
    if not handle or app.get('pid') != original['pid']:
        raise HelperError('WINDOW_UNAVAILABLE', '原应用进程已不可用，未选择其他应用')
    identity = call_helper('windowIdentity', {'appHandle': handle, 'windowId': window}) or {}
    if (identity.get('pid') != original['pid'] or identity.get('windowId') != window or
            identity.get('processIdentity') != original['processIdentity']):
        IDLE_RESUME = None
        with task_store() as data:
            data['tasks'][task_id]['state'] = 'invalidated'
        raise HelperError('TASK_INVALIDATED', '原窗口或进程代际已经变化，未恢复共享')
    check_request_cancelled()
    stopped = host_stop_reason(original, task_root())
    if stopped:
        raise HelperError('TASK_INTERRUPTED', '恢复期间宿主已停止，未恢复共享',
                          interruption={'explicitStop': True, 'reason': stopped})
    STATE['apps'][handle] = {key: app.get(key) for key in ('pid', 'name', 'bundleId')}
    restored = copy.deepcopy(original)
    restored['state'] = 'bound'
    if original.get('interactionMode') != 'desktop':
        native = call_helper('browserSession', {'action': 'attach', 'pid': original['pid'],
                            'windowId': window, 'confirm': True}) or {}
        if (native.get('state') != 'attached' or native.get('running') is not True or
                native.get('processIdentity') != original['processIdentity'] or
                native.get('windowId') != window or native.get('pid') != original['pid'] or
                not native.get('sessionId') or not any(w.get('windowId') == window for w in native.get('windows', []))):
            raise HelperError('ATTACH_UNVERIFIED', '原 Chrome 窗口重新核验未通过，未恢复共享')
        restored['sessionId'] = native['sessionId']
    TASK_OWNER.renew(restored)
    with task_store() as data:
        current = data['tasks'].get(data.get('activeTaskId'))
        if current is None or current['taskId'] != task_id or current['state'] != 'suspended' or not TASK_OWNER.owns(current):
            raise HelperError('TASK_INTERRUPTED', '恢复期间任务归属已变化，未恢复共享')
        check_request_cancelled()
        require_protection_scope(current)
        from session_registry import claim_target
        try:
            claim_target(task_root(), SESSION_KEY or TASK_OWNER.id, restored, TASK_FILE)
        except ValueError as error:
            raise HelperError(str(error).split(':', 1)[0], str(error),
                              target_conflict=getattr(error, 'conflict', None))
        data['tasks'][task_id] = restored
    TASK_BINDINGS[task_id] = {'appHandle': handle, 'connection': current_connection(), 'requiresObservation': True}
    STATE.update(app=handle, snapshot=None, snapshot_app=None, snapshot_window=None)
    SHARE_SELECTION = None
    share_args = {'action': 'share', 'appHandle': handle, 'windowId': window,
                  'source': (saved['share'] or {}).get('source', 'window')}
    next_steps = []
    try:
        if original.get('interactionMode') == 'desktop':
            hold_power_guard(restored)
            ensure_share(handle, window, source=share_args['source'], require_frame=False)
            next_steps.append({'tool': 'quietbridge_state', 'arguments': {'appHandle': handle, 'windowId': window}})
        else:
            if saved['browser']:
                resume_browser_assignment(saved['browser'], restored, TASK_FILE,
                                          Path(task_root()) / 'browser-events.json' if SESSION_KEY else None)
                TASK_BINDINGS[task_id]['browserTarget'] = copy.deepcopy(saved['browser'])
            else:
                next_steps.append({'tool': 'quietbridge_browser_events', 'arguments': {'action': 'candidates', 'taskId': task_id}})
            next_steps.extend([
                {'tool': 'quietbridge_browser_events', 'arguments': {'action': 'verify_mapping', 'taskId': task_id}},
                {'tool': 'quietbridge_windows', 'arguments': share_args},
                {'tool': 'quietbridge_browser_events', 'arguments': {'action': 'command', 'taskId': task_id, 'operation': 'snapshot'}}])
    except (HelperError, ValueError, OSError):
        # A failed recovery stays suspended, without a writable task or a
        # partly restored preview. Only a subsequent caller may try again.
        with task_store() as data:
            current = data['tasks'].get(task_id)
            if current and current.get('state') == 'bound' and TASK_OWNER.owns(current):
                current.update(state='suspended', suspensionReason='resume_failed', lease=original['lease'])
        TASK_BINDINGS.pop(task_id, None)
        release_task_preview()
        raise
    IDLE_RESUME = None
    operation_log('task.resumed', taskId=task_id, windowId=window, inputDispatched=False)
    return {'taskId': task_id, 'appHandle': handle, 'windowId': window, 'nextSteps': next_steps,
            'inputDispatched': False, 'requiresObservation': True}


def reap_task_lease():
    if TASK_OWNER.fd is None or not os.path.lexists(TASK_FILE):
        return
    expired = False
    with task_store() as data:
        record = data['tasks'].get(data.get('activeTaskId'))
        expired = bool(record and record['state'] not in ('ended', 'interrupted', 'suspended', 'invalidated') and TASK_OWNER.expired(record))
    if expired:
        binding = protection_host_binding()
        if record.get('state') == 'bound' and TASK_BINDINGS.get(record['taskId']):
            suspend_idle_task(retain_protection=SESSION_PROTECTION.record is not None and SESSION_PROTECTION.remaining(binding) > 0)
        elif SESSION_PROTECTION.record is not None and SESSION_PROTECTION.remaining(binding) > 0:
            # An idle interface phase cannot end the surrounding host run.
            with task_store() as data:
                record = data['tasks'].get(data.get('activeTaskId'))
                if record and TASK_OWNER.owns(record) and TASK_OWNER.expired(record):
                    record.update(state='interrupted', interruptionReason='idle_timeout', interruptedAt=time.time())
                    TASK_BINDINGS.pop(record['taskId'], None)
                    operation_log('task.interrupted', taskId=record['taskId'], reason='idle_timeout', protectionRetained=True)
            release_task_preview()
        else:
            interrupt_task('idle_timeout')


def protection_host_binding():
    if SESSION_PROTECTION.record is None:
        return None
    return host_binding(SESSION_PROTECTION.record, task_root())


def protection_status():
    binding = protection_host_binding()
    return {'retained': SESSION_PROTECTION.record is not None and POWER_GUARD['active'],
            'scope': 'host_run' if binding is not None else 'bounded_connector',
            'idleTimeoutSeconds': SESSION_IDLE_SECONDS if binding is not None else 120,
            'remainingSeconds': round(SESSION_PROTECTION.remaining(binding))}


def reap_protection_session():
    if SESSION_PROTECTION.record is None:
        return
    binding = protection_host_binding()
    if binding is not None and binding.get('stoppedAt') is not None:
        interrupt_task(binding.get('reason', 'host_turn_ended'))
    elif SESSION_PROTECTION.remaining(binding) <= 0:
        suspend_idle_task('session_idle_timeout')
        if SESSION_PROTECTION.record is not None:
            interrupt_task('session_idle_timeout')
    elif not HELPER._alive():
        interrupt_task('native_connection_lost')
    elif time.monotonic() >= POWER_GUARD['nextRenewAt']:
        # Native renewal is subordinate to a bounded, acknowledged host run.
        # It neither invents host activity nor renews the preview/task deadline.
        hold_power_guard()


def pending_host_stop():
    if SESSION_PROTECTION.record is not None:
        return host_stop_reason(SESSION_PROTECTION.record, task_root())
    if TASK_OWNER.fd is None or not os.path.lexists(TASK_FILE):
        return None
    data = read_file(TASK_FILE, {})
    record = data.get('tasks', {}).get(data.get('activeTaskId'))
    if record and TASK_OWNER.owns(record) and record['state'] not in ('ended', 'interrupted'):
        return host_stop_reason(record, task_root())
    return None


def renew_task_lease(name, args):
    relevant = name in TASK_TARGET_TOOLS or (name == 'quietbridge_windows' and args.get('action') in ('share', 'unshare')) or (
        name == 'quietbridge_browser_events' and args.get('action') not in ('status', 'candidates', 'annotations'))
    if not relevant or not os.path.lexists(TASK_FILE):
        return
    renewed = False
    with task_store() as data:
        record = data['tasks'].get(data.get('activeTaskId'))
        if (record and record['state'] not in ('ended', 'interrupted') and TASK_OWNER.owns(record) and
                args.get('taskId', record['taskId']) == record['taskId']):
            TASK_OWNER.renew(record)
            renewed = True
    # Do not hold the task file lock during native I/O or cleanup.
    if renewed and name == 'quietbridge_browser_events' and HELPER._alive():
        try:
            HELPER.request('preview', {'action': 'renew'}, timeout=3, retry_safe=False)
        except HelperError:
            # Preserve the browser receipt: it may already have executed.
            interrupt_task('native_lease_renewal_failed')


def start_task_watch():
    global TASK_WATCH_THREAD
    if TASK_WATCH_THREAD is not None and TASK_WATCH_THREAD.is_alive():
        return
    def watch():
        while not TASK_WATCH_STOP.wait(0.5):
            if SESSION_ROUTER is not None:
                SESSION_ROUTER.reap()
            try:
                reason = pending_host_stop()
            except Exception as exc:
                log('host lifecycle watchdog: %s' % type(exc).__name__)
                reason = None
            if reason:
                # Cancellation must also reach an in-flight operation while it
                # owns TOOL_LOCK. The native wait loop observes this event.
                REQUEST_CANCELLED.quietbridge_reason = reason
                REQUEST_CANCELLED.set()
            if TOOL_LOCK.acquire(blocking=False):
                try:
                    if reason:
                        interrupt_task(reason)
                    else:
                        reap_protection_session()
                        reap_task_lease()
                except Exception as exc:
                    log('task lease watchdog: %s' % type(exc).__name__)
                finally:
                    TOOL_LOCK.release()
    TASK_WATCH_THREAD = threading.Thread(target=watch, daemon=True)
    TASK_WATCH_THREAD.start()


def shutdown(reason='connector_disconnected'):
    TASK_WATCH_STOP.set()
    try:
        if SESSION_ROUTER is not None:
            SESSION_ROUTER.close()
        interrupt_task(reason)
    finally:
        TASK_OWNER.close()


def assert_task_connection(binding):
    if current_connection() != binding["connection"] or not HELPER._alive():
        raise HelperError("REQUIRES_REVALIDATION", "requires_revalidation：连接已改变，需显式 bind + confirm=true；不会按 PID 自动重绑")


def native_task_check(record, binding):
    assert_task_connection(binding)
    if record.get("interactionMode") == "desktop":
        target = call_helper("windowIdentity", {"appHandle": binding["appHandle"], "windowId": record["windowId"]}) or {}
        assert_task_connection(binding)
        if (target.get("processIdentity") != record["processIdentity"] or
                target.get("pid") != record["pid"] or target.get("windowId") != record["windowId"]):
            return "invalidated"
        return "bound"
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


def target_recovery_status(record):
    """Read-only recovery evidence; never create, select or bind a replacement."""
    record.pop('windowClosure', None)
    result = {'status': 'inspect_required', 'taskId': record['taskId'],
              'closedWindowConfirmed': False, 'maxAutomaticRecoveries': 2,
              'automaticRecoveriesUsed': (record.get('windowRecovery') or {}).get('attempt', 0)}
    creation = record.get('windowCreation') or {}
    binding = TASK_BINDINGS.get(record['taskId'])
    if (record.get('interactionMode') != 'background_browser' or not creation or
            creation.get('windowId') != record.get('windowId') or
            creation.get('processIdentity') != record.get('processIdentity')):
        return dict(result, reason='no_task_created_window_evidence')
    if (not TASK_OWNER.owns(record) or not binding or
            binding.get('connection') != current_connection() or not HELPER._alive() or
            not lease_live(record, Path(TASK_FILE).parent)):
        return dict(result, reason='owner_or_connection_changed')
    session = application_session()
    if not session or creation.get('applicationSession') != session or host_stop_reason(record, task_root()):
        return dict(result, reason='application_or_host_stopped')
    try:
        # A missing native session/AX title alone does not establish closure.
        # Both reads validate the original handle's live process generation.
        try:
            call_helper('windowIdentity', {'appHandle': binding['appHandle'], 'windowId': record['windowId']})
        except HelperError as error:
            if error.code != 'WINDOW_UNAVAILABLE':
                return dict(result, reason='original_process_unverified', errorCode=error.code)
        else:
            return dict(result, reason='original_window_still_exists')
        listing = call_helper('listWindows', {'appHandle': binding['appHandle']})
        rows = listing.get('windows') if isinstance(listing, dict) else listing
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or
                type(row.get('windowId')) is not int for row in rows) or
                any(row['windowId'] == record['windowId'] for row in rows)):
            return dict(result, reason='window_list_does_not_confirm_closure')
        assert_task_connection(binding)
        check_request_cancelled()
    except HelperError as error:
        return dict(result, reason='closure_check_failed', errorCode=error.code)
    if host_stop_reason(record, task_root()) or application_session() != session:
        return dict(result, reason='application_or_host_stopped')
    record['windowClosure'] = {'checkedAt': time.time(), 'applicationSession': session,
                              'appHandle': binding['appHandle'], 'connectionId': HELPER.connection_id}
    if result['automaticRecoveriesUsed'] >= result['maxAutomaticRecoveries']:
        return dict(result, status='decision_required', reason='repeated_window_closure',
                    closedWindowConfirmed=True)
    return dict(result, status='recreate_allowed', reason='confirmed_closed_task_window',
                closedWindowConfirmed=True, formerWindowId=record['windowId'],
                formerBrowserWindowId=creation['browserWindowId'],
                formerTabId=creation['tabId'], inputDispatched=False)


def interruption_evidence(record):
    stopped = host_stop_reason(record, task_root())
    reason = stopped or record.get('interruptionReason')
    return {'reason': reason, 'explicitStop': bool(stopped or reason == 'client_cancelled')}


def task_status(record):
    if record.get('state') == 'suspended':
        return dict(record, resumption={'automatic': bool(IDLE_RESUME and TASK_OWNER.owns(record)),
                    'trigger': 'next_target_tool', 'requiresObservation': True})
    if record.get('lease') and not lease_live(record, Path(TASK_FILE).parent) and record['state'] not in ('ended', 'interrupted', 'invalidated'):
        record.update(state='interrupted', interruptionReason='owner_disconnected_or_expired', interruptedAt=time.time())
        return dict(record, interruption=interruption_evidence(record))
    if record.get('lease') and not TASK_OWNER.owns(record):
        return dict(record, ownership='another_connector')
    previous_state = record["state"]
    if record['state'] == 'invalidated':
        return dict(record, targetRecovery=target_recovery_status(record))
    if record["state"] in ("awaiting_window", "ended", "interrupted"):
        return (dict(record, interruption=interruption_evidence(record))
                if record['state'] == 'interrupted' else dict(record))
    binding = TASK_BINDINGS.get(record["taskId"])
    if binding is None or binding["connection"] != current_connection() or not HELPER._alive():
        record["state"] = "requires_revalidation"
    else:
        try:
            record["state"] = native_task_check(record, binding)
        except HelperError as exc:
            if exc.code in ("APP_UNAVAILABLE", "WINDOW_UNAVAILABLE", "BROWSER_PROCESS_MISMATCH"):
                record["state"] = "invalidated"
            elif exc.code in ("REQUIRES_REVALIDATION", "STALE_APP"):
                record["state"] = "requires_revalidation"
            else:
                raise
    if record["state"] == "invalidated" and previous_state != "invalidated" and (not record.get('lease') or TASK_OWNER.owns(record)):
        # The target vanished, not the host run. End its preview while keeping
        # the independently bounded session protection for the next phase.
        release_task_preview()
    return (dict(record, targetRecovery=target_recovery_status(record))
            if record['state'] == 'invalidated' else dict(record))


def require_protection_scope(record):
    current = host_binding(record, task_root())
    if current is not None and current.get('stoppedAt') is not None:
        raise HelperError('TASK_INTERRUPTED', '宿主运行已经停止，未派发新阶段输入；不能用迟到回执复活任务',
                          interruption={'explicitStop': True, 'reason': current.get('reason', 'host_turn_ended')})
    if SESSION_PROTECTION.record is None:
        return
    anchor = SESSION_PROTECTION.record
    if (anchor.get('taskId') == record.get('taskId') and
            (anchor.get('lease') or {}).get('ownerId') == (record.get('lease') or {}).get('ownerId')):
        return
    previous = protection_host_binding()
    scope = lambda value: (value.get('sessionId'), value.get('requestId')) if value else None
    if (previous is None or current is None or previous.get('stoppedAt') is not None or
            current.get('stoppedAt') is not None or scope(previous) != scope(current)):
        raise HelperError('SESSION_ROUTING_REQUIRED',
                          '当前连接保留着会话保护，但新阶段尚未核实属于同一宿主运行；先等待宿主 begin 回执关联。若属于不同会话，需要独立连接或可靠调用上下文，不能混用或撤销原会话')


def hold_power_guard(record=None):
    """任务租约按住防睡眠断言，并监视首次系统锁屏。

    尽力而为：断言不可用时不给任务本身添麻烦，返回值仅用于回报状态。
    首次锁屏自动解锁一次后显示浮层；用户触碰后交回 macOS 自身锁屏。
    """
    now = time.monotonic()
    # 有界等待期间不续租：既不吃等待预算，也不给轮询额外加一次往返。
    if WAIT_DEADLINE is not None:
        return {}
    if record is not None:
        require_protection_scope(record)
    if record is not None and POWER_GUARD['active']:
        SESSION_PROTECTION.attach(record)
    if POWER_GUARD["active"] and now < POWER_GUARD["nextRenewAt"]:
        return {}
    try:
        params = {"action": "hold", "durationSeconds": POWER_GUARD_SECONDS}
        switches = control_snapshot()
        # A new lease records the switch. Renewals omit it so the running task stays unchanged.
        if not POWER_GUARD["active"] and isinstance(switches, dict) and switches.get("locked_use") is False:
            params["lockedUse"] = False
        payload = call_helper("powerGuard", params, timeout=5) or {}
    except HelperError:
        return {}
    guard = payload.get("guard") or {}
    privacy = payload.get("privacy") or {}
    if guard.get("holding") is True or privacy.get("active") is True:
        POWER_GUARD["active"] = True
        POWER_GUARD["nextRenewAt"] = now + POWER_GUARD_RENEW_AFTER
        if record is not None:
            SESSION_PROTECTION.attach(record)
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
    SESSION_PROTECTION.clear()
    try:
        payload = call_helper("powerGuard", {"action": "release"}, timeout=5) or {}
    except HelperError:
        return {}
    guard = payload.get("guard") or {}
    privacy = payload.get("privacy") or {}
    if isinstance(guard, dict) and isinstance(privacy, dict):
        guard = dict(guard, privacy=privacy)
    return guard


def release_task_preview():
    """Release a phase's preview without starting a helper or ending protection."""
    global SHARE_SELECTION
    SHARE_SELECTION = None
    stop_browser_preview()
    if not HELPER._alive():
        return {'status': 'connection_closed', 'nativeConnection': 'closed'}
    try:
        payload = call_helper('preview', {'action': 'release'}) or {}
    except HelperError as error:
        return {'status': 'failed', 'errorCode': error.code}
    preview = payload.get('preview') or {}
    result = {key: preview[key] for key in ('holding', 'visible', 'streamReady') if key in preview}
    result['status'] = 'released' if preview.get('holding') is False and preview.get('visible') is False else 'unconfirmed'
    if isinstance(preview.get('captureStop'), dict):
        result['captureStop'] = capture_lifecycle(preview['captureStop'])
    return result


def tool_task(args):
    global SHARE_SELECTION, IDLE_RESUME
    if not isinstance(args, dict) or set(args) - {"action", "taskId", "appHandle", "windowId", "confirm", "interactionMode", "targetSurface", "recoveryOf"}:
        raise HelperError("INVALID_ARGUMENT", "仅接受 action、taskId、appHandle、windowId、confirm、interactionMode、targetSurface、recoveryOf")
    action = args.get("action")
    if action not in ("begin", "bind", "status", "end"):
        raise HelperError("INVALID_ARGUMENT", "action 必须为 begin、bind、status、end")
    if action == "begin" and set(args) - {"action", "interactionMode", "targetSurface", "recoveryOf"}:
        raise HelperError("INVALID_ARGUMENT", "begin 仅接受 interactionMode、targetSurface、recoveryOf")
    if action != "begin" and any(key in args for key in ('interactionMode', 'targetSurface', 'recoveryOf')):
        raise HelperError("INVALID_ARGUMENT", "任务执行模式及操作对象不能中途更改；只能在 begin 时指定")
    if action == "status" and set(args) - {"action", "taskId"}:
        raise HelperError("INVALID_ARGUMENT", "status 仅接受 action 和 taskId")
    if action == "end" and (set(args) - {"action", "taskId", "confirm"} or args.get("confirm") is not True):
        raise HelperError("INVALID_ARGUMENT", "end 需要 taskId（省略时结束当前任务）与 confirm=true")
    if "taskId" in args and (not isinstance(args["taskId"], str) or not args["taskId"]):
        raise HelperError("INVALID_ARGUMENT", "taskId 必须是非空字符串")
    if action == 'begin':
        interaction_mode = task_interaction_mode(args)
        reap_protection_session()
    with task_store() as data:
        if action == "begin":
            recovery = None
            if 'recoveryOf' in args:
                origin_id = args['recoveryOf']
                origin = data['tasks'].get(origin_id) if isinstance(origin_id, str) else None
                if not origin or not TASK_OWNER.owns(origin):
                    raise HelperError('TASK_INTERRUPTED', '恢复来源不属于本连接，未重建窗口')
                stopped = host_stop_reason(origin, task_root())
                if stopped:
                    raise HelperError('TASK_INTERRUPTED', '恢复来源的宿主已停止，未重建窗口',
                                      interruption={'explicitStop': True, 'reason': stopped})
                closure = origin.get('windowClosure') or {}
                successor = data['tasks'].get((origin.get('windowRecoveryNext') or ''))
                if successor:
                    # Lost begin receipts must not produce another replacement.
                    if (data.get('activeTaskId') != successor['taskId'] or not TASK_OWNER.owns(successor) or
                            successor.get('state') in ('ended', 'interrupted', 'stopping') or
                            not lease_live(successor, Path(TASK_FILE).parent) or
                            host_stop_reason(successor, task_root()) or interaction_mode != 'background_browser' or
                            closure.get('applicationSession') != application_session() or
                            not HELPER._alive() or closure.get('connectionId') != HELPER.connection_id):
                        raise HelperError('REQUIRES_REVALIDATION', '恢复阶段已结束或运行归属改变；未复活历史阶段或建立新窗口')
                    return task_reply(successor, recoveryReused=True)
                if (origin.get('state') != 'ended' or data.get('activeTaskId') is not None or
                        not origin.get('windowCreation') or interaction_mode != 'background_browser' or
                        not closure.get('applicationSession') or closure['applicationSession'] != application_session() or
                        not HELPER._alive() or closure.get('connectionId') != HELPER.connection_id or
                        not 0 <= time.time() - closure.get('checkedAt', 0) < 120):
                    raise HelperError('REQUIRES_REVALIDATION', '先核实本任务窗口已关闭并 end 自己的失效阶段；不得覆盖活动目标或迁移进程')
                attempt = (origin.get('windowRecovery') or {}).get('attempt', 0) + 1
                if attempt > 2:
                    raise HelperError('RECOVERY_DECISION_REQUIRED', '窗口连续关闭，自动恢复已暂停；请在 Agent 选择下一步，未认定为取消')
                listing = call_helper('listWindows', {'appHandle': closure['appHandle']})
                rows = listing.get('windows') if isinstance(listing, dict) else listing
                if (not isinstance(rows, list) or any(not isinstance(row, dict) or
                        type(row.get('windowId')) is not int for row in rows) or
                        any(row['windowId'] == origin['windowId'] for row in rows)):
                    raise HelperError('REQUIRES_REVALIDATION', '原窗口关闭状态已变化，未建立恢复阶段')
                check_request_cancelled()
                stopped = host_stop_reason(origin, task_root())
                if stopped:
                    raise HelperError('TASK_INTERRUPTED', '恢复核验期间宿主已停止，未建立新阶段',
                                      interruption={'explicitStop': True, 'reason': stopped})
                if (closure['applicationSession'] != application_session() or not HELPER._alive() or
                        closure['connectionId'] != HELPER.connection_id):
                    raise HelperError('REQUIRES_REVALIDATION', '恢复核验期间运行会话或连接改变，未建立新阶段')
                recovery = {'rootTaskId': (origin.get('windowRecovery') or {}).get('rootTaskId', origin_id),
                            'previousTaskId': origin_id, 'attempt': attempt}
            previous = data["tasks"].get(data.get("activeTaskId"))
            if isinstance(previous, dict) and previous.get("state") not in ('ended', 'interrupted'):
                if previous.get('lease') and not TASK_OWNER.owns(previous) and lease_live(previous, Path(TASK_FILE).parent):
                    raise HelperError('TASK_IN_USE', '活动任务仍由另一个连接持有；不能用 begin 接管')
                if protection_host_binding() is not None:
                    raise HelperError('TASK_IN_USE', '当前界面阶段尚未结束；先 end 自己的阶段，不能用 begin 覆盖其他会话的活动目标')
                previous.update(state='interrupted', interruptionReason='replaced_by_explicit_begin', interruptedAt=time.time())
                TASK_BINDINGS.pop(previous['taskId'], None)
            SHARE_SELECTION = None
            IDLE_RESUME = None
            if isinstance(previous, dict) and previous.get("windowId") is not None and HELPER._alive():
                release_task_preview()
            task_id = str(uuid.uuid4())
            record = {"taskId": task_id, "state": "awaiting_window", "mode": None,
                      "newWindowCreated": False,
                      "interactionMode": interaction_mode}
            if 'targetSurface' in args:
                record['targetSurface'] = args['targetSurface']
            if recovery:
                record['windowRecovery'] = recovery
            TASK_OWNER.attach(record, Path(TASK_FILE).parent)
            from session_registry import reserve
            try:
                reserve(task_root(), SESSION_KEY or TASK_OWNER.id, TASK_FILE, record)
            except ValueError as error:
                raise HelperError(str(error).split(':', 1)[0], str(error))
            data["tasks"][task_id] = record
            data["activeTaskId"] = task_id
            if recovery:
                origin['windowRecoveryNext'] = task_id
            return task_reply(record)
        task_id = args.get("taskId", data["activeTaskId"])
        record = data["tasks"].get(task_id)
        if record is None:
            if action == 'status' and 'taskId' not in args:
                return json.dumps({'state': 'idle', 'activeTaskId': None, 'sessionProtection': protection_status()})
            raise HelperError("UNKNOWN_TASK", "任务不存在；先 begin")
        if action == "status":
            return task_reply(task_status(record), sessionProtection=protection_status())
        if action == "end":
            # 显式结束是唯一回到「无任务」原生模式的入口：只收尾记录，
            # 不清空历史，也不把已绑定目标转交其它窗口。
            active = data.get("activeTaskId") == task_id
            if active and record.get('lease') and not TASK_OWNER.owns(record) and lease_live(record, Path(TASK_FILE).parent):
                raise HelperError('TASK_IN_USE', '任务属于另一个活动连接，未结束其任务')
            if active:
                require_protection_scope(record)
                IDLE_RESUME = None
            TASK_BINDINGS.pop(task_id, None)
            if active:
                data["activeTaskId"] = None
            record["state"] = "ended"
            from session_registry import release
            release(task_root(), SESSION_KEY or TASK_OWNER.id, task_id)
            if not active:
                return task_reply(record, cleanup='historical_record_only')
            # 共享面板跟随界面操作任务：任务结束就释放租约，不让浮层继续留在屏幕上。
            preview_cleanup = release_task_preview()
            if SESSION_PROTECTION.record is not None:
                SESSION_PROTECTION.touch()
            operation_log('task.phase_ended', taskId=task_id, protectionRetained=POWER_GUARD['active'],
                          previewCleanup=preview_cleanup)
            return task_reply(record, previewCleanup=preview_cleanup, sessionProtection=protection_status())
        if args.get("confirm") is not True or type(args.get("windowId")) is not int or not 1 <= args["windowId"] <= 4294967295 or not args.get("appHandle"):
            raise HelperError("INVALID_ARGUMENT", "bind 需要显式 appHandle、有效 windowId、confirm=true")
        if READ_ONLY:
            raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，bind 已禁用")
        if record['state'] == 'suspended' and IDLE_RESUME and TASK_OWNER.owns(record):
            raise HelperError('TASK_SUSPENDED', '等待暂停保留原目标；请自动调用原窗口 share 恢复并重新观察，无需重新 bind 或等待用户确认')
        if record['state'] in ('ended', 'interrupted', 'suspended'):
            raise HelperError('TASK_INTERRUPTED', '任务已结束或中断；先核实目标并 begin 新任务，不能复活旧输入',
                              interruption=interruption_evidence(record))
        if record.get('lease') and not TASK_OWNER.owns(record):
            raise HelperError('TASK_IN_USE' if lease_live(record, Path(TASK_FILE).parent) else 'TASK_INTERRUPTED',
                              '原任务属于其他连接；不能静默恢复或接管')
        if data.get('activeTaskId') != task_id:
            raise HelperError('TASK_INTERRUPTED', '历史任务不能重新成为活动任务；请显式 begin')
        app = resolve_app(args["appHandle"])
        pid = STATE["apps"][app].get("pid")
        if type(pid) is not int or not 1 <= pid <= 2147483647:
            raise HelperError("INVALID_ARGUMENT", "应用必须是已有运行进程")
        if record.get("windowId") is not None and (record["windowId"] != args["windowId"] or record["pid"] != pid):
            raise HelperError("TARGET_MISMATCH", "已记录任务不能改绑其他 PID 或窗口；新任务使用 begin")
        if record["state"] == "invalidated":
            raise HelperError("TASK_INVALIDATED", "任务绑定已失效，不能复用窗口 ID")
        desktop = record.get("interactionMode") == "desktop"
        require_protection_scope(record)
        if desktop:
            native = call_helper("windowIdentity", {"appHandle": app, "windowId": args["windowId"]}) or {}
        else:
            if ALLOWLIST and not any(a in "com.google.chrome google chrome" for a in ALLOWLIST):
                raise HelperError("APP_NOT_ALLOWED", "QUIETBRIDGE_ALLOWLIST 未包含 Google Chrome")
            native = call_helper("browserSession", {"action": "attach", "pid": pid,
                                 "windowId": args["windowId"], "confirm": True}) or {}
        identity = native.get("processIdentity")
        if (native.get("pid") != pid or native.get("windowId") != args["windowId"] or
                not isinstance(identity, dict) or identity.get("pid") != pid or
                not all(isinstance(identity.get(k), str) and identity[k].isdigit()
                        for k in ("startedSeconds", "startedMicroseconds")) or (not desktop and (
                native.get("state") != "attached" or native.get("running") is not True or
                not isinstance(native.get("sessionId"), str) or not native["sessionId"] or
                not any(w.get("windowId") == args["windowId"] for w in native.get("windows", []))))):
            raise HelperError("ATTACH_UNVERIFIED", "原生服务未返回完整窗口和进程代际身份；任务未绑定")
        if record.get("processIdentity") is not None and record["processIdentity"] != identity:
            record["state"] = "invalidated"
            return task_reply(record)
        record.update(state="bound", mode="attached_existing", pid=pid, processIdentity=identity,
                      windowId=args["windowId"], newWindowCreated=False)
        if not desktop:
            record["sessionId"] = native["sessionId"]
        from session_registry import claim_target
        try:
            claim_target(task_root(), SESSION_KEY or TASK_OWNER.id, record, TASK_FILE)
        except ValueError as error:
            record.update(state='awaiting_window', mode=None)
            for key in ('pid', 'windowId', 'processIdentity', 'sessionId'):
                record.pop(key, None)
            raise HelperError(str(error).split(':', 1)[0], str(error),
                              target_conflict=getattr(error, 'conflict', None))
        TASK_BINDINGS[task_id] = {"appHandle": app, "connection": current_connection()}
        TASK_OWNER.attach(record, Path(TASK_FILE).parent)
        data["activeTaskId"] = task_id
        STATE.update(app=app, snapshot=None, snapshot_app=None, snapshot_window=None)
        # 绑定成功即进入任务运行期：按住防睡眠断言，锁屏后任务仍能继续。
        return task_reply(record, limitation="仅绑定指定应用的已有窗口，未提供数据隔离" if desktop else
                               "仅绑定已有窗口，共享 Chrome 数据；未实现新建专用窗口或数据隔离",
                          powerGuard=hold_power_guard(record) if desktop else {"active": False})


TASK_TARGET_TOOLS = {"quietbridge_situation", "quietbridge_state", "quietbridge_visual_state", "quietbridge_screenshot", "quietbridge_wait_ready",
                     "quietbridge_click", "quietbridge_hover", "quietbridge_scroll", "quietbridge_drag", "quietbridge_type_text",
                     "quietbridge_paste", "quietbridge_key", "quietbridge_set_value"}


def task_guard(name, args):
    if name == "quietbridge_task":
        return args, None
    # 无任务且记录文件不存在时不创建锁文件，保留旧行为。
    if not os.path.lexists(TASK_FILE):
        if TASK_BINDINGS:
            raise HelperError("TASK_STORE_ERROR", "已绑定任务的持久记录丢失，拒绝回退无任务模式")
        return args, None
    with task_store() as data:
        record = data["tasks"].get(data["activeTaskId"])
        if name == 'quietbridge_windows' and args.get('action') == 'unshare':
            # Cleanup is scoped to this connector's native socket, even after
            # its interface phase ends. Never release another active owner's
            # resources or require a new task just to close an old preview.
            if record and record.get('lease') and not TASK_OWNER.owns(record):
                raise HelperError('TASK_IN_USE', '共享资源属于另一个活动连接，未释放其资源')
            return args, None
        if record is None:
            if SESSION_PROTECTION.record is not None and (name in TASK_TARGET_TOOLS or
                    name == 'quietbridge_windows' and args.get('action', 'list') != 'list' or
                    name == 'quietbridge_browser_events' and args.get('action') not in ('status', 'candidates', 'annotations')):
                raise HelperError('TASK_NOT_BOUND', '会话保护仍在，当前界面阶段已结束；先 begin 并绑定新阶段，不能复用已结束目标或无任务输入')
            return args, None
        if (record.get('state') == 'suspended' and IDLE_RESUME and TASK_OWNER.owns(record) and
                (name in TASK_TARGET_TOOLS or name in ('quietbridge_windows', 'quietbridge_browser_events', 'quietbridge_browser_session'))):
            raise HelperError('TASK_SUSPENDED', '原任务因等待暂时释放共享；请立即调用原窗口 share 自动恢复，随后重新观察，无需用户提醒')
        if record.get('lease') and (record['state'] == 'interrupted' or not lease_live(record, Path(TASK_FILE).parent)):
            if name in TASK_TARGET_TOOLS or name in ('quietbridge_windows', 'quietbridge_browser_events', 'quietbridge_browser_session'):
                raise HelperError('TASK_INTERRUPTED', '原任务已中断或租约已过期；查看任务状态后显式创建新任务',
                                  interruption=interruption_evidence(record))
        if record.get('lease') and not TASK_OWNER.owns(record) and (
                name in TASK_TARGET_TOOLS or name in ('quietbridge_windows', 'quietbridge_browser_session') or
                (name == 'quietbridge_browser_events' and args.get('action') not in ('status', 'candidates', 'annotations'))):
            raise HelperError('TASK_IN_USE', '任务属于另一个活动连接，未接管其输入或共享资源')
        # 等待绑定时允许只读枚举窗口：否则「先建窗再绑定」会被守卫锁死。
        discovery = (name == "quietbridge_windows" and args.get("action", "list") == "list" and
                     record.get("state") == "awaiting_window")
        protected = name in TASK_TARGET_TOOLS or (name == "quietbridge_windows" and not discovery)
        if name == "quietbridge_browser_session" and args.get("action") not in ("prepare", "list"):
            preparing = (args.get("action") == "start" and record.get("state") == "awaiting_window" and
                         record.get("windowId") is None and record.get("interactionMode") == "background_browser")
            if not preparing:
                raise HelperError("TASK_SCOPE_REQUIRED", "已绑定任务不能迁移到其他浏览器。保留原任务与结果，通知用户确认恢复方式；"
                                  "独立浏览器可在未绑定的 background_browser 阶段 prepare/start，再依据 list 的真实 PID 和窗口 bind。"
                                  "新 profile 需用户加载扩展并登录，不自动继承原登录态。")
        if name == "quietbridge_apps" and args.get("action") == "launch":
            raise HelperError("TASK_SCOPE_REQUIRED", "任务期间不允许启动应用")
        if (record.get('targetSurface') in ('browser_ui', 'native_dialog', 'desktop_app') and
                name == 'quietbridge_browser_events' and
                args.get('action') not in ('status', 'candidates', 'result', 'cancel', 'annotations')):
            raise HelperError('TASK_ROUTE_MISMATCH', '本阶段声明为原生界面；未派发扩展操作。'
                              '先检查任务与原目标，只有真实进入网页阶段时才 end 自己的阶段并以 targetSurface=web_content begin；'
                              '不能更改活动阶段的通道或重放未知输入。')
        if record.get("interactionMode", "background_browser") == "background_browser":
            native_input = name in {"quietbridge_click", "quietbridge_hover", "quietbridge_scroll", "quietbridge_drag", "quietbridge_type_text",
                                    "quietbridge_paste", "quietbridge_key", "quietbridge_set_value"}
            native_window_write = name == "quietbridge_windows" and args.get("action", "list") not in ("list", "share", "unshare")
            if native_input or native_window_write:
                operation_log("browser.foreground_fallback_blocked", taskId=record["taskId"], tool=name)
                raise HelperError("BACKGROUND_ONLY", "后台浏览器任务仅允许固定标签页扩展输入；通道不可用时等待恢复，不回落 AX、桌面键鼠或激活窗口")
            if name == "quietbridge_windows" and args.get("action") == "share" and not native_preview_matches(record['taskId']):
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
        hold_power_guard(record)
    context = dict(binding, windowId=record["windowId"])
    if binding.get('requiresObservation') and name in {
            'quietbridge_click', 'quietbridge_hover', 'quietbridge_scroll', 'quietbridge_drag',
            'quietbridge_type_text', 'quietbridge_paste', 'quietbridge_key', 'quietbridge_set_value'}:
        raise HelperError('TASK_RESUMED', '共享刚恢复，请先对原窗口调用 quietbridge_state 或 quietbridge_visual_state；旧输入未执行')
    if name in ("quietbridge_type_text", "quietbridge_paste", "quietbridge_key") or (
            name == "quietbridge_windows" and args.get("action", "list") not in ("list", "share", "unshare")):
        raise HelperError("UNSAFE_WINDOW_DELIVERY", "原生接口仅确认应用，无法安全投递到绑定窗口，已拒绝")
    args = dict(args, appHandle=binding["appHandle"])
    if name in TASK_TARGET_TOOLS:
        element = args.get("elementIndex") is not None
        if "snapshotId" in args or element:
            snapshot = args.get("snapshotId") or STATE["snapshot"]
            if not snapshot or snapshot != STATE["snapshot"]:
                raise HelperError("STALE_SNAPSHOT", "快照已被新的观察替换；保留当前绑定，重新观察原窗口后使用新 snapshotId")
            if STATE["snapshot_app"] != binding["appHandle"] or STATE["snapshot_window"] != record["windowId"]:
                raise HelperError("TARGET_MISMATCH", "snapshot 无法通过 STATE snapshot_window 证明属于绑定窗口")
            args["snapshotId"] = snapshot
        if not element and "windowId" not in args:
            raise HelperError("TARGET_REQUIRED", "任务操作必须显式提供 windowId；元素操作必须使用已核验快照")
        if name == "quietbridge_state" and args.get("includeMenus"):
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
    if method in ('getState', 'visualState', 'screenshot'):
        for current in TASK_BINDINGS.values():
            if current.get('appHandle') == binding['appHandle'] and current.get('connection') == binding['connection']:
                current.pop('requiresObservation', None)


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
            "还没有选择应用：先调用 quietbridge_apps(action=\"get\", selector=\"应用名或 bundleId\")",
        )
    if handle not in STATE["apps"]:
        raise HelperError("UNKNOWN_APP_HANDLE", "appHandle 不在本会话缓存中（可能 helper 重启过）：重新调用 quietbridge_apps 获取")
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
    raise HelperError("NO_SNAPSHOT", "元素操作需要 snapshotId：先调用 quietbridge_state 读取界面树")


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
            raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，%s 已禁用" % action)
        if ALLOWLIST and not any(a in "com.google.chrome google chrome" for a in ALLOWLIST):
            raise HelperError("APP_NOT_ALLOWED", "QUIETBRIDGE_ALLOWLIST 未包含 Google Chrome")
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
        return "已请求打开系统设置面板（%s）。若未弹出，请手动进入 系统设置 > 隐私与安全 授权 \"quietbridge\"。" % permission

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
    # The native side hardcodes this field; the extension host is the only real
    # source for whether a browser tab is actually reachable.
    payload = {
        "platform": caps.get("platform"),
        "permissions": caps.get("permissions"),
        "sessionLocked": caps.get("sessionLocked"),
        "browserChannel": browser_channel_state(),
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
        "lockedUse waitingForManualUnlock=%s manualSuppression=%s unlockMethod=%s socketStarted=%s"
        % (
            privacy.get("waitingForManualUnlock"),
            locked_use.get("manualSuppression"),
            locked_use.get("lastUnlockMethod"),
            locked_use.get("socketStarted"),
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
            "缺失授权：请用户在 QuietBridge 权限向导中完成所需授权，再告知继续。"
            "暂停依赖该权限的操作，不能自行打开系统设置或更换工具绕过。"
        )
    return {'content': [{'type': 'text', 'text': "\n".join(lines)}], 'permissionStatus': payload}


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
        cancellable_wait(interval)


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
            raise HelperError("TARGET_REQUIRED", "新建后出现多个候选窗口，未猜测共享目标；请显式 quietbridge_windows action=share")
        if time.monotonic() >= deadline:
            raise HelperError("PREVIEW_NOT_READY", "新建窗口未在 6 秒内唯一出现，共享待补；不冒充已覆盖")
        cancellable_wait(0.25)


def tool_apps(args):
    action = (args.get("action") or "list").lower()
    selector = args.get("selector")
    query = args.get("query")
    # Lenient: selector without an explicit action means "select this app".
    if selector and action == "list":
        action = "get"
    if action in ("launch",) and READ_ONLY:
        raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，launch 已禁用")

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
                    raise HelperError("APP_NOT_ALLOWED", "QUIETBRIDGE_ALLOWLIST 未包含该应用：%s" % ident)
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
    global SHARE_SELECTION, IDLE_RESUME
    action = (args.get("action") or "list").lower()
    if action == "unshare":
        # 释放共享不需要应用句柄：目标应用可能已经退出，但浮层必须能收掉。
        if READ_ONLY:
            raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，unshare 已禁用")
        IDLE_RESUME = None
        stop_browser_preview()
        if not HELPER._alive():
            release_task_preview()
            return "本地共享选择已清理（nativeConnection=closed）；未启动新的原生连接。"
        result = call_helper("preview", {"action": "release"}) or {}
        SHARE_SELECTION = None
        preview = result.get("preview") or {}
        return "已结束窗口共享（holding=%s，visible=%s）。" % (preview.get("holding"), preview.get("visible"))
    app_handle = resolve_app(args.get("appHandle"))
    if action == "list":
        windows = call_helper("listWindows", {"appHandle": app_handle}) or []
        meta = STATE["apps"].get(app_handle, {})
        return "%s 的窗口 %d 个：\n%s" % (meta.get("name") or "应用", len(windows), fmt_windows(windows))
    if action == "activate":
        if READ_ONLY:
            raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，activate 已禁用")
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
            raise HelperError("READ_ONLY", "QUIETBRIDGE_READ_ONLY=1，share 已禁用")
        window_id = args.get("windowId")
        if type(window_id) is not int or not 1 <= window_id <= 4294967295:
            raise HelperError("INVALID_ARGUMENT", "share 需要显式有效 windowId")
        duration = args.get("durationSeconds", 1800)
        if type(duration) not in (int, float) or not 60 <= duration <= 3600:
            raise HelperError("INVALID_ARGUMENT", "durationSeconds 必须在 60 到 3600 之间")
        source = args.get("source")
        if source is not None and source not in ("application", "window", "desktop"):
            raise HelperError("INVALID_ARGUMENT", "source 必须是 application、window 或 desktop")
        ensure_share(app_handle, window_id, source=source, duration=duration)
        scope = {"browser_content": "网页内容（定时帧，非整窗视频）", "desktop": "目标应用所在的整个桌面", "application": "目标应用", "window": "单个窗口"}[SHARE_RECORD["source"]]
        return ("已开始共享%s（windowId=%d，租约=%s 秒，holding=True）。"
                "共享面板在任务结束前保持：动作会续租，quietbridge_windows action=unshare 或 quietbridge_task end 会立即关闭。"
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
    truncated = state.get("truncated", "unknown")
    if isinstance(tree, str) and len(tree) > MAX_TREE_CHARS:
        tree = tree[:MAX_TREE_CHARS]
        truncated = True
    preview_result = call_helper("preview", {"action": "status"}) or {}
    preview = preview_result.get("preview")
    situation = {"target": dict({"appHandle": app, "windowId": window},
                                                     **{k: target[k] for k in ("bounds", "isOnScreen", "onCurrentSpace") if k in target}),
                          "permissions": caps.get("permissions", "unknown"),
                          "sessionLocked": caps.get("sessionLocked", "unknown"),
                          "focus": {"focusedWindowId": state.get("focusedWindowId", "unknown"),
                                    "frontPid": state.get("frontPid", "unknown"),
                                    "targetOnCurrentSpace": target.get("onCurrentSpace", "unknown")},
                          "ax": {"available": bool(tree.strip()) if isinstance(tree, str) else "unknown",
                                 "truncated": truncated, "tree": tree,
                                 "snapshotId": state.get("snapshotId"), "maxElements": 50, "maxDepth": 6},
                          "preview": preview if isinstance(preview, dict) else {"status": "unknown"},
                          "scopeWarning": "desktop 可能包含其他应用及敏感信息；现场观察不证明共享正在运行。",
                          "suggestedActions": ["按明确目标观察 AX；空树不证明自绘或权限失败。",
                                               "操作前使用 quietbridge_windows action=share source=window，核实共享目标和视频首帧；后续自动续租。",
                                               "必须不抢焦点；需要前台或共享未就绪时报告阻塞，不激活、不置前、不切换 Space。"]}
    # Some MCP clients expose only content to their agent. Keep the bounded
    # observation visible there as well as in structuredContent.
    return {"content": [{"type": "text", "text": "只读现场观察；未启动共享或截图。\n" +
                         json.dumps(situation, ensure_ascii=False, indent=2)}],
            "situation": situation}


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
                "%s；该应用可能没有可观察窗口，先核对 quietbridge_windows 列表，不自动激活"
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
        tail.append("界面树被截断：可加大 maxElements/maxDepth，或先用 quietbridge_windows 选中具体窗口缩小范围。")
    if not tree.strip():
        observed_window = result.get("windowId") or window_id
        target = next((w for w in list_windows(app_handle) if w.get("windowId") == observed_window), {})
        if target.get("isOnScreen") is False:
            tail.append(
                "AX 树为空且该窗口 isOnScreen=false，不能据此断定 Space 或自绘状态；先核对目标与可见性，"
                "可用 quietbridge_visual_state 获取视频帧进行视觉分析；不得自动点击或猜测盲点。"
            )
        else:
            tail.append("AX 树为空或缺失AXWebArea不代表权限失败：用 quietbridge_visual_state 获取视频帧供视觉分析，不自动点击，不根据不确定视觉盲点操作。")
    elif result.get("focusedWindowId") and result.get("windowId") and result["focusedWindowId"] != result["windowId"]:
        tail.append(
            "注意：观察的是 windowId=%s，但应用焦点在 windowId=%s；必须使用绑定窗口的定向输入或同一快照的可写 AX 元素。"
            "type_text/paste/key 不提供窗口级确认，桌面任务拒绝；禁止激活窗口、切换焦点或按键兜底。"
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
            cancellable_wait(min(interval, remaining))
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
        detail = " route=%s axAction=%s focusPreserved=%s focusRestorationAttempted=%s focusRestorationAccepted=%s frontPid=%s->%s frontWindowId=%s->%s" % (
            result["route"], result.get("axAction"), result.get("focusPreserved"),
            result.get("focusRestorationAttempted"), result.get("focusRestorationAccepted"),
            result.get("frontPidBefore"), result.get("frontPidAfterAction"),
            result.get("frontWindowIdBefore"), result.get("frontWindowIdAfterAction"))
        if result.get("focusPreserved") is False:
            detail += " 前台应用或键盘窗口发生变化；点击已派发，请勿重试"
        elif result.get("focusPreserved") is None:
            detail += " 点击已派发，但焦点状态无法核实；请先检查结果，勿直接重试"
    return "已派发 %s %s dispatched=%s verified=%s（读 quietbridge_state 核实效果）%s%s" % (
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
    return "已派发 typeText %d 字符 dispatched=%s（长文本建议改用 quietbridge_paste）" % (len(text), result.get("dispatched"))


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
        "（已核对输入框内容）" if result.get("verified") else "（未核实，需读 quietbridge_state）",
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
    readback = result.get("valueReadback", "unavailable")
    return "已派发 setValue element[%s] dispatched=%s，AX 写后即时读回=%s（match 只表示即时 AX 值一致，仍须观察界面效果；mismatch 或 unavailable 时暂停核实，不要重复写入或换键盘通道重放）" % (
        element_index,
        result.get("dispatched"),
        readback,
    )


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

APP_HANDLE_PROP = {"type": "string", "description": "quietbridge_apps 返回的 appHandle；省略时使用本会话最近选择的应用"}
WINDOW_PROP = {"type": "integer", "description": "窗口 id；输入必须显式指定或使用同应用当前连接可信快照的窗口"}
SNAPSHOT_PROP = {"type": "string", "description": "quietbridge_state 返回的 snapshotId；省略时使用该应用最近快照"}
NATIVE_INPUT_GUIDANCE = (
    "必须不抢焦点并使用共享窗口：普通原生应用用 targetSurface=desktop_app，浏览器工具栏等原生控件用 browser_ui，真实原生对话框用 native_dialog，"
    "在 task begin 自动选择 desktop 后绑定真实窗口，调用 quietbridge_windows action=share source=window 核实目标与视频首帧；后续自动续租。"
    "Chrome 普通标签的网页正文、表单和页面滚动使用扩展，扩展异常不得作为改用原生操作网页的理由。"
    "Electron 桌面应用的内嵌控件仍按 desktop_app 核实实际 AX/窗口投递能力，不因此获得 Chrome 扩展 fill 或 DOM 操作能力。"
    "仅操作已确认目标，禁止 activate/AXRaise、切换 Space、移动物理鼠标或全局 HID 兜底；遇到前台要求、共享或投递错误时停止并报告。"
)
NATIVE_OBSERVATION_GUIDANCE = (
    "保持用户焦点；调用会核验共享目标与视频首帧并自动续租。操作任务开始时先用 quietbridge_windows action=share source=window 建立共享；"
    "共享未就绪时等待，不激活、不置前或切换 Space 来恢复观察。"
)
MODIFIERS_PROP = {
    "type": "array",
    "items": {"type": "string", "enum": ["command", "control", "option", "shift", "fn"]},
    "description": "修饰键",
}

TOOLS = [
    {"name": "quietbridge_situation", "description": "只读现场：必须显式 appHandle/windowId，经任务守卫核验，仅读取能力、窗口与有界AX（50元素/6层），不截图、不启动共享、不重绑。保持用户焦点；返回的 preview 只是状态，不能证明已建立共享，后续界面操作必须使用共享窗口能力。焦点、AX、共享未知值诚实返回，不推断空树为自绘。选路：普通原生 App、浏览器自身控件和真实原生对话框可走本工具及 AX 系列；网页正文和表单用 quietbridge_browser_events，不因空树或扩展失败转为原生输入。纯命令行或文件操作不需要 QuietBridge。",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"appHandle": {"type": "string", "minLength": 1},
                                    "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295}},
                     "required": ["appHandle", "windowId"]},
     "handler": tool_situation, "readOnly": True,
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {
        "name": "quietbridge_task",
        "description": BROWSER_ROUTING_GUIDANCE + "界面任务必须不抢焦点，绑定后使用 quietbridge_windows action=share source=window 建立系统窗口连续视频预览；仅显式 screenshot 或诊断 capture 会截图，不以截图维持视频。绑定不等于首帧就绪。begin 推荐指定 targetSurface，自动选择对应执行模式；显式 interactionMode 与之冲突会拒绝。省略 targetSurface 时兼容原调用：默认 background_browser，或显式 desktop。网页阶段禁止所有原生输入和窗口激活；扩展未就绪必须等待。操作对象和模式只能在 begin 指定，不能中途更改。begin/status/bind/end 回执含 routing，表明选路及前提，不证明已绑定、已授权或已共享。begin 生成 UUID，保持 awaiting_window，不创建或启动浏览器。bind 必须显式 appHandle、windowId、confirm=true，仅绑定已有窗口，mode=attached_existing，不等于新建或数据隔离。status 只读核验进程代际和窗口；跨连接 requires_revalidation，需重新显式确认，绝不按 PID 自动接管。任务期间观察和输入严格限制目标，应用级键盘接口拒绝。网页共享前先 assign 并 verify_mapping。"
                       "窗口关闭一次不表示用户取消；status.targetRecovery 为 recreate_allowed 时，end 自己的失效阶段后 begin(recoveryOf=旧taskId,targetSurface=web_content)，再 create_target 重建本任务专用窗口，继承最多两次的自动恢复预算；重复 begin 复用仍有效的恢复阶段。两次自动恢复后再次关闭则暂停并用 Agent 弹框提供重建后继续/保持暂停/结束任务，不推断关闭意图。后台模式不启动桌面接管；共享预览独立维持原目标和宿主位置。end 只释放本阶段绑定和共享预览，返回 sessionProtection；同一宿主本轮下一阶段可 begin 新任务，不主动交还系统锁屏。会话停止、取消或断连才释放会话保护；已核实宿主归属时保护最多允许30分钟无实际工具活动，无归属证据时120秒兜底。界面阶段120秒无操作时暂停并释放共享，同一存活 worker 的下次原目标调用自动核验恢复；TASK_SUSPENDED/TASK_RESUMED 应立即重建共享并取新快照，无需用户提醒。状态查询与心跳不续期。明确结束或取消的旧任务不能复活，未知输入不得重放。其他连接的活动任务不能接管；TARGET_MISMATCH 不等于旧绑定已失效。",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["begin", "bind", "status", "end"]},
                "taskId": {"type": "string", "minLength": 1},
                "recoveryOf": {"type": "string", "minLength": 1,
                               "description": "仅 begin：接续本连接已核实关闭并 end 的专用 Chrome 阶段 taskId；继承恢复计数，连续最多两次，重复调用复用同一新阶段。不是用户停止后的自动重启许可。"},
                "appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP,
                "confirm": {"type": "boolean", "const": True},
                "interactionMode": {"type": "string", "enum": ["background_browser", "desktop"],
                                    "description": "省略时按 targetSurface 自动选择；两者都省略时兼容默认为 background_browser。"},
                "targetSurface": {"type": "string", "enum": list(TARGET_SURFACE_MODES),
                                  "description": "仅 begin：web_content 自动选 background_browser；browser_ui/native_dialog/desktop_app 自动选 desktop。按真实控件类型声明，不能用于扩展故障兜底。可省略 interactionMode，显式指定时必须一致。"},
            },
            "required": ["action"],
            "oneOf": [
                {"properties": {"action": {"const": "begin"}},
                 "not": {"anyOf": [{"required": [k]} for k in ("taskId", "appHandle", "windowId", "confirm")]}},
                {"properties": {"action": {"const": "bind"}}, "required": ["appHandle", "windowId", "confirm"],
                 "not": {"anyOf": [{"required": [k]} for k in ("interactionMode", "targetSurface", "recoveryOf")]}},
                {"properties": {"action": {"const": "status"}},
                 "not": {"anyOf": [{"required": [k]} for k in ("appHandle", "windowId", "confirm", "interactionMode", "targetSurface", "recoveryOf")]}},
                {"properties": {"action": {"const": "end"}}, "required": ["confirm"],
                 "not": {"anyOf": [{"required": [k]} for k in ("appHandle", "windowId", "interactionMode", "targetSurface", "recoveryOf")]}},
            ],
        },
        "handler": tool_task, "readOnly": True,
    },
    {
        "name": "quietbridge_browser_session",
        "description": "受管 Chrome 会话；请求不激活窗口，启动后仍需核验真实目标与共享。prepare/list 可在任务内查询；start 仅在无活动任务或尚未绑定窗口的 background_browser 阶段允许，必须 sessionId+confirm=true，返回 pending/started，不自动绑定。list 返回独立进程 PID/窗口，须按这些真实结果重新获取 appHandle 后 task bind，不能选用户的其他 Chrome 窗口。已绑定任务不能自动迁移，先通知用户确认新阶段。新 profile 没有原登录态，也不会自动加载 QuietBridge 扩展；需用户加载扩展、授权站点并登录，不复制旧 profile/cookie，不把受管启动作为即用恢复路径。start 不重试，超时可能已启动；禁止重复启动，不关闭浏览器、不切换 Space。attach(pid,windowId,confirm=true) 共享既有 Chrome 数据，不启动、不写 profile、不激活、不 raise；任务内仅通过 task bind 使用。READ_ONLY 拦截 start/attach。",
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
        "name": "quietbridge_permissions",
        "description": "查看 macOS 辅助功能/屏幕录制授权、平台能力、会话锁定与任务隐私层状态；不抢焦点的界面操作前先核实权限，随后核实共享窗口。action=open_settings 会打开系统设置，仅在用户明确要求权限设置时使用，后台任务不能自行调用来恢复连接。"
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
        "name": "quietbridge_apps",
        "description": "应用层入口：action=list 列出运行中的应用；find 搜索已安装应用；get 按名称/bundleId/PID/路径解析应用并拿到 appHandle，不改变用户焦点。launch 启动已选应用，但第三方应用可能自行激活；要求不抢焦点时优先复用已有进程，未运行则报告并等待用户手动打开。启动回执不代表共享就绪，界面操作前仍须建立共享窗口。",
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
        "name": "quietbridge_windows",
        "description": "不抢焦点地列出窗口或使用共享窗口能力。界面任务绑定后，先 action=share 并显式 appHandle/windowId/source=window，仅共享已确认目标，核对 streamReady 与 trackingWindowId；浏览器窗口须先 verify_mapping。后续观察和输入自动核验、续租并复用捕获流，不要逐次 unshare/share。share 持续到 unshare 或 quietbridge_task end；不激活目标或切换 Space，也不提供焦点隔离。"
                       "后台浏览器任务同样使用系统窗口连续视频；不在动作或导航后自动截图，也不轮询截图促帧。原生任务省略范围时默认 application。显式 application/desktop 保留系统捕获范围；只有任务确实需要跨应用、Dock 或系统界面时才显式 source=desktop，不能为解决点击失败扩大范围。"
                       "unshare 是幂等清理，阶段 end 后仍可调用，只释放本连接共享，不结束宿主会话保护，也不为清理启动新连接。activate 会改变前台焦点，不抢焦点任务禁止调用，更不能作为失败兜底。锁屏时共享立即关闭并清除画面。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "activate", "share", "unshare"], "default": "list"},
                "appHandle": APP_HANDLE_PROP,
                "windowId": {"type": "integer", "minimum": 1, "maximum": 4294967295},
                "durationSeconds": {"type": "number", "minimum": 60, "maximum": 3600, "default": 1800},
                "source": {"type": "string", "enum": ["application", "window", "desktop"],
                           "description": "window 在后台浏览器和原生任务中均表示系统目标窗口连续视频；不在网页动作后自动截图。application 使用系统应用捕获，后台 Space 时原生端收窄为目标窗口；desktop 包含目标显示器当前桌面，仅用于确有跨应用需要的任务。共享不授权激活、置前或切换 Space；显式应用/桌面范围随动作续租。"},
            },
        },
        "handler": tool_windows,
        "readOnly": True,
    },
    {
        "name": "quietbridge_state",
        "description": NATIVE_OBSERVATION_GUIDANCE + "读取应用的辅助功能界面树（紧凑文本，含 [elementIndex]、role、title/value、可用 actions），返回 snapshotId 供元素级操作。AXWebArea缺失或树不完整不代表权限失败，可调用 quietbridge_visual_state 获取真实视频帧供视觉分析；禁止自动点击或根据不确定视觉盲点操作。"
                       "浏览器里的网页内容优先 quietbridge_browser_events，AX 对网页结构通常不完整。",
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
        "name": "quietbridge_wait_ready",
        "description": NATIVE_OBSERVATION_GUIDANCE + "只读有界 AX 加载观察：显式应用、窗口、expectedUrl 和 expectedText；同一真实 AXWebArea URL与正文静态文本连续两次匹配才 ready，completionEvidence=ax_url_and_content，否则 unknown。单次调用内按 intervalSeconds>=1 轮询，共享 monotonic 总预算默认/最多30秒，包含任务核验，不重试。URL规范化scheme/host/defaultport/path，保留query、忽略fragment；不支持credentials。最多3个视频帧，仅返回最新供主代理审核，结束timeout或needs_visual_review，不自动视觉分类。权限/锁屏/进程/窗口错误立即传播。不启动、导航、点击或刷新，不访问HTTP/JS/调试接口。不证明DOM readyState或网络完成；页面身份仅AX树位置+URL，同URL重载不可区分。",
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
        "name": "quietbridge_visual_state",
        "description": NATIVE_OBSERVATION_GUIDANCE + "AXWebArea缺失、AX树不完整或自绘界面的视觉兜底观察：从目标window的SCStream取请求后的首个新complete视频帧并stop，等待最多4秒，返回PNG image、frameId、snapshotId、单调采集时间/age、point bounds及pixel尺寸。保留右上角实时视频预览，不是截图轮播。不绕过进程身份、owner、解锁、屏幕录制与真实坐标几何校验。仅供主agent视觉分析，不做OCR或视觉规划，不自动点击，禁止根据不确定视觉盲点操作；首帧不保证是实际新页面。snapshotId仅用于坐标观察，不含AX元素。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP},
            "additionalProperties": False,
        },
        "handler": tool_visual_state,
        "readOnly": True,
    },
    {
        "name": "quietbridge_screenshot",
        "description": NATIVE_OBSERVATION_GUIDANCE + "截取应用窗口的 PNG（ScreenCaptureKit），返回图片、像素尺寸、屏幕 bounds 与落盘路径。坐标换算：窗口坐标 = 屏幕坐标 - bounds.x/y；像素与 point 比例应按实际 pixelWidth/pixelHeight 和 bounds 宽高分别计算，不能假定固定 2x。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "windowId": WINDOW_PROP},
        },
        "handler": tool_screenshot,
        "readOnly": True,
    },
    {
        "name": "quietbridge_click",
        "description": NATIVE_INPUT_GUIDANCE + "点击优先使用最新 snapshotId + elementIndex；仅当前台应用和键盘窗口均为目标时用 AXPress/AXPick，后台目标走 PID+窗口定向事件。坐标点击用 windowId + x/y（窗口左上角起算的 point）；clickCount=2 双击。"
                       "元素路径不可用时先用 quietbridge_visual_state/quietbridge_screenshot 核实位置，不猜坐标。核对 focusPreserved 及前后 PID/窗口；false/null 时暂停后续输入并查明原因，不自动恢复焦点。FOCUS_STATE_UNKNOWN 表示拒绝派发；已派发或结果未知时先读回，不直接重试。",
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
        "name": "quietbridge_type_text",
        "description": NATIVE_INPUT_GUIDANCE + "向指定应用当前输入控件发送逐字符 Unicode 事件，不具备 windowId 级投递确认，desktop 绑定任务会以 UNSAFE_WINDOW_DELIVERY 拒绝。不要激活应用、点击抢焦点或改为无任务模式绕过；原生任务使用最新 AX 快照中的可写元素 quietbridge_set_value，网页用扩展 fill。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_type_text,
        "readOnly": False,
    },
    {
        "name": "quietbridge_paste",
        "description": NATIVE_INPUT_GUIDANCE + "通过剪贴板粘贴到目标应用现有输入控件并核对内容，会临时使用系统剪贴板。没有窗口级投递确认，desktop 绑定任务会以 UNSAFE_WINDOW_DELIVERY 拒绝；不通过激活或切换焦点使粘贴生效，原生任务用可写 AX 元素，网页用扩展 fill。",
        "inputSchema": {
            "type": "object",
            "properties": {"appHandle": APP_HANDLE_PROP, "text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": tool_paste,
        "readOnly": False,
    },
    {
        "name": "quietbridge_key",
        "description": NATIVE_INPUT_GUIDANCE + "向指定应用发送按键或组合键，如 key=return/tab/escape 或单字符配 modifiers。没有窗口级投递确认，desktop 绑定任务会以 UNSAFE_WINDOW_DELIVERY 拒绝。不能用于切换用户焦点、Command+Tab 或全局快捷键兜底；网页按键使用 quietbridge_browser_events command(operation=keypress)。",
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
        "name": "quietbridge_scroll",
        "description": NATIVE_INPUT_GUIDANCE + "在绑定窗口内滚动，后台使用 AX 滚动条或窗口定向滚轮，不通过置前促成滚动。deltaLinesY>0 显示上方内容、<0 显示下方内容；每轴 -10..10。省略 x/y 时用窗口中心。派发后读回核实是否移动；网页滚动走扩展 scroll。",
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
        "name": "quietbridge_drag",
        "description": NATIVE_INPUT_GUIDANCE + "窗口内拖拽使用窗口定向事件，不移动物理鼠标；根据同一窗口最新快照确认起止点（窗口内 point）。用于滑块、框选、拖放排序，默认 400ms / 15 步。派发不代表成功，拖拽后观察核实；不可用时不改为全局拖拽。",
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
        "name": "quietbridge_set_value",
        "description": NATIVE_INPUT_GUIDANCE + "直接写入同一窗口最新 quietbridge_state 快照中 valueWritable=true 的 AX 元素，不要求把目标输入框切到用户前台。只对可写 AX value 生效；写后即时回读 AX 值并报告 match、mismatch 或 unavailable，match 不代表业务效果。观察界面确认结果；不匹配或不可读时暂停核实，不通过重复写入、聚焦、按键或粘贴兜底。网页填写用扩展 fill。",
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

TOOLS.append(dict(next(t for t in TOOLS if t["name"] == "quietbridge_click"),
                  name="quietbridge_hover", description=NATIVE_INPUT_GUIDANCE + "悬停指定窗口局部坐标或最新快照元素；使用 AX 展开动作或窗口定向 hover，不移动物理鼠标。执行严格任务目标校验；无法后台悬停时报告阻塞，不能把鼠标移到用户当前桌面兜底。",
                  handler=lambda args: tool_click(args, method="hover")))
def remember_created_browser_window(task_id):
    """Persist native/extension provenance only after independent mapping."""
    browser_data = read_file(Path(TASK_FILE).with_name('browser-events.json'), {})
    browser = browser_data.get('tasks', {}).get(task_id) or {}
    target = browser.get('targetEvidence') or {}
    created = next((item for item in browser_data.get('peer', {}).get('createdTargets', [])
                    if item.get('taskId') == task_id and item.get('requestId') == target.get('createdRequestId') and
                    item.get('browserWindowId') == browser.get('browserWindowId') and
                    item.get('tabId') == browser.get('rootTabId', browser.get('tabId'))), None)
    if not created or target.get('mode') != 'created' or not browser.get('mappingVerified'):
        return
    with task_store() as data:
        record = data['tasks'].get(task_id)
        if (record and data.get('activeTaskId') == task_id and TASK_OWNER.owns(record) and
                record.get('state') == 'bound' and
                (browser.get('mapping') or {}).get('windowId') == record.get('windowId') and
                browser.get('targetIdentity') == {key: record.get(key) for key in
                    ('taskId', 'state', 'windowId', 'pid', 'processIdentity', 'sessionId')}):
            record['windowCreation'] = {key: copy.deepcopy(created[key]) for key in
                                        ('requestId', 'browserWindowId', 'tabId')}
            record['windowCreation'].update(windowId=record['windowId'],
                processIdentity=copy.deepcopy(record['processIdentity']), applicationSession=application_session())


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
    from browser_events import valid_window_bounds
    return [{'windowId': row['windowId'], 'title': row.get('title') or '',
             **({'bounds': row['bounds']} if valid_window_bounds(row.get('bounds')) else {})}
            for row in rows if isinstance(row, dict) and isinstance(row.get('windowId'), int)]


_events_tool = make_browser_events_tool(lambda: TASK_FILE, validate_browser_assignment, check_request_cancelled,
                                      preflight_root=task_root)
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
    if isinstance(args, dict) and args.get('action') == 'verify_mapping':
        remember_created_browser_window(args.get('taskId'))
    if isinstance(args, dict) and args.get('action') in ('assign', 'select_tab'):
        stop_browser_preview()
        # Preserve only target metadata across expiry; document refs expire.
        binding = TASK_BINDINGS.get(args.get('taskId'))
        if binding is not None:
            browser = read_file(Path(TASK_FILE).with_name('browser-events.json'), {}).get('tasks', {}).get(args['taskId'])
            if browser:
                binding['browserTarget'] = {k: copy.deepcopy(v) for k, v in browser.items() if k not in ('event', 'mapping')}
    if isinstance(args, dict) and args.get('action') == 'assign':
        payload = json.loads(output) if isinstance(output, str) else output
        operation_log('browser.assigned', taskId=args.get('taskId'),
                      browserWindowId=args.get('browserWindowId'), tabId=args.get('tabId'),
                      candidateRefreshed=payload.get('candidateRefreshed', False),
                      candidateAgeMs=payload.get('candidateAgeMs'))
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
                      **({'stage': args['stage']} if args.get('operation') == 'render_probe' and args.get('stage') in RENDER_PROBE_STAGES else {}),
                      commandId=payload.get('commandId'), tabId=payload.get('tabId'),
                      target=diagnostic_fields(payload.get("target", {})), route="background_browser")
    if isinstance(args, dict) and args.get('action') == 'result':
        payload = json.loads(output) if isinstance(output, str) else output
        result = payload.get('result', {}) if isinstance(payload, dict) else {}
        if payload.get('operation') == 'render_probe' and payload.get('status') == 'ok' and isinstance(result, dict):
            try:
                safe = validate_render_probe(result, {'stage': result.get('stage')})
            except (ValueError, TypeError, KeyError, OverflowError):
                safe = None
            if safe is not None:
                operation_log('browser.render_probe_result', taskId=args.get('taskId'),
                              commandId=args.get('commandId'), result=safe)
        if payload.get('operation') == 'scroll' and isinstance(result, dict):
            observed = result.get('observed') or {}
            scroll = diagnostic_geometry(observed, ('beforeY', 'afterY', 'maxY', 'expectedY'))
            for key in ('moved', 'atBoundary'):
                if type(observed.get(key)) is bool:
                    scroll[key] = observed[key]
            for key, allowed in (('direction', ('up', 'down', 'none')),
                                 ('outcome', ('scrolled', 'at_boundary', 'no_op', 'no_movement'))):
                if observed.get(key) in allowed:
                    scroll[key] = observed[key]
            operation_log('browser.scroll_result', taskId=args.get('taskId'), commandId=args.get('commandId'),
                          performed=result.get('performed'), verified=result.get('verified'), observed=scroll)
        if result.get('mimeType') == 'image/jpeg' and result.get('data'):
            metadata = dict(payload, result={k: v for k, v in result.items() if k != 'data'})
            return {"content": [{"type": "image", "mimeType": "image/jpeg", "data": result['data']},
                                {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}]}
    return output


TOOLS.append(dict(_events_tool, handler=_events_dispatch))
for _tool in TOOLS:
    if _tool['name'] in TOOL_SUMMARIES:
        _tool['description'] = TOOL_SUMMARIES[_tool['name']] + '\n' + _tool['description']
    _tool['description'] += "\n" + RECOVERY_GUIDANCE
TOOL_MAP = dict((t["name"], t) for t in TOOLS)
HINTS = {
    "TASK_ROUTE_MISMATCH": "先 quietbridge_task status 核实本阶段 routing；网页和原生界面是不同阶段，不为故障重试更换通道。",
    "BACKGROUND_ONLY": "继续使用 quietbridge_browser_events 的原任务、原标签；先检查 status，不能改用前台输入或新建窗口。",
    "TARGET_LOST": "先 task status 核实 targetRecovery；本任务创建且确认关闭的 Chrome 专用窗口按恢复计划重建，不能选择其他现存窗口或将一次关闭视为取消。",
    "PERMISSION_DENIED": "请用户通过 QuietBridge 的权限向导授予辅助功能/屏幕录制；暂停操作，不自行打开系统设置抢焦点。",
    "FOREGROUND_REQUIRED": "此路径需要前台，不符合不抢焦点要求；停止并报告阻塞，禁止调用 activate/AXRaise、切换 Space 或移动物理鼠标兜底。",
    "FOCUS_STATE_UNKNOWN": "焦点状态无法确认，原生点击已拒绝派发；先只读观察并报告，不能激活窗口或绕过焦点检查。",
    "SHARE_NOT_READY": "保留原目标，等待共享窗口恢复并确认 streamReady；未就绪时不派发输入，不激活、不置前、不切换 Space。",
    "PREVIEW_NOT_READY": "共享窗口尚未核实；保留原任务和范围，等待同一共享流就绪，不反复 unshare/share 或换前台绕过。",
    "SHARE_REQUIRED": "界面操作需要共享窗口能力；只读模式可用 quietbridge_situation 检查状态，不能绕过共享派发输入。",
    "UNSAFE_WINDOW_DELIVERY": "缺少绑定窗口的投递确认，不是系统授权失败；仅使用同窗口已确认的可写 AX 元素，扩展输入只适用于可分配的 Chrome 标签，飞书等 Electron 内嵌控件不适用。无法继续时请求用户选择，不能结束绑定、激活窗口或改用应用级键盘绕过。",
    "STALE_SNAPSHOT": "快照已失效：重新 quietbridge_state 后再操作。",
    "STALE_APP": "应用句柄或路径身份未通过核验：最多两次只读 quietbridge_apps(action=\"get\")；仍失败则通知用户，不自动重启或绕过校验。",
    "WINDOW_UNAVAILABLE": "窗口不存在或已关闭：重新 quietbridge_windows。",
    "CAPTURE_BUSY": "上一次屏幕捕获未结束：保留任务等待，重新观察共享就绪状态；不能跳过共享或改为前台输入。",
    "APP_NOT_ALLOWED": "QUIETBRIDGE_ALLOWLIST 拦截：通知用户确认目标访问范围，暂停并等待；不能更换目标绕过。",
    "SESSION_LOCKED": "屏幕已锁定：解锁后再操作。",
    "READ_ONLY": "当前 QUIETBRIDGE_READ_ONLY=1，只允许观察类操作。",
}


def call_tool(name, args):
    global CALL_RECORDS, SHARE_RECORD, OPERATION_TRACE_ID
    name = canonical_tool(name)
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
            payload = result.get('permissionStatus')
            if name == 'quietbridge_task':
                try:
                    payload = json.loads(result['content'][0]['text'])
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
            if name == _events_tool['name']:
                try:
                    payload = json.loads(result['content'][0]['text'])
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                if isinstance(payload, dict):
                    if payload.get('status') == 'queued':
                        execution = 'queued'
                    elif isinstance(args, dict) and args.get('action') == 'result':
                        execution = ('not_dispatched' if (payload.get('debuggerFailure') or {}).get('inputDispatched') is False
                                     else 'unknown')
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
            if (not error_code and name == _events_tool['name'] and isinstance(payload, dict) and
                    payload.get('status') == 'error'):
                error_code = payload.get('errorCode') or 'BROWSER_COMMAND_FAILED'
            if result.get('isError') and error_code is None:
                error_code = 'READ_ONLY' if READ_ONLY else 'UNKNOWN_TOOL' if name not in TOOL_MAP else 'BRIDGE_ERROR'
            structured['errorCode'] = error_code
            structured['suggestedNext'] = ('query_existing_result' if execution in ('queued', 'unknown') else
                                           'observe_target' if error_code else 'inspect_verification')
            if result.get('interruption') is not None:
                payload = dict(payload or {}, interruption=result['interruption'])
            recovery = recovery_policy(error_code, execution, payload)
            if recovery:
                structured['recovery'] = recovery
                structured['suggestedNext'] = ('ask_user_choice' if recovery.get('waitForUser') and recovery.get('userDecision') else
                                               'notify_user_and_wait' if recovery.get('waitForUser') else recovery['action'])
                for block in result['content']:
                    if block.get('type') == 'text':
                        if result.get('isError') or 'permissionStatus' in result:
                            block['text'] += '\n恢复指引（%s）: %s 禁止换工具绕过或重放副作用。' % (recovery['status'], recovery['message'])
                        elif isinstance(payload, dict):
                            payload['recovery'] = recovery
                            block['text'] = json.dumps(payload, ensure_ascii=False)
                        break
            if 'situation' in result:
                structured['situation'] = result.pop('situation')
            if 'resumption' in result:
                structured['resumption'] = result.pop('resumption')
            if 'targetConflict' in result:
                structured['targetConflict'] = result.pop('targetConflict')
            if 'interruption' in result:
                structured['interruption'] = result.pop('interruption')
            if 'permissionStatus' in result:
                structured['permissionStatus'] = result.pop('permissionStatus')
            if SHARE_RECORD is not None:
                structured['share'] = SHARE_RECORD
            if isinstance(payload, dict):
                structured['page'] = {k: payload[k] for k in ('status', 'commandId', 'performed', 'verified', 'mayHaveExecuted') if k in payload}
            if OPERATION_TRACE_ID:
                directory = operation_log_directory()
                structured['diagnostics'] = {"enabled": environment("QUIETBRIDGE_OPERATION_LOG") != "0", "toolTraceId": OPERATION_TRACE_ID,
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


def capability_failure(name, key):
    if key == 'application':
        return {"content": [{"type": "text", "text": "APP_NOT_RUNNING: QuietBridge 已退出，请先打开应用；本次调用未执行"}],
                "isError": True, "errorCode": "APP_NOT_RUNNING"}
    return {"content": [{"type": "text", "text": "CAPABILITY_DISABLED: %s 已关闭，本次调用未执行" % key}],
            "isError": True, "errorCode": "CAPABILITY_DISABLED"}


def _call_tool(name, args):
    global TASK_DISPATCH, WAIT_DEADLINE
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}
    switches = control_snapshot()
    blocked = capability_block(name, args, switches or {})
    if blocked:
        return capability_failure(name, blocked)
    if READ_ONLY and not tool["readOnly"]:
        return {"content": [{"type": "text", "text": "QUIETBRIDGE_READ_ONLY=1，输入类工具已禁用"}], "isError": True}
    try:
        check_request_cancelled()
        reap_task_lease()
        resumption = resume_idle_task(name, args or {})
        if resumption:
            return {'isError': True, 'errorCode': 'TASK_RESUMED', 'resumption': resumption,
                    'content': [{'type': 'text', 'text': 'TASK_RESUMED: 等待超时后的原目标已重新核验，本次输入未派发。'
                                '请立即按 nextSteps 自动恢复共享并重新观察，无需用户提醒重连。\n' +
                                json.dumps(resumption, ensure_ascii=False)}]}
        if name == "quietbridge_wait_ready":
            timeout, _ = wait_ready_options(args)
            WAIT_DEADLINE = time.monotonic() + timeout
        guarded, context = task_guard(name, args or {})
        TASK_DISPATCH = context
        if name == "quietbridge_situation" and (not isinstance(args, dict) or set(args) != {"appHandle", "windowId"}):
            raise HelperError("INVALID_ARGUMENT", "现场观察必须显式提供 appHandle 和 windowId")
        if name in ("quietbridge_state", "quietbridge_visual_state", "quietbridge_screenshot", "quietbridge_wait_ready"):
            if name == "quietbridge_visual_state" and (set(guarded) - {"appHandle", "windowId"} or
                    ("appHandle" in guarded and (not isinstance(guarded["appHandle"], str) or not guarded["appHandle"].strip())) or
                    ("windowId" in guarded and (type(guarded["windowId"]) is not int or not 1 <= guarded["windowId"] <= 4294967295))):
                raise HelperError("INVALID_ARGUMENT", "视觉观察仅接受有效 appHandle/windowId")
            if READ_ONLY:
                raise HelperError("SHARE_REQUIRED", "界面观察需要共享目标应用；只读模式不允许启动共享，请使用 quietbridge_situation")
            app = resolve_app(guarded.get("appHandle"))
            window = resolve_window(app, guarded.get("windowId"))
            ensure_share(app, window)
            guarded = dict(guarded, windowId=window)
        out = tool["handler"](guarded)
        check_request_cancelled()
        renew_task_lease(name, args)
        if isinstance(out, dict):
            return out
        return {"content": [{"type": "text", "text": out}]}
    except HelperError as exc:
        # The failed attempt is not retried. Only the current switch state can replace its result.
        blocked = capability_block(name, args, control_snapshot() or {})
        if blocked:
            return capability_failure(name, blocked)
        text = "%s: %s" % (exc.code, exc.message)
        hint = HINTS.get(exc.code)
        if not hint and "absent from the selected snapshot" in exc.message:
            hint = "快照里没有该 index：重新观察 AX 或视频帧；空树不证明自绘或可安全点击，不能猜测坐标。"
        if not hint and "resolve window ID failed" in exc.message:
            hint = "重新 quietbridge_windows 核对确切窗口、Space 与可见性；不要自动激活或换到其他窗口。"
        if hint:
            text += "\n建议: " + hint
        result = {"content": [{"type": "text", "text": text}], "isError": True, "errorCode": exc.code}
        if exc.target_conflict is not None:
            result['targetConflict'] = exc.target_conflict
        if exc.interruption is not None:
            result['interruption'] = exc.interruption
        return result
    except ValueError as exc:
        if name != "quietbridge_browser_events":
            return {"content": [{"type": "text", "text": "INVALID_ARGUMENT: %s" % exc}], "isError": True, "errorCode": "INVALID_ARGUMENT"}
        message = str(exc)
        code = next((c for c in ("BROWSER_HOST_UNAVAILABLE", "BROWSER_PERMISSION_MISSING", "BROWSER_DISABLED", "BACKGROUND_WAITING", "STALE_SNAPSHOT", "CANDIDATE_REFRESH_TIMEOUT",
                                "BROWSER_OBSERVER_NOT_READY", "BROWSER_LIFECYCLE_TIMEOUT", "BROWSER_LIFECYCLE_FAILED", "BROWSER_MAPPING_PENDING",
                                "BROWSER_TARGET_CHANGED", "STALE_TARGET", "STALE_DOCUMENT") if message.startswith(c)), "BROWSER_TARGET_WAITING")
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


def routed_schema(schema):
    """Declare the adapter envelope so host validation can preserve it.

    This field is never trusted without its one-use host-side record. The model
    does not choose session IDs and the worker handlers never see the envelope.
    """
    import copy
    value = copy.deepcopy(schema)
    if value.get('type') == 'object' or 'properties' in value:
        value.setdefault('properties', {})['_quietbridgeRoute'] = {
            'type': 'string', 'description': '宿主适配器自动填写的一次性调用凭据；Agent 不应填写。'}
    for key in ('anyOf', 'oneOf', 'allOf'):
        if key in value:
            value[key] = [routed_schema(part) for part in value[key]]
    return value


def public_schema(schema):
    """Advertise the one-use envelope only to token-routed hosts.

    A host-identity connection (DSH) rejects that field with
    SESSION_ROUTING_INVALID, so advertising it there would only invite errors.
    """
    return routed_schema(schema) if ROUTING_ENABLED and HOST_ROUTING is None else schema


def handle(msg, cancel_event=None):
    global REQUEST_CANCELLED, SESSION_ROUTER, CONNECTOR_REVALIDATE
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        if rid == 'upgrade-check' and Path(sys.argv[0]).name == 'connector.py':
            import stdio_transport
            if not getattr(stdio_transport, 'SUPPORTS_ROUTED_SESSIONS', False):
                # The immutable old bootstrap imports the updated transport on
                # exec. Keep the same stdio pipes and config hash, between calls.
                bootstrap = Path(sys.argv[0])
                info = bootstrap.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022:
                    send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
                    os.environ['QUIETBRIDGE_CONNECTOR_REEXEC'] = '1'
                    os.execv(sys.executable, [sys.executable, '-B', str(bootstrap)])
        # Also works when an older stable connector hot-loads this bridge.
        start_task_watch()
        client_version = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "QuietBridge", "version": SERVER_VERSION,
                               "icons": TOOL_ICONS},
                "instructions": AGENT_INSTRUCTIONS,
            },
        }

    if method and method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        public = [
            dict({"name": t["name"], "description": t["description"],
                  "inputSchema": public_schema(t["inputSchema"])},
                 icons=TOOL_ICONS,
                 **({"annotations": t["annotations"]} if "annotations" in t else {}))
            for t in TOOLS
            if not (READ_ONLY and not t["readOnly"])
        ]
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": public}}

    if method == "tools/call":
        if cancel_event is not None and cancel_event.is_set():
            if TASK_OWNER.fd is not None:
                interrupt_task('client_cancelled')
            from session_routing import failure
            return {'jsonrpc': '2.0', 'id': rid, 'result': failure('TASK_CANCELLED: 当前调用已取消，未派发操作')}
        if CONNECTOR_REVALIDATE:
            CONNECTOR_REVALIDATE = False
            from session_routing import failure
            return {'jsonrpc': '2.0', 'id': rid, 'result': failure(
                'CONNECTOR_UPDATED: 连接器已自动更新；本次调用未执行。请先重新核实权限与目标，建立新的界面阶段；不要重放旧输入。')}
        if ROUTING_ENABLED:
            from session_routing import SessionRouter
            with TOOL_LOCK:
                if SESSION_ROUTER is None:
                    SESSION_ROUTER = SessionRouter(Path(__file__), config_dir())
            return {'jsonrpc': '2.0', 'id': rid, 'result': SESSION_ROUTER.call(msg, cancel_event)}
        previous = REQUEST_CANCELLED
        REQUEST_CANCELLED = cancel_event if cancel_event is not None else threading.Event()
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            context = params.get('_quietbridgeContext')
            # Host-context workers never write WorkBuddy hook receipts: the DSH
            # adapter owns those through turn events, and a second receipt would
            # add an agent-less duplicate turn to the same lifecycle store.
            if SESSION_KEY and isinstance(context, dict) and not context.get('host') \
                    and not result.get('isError'):
                from adapters.workbuddy.lifecycle import handle_hook
                handle_hook({'hook_event_name': 'PostToolUse', 'session_id': context['sessionId'],
                    'tool_name': params.get('name'), 'tool_input': params.get('arguments') or {},
                    'tool_response': result}, config_dir(), context.get('requestId'))
            if REQUEST_CANCELLED.is_set():
                interrupt_task(getattr(REQUEST_CANCELLED, 'quietbridge_reason', 'client_cancelled'))
        finally:
            REQUEST_CANCELLED = previous
        reply = {"jsonrpc": "2.0", "id": rid, "result": result}
        if SESSION_KEY:
            reply['_quietbridgeRetained'] = TASK_OWNER.fd is not None or SESSION_PROTECTION.record is not None
        return reply

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
    from stdio_transport import serve
    start_task_watch()
    serve(handle, send, on_close=shutdown, concurrent_calls=not bool(SESSION_KEY))


if __name__ == "__main__":
    main()
