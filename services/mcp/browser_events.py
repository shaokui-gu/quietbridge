"""独立的浏览器观察存储；不接触 helper、输入权限或系统窗口映射。"""
import copy
import fcntl
import json
import math
import os
from compatibility import application_runtime, browser_layout, browser_runtime_root, environment
from pathlib import Path
import re
import secrets
import stat
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
# 运行态必须在非 TCC 目录：Chrome 派生的解释器读不了 ~/Documents，
# 因此安装后的任务记录放在 ~/Library/Application Support，项目内副本只作源码。
RUNTIME = browser_runtime_root()


def config_dir():
    override = environment('QUIETBRIDGE_CONFIG_DIR')
    if override:
        return Path(override)
    # Unified runtime: the native host and the connector share the app runtime
    # root (browser task state lives beside releases/ and connector/).
    if RUNTIME.is_dir():
        return RUNTIME
    return ROOT / 'quietbridge-config'


CONFIG_DIR = config_dir()
SESSION_KEY = os.environ.get('QUIETBRIDGE_SESSION_KEY', '')
if SESSION_KEY and not re.fullmatch('[a-f0-9]{64}', SESSION_KEY):
    raise ValueError('invalid session key')
SESSION_DIR = CONFIG_DIR / 'sessions' / SESSION_KEY if SESSION_KEY else CONFIG_DIR
TASK_FILE = SESSION_DIR / 'task-windows.json'
EVENT_FILE = SESSION_DIR / 'browser-events.json'
MAX_BYTES = 1024 * 1024


def application_session(runtime=None):
    """A stale file is not a live app: the menu must still hold its kernel lock."""
    runtime = Path(runtime or application_runtime())
    try:
        info = runtime.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return None
        with directory(runtime.resolve()) as parent:
            source = os.open('application.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(source, 'rb') as stream:
                info = os.fstat(source)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    return None
                try:
                    fcntl.flock(source, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    return None
                except BlockingIOError:
                    pass
                raw = stream.read(256)
                if len(raw) >= 256:
                    return None
                record = json.loads(raw)
                if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 2:
                    return None
                value = record.get('sessionId')
                return value if isinstance(value, str) and re.fullmatch(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', value) else None
    except (OSError, ValueError, UnicodeError):
        return None


def require(condition, message):
    if not condition:
        raise ValueError(message)


@contextmanager
def directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name, default=None):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return copy.deepcopy(default)
    with os.fdopen(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), '记录必须是当前用户的普通文件')
        raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, '记录超出大小限制')
        return json.loads(raw)


def read_file(path, default=None):
    path = Path(path)
    with directory(path.parent) as fd:
        return read_at(fd, path.name, default)


def atomic_write(fd, name, data):
    temporary = '.browser-events-' + secrets.token_hex(16) + '.tmp'
    raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
    require(len(raw) <= MAX_BYTES, '记录超出大小限制')
    try:
        target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        with os.fdopen(target, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), '拒绝替换非普通记录')
        except FileNotFoundError:
            pass
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


@contextmanager
def event_store(path):
    path = Path(path)
    with directory(path.parent) as fd:
        lock = os.open(path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=fd)
        try:
            info = os.fstat(lock)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), '锁必须是当前用户普通文件')
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read_at(fd, path.name, {'version': 1, 'tasks': {}})
            require(isinstance(data, dict) and data.get('version') == 1 and
                    isinstance(data.get('tasks'), dict), '观察记录格式错误')
            before = copy.deepcopy(data)
            yield data
            if before != data:
                atomic_write(fd, path.name, data)
        finally:
            os.close(lock)


def task_exists(task_id, task_file):
    require(isinstance(task_id, str) and 0 < len(task_id) <= 128, 'taskId 类型或长度错误')
    data = read_file(task_file)
    require(isinstance(data, dict) and data.get('version') == 1 and
            isinstance(data.get('tasks'), dict), '任务记录不存在或格式错误')
    task = data['tasks'].get(task_id)
    require(isinstance(task, dict) and task.get('taskId') == task_id and
            task.get('state') in ('awaiting_window', 'bound', 'requires_revalidation'), '任务不存在或已失效')


def valid_int(value, low=0, high=2147483647):
    return type(value) is int and low <= value <= high


def authorized_origin(value):
    require(type(value) is str and len(value) <= 8192 and value.isascii() and
            not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) and
            not any(c in value for c in ('\\', '%', '?', '#')), '站点 origin 格式错误')
    parsed = urlsplit(value)
    require(parsed.scheme in ('http', 'https') and parsed.hostname and
            parsed.username is None and parsed.password is None and not parsed.path and
            parsed.netloc == parsed.netloc.lower(), '仅允许规范 http/https origin')
    port = parsed.port
    require(port not in (80 if parsed.scheme == 'http' else 443,) and
            not parsed.netloc.endswith(':'), 'origin 必须使用规范端口')
    return value


def valid_selector(value):
    return type(value) is str and len(value) <= 128 and (not value or
        re.fullmatch(r'(?:[a-zA-Z][a-zA-Z0-9-]*)?(?:[.#][a-zA-Z_][a-zA-Z0-9_-]*)*', value) is not None)


CREATED_FIELDS = {'requestId', 'browserWindowId', 'tabId'}
COMMAND_TTL = 15
CANDIDATE_TTL = 20
CANDIDATE_REFRESH_TIMEOUT = 3
CANDIDATE_QUERY_TIMEOUT = 3
NAVIGATION_OBSERVATION_TIMEOUT = 3
MAX_RESULTS = 8
MAX_SEEN = 64
OPERATIONS = ('snapshot', 'read', 'click', 'fill', 'select', 'scroll', 'navigate', 'diagnostics', 'screenshot', 'keypress', 'render_probe')
OBSERVATIONS = ('snapshot', 'read', 'diagnostics', 'screenshot', 'render_probe')
RENDER_PROBE_STAGES = ('inspect', 'attach', 'enable', 'layout', 'capture')
MAX_ELEMENTS = 300
MAX_LABEL = 80
ACTION_FIELDS = ('ref', 'value', 'deltaY', 'direction', 'lines', 'url', 'snapshotId', 'key',
                 'mode', 'offset', 'limit', 'maxChars', 'readId', 'stage')


def verification_next(peer):
    verify = peer.get('pendingVerify')
    return ('navigation_observation' if verify.get('operation') == 'navigate' and
            not verify.get('destinationChanged') else 'snapshot') if verify else 'done'


def mark_navigation_destination(peer, pending, record, outcome):
    """A valid observation of a redirected page needs explicit snapshot review."""
    verify = peer.get('pendingVerify') or {}
    event = (record or {}).get('event') or {}
    if (verify.get('operation') == 'navigate' and record and
            all(verify.get(k) == pending.get(k) == record.get(k)
                for k in ('taskId', 'bindingId', 'tabId', 'browserWindowId')) and
            event.get('documentId') == pending.get('documentId') and
            event.get('navigationEpoch') == pending.get('navigationEpoch') and
            event.get('navigationEpoch', 0) > verify.get('navigationEpoch', 0) and
            event.get('domReady') and event.get('readyState') in ('interactive', 'complete') and
            event.get('url') != verify.get('url') and
            (pending['operation'] == 'screenshot' or outcome.get('url') == event.get('url'))):
        verify['destinationChanged'] = True
        verify['observedUrl'] = event['url']


def finish_pending(peer, status, now):
    command = peer.get('command')
    if not command:
        return
    terminal = {'commandId': command['requestId'], 'status': status, 'at': now,
                'operation': command['operation'], 'taskId': command['taskId'],
                'mayHaveExecuted': bool(command.get('delivered'))}
    peer['commandTerminals'] = (peer.get('commandTerminals', []) + [terminal])[-MAX_SEEN:]
    if status == 'expired':
        peer['commandExpired'] = command['requestId']  # Read old stores compatibly.
    else:
        peer['commandCanceled'] = {**terminal, 'delivered': terminal['mayHaveExecuted']}
    if command.get('delivered') and command['operation'] not in OBSERVATIONS:
        # Unknown input outcomes still require a snapshot, including navigation
        # whose dispatch outcome is unknown. Never infer success or replay it.
        peer['pendingVerify'] = {'commandId': command['requestId'], 'taskId': command['taskId']}
    peer['command'] = None


def expire_pending(peer, now):
    command = peer.get('command')
    if command and now >= command.get('expiresAt', 0):
        finish_pending(peer, 'expired', now)


def redacted_url(value):
    parsed = urlsplit(value)
    return parsed.scheme + '://' + parsed.netloc + (parsed.path or '/')


def navigation_verified(peer, pending, record, outcome):
    verify = peer.get('pendingVerify') or {}
    event = (record or {}).get('event') or {}
    if not (verify.get('operation') == 'navigate' and record and
            all(verify.get(k) == pending.get(k) == record.get(k)
                for k in ('taskId', 'bindingId', 'tabId', 'browserWindowId')) and
            event.get('documentId') == pending.get('documentId') and
            event.get('navigationEpoch') == pending.get('navigationEpoch') and
            event.get('navigationEpoch', 0) > verify.get('navigationEpoch', 0) and
            event.get('domReady') and event.get('readyState') in ('interactive', 'complete') and
            event.get('url') == verify.get('url')):
        return False
    # A screenshot attests only to this current viewport, never the entire page
    # or the business outcome. Other observations also attest their own URL.
    return pending['operation'] == 'screenshot' or (
        outcome.get('url') == event['url'] and (pending['operation'] == 'read' or
        outcome.get('readyState') in ('interactive', 'complete')))


def command_params(args, operation):
    allowed = {'snapshot': set(), 'read': {'mode', 'offset', 'limit', 'maxChars', 'readId'},
               'click': {'ref'}, 'fill': {'ref', 'value'},
               'select': {'ref', 'value'}, 'scroll': {'ref', 'direction', 'lines'},
               'navigate': {'url'}, 'diagnostics': set(), 'screenshot': set(), 'keypress': {'key'},
               'render_probe': {'stage'}}[operation]
    if operation == 'scroll' and 'deltaY' in args:
        raise ValueError('浏览器滚动请使用 direction=down/up 和 lines，不接受有符号 deltaY')
    if operation in ('click', 'fill', 'select', 'scroll'):
        allowed.add('snapshotId')
    given = {key for key in ACTION_FIELDS if key in args}
    require(given <= allowed, '该命令不接受这些参数')
    if operation == 'render_probe':
        require(args.get('stage') in RENDER_PROBE_STAGES, 'render_probe 需要固定 stage')
    if operation == 'read':
        require(args.get('mode', 'content') in ('links', 'content'), 'read mode 必须为 links/content')
        for key, low, high, default in (('offset', 0, 2000, 0), ('limit', 1, 100, 30),
                                        ('maxChars', 4000, 16000, 10000)):
            require(valid_int(args.get(key, default), low, high), 'read %s 超出范围' % key)
        require('readId' not in args or (type(args['readId']) is str and 0 < len(args['readId']) <= 180),
                'readId 格式错误')
        require(args.get('offset', 0) == 0 or 'readId' in args, '分页必须传原 readId')
    if 'snapshotId' in args:
        require(type(args['snapshotId']) is str and 0 < len(args['snapshotId']) <= 160, '快照标识错误')
    if operation == 'keypress':
        require(args.get('key') in ('Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight',
                'Home', 'End', 'PageUp', 'PageDown', 'Backspace', 'Delete', 'Space'), '页面按键不支持')
    if 'ref' in args:
        require(valid_int(args['ref'], 1), '元素引用必须是正整数')
    if 'value' in args:
        require(type(args['value']) is str and len(args['value']) <= 1000, '输入值类型或长度错误')
    if operation == 'scroll':
        require(args.get('direction') in ('up', 'down'), '滚动必须指定 direction=down/up')
        require(valid_int(args.get('lines', 6), 1, 10), 'lines 必须是 1..10 的整数')
    if operation in ('click', 'fill', 'select'):
        require('ref' in args, '该命令必须指定元素引用')
    if operation == 'navigate':
        require('url' in args, '导航命令必须指定地址')
        creation_url(args['url'])
    params = {key: args[key] for key in ACTION_FIELDS if key in args}
    if operation == 'scroll' and 'direction' in params:
        # Preserve the installed extension's legacy sign convention on the wire.
        # Callers use words for direction and never need to infer a sign.
        params['deltaY'] = params.pop('lines', 6) * (-1 if params.pop('direction') == 'down' else 1)
    return params


def validate_diagnostics(result):
    require(type(result) is dict and
            set(result) == {'url', 'readyState', 'errors', 'viewport', 'scroll'}, '诊断字段错误')
    require(type(result['url']) is str and 0 < len(result['url']) <= 8192, '诊断 URL 错误')
    require(result['readyState'] in ('loading', 'interactive', 'complete'), '诊断文档状态错误')
    errors = result['errors']
    require(type(errors) is list and len(errors) <= 20, '诊断错误数量错误')
    for item in errors:
        require(type(item) is dict and set(item) == {'message', 'at'}, '诊断错误字段错误')
        require(type(item['message']) is str and 0 < len(item['message']) <= 200, '诊断错误文本错误')
        require(valid_int(item['at'], high=4102444800000), '诊断错误时间错误')
    viewport = result['viewport']
    require(type(viewport) is dict and set(viewport) == {'width', 'height'}, '诊断视口字段错误')
    for value in viewport.values():
        require(valid_int(value, high=100000), '诊断视口数值错误')
    scroll = result['scroll']
    require(type(scroll) is dict and set(scroll) == {'x', 'y'}, '诊断滚动字段错误')
    for value in scroll.values():
        require(type(value) is int and -1000000 <= value <= 1000000, '诊断滚动数值错误')
    return copy.deepcopy(result)


def validate_render_probe(result, params):
    base = {'stage', 'startedAt', 'completedAt', 'performed', 'attachedBefore', 'pageEnabledBefore',
            'attachedAfter', 'pageEnabledAfter'}
    stage = params.get('stage')
    require(stage in RENDER_PROBE_STAGES, '呈现探针阶段错误')
    extra = {'viewportWidth', 'viewportHeight'} if stage == 'layout' else {'captureDataLength'} if stage == 'capture' else set()
    require(type(result) is dict and set(result) == base | extra and result.get('stage') == stage,
            '呈现探针回执阶段或字段错误')
    for key in ('performed', 'attachedBefore', 'pageEnabledBefore', 'attachedAfter', 'pageEnabledAfter'):
        require(type(result[key]) is bool, '呈现探针状态格式错误')
    for key in ('startedAt', 'completedAt'):
        require(type(result[key]) in (int, float) and 0 < result[key] < 4102444800, '呈现探针时间错误')
    require(result['startedAt'] <= result['completedAt'] and result['completedAt'] - result['startedAt'] <= COMMAND_TTL,
            '呈现探针执行时间错误')
    for key in extra:
        require(type(result[key]) in (int, float) and 0 < result[key] <= (100000 if stage == 'layout' else 34000000),
                '呈现探针数值错误')
    a, b = result['attachedBefore'], result['attachedAfter']
    e, f = result['pageEnabledBefore'], result['pageEnabledAfter']
    require((not e or a) and (not f or b), '呈现探针连接状态矛盾')
    expected = {'inspect': (a, e, False), 'attach': (True, e, not a),
                'enable': (True, True, not e), 'layout': (True, True, True),
                'capture': (True, True, True)}[stage]
    require((b, f, result['performed']) == expected and
            (stage not in ('enable', 'layout', 'capture') or a) and
            (stage not in ('layout', 'capture') or e), '呈现探针阶段状态矛盾')
    if stage == 'capture':
        require(type(result['captureDataLength']) is int, '呈现探针长度必须是整数')
    return copy.deepcopy(result)


def validate_action_result(result, operation):
    require(type(result) is dict and {'performed', 'verified', 'observed'} <= set(result) <= {'performed', 'verified', 'observed', 'children'},
            '动作结果字段错误')
    require(type(result['performed']) is bool and type(result['verified']) is bool, '动作结果状态错误')
    observed = result['observed']
    require(type(observed) is dict and len(observed) <= 16, '动作观测字段错误')
    for key, value in observed.items():
        require(type(key) is str and 0 < len(key) <= 32, '观测键错误')
        # 点击/悬停的观测点来自 CSS 坐标，宽度可能带小数；接受有界有限浮点。
        require(type(value) in (bool, int) or
                (type(value) is float and math.isfinite(value) and abs(value) <= 1e9) or
                (type(value) is str and len(value) <= 200),
                '观测值类型或长度错误')
    children = result.get('children', [])
    require(type(children) is list and len(children) <= 4, '子标签数量错误')
    for child in children:
        require(type(child) is dict and set(child) == {'tabId', 'browserWindowId', 'url'} and
                valid_int(child['tabId'], 1) and valid_int(child['browserWindowId'], 1), '子标签身份错误')
        creation_url(child['url'])
    return {'operation': operation, 'performed': result['performed'],
            'verified': result['verified'], 'observed': copy.deepcopy(observed),
            **({'children': copy.deepcopy(children)} if children else {})}


def validate_snapshot(result):
    require(type(result) is dict and {'url', 'readyState', 'elementCount', 'elements'} <= set(result) <= {'url', 'readyState', 'elementCount', 'elements', 'snapshotId', 'coverage'},
            '快照字段错误')
    require(type(result['url']) is str and 0 < len(result['url']) <= 8192, '快照 URL 错误')
    require(result['readyState'] in ('loading', 'interactive', 'complete'), '快照文档状态错误')
    require(valid_int(result['elementCount'], high=10000), '快照元素总数错误')
    if 'snapshotId' in result:
        require(type(result['snapshotId']) is str and 0 < len(result['snapshotId']) <= 160, '快照标识错误')
    elements = result['elements']
    require(type(elements) is list and len(elements) <= MAX_ELEMENTS, '快照元素数量错误')
    if 'coverage' in result:
        coverage = result['coverage']
        require(type(coverage) is dict and set(coverage) ==
                {'returnedElements', 'limit', 'truncated', 'scope', 'elementCountScope'} and
                type(coverage['returnedElements']) is int and coverage['returnedElements'] == len(elements) and
                type(coverage['limit']) is int and coverage['limit'] == MAX_ELEMENTS and
                type(coverage['truncated']) is bool and (not coverage['truncated'] or len(elements) == MAX_ELEMENTS) and
                coverage['scope'] == 'visible_interactive_top_and_accessible_frames' and
                coverage['elementCountScope'] == 'top_document_dom_nodes_capped_10000', '快照覆盖范围错误')
    seen = set()
    for element in elements:
        base = {'ref', 'tag', 'role', 'name', 'disabled', 'x', 'y', 'width', 'height'}
        require(type(element) is dict and (set(element) == base or
                set(element) == base | {'href', 'urlHasQuery', 'urlHasFragment'}), '快照元素字段错误')
        if 'href' in element:
            validate_link(element)
        require(valid_int(element['ref'], 1) and element['ref'] not in seen, '快照元素引用重复或非法')
        seen.add(element['ref'])
        require(type(element['tag']) is str and 1 <= len(element['tag']) <= 32, '元素标签错误')
        require(type(element['role']) is str and len(element['role']) <= 32, '元素角色错误')
        require(type(element['name']) is str and len(element['name']) <= MAX_LABEL, '元素名称错误')
        require(type(element['disabled']) is bool, '元素禁用状态错误')
        for key in ('x', 'y', 'width', 'height'):
            require(type(element[key]) is int and -100000 <= element[key] <= 100000, '元素几何错误')
    return copy.deepcopy(result)


def validate_link(item):
    require(type(item.get('href')) is str and 0 < len(item['href']) <= 2048, '链接长度错误')
    url = urlsplit(item['href'])
    base = authorized_origin(url.scheme + '://' + url.netloc)
    require(item['href'] == base + url.path and url.path.startswith('/') and
            not url.query and not url.fragment, '链接必须为脱敏 HTTP(S) URL')
    require(type(item.get('urlHasQuery')) is bool and type(item.get('urlHasFragment')) is bool, '链接脱敏标记错误')


def validate_read(result, params):
    fields = {'readId', 'mode', 'url', 'title', 'offset', 'nextOffset', 'totalItems',
              'sourceTruncated', 'domOnly', 'observedAt', 'items'}
    require(type(result) is dict and set(result) == fields, '正文读取字段错误')
    require(type(result['readId']) is str and 0 < len(result['readId']) <= 180, '读取标识错误')
    require(result['mode'] == params.get('mode', 'content'), '读取模式不符')
    if params.get('readId'):
        require(result['readId'] == params['readId'], '读取分页标识已变化')
    require(type(result['url']) is str and len(result['url']) <= 8192 and
            type(result['title']) is str and len(result['title']) <= 200, '读取页面信息错误')
    require(valid_int(result['offset'], 0, 2000) and result['offset'] == params.get('offset', 0) and
            valid_int(result['totalItems'], result['offset'], 2000), '读取范围错误')
    items = result['items']
    require(type(items) is list and len(items) <= params.get('limit', 30), '读取条目超限')
    end = result['offset'] + len(items)
    require(end <= result['totalItems'] and
            ((end == result['totalItems'] and result['nextOffset'] is None) or
             (end < result['totalItems'] and items and type(result['nextOffset']) is int and result['nextOffset'] == end)),
            '读取分页不连续')
    require(type(result['sourceTruncated']) is bool and result['domOnly'] is True and
            valid_int(result['observedAt'], 0, 4102444800000), '读取范围或时间标记错误')
    cost = 0
    for item in items:
        if result['mode'] == 'links':
            require(type(item) is dict and set(item) == {'name', 'href', 'urlHasQuery', 'urlHasFragment'} and
                    type(item['name']) is str and len(item['name']) <= 160, '读取链接字段错误')
            validate_link(item)
        else:
            require(type(item) is dict and set(item) == {'id', 'kind', 'text', 'textOffset', 'textComplete'} and
                    item['kind'] in ('article', 'text') and type(item['id']) is str and 0 < len(item['id']) <= 120 and
                    type(item['text']) is str and 0 < len(item['text']) <= 2000 and
                    valid_int(item['textOffset'], 0, 262144) and type(item['textComplete']) is bool,
                    '读取正文字段错误')
        cost += len(json.dumps(item, ensure_ascii=False, separators=(',', ':')))
    require(cost <= params.get('maxChars', 10000), '读取字符超限')
    return copy.deepcopy(result)


def validate_event(event):
    fields = {'browserWindowId', 'tabId', 'documentId', 'navigationEpoch', 'url', 'phase',
              'loaded', 'domReady', 'contentReady', 'bodyPresent', 'bodyVisible',
              'elementCount', 'readyState', 'conditionMatched', 'sequence', 'mappingVerified', 'bindingId'}
    require(type(event) is dict and set(event) == fields, '事件字段不合法')
    for key in ('browserWindowId', 'tabId', 'navigationEpoch', 'sequence'):
        require(valid_int(event[key]), key + ' 必须是有效整数')
    require(valid_int(event['elementCount'], high=10000), 'elementCount 超出限制')
    for key in ('loaded', 'domReady', 'bodyPresent', 'bodyVisible'):
        require(type(event[key]) is bool, key + ' 必须是布尔值')
    require(event['contentReady'] is None, 'contentReady 必须为 unknown/null')
    require(event['conditionMatched'] is None or type(event['conditionMatched']) is bool, '条件状态类型错误')
    require(event['readyState'] in (None, 'loading', 'interactive', 'complete'), '文档状态错误')
    require(event['mappingVerified'] is False, '观察不能验证系统窗口映射')
    require(type(event['bindingId']) is str and re.fullmatch('[0-9a-f]{32}', event['bindingId']), '绑定生命周期标识错误')
    require(event['documentId'] is None or
            (type(event['documentId']) is str and 0 < len(event['documentId']) <= 128), 'documentId 不合法')
    require(type(event['phase']) is str and event['phase'] in
            ('bound', 'before', 'committed', 'dom', 'completed', 'history', 'content', 'error', 'pageshow', 'paused'), '事件阶段不合法')
    url = event['url']
    if url is not None:
        require(type(url) is str and len(url) <= 8192 and
                not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) and
                not any(c in url for c in ('\\', '?', '#')), 'URL 类型或脱敏格式错误')
        parsed = urlsplit(url)
        base = authorized_origin(parsed.scheme + '://' + parsed.netloc)
        require(url == base + parsed.path and parsed.path.startswith('/'), 'URL 必须为脱敏规范地址')
    else:
        require(event['phase'] in ('bound', 'paused'), '此阶段需要同源 URL')
    if event['phase'] in ('bound', 'before', 'committed', 'history', 'error', 'paused'):
        require(not any(event[k] for k in ('loaded', 'domReady', 'bodyPresent', 'bodyVisible', 'elementCount')) and
                event['readyState'] is None and event['conditionMatched'] is None, '该阶段必须清空就绪信息')
    if event['phase'] in ('bound', 'before', 'paused'):
        require(event['documentId'] is None, '该阶段不能保留文档身份')
    elif event['phase'] != 'error':
        require(event['documentId'] is not None, '缺少文档身份')
    if event['readyState'] is not None:
        require(event['loaded'] == (event['readyState'] == 'complete') and
                event['domReady'] == (event['readyState'] != 'loading'), '文档加载状态不一致')
    return copy.deepcopy(event)


def lease_live(record, parent, now=None):
    lease = record.get('lease')
    if lease is None:
        return True  # Legacy records still require the existing explicit revalidation.
    if not isinstance(lease, dict):
        return False
    try:
        token = lease['ownerId']
        # A revoked worker keeps the window excluded while its native channel
        # closes. Expiring the activity timer must not admit a new writer before
        # cleanup; the kernel owner lock still releases on worker exit.
        if (str(uuid.UUID(token)) != token or (record.get('state') != 'stopping' and (
                not 0 < lease['expiresAt'] - (time.time() if now is None else now) <= 125 or
                not 0 < lease['expiresAtUptime'] - time.monotonic() <= 125))):
            return False
        path = Path(parent) / ('task-owner-' + token + '.lock')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return False
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return stream.read(64).decode('ascii') == token
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False


def active_task(task_file):
    data = read_file(task_file, {'version': 1, 'tasks': {}})
    require(type(data) is dict and data.get('version') == 1 and type(data.get('tasks')) is dict,
            '任务记录格式错误')
    task_id = data.get('activeTaskId')
    task = data['tasks'].get(task_id) if type(task_id) is str else None
    if task is not None and (task.get('state') in ('ended', 'interrupted') or not lease_live(task, Path(task_file).parent)):
        return None, None
    if task is not None:
        from lifecycle_store import stop_reason
        parent = Path(task_file).parent
        config = parent.parent.parent if parent.parent.name == 'sessions' else parent
        if task.get('state') == 'stopping' or stop_reason(task, config):
            return None, None
    return task_id, task


def target_identity(task):
    return {k: copy.deepcopy(task.get(k)) for k in
            ('taskId', 'state', 'windowId', 'pid', 'processIdentity', 'sessionId')}


def resume_assignment(previous, task, task_file, preflight_event_file=None):
    """A fresh document lease for an idle-resumed task; never reuse input refs."""
    with event_store(Path(task_file).with_name('browser-events.json')) as data:
        peer = data.setdefault('peer', {})
        root_peer = connected_peer(preflight_event_file) if preflight_event_file else {}
        root_ready = (root_peer.get('enabled') and root_peer.get('permission') and
                      root_peer.get('origin') == previous.get('origin'))
        # Idle tasks leave the extension's channel registry. A fresh root
        # heartbeat can prove consent while that private channel restarts;
        # no root tab/document evidence is imported.
        require(peer.get('applicationSession') == application_session() and
                peer.get('enabled') and peer.get('permission') and
                (root_ready or 0 <= time.time() - peer.get('lastSeen', 0) < CANDIDATE_TTL),
                'BROWSER_HOST_UNAVAILABLE: 等待原扩展通道恢复后再核验原标签')
        require(previous.get('taskId') == task['taskId'] and not previous.get('paused') and
                not data.get('tasks', {}).get(task['taskId'], {}).get('paused') and
                previous.get('origin') == peer.get('origin'), 'STALE_TARGET: 原标签授权或归属已经变化')
        version = max(data.get('assignmentVersion', 0), previous.get('assignmentVersion', 0)) + 1
        data['assignmentVersion'] = version
        record = copy.deepcopy(previous)
        record.update(token=secrets.token_hex(32), bindingId=secrets.token_hex(16),
                      assignmentVersion=version, targetIdentity=target_identity(task),
                      mappingVerified=False, event=None)
        record.pop('mapping', None)
        finish_pending(peer, 'canceled', time.time())
        peer.update(snapshot=None, pendingVerify={'taskId': task['taskId'], 'operation': 'resume',
                                                 'commandId': 'idle-resume'})
        for key in ('assignmentTitle', 'windowEvidence', 'observerStatus', 'previewLease', 'previewFrame', 'windowPreview'):
            peer.pop(key, None)
        data['tasks'] = {task['taskId']: record}


def prune_creation(peer, task_id, task):
    request = peer.get('createRequest')
    if request and (request.get('taskId') != task_id or not task or
                    task.get('state') not in ('awaiting_window', 'bound')):
        # Keep only IDs so late acknowledgements can drain without re-creating
        # a window or blocking every following extension heartbeat.
        peer['canceledCreateRequests'] = ([request['requestId']] + peer.get('canceledCreateRequests', []))[:8]
        peer['createRequest'] = None


def mapping_conflict(record, task):
    """已映射但映射窗口与任务绑定窗口不同时返回 True；未映射返回 None。"""
    mapping = record.get('mapping') or {}
    if not record.get('mappingVerified') or not isinstance(mapping.get('windowId'), int):
        return None
    return mapping['windowId'] != task.get('windowId')


def current_assignment(data, task_file):
    task_id, task = active_task(task_file)
    record = data['tasks'].get(task_id)
    if (not task or task.get('state') != 'bound' or not record or
            not record.get('globalOrigins') or record.get('targetIdentity') != target_identity(task)):
        return None
    return record


def creation_url(value):
    require(type(value) is str and 0 < len(value) <= 2048, '创建 URL 类型或长度错误')
    require(not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value), '创建 URL 含控制字符')
    parsed = urlsplit(value)
    require(parsed.scheme in ('http', 'https') and bool(parsed.hostname) and
            parsed.username is None and parsed.password is None, '仅允许无凭据的 http/https 地址')
    return value


TITLE_SEPARATORS = (' - ', ' — ', ' – ', ' | ', ' · ')


def window_title_relation(window_title, target):
    """Compare a native window title with the bound tab title.

    macOS Chrome always decorates the window title as
    "<page title> - Google Chrome - <profile>", so exact equality alone fails
    whenever the title has to come from the window list instead of the
    extension's active-tab evidence. A prefixed match is accepted only when the
    page title is followed by a real separator, so "Step 5" can never match
    "Step 50" or an unrelated window that merely contains the same text.
    """
    window = window_title.strip()
    want = target.strip()
    if not window or not want:
        return None
    if window == want:
        return 'exact'
    for separator in TITLE_SEPARATORS:
        if window.startswith(want + separator):
            return 'prefixed'
    return None


def valid_window_bounds(bounds):
    return (type(bounds) is dict and set(bounds) == {'x', 'y', 'width', 'height'} and
            all(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1000000
                for value in bounds.values()) and bounds['width'] > 0 and bounds['height'] > 0)


def bound_native_target(task):
    return (task is not None and task.get('state') == 'bound' and
            valid_int(task.get('windowId'), 1, 4294967295) and
            valid_int(task.get('pid'), 1) and
            type(task.get('processIdentity')) is dict and
            task['processIdentity'].get('pid') == task['pid'] and
            all(type(task['processIdentity'].get(k)) is str and
                task['processIdentity'][k].isdigit()
                for k in ('startedSeconds', 'startedMicroseconds')) and
            type(task.get('sessionId')) is str and bool(task['sessionId']))


LIFECYCLE_STAGES = {'observer_load', 'observer_configure', 'observer_stop', 'guard_stop', 'guard_install',
                    'target_release', 'debugger_detach', 'probe_attach', 'document_lookup', 'assignment_lookup',
                    'window_evidence', 'candidate_lookup'}
LIFECYCLE_CODES = {'BROWSER_LIFECYCLE_TIMEOUT', 'BROWSER_LIFECYCLE_FAILED'}


def validate_debugger_failure(value):
    fields = {'reason', 'stage', 'chromeMessage', 'foreignFrameExtensionIds', 'frameEvidence'}
    require(type(value) is dict and set(value) in (fields, fields | {'inputDispatched'}) and
            ('inputDispatched' not in value or value['inputDispatched'] is False), '调试失败证据字段错误')
    require(value['reason'] in ('foreign_extension_page', 'debugger_in_use') and
            type(value['stage']) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,79}', value['stage']),
            '调试失败阶段错误')
    message = value['chromeMessage']
    require(type(message) is str and len(message) <= 200 and
            (re.fullmatch(r'Cannot access a chrome-extension:// URL of different extension\.?', message) if
             value['reason'] == 'foreign_extension_page' else
             re.fullmatch(r'Another debugger is already attached to the (?:tab|target|background page) with id: [\w-]+\.?', message)),
            'Chrome 调试错误原文不匹配')
    ids = value['foreignFrameExtensionIds']
    require(type(ids) is list and len(ids) <= 8 and all(type(x) is str and re.fullmatch('[a-p]{32}', x) for x in ids) and
            len(set(ids)) == len(ids), '调试失败 frame 身份错误')
    require(value['frameEvidence'] in ('not_checked', 'top_document_iframe_src') and
            (not ids or value['frameEvidence'] == 'top_document_iframe_src') and
            (value['reason'] == 'foreign_extension_page' or (not ids and value['frameEvidence'] == 'not_checked')),
            '调试失败 frame 证据错误')
    return copy.deepcopy(value)


def current_observer_status(peer, record):
    observation = peer.get('observerStatus')
    event = (record or {}).get('event') or {}
    if (observation and record and observation['bindingId'] == record['bindingId'] and
            observation['documentId'] == event.get('documentId') and
            observation['navigationEpoch'] == event.get('navigationEpoch')):
        return observation
    return None


def sync_peer(message, origin, task_file, event_file):
    session = application_session()
    require(session is not None, 'APP_NOT_RUNNING: 请先打开 QuietBridge')
    base = {'action', 'enabled', 'permission'}
    optional = ('candidates', 'created', 'commandResult', 'clientVersion', 'assignmentTitle', 'capabilities', 'panelState', 'panelResult', 'applicationSession', 'windowEvidence', 'observerStatus', 'lifecycleError', 'previewFrame', 'navigationCapture')
    require(type(message) is dict and base <= message.keys() <= base | set(optional), '握手字段错误')
    require(type(message['enabled']) is bool and type(message['permission']) is bool, '权限类型错误')
    require('applicationSession' not in message or message['applicationSession'] is None or
            (type(message['applicationSession']) is str and 0 < len(message['applicationSession']) <= 128), '应用会话格式错误')
    now = time.time()
    with event_store(event_file) as data:
        peer = data.setdefault('peer', {})
        session_reset = (peer.get('applicationSession') not in (None, session) or
                         ('applicationSession' in message and message['applicationSession'] != session))
        if peer.get('applicationSession') != session:
            # A fresh app session must never resume a previous task or replay commands.
            data['tasks'] = {}
            peer.clear()
            peer['applicationSession'] = session
        if session_reset:
            # An old in-flight sync may carry results after the app restarts.
            # Acknowledge the new session without importing that old evidence.
            message = {key: value for key, value in message.items() if key in
                       ('action', 'enabled', 'permission', 'clientVersion', 'capabilities')}
        if 'panelState' in message:
            state = message['panelState']
            require(type(state) is dict and set(state) == {'taskSummary', 'activity', 'downloads', 'review'}, '面板状态字段错误')
            require(len(json.dumps(state, ensure_ascii=False).encode('utf-8')) <= 600000, '面板状态过大')
            task_id, _ = active_task(task_file)
            summary = state['taskSummary']
            require(summary is None or type(summary) is dict, '面板任务格式错误')
            if summary and summary.get('taskId') != task_id:
                state = {'taskSummary': None, 'activity': [], 'downloads': [], 'review': None}
                summary = None
            require(type(state['activity']) is list and len(state['activity']) <= 100 and
                    type(state['downloads']) is list and len(state['downloads']) <= 50, '面板列表无效')
            review = state['review']
            require(review is None or (type(review) is dict and summary and review.get('taskId') == task_id and
                    type(review.get('image')) is str and len(review['image']) <= 500000 and
                    re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', review['image']) and
                    type(review.get('annotations')) is list and len(review['annotations']) <= 20), '面板截图无效')
            peer['panelState'] = {**copy.deepcopy(state), 'at': now}
            if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_BYTES - 32768:
                peer['panelState']['review'] = None
                peer['panelState']['reviewOmitted'] = bool(review)
        if 'panelResult' in message:
            result = message['panelResult']
            pending = peer.get('panelCommand')
            require(type(result) is dict and set(result) == {'requestId', 'status', 'error'} and
                    result['status'] in ('ok', 'error') and type(result['error']) is str and
                    len(result['error']) <= 200, '面板命令结果无效')
            if pending and result['requestId'] == pending.get('requestId'):
                peer['panelResult'] = {**result, 'at': now}
                peer['panelCommand'] = None
        requested = peer.get('requestCandidates', False)
        if 'candidates' in message:
            require(requested and message['enabled'] and message['permission'], '候选列表未请求或未授权')
            candidates = message['candidates']
            require(type(candidates) is list and len(candidates) <= 1000, '候选数量错误')
            for candidate in candidates:
                require(type(candidate) is dict and set(candidate) == {'tabId', 'browserWindowId', 'url'} and
                        valid_int(candidate['tabId']) and valid_int(candidate['browserWindowId']), '候选身份错误')
                url = candidate['url']
                require(type(url) is str and len(url) <= 8192, '候选 URL 错误')
                parsed = urlsplit(url)
                base = authorized_origin(parsed.scheme + '://' + parsed.netloc)
                require(url == base + parsed.path and parsed.path.startswith('/') and not parsed.query and
                        not parsed.fragment and parsed.hostname not in ('chromewebstore.google.com',) and
                        not (parsed.hostname == 'chrome.google.com' and parsed.path.startswith('/webstore')),
                        '候选必须为普通脱敏 HTTP(S) URL')
            require(len({c['tabId'] for c in candidates}) == len(candidates), '候选标签重复')
            peer.update(candidates=copy.deepcopy(candidates), candidatesAt=now, requestCandidates=False)
        peer.update(origin=origin, lastSeen=now, enabled=message['enabled'], permission=message['permission'])
        if ('clientVersion' in message and message['clientVersion'] != peer.get('clientVersion')):
            peer.pop('capabilities', None)
        if 'capabilities' in message:
            capabilities = message['capabilities']
            require(type(capabilities) is list and len(capabilities) <= 16 and
                    all(type(c) is str and 0 < len(c) <= 40 for c in capabilities), '客户端能力错误')
            peer['capabilities'] = capabilities
        if 'clientVersion' in message:
            version_text = message['clientVersion']
            require(type(version_text) is str and 0 < len(version_text) <= 32 and
                    all(c.isalnum() or c in '.-' for c in version_text), '客户端版本标识不合法')
            peer['clientVersion'] = version_text
        if 'assignmentTitle' in message:
            title = message['assignmentTitle']
            require(type(title) is str and 0 < len(title) <= 200 and
                    not any(ord(c) < 32 for c in title), '标签标题不合法')
            peer['assignmentTitle'] = title
        if 'windowEvidence' in message:
            evidence = message['windowEvidence']
            if evidence is not None:
                require(type(evidence) is dict and set(evidence) in (
                            {'bindingId', 'browserWindowId', 'tabId', 'title'},
                            {'bindingId', 'browserWindowId', 'tabId', 'title', 'bounds'}) and
                        type(evidence['bindingId']) is str and re.fullmatch('[a-f0-9]{32}', evidence['bindingId']) and
                        valid_int(evidence['browserWindowId']) and valid_int(evidence['tabId']) and
                        type(evidence['title']) is str and 0 < len(evidence['title']) <= 200 and
                        evidence['title'].strip() and not any(ord(c) < 32 for c in evidence['title']),
                        '浏览器窗口证据格式错误')
                require('bounds' not in evidence or valid_window_bounds(evidence['bounds']), '浏览器窗口几何证据错误')
            assignment_now = current_assignment(data, task_file)
            if (message['enabled'] and message['permission'] and evidence is not None and assignment_now and
                    evidence['bindingId'] == assignment_now['bindingId'] and
                    evidence['browserWindowId'] == assignment_now['browserWindowId']):
                peer['windowEvidence'] = {**copy.deepcopy(evidence), 'at': now}
            else:
                peer.pop('windowEvidence', None)
        if 'lifecycleError' in message:
            issue = message['lifecycleError']
            require(issue is None or (type(issue) is dict and set(issue) == {'stage', 'code', 'at'} and
                    type(issue['stage']) is str and issue['stage'] in LIFECYCLE_STAGES and
                    type(issue['code']) is str and issue['code'] in LIFECYCLE_CODES and
                    type(issue['at']) in (int, float) and 0 < issue['at'] <= now + 60),
                    '生命周期诊断格式错误')
            peer['lifecycleError'] = copy.deepcopy(issue)
        if 'observerStatus' in message:
            observation = message['observerStatus']
            require(observation is None or (type(observation) is dict and set(observation) ==
                    {'state', 'bindingId', 'documentId', 'navigationEpoch', 'attempt', 'stage', 'code', 'at'} and
                    observation['state'] in ('initializing', 'ready', 'failed') and
                    type(observation['bindingId']) is str and re.fullmatch('[a-f0-9]{32}', observation['bindingId']) and
                    type(observation['documentId']) is str and 0 < len(observation['documentId']) <= 128 and
                    valid_int(observation['navigationEpoch'], low=1) and valid_int(observation['attempt'], low=1, high=2) and
                    observation['stage'] in ('observer_load', 'observer_configure') and
                    (type(observation['code']) is str and observation['code'] in LIFECYCLE_CODES if observation['state'] == 'failed' else observation['code'] is None) and
                    type(observation['at']) in (int, float) and 0 < observation['at'] <= now + 60),
                    '页面观察状态格式错误')
            assignment_now = current_assignment(data, task_file)
            peer['observerStatus'] = (copy.deepcopy(observation) if observation and message['enabled'] and
                message['permission'] and assignment_now and observation['bindingId'] == assignment_now['bindingId'] else None)
        active_id, active_record = active_task(task_file)
        prune_creation(peer, active_id, active_record)
        if (isinstance(message.get('created'), dict) and
                message['created'].get('requestId') in peer.get('canceledCreateRequests', [])):
            message = dict(message)
            message.pop('created')  # Acknowledge a late result; never assign it.
        if 'created' in message:
            created = message['created']
            require(type(created) is dict and set(created) in (CREATED_FIELDS,
                    CREATED_FIELDS | {'navigationState'}, CREATED_FIELDS | {'navigationState', 'navigationError'}),
                    '创建结果字段错误')
            navigation = created.get('navigationState', 'unknown')
            require(navigation in ('not_requested', 'issued', 'failed', 'unknown'), '创建导航状态错误')
            if 'navigationError' in created:
                require(navigation in ('failed', 'unknown') and type(created['navigationError']) is str and
                        0 < len(created['navigationError']) <= 200, '创建导航错误信息不合法')
            request = peer.get('createRequest')
            require(type(created['requestId']) is str and re.fullmatch('[0-9a-f]{32}', created['requestId']),
                    '创建请求标识错误')
            require(valid_int(created['browserWindowId']) and valid_int(created['tabId']), '创建结果身份错误')
            task_id_now, task_now = active_task(task_file)
            # 已接受的完全相同回传可再次确认，覆盖 worker 重启/确认响应丢失。
            accepted = next((t for t in peer.get('createdTargets', [])
                             if t.get('taskId') == task_id_now and
                             all(t.get(k) == created[k] for k in CREATED_FIELDS)), None)
            if accepted and request is None:
                request = {'taskId': task_id_now, 'requestId': created['requestId']}
            require(type(request) is dict and created['requestId'] == request.get('requestId'),
                    '未被请求的窗口创建结果')
            require(task_now is not None and task_now.get('state') in ('awaiting_window', 'bound') and
                    request.get('taskId') == task_id_now, '创建请求已失效')
            peer['createdTargets'] = ([{
                'requestId': created['requestId'], 'taskId': task_id_now,
                'browserWindowId': created['browserWindowId'], 'tabId': created['tabId'],
                'navigationState': navigation,
                **({'navigationError': created['navigationError']} if 'navigationError' in created else {}),
                'at': now}] + [t for t in peer.get('createdTargets', [])
                               if t.get('tabId') != created['tabId']])[:8]
            peer['createRequest'] = None
        # 建窗证据只对当前活动任务有效；单活动任务模型下旧归属不得阻塞新任务。
        active_id, _ = active_task(task_file)
        peer['createdTargets'] = [t for t in peer.get('createdTargets', [])
                                  if t.get('taskId') == active_id]
        command_ack = None
        pending_assignment = current_assignment(data, task_file)
        command = peer.get('command')
        if command and (not pending_assignment or command.get('bindingId') != pending_assignment['bindingId']):
            finish_pending(peer, 'canceled', now)
        expire_pending(peer, now)
        if 'commandResult' in message:
            reply = message['commandResult']
            require(type(reply) is dict and type(reply.get('requestId')) is str and
                    re.fullmatch('[a-f0-9]{32}', reply['requestId']), '命令结果标识错误')
            terminal_ids = [t['commandId'] for t in peer.get('commandTerminals', [])]
            canceled = peer.get('commandCanceled') or {}
            known = (reply['requestId'] in peer.get('commandSeen', []) or
                     reply['requestId'] in terminal_ids or reply['requestId'] == canceled.get('commandId') or
                     reply['requestId'] == peer.get('commandExpired'))
            if known:
                command_ack = {'requestId': reply['requestId'], 'status': 'discarded'}
            else:
                pending = peer.get('command')
                require(type(pending) is dict and reply['requestId'] == pending.get('requestId'),
                        '未被请求的命令结果')
                latency = (round((now - pending['deliveredAt']) * 1000, 1)
                           if pending.get('deliveredAt') else None)
                assignment_now = current_assignment(data, task_file)
                event_now = (assignment_now or {}).get('event') or {}
                try:
                    require(set(reply) in ({'requestId', 'status', 'error'}, {'requestId', 'status', 'error', 'debuggerFailure'},
                                           {'requestId', 'status', 'result'}) and
                            reply.get('status') in ('ok', 'error') and
                            ('result' if reply['status'] == 'ok' else 'error') in reply, '命令结果字段错误')
                    if reply['status'] == 'ok' and pending.get('documentId') and pending['operation'] in OBSERVATIONS and (
                            not assignment_now or assignment_now['bindingId'] != pending.get('bindingId') or
                            event_now.get('documentId') != pending.get('documentId') or
                            event_now.get('navigationEpoch') != pending.get('navigationEpoch')):
                        reply = {'requestId': reply['requestId'], 'status': 'error', 'error': 'STALE_DOCUMENT：观察结果所属文档已变化'}
                    if reply['status'] == 'ok':
                        if pending['operation'] == 'snapshot':
                            outcome = validate_snapshot(reply['result'])
                            peer['pendingVerify'] = None
                            peer['snapshot'] = {k: pending.get(k) for k in
                                                ('tabId', 'documentId', 'navigationEpoch', 'bindingId')}
                            peer['snapshot']['snapshotId'] = outcome.get('snapshotId')
                        elif pending['operation'] == 'screenshot':
                            outcome = reply['result']
                            require(type(outcome) is dict and set(outcome) == {'mimeType', 'data', 'tabId', 'documentId'} and
                                    outcome['mimeType'] == 'image/jpeg' and type(outcome['data']) is str and
                                    0 < len(outcome['data']) <= 500000 and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', outcome['data']) and
                                    outcome['tabId'] == pending['tabId'] and outcome['documentId'] == pending['documentId'], '截图身份或数据错误')
                            # Keep one image; do not grow the JSON store with old screenshots.
                            for previous in peer.get('commandResults', []):
                                if previous.get('operation') == 'screenshot':
                                    previous['result'] = {'expiredImage': True}
                        elif pending['operation'] == 'read':
                            outcome = validate_read(reply['result'], pending.get('params') or {})
                        elif pending['operation'] == 'diagnostics':
                            outcome = validate_diagnostics(reply['result'])
                        elif pending['operation'] == 'render_probe':
                            outcome = validate_render_probe(reply['result'], pending.get('params') or {})
                        else:
                            outcome = validate_action_result(reply['result'], pending['operation'])
                            if outcome.get('children'):
                                require(assignment_now is not None, '子标签所属任务已失效')
                                owned = copy.deepcopy(assignment_now.get('ownedTabs', [{'tabId': assignment_now['tabId'],
                                    'browserWindowId': assignment_now['browserWindowId']}]))
                                for child in outcome['children']:
                                    require(child['browserWindowId'] == assignment_now['browserWindowId'] and
                                            not any(other.get('tabId') == child['tabId'] for other in owned), '子标签必须属于固定任务窗口且不能重复')
                                    require(len(owned) < 64, '任务子标签数量达到上限')
                                    owned.append(copy.deepcopy(child))
                                assignment_now['ownedTabs'] = owned
                            # 只有真正执行过的动作才要求复核；失败或无效引用不制造虚假闭环。
                            peer['pendingVerify'] = ({'commandId': reply['requestId'],
                                                      'operation': pending['operation'],
                                                      'taskId': pending['taskId'],
                                                      **({k: pending.get(k) for k in ('bindingId', 'tabId', 'browserWindowId', 'navigationEpoch')} |
                                                         {'url': redacted_url(pending['params']['url'])} if pending['operation'] == 'navigate' else {})}
                                                     if outcome['performed'] else None)
                        if pending['operation'] in ('read', 'diagnostics', 'screenshot'):
                            if navigation_verified(peer, pending, assignment_now, outcome):
                                peer['pendingVerify'] = None
                            else:
                                mark_navigation_destination(peer, pending, assignment_now, outcome)
                        peer['commandResult'] = {'requestId': reply['requestId'], 'status': 'ok',
                                                 'operation': pending['operation'],
                                                 'latencyMs': latency,
                                                 'result': outcome, 'at': now}
                    else:
                        error = reply['error']
                        require(type(error) is str and 0 < len(error) <= 200, '命令错误信息不合法')
                        if pending['operation'] not in OBSERVATIONS:
                            peer['pendingVerify'] = {'commandId': reply['requestId'], 'taskId': pending['taskId']}
                        peer['commandResult'] = {'requestId': reply['requestId'], 'status': 'error',
                                                 'operation': pending['operation'],
                                                 'latencyMs': latency,
                                                 'error': error, 'at': now}
                        if 'debuggerFailure' in reply:
                            evidence = validate_debugger_failure(reply['debuggerFailure'])
                            expected = ('BROWSER_DEBUGGER_RESTRICTED' if evidence['reason'] == 'foreign_extension_page'
                                        else 'BROWSER_DEBUGGER_IN_USE')
                            require(error.startswith(expected + '：') or error.startswith(expected + ':'),
                                    '调试失败错误码不匹配')
                            peer['commandResult']['debuggerFailure'] = evidence
                            # Attachment was rejected before guard setup and
                            # requested input. This is a known pre-input stop.
                            if evidence.get('inputDispatched') is False and pending['operation'] not in OBSERVATIONS:
                                peer['pendingVerify'] = None
                except (ValueError, TypeError, KeyError, OverflowError):
                    # A known command's malformed result must not poison all later
                    # heartbeats. Reject its content, record an error and acknowledge
                    # consumption. Unknown IDs still reject the entire request.
                    peer['commandResult'] = {'requestId': reply['requestId'], 'status': 'error',
                        'operation': pending['operation'], 'latencyMs': latency, 'at': now,
                        'error': 'INVALID_BROWSER_RESULT：扩展回执格式或身份不合法；未采信结果，请核实原目标'}
                    if pending['operation'] not in OBSERVATIONS:
                        peer['pendingVerify'] = {'commandId': reply['requestId'], 'taskId': pending['taskId']}
                    command_ack = {'requestId': reply['requestId'], 'status': 'rejected', 'code': 'INVALID_BROWSER_RESULT'}
                else:
                    command_ack = {'requestId': reply['requestId'], 'status': 'accepted'}
                peer['commandResults'] = (peer.get('commandResults', []) +
                                          [peer['commandResult']])[-MAX_RESULTS:]
                # Status needs metadata only; keep large screenshot data once.
                peer['commandResult'] = {k: v for k, v in peer['commandResult'].items() if k != 'result'}
                if (type(peer.get('panelState')) is dict and
                        len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_BYTES - 32768):
                    peer['panelState']['reviewOmitted'] = bool(peer['panelState'].get('review'))
                    peer['panelState']['review'] = None
                peer['commandSeen'] = (peer.get('commandSeen', []) +
                                       [reply['requestId']])[-MAX_SEEN:]
                peer['command'] = None
                # Screenshots and large DOM results must not exhaust the bounded
                # store. Keep recent results while acknowledging older IDs as evicted.
                while len(peer.get('commandResults', [])) > 1 and len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_BYTES * 3 // 4:
                    peer['commandResults'].pop(0)
        if not message['enabled'] or not message['permission']:
            data['tasks'] = {k: v for k, v in data['tasks'].items() if v.get('origin') != origin}
            peer.update(candidates=[], candidatesAt=0, requestCandidates=False,
                        createRequest=None, createdTargets=[],
                        command=None, commandResult=None, pendingVerify=None,
                        commandResults=[], commandSeen=[])
            peer['panelState'] = {'taskSummary': None, 'activity': [], 'downloads': [], 'review': None, 'at': now}
        # 单活动任务模型：其他任务的浏览器分配一律淘汰，避免失效任务长期占用标签。
        active_id, _ = active_task(task_file)
        data['tasks'] = {k: v for k, v in data['tasks'].items() if k == active_id}
        record = current_assignment(data, task_file)
        # Ignore an old extension's experiment report and never authorize a new
        # capture. Normal preview remains the native continuous window stream.
        for key in ('navigationCapture', 'windowPreview'):
            peer.pop(key, None)
        task_id, task = active_task(task_file)
        assignment = copy.deepcopy(record) if record and record['origin'] == origin else None
        panel_command = peer.get('panelCommand')
        if panel_command:
            valid = (type(panel_command) is dict and
                     type(panel_command.get('requestId')) is str and
                     re.fullmatch('[0-9a-f]{32}', panel_command['requestId']) and
                     panel_command.get('operation') in ('stop_task', 'capture_review', 'clear_activity', 'annotate_review', 'remove_review_annotation') and
                     task is not None and panel_command.get('taskId') == task_id and
                     (panel_command.get('operation') not in ('stop_task', 'capture_review') or
                      (assignment is not None and not assignment.get('paused'))) and
                     type(panel_command.get('expiresAt')) in (int, float) and now < panel_command['expiresAt'] <= now + 30 and
                     message['enabled'] and message['permission'])
            if not valid:
                peer['panelResult'] = {'requestId': panel_command.get('requestId', '') if type(panel_command) is dict else '', 'status': 'error',
                                       'error': '请求已过期或任务已变化，已下发的操作可能已执行', 'at': now}
                peer['panelCommand'] = None
                panel_command = None
        if assignment is not None:
            # WindowServer mapping evidence belongs to the host, not the observer.
            # Its read-only wire contract must not claim or inherit that attestation.
            assignment['mappingVerified'] = False
            assignment.pop('mapping', None)
        command = peer.get('command')
        if command and (not assignment or command.get('bindingId', assignment['bindingId']) != assignment['bindingId']):
            finish_pending(peer, 'canceled', now)
            command = None
        if command:
            # 记录已投递，取消时才能诚实区分「可能已执行」与「尚未下发」。
            command['delivered'] = True
            command.setdefault('deliveredAt', now)
            peer['command'] = command
        # Retire screenshot-mailbox preview even if a legacy peer sends a frame.
        # Explicit screenshot commands above remain available as observations.
        peer.pop('previewLease', None)
        peer.pop('previewFrame', None)
        return {'ok': True, 'protocolVersion': 2, 'heartbeatAt': now,
                'previewLease': None,
                'applicationSession': session,
                'sessionReset': session_reset,
                'commandAck': command_ack,
                'panelBridge': True,
                'activeTaskId': task_id,
                'requestCandidates': bool(peer.get('requestCandidates')),
                'createRequest': copy.deepcopy(peer.get('createRequest')),
                'command': copy.deepcopy(command),
                'panelCommand': copy.deepcopy(panel_command),
                'taskAssignment': ('permission_missing' if not message['permission'] else
                    'disabled' if not message['enabled'] else 'paused' if assignment and assignment.get('paused') else
                    'assigned' if assignment else
                    'needs_target_selection' if task else 'no_assignment'),
                'assignment': assignment}


class CandidateEvidenceExpired(ValueError):
    def __init__(self, candidate, task, peer):
        super().__init__('扩展候选证据过期')
        self.expected = {'candidate': copy.deepcopy(candidate), 'targetIdentity': target_identity(task),
                         'applicationSession': peer.get('applicationSession'),
                         'candidatesAt': peer.get('candidatesAt', 0)}


class NavigationDocumentPending(ValueError):
    def __init__(self, record, task, peer):
        super().__init__('BACKGROUND_WAITING：等待原标签导航就绪；未下发观察命令，不需重新 assign')
        self.expected = {k: copy.deepcopy(record[k]) for k in
                         ('taskId', 'bindingId', 'assignmentVersion', 'tabId', 'browserWindowId')}
        self.target = target_identity(task)
        self.session = peer.get('applicationSession')


def wait_navigation_observation(args, waiting, task_file, event_file, revalidate, check_cancelled):
    deadline = time.monotonic() + NAVIGATION_OBSERVATION_TIMEOUT
    while True:
        if check_cancelled:
            check_cancelled()
        data = read_file(event_file)
        record = current_assignment(data, task_file)
        _, task = active_task(task_file)
        require(record is not None and not record.get('paused') and
                all(record.get(k) == v for k, v in waiting.expected.items()) and
                task is not None and target_identity(task) == waiting.target and
                application_session() == waiting.session, 'STALE_TARGET：等待期间任务或绑定已变化，未下发命令')
        event = record.get('event') or {}
        ready = (args['operation'] == 'snapshot' or
                 (event.get('domReady') and event.get('readyState') in ('interactive', 'complete')))
        if event.get('documentId') and event.get('phase') not in ('before', 'paused', 'error') and ready:
            if revalidate:
                revalidate(args['taskId'])
            # No command existed while waiting. Revalidate and pin the current
            # document under the lock, rather than retargeting a queued command.
            return _internal_action(args, task_file, event_file, observation=waiting)
        if time.monotonic() >= deadline:
            raise waiting
        time.sleep(0.1)


def validate_candidate_refresh(data, expected, task_file):
    peer = data.get('peer', {})
    task_id, task = active_task(task_file)
    require(bound_native_target(task) and task_id == expected['targetIdentity']['taskId'] and
            target_identity(task) == expected['targetIdentity'],
            'BROWSER_TARGET_CHANGED: 刷新期间原生任务目标已变化，未分配标签')
    require(application_session() == expected['applicationSession'] == peer.get('applicationSession') and
            time.time() - peer.get('lastSeen', 0) < CANDIDATE_TTL and
            peer.get('enabled') and peer.get('permission'),
            '刷新期间应用会话、扩展连接或授权已变化，未分配标签')


def candidate_response(peer, session):
    now = time.time()
    live = 0 <= now - peer.get('lastSeen', 0) < CANDIDATE_TTL
    available = (session is not None and live and peer.get('enabled') and peer.get('permission') and
                 peer.get('applicationSession') == session)
    age = now - peer.get('candidatesAt', 0)
    pending = bool(available and peer.get('requestCandidates'))
    return {'nativeConnection': 'connected' if live else 'host_unavailable',
            'clientVersion': peer.get('clientVersion') if live and peer.get('applicationSession') == session else None,
            'capabilities': copy.deepcopy(peer.get('capabilities', [])) if available else [],
            'globalPermission': bool(peer.get('permission')) if live else None,
            'candidates': copy.deepcopy(peer.get('candidates', [])) if available and 0 <= age < CANDIDATE_TTL else [],
            'refreshPending': pending, 'mappingVerified': False,
            'refreshStatus': 'pending' if pending else 'ready' if available else 'unavailable',
            'retryAfterMs': 1000 if pending else None,
            'candidateAgeMs': round(max(0, age) * 1000) if peer.get('candidatesAt') else None,
            'candidateTtlMs': CANDIDATE_TTL * 1000}


def connected_peer(event_file):
    """A current handshake is distinct from enabled/permission checks."""
    data = read_file(event_file, {'version': 1, 'tasks': {}})
    peer = data.get('peer') if isinstance(data, dict) else None
    session = application_session()
    if isinstance(peer, dict) and session is not None and peer.get('applicationSession') == session:
        seen = peer.get('lastSeen')
        if type(seen) in (int, float) and 0 <= time.time() - seen < CANDIDATE_TTL and peer.get('clientVersion'):
            return peer
    return {}


def extension_presence(event_file):
    """Global consent/heartbeat only; never grants a private task assignment."""
    peer = connected_peer(event_file)
    return bool(peer.get('enabled') and peer.get('permission'))


def preflight_candidates(event_file, check_cancelled=None):
    """Request root metadata without admitting a task or touching its records."""
    if check_cancelled:
        check_cancelled()
    with event_store(event_file) as data:
        peer = data.setdefault('peer', {})
        peer['requestCandidates'] = True
        session = application_session()
        result = candidate_response(peer if session is not None and peer.get('applicationSession') == session else {}, session)
    result = wait_for_candidates({'action': 'candidates'}, result, None, event_file, check_cancelled)
    return dict(result, connectionScope='extension_preflight', taskChannelReady=False)


def wait_for_private_channel(task_file, event_file, root_event_file, task_id, check_cancelled):
    """Wait only for the extension's first sync; never copy root task evidence."""
    deadline = time.monotonic() + CANDIDATE_QUERY_TIMEOUT
    while True:
        if check_cancelled:
            check_cancelled()
        require(active_task(task_file)[0] == task_id, 'BROWSER_TARGET_CHANGED: 等待通道期间任务已变化')
        if connected_peer(event_file) or not extension_presence(root_event_file) or time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def wait_for_candidates(args, result, task_file, event_file, check_cancelled):
    # Release the file lock while the extension answers. Waiting only for
    # metadata is safe; no input, creation, assignment or navigation is retried.
    if not result['refreshPending'] or result['candidates']:
        return result
    deadline = time.monotonic() + CANDIDATE_QUERY_TIMEOUT
    while True:
        if check_cancelled:
            check_cancelled()
        if 'taskId' in args:
            require(active_task(task_file)[0] == args['taskId'], '需要当前活动任务的 taskId')
        peer = (read_file(event_file) or {}).get('peer', {})
        result = candidate_response(peer, application_session())
        if not result['refreshPending'] or time.monotonic() >= deadline:
            return result
        time.sleep(0.1)


def internal_action(args, task_file=TASK_FILE, event_file=EVENT_FILE, revalidate=None, check_cancelled=None):
    if check_cancelled:
        check_cancelled()
    try:
        result = _internal_action(args, task_file, event_file)
        if args.get('action') == 'candidates':
            return wait_for_candidates(args, result, task_file, event_file, check_cancelled)
        return result
    except CandidateEvidenceExpired as expired:
        expected = expired.expected
    except NavigationDocumentPending as waiting:
        return wait_navigation_observation(args, waiting, task_file, event_file, revalidate, check_cancelled)
    # Refresh only metadata for an already selected candidate. No input,
    # navigation, creation or assignment has been dispatched by the failed check.
    with event_store(event_file) as data:
        validate_candidate_refresh(data, expected, task_file)
        data['peer']['requestCandidates'] = True
    deadline = time.monotonic() + CANDIDATE_REFRESH_TIMEOUT
    while True:
        data = read_file(event_file)
        validate_candidate_refresh(data, expected, task_file)
        peer = data['peer']
        if not peer.get('requestCandidates') and peer.get('candidatesAt', 0) > expected['candidatesAt']:
            if revalidate:
                revalidate(args['taskId'])
            # Recheck identity, URL and freshness under the assignment's lock.
            return _internal_action(args, task_file, event_file, refreshed=expected)
        if time.monotonic() >= deadline:
            raise ValueError('CANDIDATE_REFRESH_TIMEOUT: 扩展未在 3 秒内回传新候选，未分配标签；'
                             '保持原任务与标签，连接恢复后重试 assign')
        time.sleep(0.1)
        if check_cancelled:
            check_cancelled()


def _internal_action(args, task_file, event_file, refreshed=None, observation=None):
    action = args.get('action')
    require(action in ('candidates', 'assign', 'create_target', 'command', 'result', 'cancel',
                       'annotate', 'annotations', 'verify_mapping', 'tabs', 'select_tab'), '内部操作错误')
    expected = ({'action'} | ({'taskId'} if 'taskId' in args else set()) if action == 'candidates' else
                {'action', 'taskId'} if action in ('create_target', 'annotations', 'tabs') else
                {'action', 'taskId', 'tabId'} if action == 'select_tab' else
                {'action', 'taskId', 'windows'} if action == 'verify_mapping' else
                {'action', 'taskId', 'ref', 'note'} if action == 'annotate' else
                {'action', 'taskId', 'operation'} if action == 'command' else
                {'action', 'taskId', 'commandId'} if action in ('result', 'cancel') else
                {'action', 'taskId', 'browserWindowId', 'tabId'})
    if action == 'command':
        expected = expected | {key for key in ACTION_FIELDS if key in args}
    if action == 'create_target' and 'url' in args:
        expected = expected | {'url'}
    missing, unexpected = expected - set(args), set(args) - expected
    require(not missing and not unexpected,
            '内部操作字段错误：缺少 [%s]；多余 [%s]' %
            (', '.join(sorted(missing)), ', '.join(sorted(unexpected))))
    with event_store(event_file) as data:
        peer = data.get('peer', {})
        expire_pending(peer, time.time())
        # 单活动任务模型：其他任务的浏览器分配一律淘汰（assign 可能早于下一次 sync）。
        active_now, active_record = active_task(task_file)
        prune_creation(peer, active_now, active_record)
        data['tasks'] = {k: v for k, v in data.get('tasks', {}).items() if k == active_now}
        if observation:
            record = current_assignment(data, task_file)
            require(record is not None and not record.get('paused') and active_record is not None and
                    all(record.get(k) == v for k, v in observation.expected.items()) and
                    target_identity(active_record) == observation.target and
                    application_session() == observation.session,
                    'STALE_TARGET：等待后的绑定校验失败，未下发命令')
        live = time.time() - peer.get('lastSeen', 0) < 20
        session = application_session()
        available = (session is not None and live and peer.get('enabled') and peer.get('permission') and
                     peer.get('applicationSession') == session)
        if action == 'candidates':
            if 'taskId' in args:
                require(type(args['taskId']) is str and args['taskId'] == active_now and active_now is not None,
                        '需要当前活动任务的 taskId')
            peer['requestCandidates'] = True
            data['peer'] = peer
            return candidate_response(peer, session)
        require(session is not None and (live or action in ('result', 'cancel')) and peer.get('applicationSession') == session,
                'BROWSER_HOST_UNAVAILABLE: 扩展未连接，请用户恢复扩展连接')
        require(peer.get('permission'), 'BROWSER_PERMISSION_MISSING: 浏览器能力未授权，请用户完成授权')
        require(peer.get('enabled'), 'BROWSER_DISABLED: 浏览器能力已关闭，请用户在控制面板开启')
        task_id, task = active_task(task_file)
        if action == 'create_target':
            # 建窗发生在绑定之前，因此只要求活动任务，不要求 sessionId（那是绑定后才有的）。
            require(args['taskId'] == task_id and task and task.get('state') == 'awaiting_window' and
                    task.get('windowId') is None, '仅等待绑定窗口的活动任务可新建；已有目标须复用')
            requested_url = creation_url(args['url']) if 'url' in args else None
            created = next((t for t in peer.get('createdTargets', [])
                            if t.get('taskId') == task_id), None)
            if created:
                return {'requested': False, 'reused': True, 'requestId': created['requestId'],
                        'browserWindowId': created['browserWindowId'], 'tabId': created['tabId'],
                        'navigationState': created.get('navigationState', 'unknown'),
                        **({'navigationError': created['navigationError']} if 'navigationError' in created else {}),
                        'mappingVerified': False, 'nextAction': 'candidates_then_assign' if
                            created.get('navigationState') == 'issued' else 'inspect_created_tab',
                        'limitation': 'issued 仅表示导航请求已受理；须在 HTTP(S) 候选中出现才能 assign。失败或结果未知时不重建窗口、不重放导航。'}
            pending = peer.get('createRequest')
            if pending and pending.get('taskId') == task_id:
                require(pending.get('url') == requested_url, '已有创建请求；复用原目标，不因地址变化另开窗口')
                return {'requested': True, 'reused': True, 'requestId': pending['requestId'],
                        'mappingVerified': False, 'nextAction': 'wait_for_created_target',
                        'limitation': '结果未知时保留原请求，不重发新建窗口'}
            request = {'requestId': secrets.token_hex(16), 'taskId': task_id,
                       'issuedAt': time.time(),
                       'url': requested_url}
            peer['createRequest'] = request
            return {'requested': True, 'requestId': request['requestId'],
                    'mappingVerified': False,
                    'limitation': '扩展创建专用窗口后仍须在候选证据中出现并显式 assign'}
        if action == 'verify_mapping':
            require(args['taskId'] == task_id and task and task.get('state') == 'bound',
                    '需要已绑定的活动任务')
            record = current_assignment(data, task_file)
            require(record is not None, '任务目标未绑定')
            evidence = peer.get('windowEvidence') or {}
            verified_evidence = (evidence.get('bindingId') == record['bindingId'] and
                evidence.get('browserWindowId') == record['browserWindowId'] and
                0 <= time.time() - evidence.get('at', 0) < CANDIDATE_TTL)
            title = evidence['title'] if verified_evidence else peer.get('assignmentTitle')
            method = 'window_active_title' if verified_evidence else 'title'
            require(type(title) is str and title, 'BROWSER_MAPPING_PENDING: 扩展尚未回传标签标题；只读等待原映射证据')
            windows = args['windows']
            require(type(windows) is list and len(windows) <= 200, '窗口证据数量错误')
            for window in windows:
                require(type(window) is dict and set(window) in ({'windowId', 'title'}, {'windowId', 'title', 'bounds'}) and
                        valid_int(window['windowId'], 1) and type(window['title']) is str and
                        len(window['title']) <= 200, '窗口证据格式错误')
                require('bounds' not in window or valid_window_bounds(window['bounds']), '原生窗口几何证据错误')
            exact = [w for w in windows if window_title_relation(w['title'], title) == 'exact']
            prefixed = [w for w in windows if window_title_relation(w['title'], title) == 'prefixed']
            hits = exact or prefixed
            match = 'exact' if exact else ('prefixed' if prefixed else None)
            if (len(hits) > 1 and verified_evidence and valid_window_bounds(evidence.get('bounds')) and
                    all(valid_window_bounds(w.get('bounds')) for w in hits)):
                matched_bounds = [w for w in hits if valid_window_bounds(w.get('bounds')) and
                    all(abs(w['bounds'][k] - evidence['bounds'][k]) <= 2 for k in ('x', 'y', 'width', 'height'))]
                if len(matched_bounds) == 1:
                    hits = matched_bounds
                    method = 'window_active_title_and_bounds'
            if len(hits) == 1:
                visibility = ({'windowActiveTabId': evidence['tabId'],
                               'targetTabActiveAtVerification': evidence['tabId'] == record['tabId'],
                               'previewLimitation': '原生窗口共享显示活动标签；后台任务标签的内容请用其 snapshot/screenshot 复核，不切换标签。'}
                              if verified_evidence else {})
                record['mappingVerified'] = True
                record['mapping'] = {'windowId': hits[0]['windowId'], 'method': method,
                                     'title': title, 'match': match, 'at': time.time(), **visibility}
                return {'verified': True, 'windowId': hits[0]['windowId'], 'method': method,
                        'match': match, 'candidates': 1, **visibility}
            record['mappingVerified'] = False
            record['mapping'] = {'verified': False, 'method': method, 'title': title,
                                 'reason': 'no_match' if not hits else 'ambiguous',
                                 'candidates': len(hits), 'at': time.time()}
            return {'verified': False, 'method': method,
                    'reason': 'no_match' if not hits else 'ambiguous', 'candidates': len(hits),
                    'limitation': '接受唯一标题匹配；同名时须有新鲜、完整、唯一的窗口位置与尺寸证据。无法区分时保留原目标并报告，不导航改标题、不移动或激活窗口。'}
        if action in ('annotate', 'annotations'):
            require(args['taskId'] == task_id and task and
                    task.get('state') in ('awaiting_window', 'bound'), '需要当前活动任务')
            if action == 'annotations':
                return {'annotations': copy.deepcopy([a for a in peer.get('annotations', [])
                                                      if a['taskId'] == task_id])}
            require(current_assignment(data, task_file) is not None, '任务目标未绑定')
            note = args['note']
            require(type(note) is str and 0 < len(note.strip()) <= 200, '标注文本长度必须为 1..200')
            require(valid_int(args['ref'], 1), '标注元素引用必须是正整数')
            entry = {'taskId': task_id, 'ref': args['ref'], 'note': note.strip(), 'at': time.time()}
            peer['annotations'] = ([entry] + [a for a in peer.get('annotations', [])
                                              if not (a['taskId'] == task_id and a['ref'] == args['ref'])])[:50]
            data['peer'] = peer
            return {'annotated': True, 'ref': entry['ref'],
                    'count': len([a for a in peer['annotations'] if a['taskId'] == task_id]),
                    'limitation': '标注只登记元素引用，不证明页面语义；界面改版后引用可能失效'}
        require(args['taskId'] == task_id and bound_native_target(task),
                '需要已绑定的活动任务及有效原生目标')
        record = current_assignment(data, task_file)
        if action in ('tabs', 'select_tab'):
            require(record is not None, '任务目标未绑定')
            owned = record.get('ownedTabs', [{'tabId': record['tabId'], 'browserWindowId': record['browserWindowId']}])
            if action == 'tabs':
                return {'tabs': copy.deepcopy(owned), 'logicalActiveTabId': record['tabId'], 'rootTabId': record.get('rootTabId', record['tabId'])}
            require(not peer.get('command'), '命令执行期间不能切换标签')
            require(any(t['tabId'] == args['tabId'] for t in owned), '只能选择本任务已登记的标签')
            version = max(data.get('assignmentVersion', 0), record['assignmentVersion']) + 1
            data['assignmentVersion'] = version
            record.update(tabId=args['tabId'], bindingId=secrets.token_hex(16),
                          assignmentVersion=version, event=None, paused=False)
            peer.update(snapshot=None, pendingVerify={'taskId': task_id, 'operation': 'select_tab',
                                                     'commandId': 'binding-change'})
            return {'selected': True, 'tabId': record['tabId'], 'background': True, 'nextAction': 'snapshot'}
        if action == 'command':
            require(args['operation'] in OPERATIONS, '不支持的命令')
            require(record is not None and not record.get('paused'), '任务目标未绑定或已暂停')
            observation = current_observer_status(peer, record)
            require(not observation or observation['state'] == 'ready',
                    'BROWSER_OBSERVER_NOT_READY：原标签观察尚未就绪；先只读查询状态，持续失败时通知用户')
            require(mapping_conflict(record, task) is not True,
                    '映射窗口与任务绑定窗口不一致：先重新 verify_mapping')
            if args['operation'] not in OBSERVATIONS:
                verify = peer.get('pendingVerify')
                require(not verify or verify.get('taskId') != task_id,
                        '导航落地地址已变化：请获取当前 snapshot 核实一次；保留原绑定，不需重新分配标签' if (verify or {}).get('destinationChanged') else
                        '上一个动作尚未用 snapshot 复核' if verification_next(peer) == 'snapshot' else
                        '上次导航尚未复核：等待原标签新文档就绪，再用 screenshot/read/diagnostics 或 snapshot 复核')
            event = record.get('event') or {}
            if args['operation'] == 'render_probe':
                require(not peer.get('pendingVerify'), 'RENDER_PROBE_NOT_READY: 先完成原动作的正常复核；探针不能代替复核')
                require(event.get('domReady') and event.get('readyState') in ('interactive', 'complete'),
                        'RENDER_PROBE_NOT_READY: 等待原文档就绪；未执行探针')
            waiting_document = not event.get('documentId') or event.get('phase') == 'before'
            waiting_ready = (args['operation'] in ('screenshot', 'read', 'diagnostics') and
                             event.get('phase') != 'error' and
                             (not event.get('domReady') or event.get('readyState') not in ('interactive', 'complete')))
            if (waiting_document or waiting_ready) and args['operation'] in OBSERVATIONS and (peer.get('pendingVerify') or {}).get('operation') == 'navigate':
                raise NavigationDocumentPending(record, task, peer)
            require(event.get('documentId') and event.get('phase') not in ('before', 'paused', 'error'), 'BACKGROUND_WAITING：等待原标签文档就绪')
            params = command_params(args, args['operation'])
            if args['operation'] in ('click', 'fill', 'select', 'scroll'):
                snapshot = peer.get('snapshot') or {}
                require(params.get('snapshotId') and params['snapshotId'] == snapshot.get('snapshotId') and
                        all(snapshot.get(k) == (record.get(k) if k in ('tabId', 'bindingId') else event.get(k))
                            for k in ('tabId', 'bindingId', 'documentId', 'navigationEpoch')), 'STALE_SNAPSHOT：请先获取当前文档快照并传 snapshotId')
            if task.get('interactionMode', 'background_browser') == 'background_browser':
                require({'cdp_input', 'document_binding', 'background_popups'} <= set(peer.get('capabilities', [])),
                        'BACKGROUND_WAITING：扩展版本不支持严格后台输入，请重载 0.3.0 或更新版本，不回落桌面输入')
            if args['operation'] == 'render_probe':
                require('render_probe' in peer.get('capabilities', []),
                        'EXTENSION_UPGRADE_REQUIRED: 分阶段呈现诊断需要 QuietBridge Browser 0.4.7，旧扩展未执行探针')
            if args['operation'] == 'read':
                require('paged_read' in peer.get('capabilities', []),
                        'EXTENSION_UPGRADE_REQUIRED: 分页正文读取需要 QuietBridge Browser 0.4.3 或更新版本')
            pending = peer.get('command')
            require(not pending, '已有未完成的命令')
            request = {'requestId': secrets.token_hex(16), 'taskId': task_id,
                       'operation': args['operation'], 'params': params,
                       'bindingId': record['bindingId'], 'assignmentVersion': record['assignmentVersion'],
                       'tabId': record['tabId'], 'browserWindowId': record['browserWindowId'],
                       'documentId': event['documentId'], 'navigationEpoch': event['navigationEpoch'],
                       'issuedAt': time.time(), 'expiresAt': time.time() + COMMAND_TTL}
            peer.update(command=request, commandResult=None)
            return {'commandId': request['requestId'], 'status': 'queued',
                    'expiresInSeconds': COMMAND_TTL,
                    'target': {k: request[k] for k in ('tabId', 'browserWindowId', 'documentId', 'navigationEpoch')},
                    'route': 'background_browser',
                    'limitation': '输入只走固定标签的 CDP/DOM；不激活窗口，不回落桌面输入。导航可用新文档 screenshot/read/diagnostics 复核；其他输入须 snapshot 复核。performed 不代表业务成功'}
        if action == 'result':
            terminal = next((t for t in reversed(peer.get('commandTerminals', []))
                             if t['commandId'] == args['commandId']), None)
            if terminal:
                return copy.deepcopy(terminal) | {'nextAction': verification_next(peer)}
            if peer.get('commandExpired') == args['commandId']:
                return {'commandId': args['commandId'], 'status': 'expired'}
            pending = peer.get('command')
            if pending and pending['requestId'] == args['commandId']:
                return {'commandId': args['commandId'], 'status': 'pending', 'nextAction': 'wait'}
            canceled = peer.get('commandCanceled')
            if canceled and canceled.get('commandId') == args['commandId']:
                return {'commandId': args['commandId'], 'status': 'canceled',
                        'mayHaveExecuted': bool(canceled.get('delivered')),
                        'nextAction': 'snapshot' if peer.get('pendingVerify') else 'done'}
            done = next((item for item in reversed(peer.get('commandResults', []))
                         if item['requestId'] == args['commandId']), None)
            if done and done['requestId'] == args['commandId']:
                reply = {'commandId': args['commandId'], 'status': done['status'],
                         'operation': done.get('operation')}
                if done.get('latencyMs') is not None:
                    reply['latencyMs'] = done['latencyMs']
                if done['status'] == 'ok':
                    reply['result'] = copy.deepcopy(done['result'])
                    reply['nextAction'] = (verification_next(peer) if peer.get('pendingVerify') else
                                           'observe' if done.get('operation') in ('snapshot', 'read', 'diagnostics', 'render_probe')
                                           else 'done')
                    verify = peer.get('pendingVerify') or {}
                    if verify.get('destinationChanged'):
                        reply['verification'] = {'reason': 'navigation_destination_changed',
                            'expectedUrl': verify['url'], 'observedUrl': verify['observedUrl'],
                            'nextAction': 'snapshot', 'bindingPreserved': True,
                            'message': '原标签已落地到不同地址（可能为重定向或规范化）；获取当前 snapshot 核实一次即可继续，不需重新分配标签。'}
                else:
                    reply['error'] = copy.deepcopy(done['error'])
                    if 'debuggerFailure' in done:
                        reply['debuggerFailure'] = copy.deepcopy(done['debuggerFailure'])
                    message = done['error']
                    code = re.match(r'^([A-Z][A-Z0-9_]{1,63})[:：]', message)
                    reply['errorCode'] = ('BROWSER_DEBUGGER_RESTRICTED' if
                        'Cannot access a chrome-extension:// URL of different extension' in message else
                        'BROWSER_DEBUGGER_IN_USE' if message.startswith('Another debugger is already attached to the ') else
                        code.group(1) if code else 'BROWSER_COMMAND_FAILED')
                    reply['nextAction'] = 'stop'
                return reply
            if args['commandId'] in peer.get('commandSeen', []):
                return {'commandId': args['commandId'], 'status': 'evicted',
                        'note': '结果已被后续命令挤出保留窗口'}
            return {'commandId': args['commandId'], 'status': 'unknown'}
        if action == 'cancel':
            terminal = next((t for t in reversed(peer.get('commandTerminals', []))
                             if t['commandId'] == args['commandId']), None)
            if terminal:
                return copy.deepcopy(terminal) | {'nextAction': verification_next(peer)}
            command = peer.get('command')
            if command and command['requestId'] == args['commandId']:
                finish_pending(peer, 'canceled', time.time())
                return {'commandId': command['requestId'], 'status': 'canceled',
                        'mayHaveExecuted': bool(command.get('delivered')),
                        'nextAction': 'snapshot' if peer.get('pendingVerify') else 'done'}
            canceled = peer.get('commandCanceled')
            if canceled and canceled.get('commandId') == args['commandId']:
                return {'commandId': args['commandId'], 'status': 'canceled',
                        'mayHaveExecuted': bool(canceled.get('delivered'))}
            done = next((item for item in reversed(peer.get('commandResults', []))
                         if item['requestId'] == args['commandId']), None)
            if done and done['requestId'] == args['commandId']:
                return {'commandId': args['commandId'], 'status': 'completed',
                        'operation': done.get('operation'),
                        'note': '该命令已结束，取消不适用'}
            if args['commandId'] in peer.get('commandSeen', []):
                return {'commandId': args['commandId'], 'status': 'evicted',
                        'note': '结果已被后续命令挤出保留窗口'}
            return {'commandId': args['commandId'], 'status': 'unknown'}
        require(valid_int(args['tabId']) and valid_int(args['browserWindowId']), '标签身份错误')
        candidate = next((c for c in peer.get('candidates', []) if c['tabId'] == args['tabId'] and
                          c['browserWindowId'] == args['browserWindowId']), None)
        require(candidate is not None, '缺少扩展目标证据；不能使用 CGWindowID 代替浏览器窗口 ID')
        if refreshed:
            validate_candidate_refresh(data, refreshed, task_file)
            require(candidate == refreshed['candidate'],
                    'BROWSER_TARGET_CHANGED: 刷新后标签地址已变化，请重新查询候选并确认目标')
            require(peer.get('candidatesAt', 0) > refreshed['candidatesAt'], '未收到新的扩展候选证据')
        candidate_age = time.time() - peer.get('candidatesAt', 0)
        if not 0 <= candidate_age < CANDIDATE_TTL:
            raise CandidateEvidenceExpired(candidate, task, peer)
        require(not any(r['tabId'] == args['tabId'] and k != task_id for k, r in data['tasks'].items()),
                '标签属于其他任务')
        created = next((t for t in peer.get('createdTargets', [])
                        if t['tabId'] == args['tabId'] and
                        t['browserWindowId'] == args['browserWindowId']), None)
        require(not any(t['tabId'] == args['tabId'] and t['taskId'] != task_id
                        for t in peer.get('createdTargets', [])), '窗口由其他任务创建')
        evidence = {'mode': 'created' if created else 'selected',
                    'tabId': args['tabId'], 'browserWindowId': args['browserWindowId'],
                    'createdRequestId': created['requestId'] if created else None,
                    'at': time.time()}
        if record:
            require(record['browserWindowId'] == args['browserWindowId'] and
                    any(t['tabId'] == args['tabId'] for t in record.get('ownedTabs', [{'tabId': record['tabId']}])),
                    '固定任务不能改绑其他窗口或未归属标签')
        version = data.get('assignmentVersion', 0) + 1
        data['assignmentVersion'] = version
        old_command = peer.get('command') or {}
        # Never relabel another task's verification barrier as this task's.
        # Reassigning this same task still preserves unknown input outcomes.
        verify = peer.get('pendingVerify') or {}
        needs_verify = ((verify if verify.get('taskId') == task_id else None) or
                        (old_command if old_command.get('taskId') == task_id and old_command.get('delivered') else None))
        if record is None:
            # Retain the old command's bounded terminal receipt so a delayed
            # worker reply is discarded and uncertainty is not rewritten as
            # success. Its input barrier belongs only to the ended task.
            finish_pending(peer, 'canceled', time.time())
            for key in ('command', 'commandResult', 'commandResults', 'commandSeen', 'commandCanceled',
                        'commandExpired', 'pendingVerify', 'snapshot',
                        'assignmentTitle', 'windowEvidence', 'observerStatus', 'lifecycleError'):
                peer.pop(key, None)
            needs_verify = None
        peer['snapshot'] = None
        peer['pendingVerify'] = {'taskId': task_id, 'operation': 'reassign', 'commandId': 'binding-change'} if needs_verify else None
        data['tasks'][task_id] = {'taskId': task_id, 'origin': peer['origin'],
            'token': secrets.token_hex(32), 'bindingId': secrets.token_hex(16),
            'assignmentVersion': version, 'globalOrigins': True, 'selector': '',
            'authorizedOrigin': urlsplit(candidate['url']).scheme + '://' + urlsplit(candidate['url']).netloc,
            'tabId': args['tabId'], 'browserWindowId': args['browserWindowId'],
            'targetEvidence': evidence,
            'rootTabId': record.get('rootTabId', record['tabId']) if record else args['tabId'],
            'ownedTabs': copy.deepcopy(record['ownedTabs']) if record and record.get('ownedTabs') else
                         [{'tabId': args['tabId'], 'browserWindowId': args['browserWindowId']}],
            'targetIdentity': target_identity(task), 'mappingVerified': False, 'paused': False, 'event': None}
        return {'assigned': True, 'assignmentVersion': version, 'mappingVerified': False,
                'candidateRefreshed': refreshed is not None, 'candidateAgeMs': round(candidate_age * 1000),
                'targetEvidence': copy.deepcopy(evidence),
                'limitation': '创建或选择仅为浏览器标签证据；系统窗口自动映射尚未实现，不授权桌面输入'}


def public_status(record):
    if record is None:
        return {'paired': False, 'mappingVerified': False, 'observationOnly': True}
    return {k: copy.deepcopy(v) for k, v in record.items() if k != 'token'} | {
        'paired': True, 'mappingVerified': bool(record.get('mappingVerified')),
        'observationOnly': True}


def status(task_id, task_file=TASK_FILE, event_file=EVENT_FILE):
    task_exists(task_id, task_file)
    data = read_file(event_file, {'version': 1, 'tasks': {}})
    require(type(data) is dict and data.get('version') == 1 and type(data.get('tasks')) is dict,
            '观察记录格式错误')
    session = application_session()
    if session is None or data.get('peer', {}).get('applicationSession') != session:
        data = {'version': 1, 'tasks': {}}
    record = data['tasks'].get(task_id)
    if record and current_assignment(data, task_file) is not record:
        record = None
    peer = data.get('peer', {})
    live = time.time() - peer.get('lastSeen', 0) < 20
    reason = ('host_unavailable' if not live else 'permission_missing' if not peer.get('permission') else
              'disabled' if not peer.get('enabled') else 'no_assignment')
    pending = peer.get('command')
    verify = peer.get('pendingVerify')
    conflict = mapping_conflict(record, active_task(task_file)[1] or {}) if record else None
    task = active_task(task_file)[1] or {}
    capabilities = {'cdp_input', 'document_binding', 'background_popups'} <= set(peer.get('capabilities', []))
    created = next((t for t in peer.get('createdTargets', []) if t.get('taskId') == task_id), None)
    observation = current_observer_status(peer, record)
    waiting = (reason if not live or not peer.get('permission') or not peer.get('enabled') else
               'extension_upgrade_required' if not capabilities else 'target_not_assigned' if not record else
               'target_paused' if record.get('paused') else 'document_pending' if not (record.get('event') or {}).get('documentId') else
               'observer_not_ready' if observation and observation['state'] != 'ready' else None)
    return public_status(record) | {'nativeConnection': 'connected' if live else 'host_unavailable',
        'lastHeartbeatAt': peer.get('lastSeen'), 'globalPermission': peer.get('permission') if live else None,
        'commandState': {'pendingOperation': pending.get('operation') if pending else None,
                         'verificationRequired': bool(verify),
                         'verificationOperation': verify.get('operation') if verify else None,
                         'verificationNextAction': verification_next(peer),
                         'verificationReason': 'navigation_destination_changed' if (verify or {}).get('destinationChanged') else None,
                         'lastResultStatus': (peer.get('commandResult') or {}).get('status')},
        'clientVersion': peer.get('clientVersion'),
        'observerStatus': copy.deepcopy(observation),
        'lifecycleError': copy.deepcopy(peer.get('lifecycleError')),
        'createdTarget': copy.deepcopy(created),
        'capabilities': copy.deepcopy(peer.get('capabilities', [])),
        'observationOnly': bool(waiting),
        'executionPolicy': {'mode': task.get('interactionMode', 'background_browser'),
                            'fallbackAllowed': task.get('interactionMode', 'background_browser') == 'desktop',
                            'waitingReason': waiting, 'inputRoute': 'browser_cdp'},
        'crossCheck': {'mappedWindowId': (record or {}).get('mapping', {}).get('windowId')
                       if record and record.get('mappingVerified') else None,
                       'boundWindowId': (active_task(task_file)[1] or {}).get('windowId'),
                       'consistent': None if conflict is None else not conflict},
        'taskAssignment': reason if not live or not peer.get('permission') or not peer.get('enabled') else
            'paused' if record and record.get('paused') else 'assigned' if record else
            'needs_target_selection' if active_task(task_file)[1] else 'no_assignment'}


def dispatch(message, origin, allowed_origins, task_file=TASK_FILE, event_file=EVENT_FILE):
    require(type(origin) is str and re.fullmatch(r'chrome-extension://[a-p]{32}/', origin)
            and origin in allowed_origins, '扩展 origin 不在允许列表')
    require(type(message) is dict, '请求必须是对象')
    session = application_session()
    if session is None:
        return {'ok': False, 'code': 'APP_NOT_RUNNING', 'error': 'QuietBridge 已退出，请先打开应用'}
    if message.get('action') == 'channels':
        require(set(message) == {'action'}, 'channel request fields invalid')
        from session_registry import channels
        return {'ok': True, 'channels': channels(Path(task_file).parent), 'applicationSession': session}
    channel = message.get('channel')
    if channel is not None:
        from session_registry import channels, session_directory
        root = Path(task_file).parent
        require(channel in channels(root), 'SESSION_ROUTING_INVALID: channel inactive')
        scoped = session_directory(root, channel)
        task_file, event_file = scoped / 'task-windows.json', scoped / 'browser-events.json'
        message = {k: v for k, v in message.items() if k != 'channel'}
    action = message.get('action')
    if action == 'bind':
        return {'ok': False, 'code': 'LEGACY_UNSUPPORTED',
                'error': 'legacy bind unsupported：旧手动 bind 已禁用；请由可信 agent 使用内部 assign 分配已核验任务和候选标签'}
    if action == 'sync':
        return sync_peer(message, origin, task_file, event_file)
    if action == 'ping':
        # 长期端口保活：只需确认 host 可达，不改动任何任务或观察状态。
        require(set(message) == {'action'}, 'ping 字段不合法')
        return {'ok': True, 'protocolVersion': 2, 'heartbeatAt': time.time(), 'applicationSession': session}
    shapes = {'status': {'action', 'taskId'},
              'unbind': {'action', 'taskId', 'token'},
              'pause': {'action', 'taskId', 'token'},
              'event': {'action', 'taskId', 'token', 'event'}}
    require(type(action) is str and action in shapes and set(message) == shapes[action], '请求字段不合法')
    task_id = message['taskId']
    task_exists(task_id, task_file)
    if action == 'status':
        return {'ok': True, 'status': status(task_id, task_file, event_file)}
    with event_store(event_file) as data:
        # 在持有观察锁之后再次核验，不创建或改写任务记录。
        require(application_session() == session and data.get('peer', {}).get('applicationSession') == session,
                'APP_SESSION_EXPIRED: 应用运行会话已变化，请重新分配任务')
        task_exists(task_id, task_file)
        records = data['tasks']
        record = records.get(task_id)
        require(record is not None and current_assignment(data, task_file) is record,
                'legacy assignment unsupported 或活动任务/目标已更换；请使用内部 assign')
        token = message['token']
        require(type(token) is str and len(token) == 64 and record is not None and
                record['origin'] == origin and secrets.compare_digest(token, record['token']), '任务配对凭据无效')
        if action == 'unbind':
            del records[task_id]
        elif action == 'pause':
            record['paused'] = True
            peer = data.setdefault('peer', {})
            command = peer.get('command')
            if command and command.get('taskId') == task_id:
                finish_pending(peer, 'canceled', time.time())
        else:
            event = validate_event(message['event'])
            require(event['tabId'] == record['tabId'] and event['browserWindowId'] == record['browserWindowId'], '事件不属于任务配对标签')
            require(not record.get('paused') and record.get('authorizedOrigin') and
                    event['bindingId'] == record.get('bindingId'), '绑定已暂停、过期或缺少明确站点授权')
            if event['url'] is not None:
                parsed = urlsplit(event['url'])
                require(parsed.hostname != 'chromewebstore.google.com' and not
                        (parsed.hostname == 'chrome.google.com' and parsed.path.startswith('/webstore')), '受限页面')
            require(record['selector'] or event['conditionMatched'] is None, '绑定未配置 CSS 条件')
            if event['phase'] in ('content', 'pageshow'):
                require(event['readyState'] is not None and
                        (type(event['conditionMatched']) is bool if record['selector'] else
                         event['conditionMatched'] is None), '布局报告必须包含文档状态和绑定条件结果')
            previous = record['event']
            if previous:
                require(previous['phase'] != 'error' or event['phase'] in ('before', 'paused'), '错误文档必须等待新导航')
                require(event['sequence'] > previous['sequence'] and
                        event['navigationEpoch'] >= previous['navigationEpoch'], '拒绝迟到或重放事件')
                if event['navigationEpoch'] == previous['navigationEpoch']:
                    require(event['phase'] not in ('before', 'history', 'paused', 'bound') and
                            (event['documentId'] == previous['documentId'] or
                             (previous['phase'] == 'before' and event['phase'] == 'committed')), '拒绝旧文档或错误 epoch')
                else:
                    require(event['phase'] in ('before', 'committed', 'history', 'paused'), '新 epoch 必须由导航建立')
                    if event['phase'] == 'history':
                        require(event['documentId'] == previous['documentId'] and
                                previous['documentId'] is not None, 'SPA 导航不能更换文档身份')
            record['paused'] = event['phase'] == 'paused'
            record['event'] = event
        return {'ok': True, 'mappingVerified': False}


def make_tool(task_file, validate_task=None, check_cancelled=None, preflight_root=None):
    def shape(action, required=(), optional=(), operation=None):
        fields = {'action': {'const': action}, **{key: {} for key in (*required, *optional)}}
        needed = ['action', *required]
        if operation:
            fields['operation'] = {'const': operation}
            needed.append('operation')
        return {'type': 'object', 'properties': fields, 'required': needed, 'additionalProperties': False}

    shapes = [shape('candidates', optional=('taskId',)),
              shape('status', ('taskId',)), shape('create_target', ('taskId',), ('url',)),
              shape('assign', ('taskId', 'browserWindowId', 'tabId')),
              shape('result', ('taskId', 'commandId')), shape('cancel', ('taskId', 'commandId')),
              shape('annotate', ('taskId', 'ref', 'note')), shape('annotations', ('taskId',)),
              shape('verify_mapping', ('taskId',)), shape('tabs', ('taskId',)), shape('select_tab', ('taskId', 'tabId'))]
    for operation, required, optional in (
            ('snapshot', (), ()), ('read', (), ('mode', 'readId', 'offset', 'limit', 'maxChars')),
            ('diagnostics', (), ()), ('screenshot', (), ()), ('render_probe', ('stage',), ()), ('keypress', ('key',), ()),
            ('click', ('ref', 'snapshotId'), ()), ('fill', ('ref', 'snapshotId'), ('value',)),
            ('select', ('ref', 'snapshotId'), ('value',)),
            ('scroll', ('snapshotId', 'direction'), ('ref', 'lines')),
            ('navigate', ('url',), ())):
        shapes.append(shape('command', ('taskId', *required), optional, operation))

    def handler(args):
        require(type(args) is dict, '参数必须是对象')
        root_events = Path(preflight_root()) / 'browser-events.json' if preflight_root else None
        private_tasks = task_file()
        private_events = Path(private_tasks).with_name('browser-events.json')
        if root_events is not None and args == {'action': 'candidates'} and not active_task(private_tasks)[1]:
            return json.dumps(preflight_candidates(root_events, check_cancelled), ensure_ascii=False)
        if args.get('action') in ('candidates', 'assign', 'create_target', 'command', 'result',
                                  'cancel', 'annotate', 'annotations', 'verify_mapping', 'tabs', 'select_tab'):
            if root_events is not None and args.get('action') in ('candidates', 'create_target'):
                current_id, current = active_task(private_tasks)
                if current and ('taskId' not in args or args['taskId'] == current_id):
                    wait_for_private_channel(private_tasks, private_events, root_events, current_id, check_cancelled)
                    if args['action'] == 'create_target' and not connected_peer(private_events) and extension_presence(root_events):
                        raise ValueError('BROWSER_CHANNEL_STARTING: 扩展已连接，会话通道尚未就绪；只读复查 candidates，不重建任务或重载扩展')
            if args.get('action') == 'assign' and validate_task:
                validate_task(args.get('taskId'))
            options = {'revalidate': validate_task} if args.get('action') in ('assign', 'command') else {}
            result = internal_action(args, task_file=private_tasks, event_file=private_events,
                                     check_cancelled=check_cancelled, **options)
            if root_events is not None and args.get('action') == 'candidates':
                ready = extension_presence(private_events)
                result = dict(result, connectionScope='task_channel', taskChannelReady=ready)
                if not connected_peer(private_events) and extension_presence(root_events):
                    result.update(extensionConnection='connected', refreshStatus='channel_pending',
                                  refreshPending=True, retryAfterMs=1000,
                                  executionPolicy={'waitingReason': 'browser_channel_starting'})
            return json.dumps(result, ensure_ascii=False)
        require(set(args) == {'action', 'taskId'} and args['action'] == 'status', '状态查询需要 taskId')
        return json.dumps(status(args['taskId'], task_file=private_tasks, event_file=private_events), ensure_ascii=False)
    return {'name': 'quietbridge_browser_events', 'description': '网页操作必须不抢焦点并使用共享窗口能力。保持用户的前台应用、键盘窗口、物理鼠标和当前 Space；只走固定标签扩展通道，禁止 activate/AXRaise、切换 Space、前台键鼠或原生输入兜底。bind+assign 后用 verify_mapping 核验系统窗口与标签对应关系，再调用 quietbridge_windows action=share source=window 建立系统窗口连续视频。网页动作和导航不自动截图，不启动定时截图实验；预览只使用系统窗口视频。单次 screenshot 仍是独立观察证据，不等于持续预览已建立。'
                                                          '长页面阅读优先 command operation=read：mode=links 返回脱敏 href/name（query/fragment 只标记存在，不含原值），mode=content 返回当前已渲染正文 article/text。每页 limit<=100、maxChars=4000..16000；续读携带相同 mode/readId 和 nextOffset。sourceTruncated=true 表示采集达到上限，不能声称完整。正文分片按 URL+id 去重，textOffset/textComplete 用于拼接，同一帖子多片不能重复计数；text 块不等于评论数。只读当前已加载 DOM，无后台请求、不含未加载楼层；403/404/未登录应报告，不能猜接口或复制登录资料。read 不产生输入 ref；成功导航后可用新文档 read/screenshot/diagnostics 轻量复核，点击、填写等输入仍须 snapshot 复核。'
                                                          '无活动任务时 candidates 仅查询全局扩展心跳与候选，connectionScope=extension_preflight、taskChannelReady=false 不表示扩展掉线或已允许任务输入；clientVersion 来自真实心跳。begin 后 candidates 使用独立任务通道，不能将全局候选当作 assign 证据。会话首个握手最多等待 3 秒；channel_pending/BROWSER_CHANNEL_STARTING 时按 retryAfterMs 最多两次只读复查，不重建任务或重载扩展。首次 candidates 无缓存时最多等待 3 秒；refreshPending=true 仅表示尚待扩展回传，按 retryAfterMs 只读复查，不推断无标签或权限失败。verify_mapping 用窗口活动标签标题核验归属，同名时用新鲜的唯一位置与尺寸匹配，不导航改标题、不切换焦点；系统窗口连续视频可能显示窗口当前活动标签，snapshot/screenshot 仍可独立复核逻辑绑定标签。BROWSER_DEBUGGER_RESTRICTED 表示目标含其他扩展页面；BROWSER_DEBUGGER_IN_USE 才表示已有调试客户端。debuggerFailure 保留 Chrome 原文、阶段及同文档声明的扩展 iframe ID（不含路径和页面内容）。不要根据 debugger 权限或 JSON 后缀猜测原因，不重载重试或自动换目标。'
                                                          '通过已授权扩展观察及操作固定浏览器标签。优先 candidates（可带当前 taskId；refreshPending 表示异步刷新），先 quietbridge_task bind 真实系统窗口，再以候选中的 browserWindowId/tabId 显式 assign；ID 不能用 CGWindowID 代替。候选超过 20 秒时 assign 自动等待至多 3 秒获取新证据，原标签、窗口、地址及任务目标均未变化才分配；不必反复刷新候选，不自动重建窗口。绑定后持续复用同一标签，不按前台焦点换目标。create_target 仅在任务明确需要新建且尚无目标窗口时使用，可带 url，focused=false；仅 task status 确认本任务创建窗口关闭并给出 recreate_allowed 时，可在 begin(recoveryOf=旧taskId) 的未绑定阶段恢复专用窗口；不能用于权限/占用/调试器错误兜底；同一任务重复申请复用原请求或结果，不因超时另开窗口。command 下发限定操作（snapshot/read/click/fill/select/scroll/navigate/diagnostics/screenshot/keypress，单飞，15 秒过期，含扩展 service worker 冷启动预算），result 轮询（pending/ok/error/expired/canceled/evicted/unknown，不重放，附 latencyMs），cancel 如实报告 mayHaveExecuted。navigate 成功受理后等待原标签新文档就绪，可用 screenshot/read/diagnostics 复核导航，不必每页完整 snapshot；navigation_observation 表示此步骤。仅当前绑定、新 epoch、就绪且地址匹配才解除导航等待；落地地址变化时返回 verification.reason=navigation_destination_changed、nextAction=snapshot；获取当前 snapshot 核实后继续原绑定，无需重新 assign。文档未就绪时继续只读等待。其他输入或结果未知仍须 snapshot 复核；获取输入 ref 也须 snapshot。performed 不代表业务成功，浏览器分配不授权桌面输入或 wait_ready 判定。'
                                                          'render_probe 仅在用户明确同意呈现诊断时使用，stage 依次 inspect/attach/enable/layout/capture；各阶段是独立命令，非自动串联。inspect 不附加，attach 不 Page.enable，enable 不查询布局，layout 不截图，capture 仅对该文档 30 秒内 layout 的视口单次截图并丢弃图像，只返回时间/状态/长度。既有 attachedBefore=true 表示已有调试连接，不能假称首次附加对照。禁止任意 CDP、自动重复 capture、切桌、激活或将探针当作后台绘制修复；不会清除已有动作复核要求。'
                                                          '前提：Chrome 已加载 QuietBridge Browser 并授予站点权限、原生宿主已登记（可在 quietbridge_permissions 或安装页核对）。网页内容在 task begin 声明 targetSurface=web_content，自动选 background_browser；导航使用本通道 navigate，不为导航操作地址栏。浏览器自身工具栏与真实原生打开/保存对话框属于 browser_ui/native_dialog 的独立原生阶段，不在本通道操作。网页里的弹窗、跨域 frame、受限 URL 或 AX 空树不是原生阶段证据。扩展未就绪时通知用户恢复，禁止回落 AX 或桌面输入。snapshot 返回 snapshotId 和 coverage：elementCount 是顶层 DOM 节点数（最多 10000），elements 是可见交互元素（最多 300），二者不必相等；coverage.truncated 表示交互元素确实被截断，长文改用 read。click/fill/select/scroll 必须传 snapshotId。滚动使用 direction=down/up、lines=6（1..10）；不要猜 deltaY 符号。到达边界与滚动未生效会分别报告，不把 performed 当作已移动。screenshot 直接截取该标签。tabs 列出任务归属标签，select_tab 只改逻辑目标，不激活标签或窗口。keypress 只支持页面按键。',
            'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status', 'candidates', 'create_target', 'command', 'result', 'cancel', 'annotate', 'annotations', 'verify_mapping', 'assign', 'tabs', 'select_tab']},
                'taskId': {'type': 'string'}, 'browserWindowId': {'type': 'integer'}, 'tabId': {'type': 'integer'},
                'operation': {'type': 'string', 'enum': list(OPERATIONS)},
                'stage': {'type': 'string', 'enum': list(RENDER_PROBE_STAGES)},
                'snapshotId': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                'mode': {'type': 'string', 'enum': ['links', 'content'], 'description': 'read 的内容类型；默认 content'},
                'readId': {'type': 'string', 'minLength': 1, 'maxLength': 180, 'description': '续读必须使用同一次读取返回的 readId；不是操作快照'},
                'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2000, 'description': 'read 的 nextOffset，首次为 0'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 30},
                'maxChars': {'type': 'integer', 'minimum': 4000, 'maximum': 16000, 'default': 10000},
                'key': {'type': 'string'},
                'ref': {'type': 'integer', 'minimum': 1, 'description': '快照中的元素引用'},
                'note': {'type': 'string', 'maxLength': 200, 'description': 'annotate 的标注文本'},
                'value': {'type': 'string', 'maxLength': 1000, 'description': 'fill/select 的目标值'},
                'direction': {'type': 'string', 'enum': ['down', 'up'], 'description': 'scroll 必填方向：down 向下，up 向上'},
                'lines': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 6, 'description': 'direction 模式的滚动行数，每行 40 CSS px，默认 6'},
                'commandId': {'type': 'string', 'description': 'command 返回的标识，用于 result/cancel'},
                'url': {'type': 'string', 'description': 'create_target 起始地址或 navigate 目标，仅 http/https 且不允许凭据'}},
                'required': ['action'], 'additionalProperties': False, 'oneOf': shapes,
                'examples': [{'action': 'candidates', 'taskId': 'current-task'},
                             {'action': 'assign', 'taskId': 'current-task', 'browserWindowId': 2, 'tabId': 7},
                             {'action': 'command', 'taskId': 'current-task', 'operation': 'snapshot'},
                             {'action': 'command', 'taskId': 'current-task', 'operation': 'scroll',
                              'snapshotId': 'snapshot-from-last-result', 'direction': 'down', 'lines': 6}]},
            'handler': handler, 'readOnly': False}
