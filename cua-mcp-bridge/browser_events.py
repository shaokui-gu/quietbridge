"""独立的浏览器观察存储；不接触 helper、输入权限或系统窗口映射。"""
import copy
import fcntl
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
# 运行态必须在非 TCC 目录：Chrome 派生的解释器读不了 ~/Documents，
# 因此安装后的任务记录放在 ~/Library/Application Support，项目内副本只作源码。
RUNTIME = Path.home() / 'Library/Application Support/WorkbuddyExtension'


def config_dir():
    override = os.environ.get('CUA_CONFIG_DIR')
    if override:
        return Path(override)
    if (RUNTIME / 'cua-helper-config').is_dir():
        return RUNTIME / 'cua-helper-config'
    return ROOT / 'cua-helper-config'


CONFIG_DIR = config_dir()
TASK_FILE = CONFIG_DIR / 'task-windows.json'
EVENT_FILE = CONFIG_DIR / 'browser-events.json'
MAX_BYTES = 1024 * 1024


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
COMMAND_TTL = 8
MAX_RESULTS = 8
MAX_SEEN = 64
OPERATIONS = ('snapshot', 'click', 'fill', 'select', 'scroll', 'navigate', 'diagnostics', 'screenshot', 'keypress')
MAX_ELEMENTS = 300
MAX_LABEL = 80
ACTION_FIELDS = ('ref', 'value', 'deltaY', 'url', 'snapshotId', 'key')


def command_params(args, operation):
    allowed = {'snapshot': set(), 'click': {'ref'}, 'fill': {'ref', 'value'},
               'select': {'ref', 'value'}, 'scroll': {'ref', 'deltaY'},
               'navigate': {'url'}, 'diagnostics': set(), 'screenshot': set(), 'keypress': {'key'}}[operation]
    if operation in ('click', 'fill', 'select', 'scroll'):
        allowed.add('snapshotId')
    given = {key for key in ACTION_FIELDS if key in args}
    require(given <= allowed, '该命令不接受这些参数')
    if 'snapshotId' in args:
        require(type(args['snapshotId']) is str and 0 < len(args['snapshotId']) <= 160, '快照标识错误')
    if operation == 'keypress':
        require(args.get('key') in ('Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight',
                'Home', 'End', 'PageUp', 'PageDown', 'Backspace', 'Delete', 'Space'), '页面按键不支持')
    if 'ref' in args:
        require(valid_int(args['ref'], 1), '元素引用必须是正整数')
    if 'value' in args:
        require(type(args['value']) is str and len(args['value']) <= 1000, '输入值类型或长度错误')
    if 'deltaY' in args:
        require(valid_int(args['deltaY'], -10, 10), '滚动量必须是 -10..10')
    if operation in ('click', 'fill', 'select'):
        require('ref' in args, '该命令必须指定元素引用')
    if operation == 'navigate':
        require('url' in args, '导航命令必须指定地址')
        creation_url(args['url'])
    return {key: args[key] for key in ACTION_FIELDS if key in args}


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


def validate_action_result(result, operation):
    require(type(result) is dict and {'performed', 'verified', 'observed'} <= set(result) <= {'performed', 'verified', 'observed', 'children'},
            '动作结果字段错误')
    require(type(result['performed']) is bool and type(result['verified']) is bool, '动作结果状态错误')
    observed = result['observed']
    require(type(observed) is dict and len(observed) <= 16, '动作观测字段错误')
    for key, value in observed.items():
        require(type(key) is str and 0 < len(key) <= 32, '观测键错误')
        require(type(value) in (bool, int) or (type(value) is str and len(value) <= 200),
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
    require(type(result) is dict and {'url', 'readyState', 'elementCount', 'elements'} <= set(result) <= {'url', 'readyState', 'elementCount', 'elements', 'snapshotId'},
            '快照字段错误')
    require(type(result['url']) is str and 0 < len(result['url']) <= 8192, '快照 URL 错误')
    require(result['readyState'] in ('loading', 'interactive', 'complete'), '快照文档状态错误')
    require(valid_int(result['elementCount'], high=10000), '快照元素总数错误')
    if 'snapshotId' in result:
        require(type(result['snapshotId']) is str and 0 < len(result['snapshotId']) <= 160, '快照标识错误')
    elements = result['elements']
    require(type(elements) is list and len(elements) <= MAX_ELEMENTS, '快照元素数量错误')
    seen = set()
    for element in elements:
        require(type(element) is dict and set(element) ==
                {'ref', 'tag', 'role', 'name', 'disabled', 'x', 'y', 'width', 'height'}, '快照元素字段错误')
        require(valid_int(element['ref'], 1) and element['ref'] not in seen, '快照元素引用重复或非法')
        seen.add(element['ref'])
        require(type(element['tag']) is str and 1 <= len(element['tag']) <= 32, '元素标签错误')
        require(type(element['role']) is str and len(element['role']) <= 32, '元素角色错误')
        require(type(element['name']) is str and len(element['name']) <= MAX_LABEL, '元素名称错误')
        require(type(element['disabled']) is bool, '元素禁用状态错误')
        for key in ('x', 'y', 'width', 'height'):
            require(type(element[key]) is int and -100000 <= element[key] <= 100000, '元素几何错误')
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


def active_task(task_file):
    data = read_file(task_file, {'version': 1, 'tasks': {}})
    require(type(data) is dict and data.get('version') == 1 and type(data.get('tasks')) is dict,
            '任务记录格式错误')
    task_id = data.get('activeTaskId')
    task = data['tasks'].get(task_id) if type(task_id) is str else None
    return task_id, task


def target_identity(task):
    return {k: copy.deepcopy(task.get(k)) for k in
            ('taskId', 'state', 'windowId', 'pid', 'processIdentity', 'sessionId')}


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


def sync_peer(message, origin, task_file, event_file):
    base = {'action', 'enabled', 'permission'}
    optional = ('candidates', 'created', 'commandResult', 'clientVersion', 'assignmentTitle', 'capabilities')
    allowed = {frozenset(base | set(combo))
               for size in range(len(optional) + 1)
               for combo in itertools.combinations(optional, size)}
    require(frozenset(message) in allowed, '握手字段错误')
    require(type(message['enabled']) is bool and type(message['permission']) is bool, '权限类型错误')
    now = time.time()
    with event_store(event_file) as data:
        peer = data.setdefault('peer', {})
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
        if 'created' in message:
            created = message['created']
            require(type(created) is dict and set(created) == CREATED_FIELDS, '创建结果字段错误')
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
                'at': now}] + [t for t in peer.get('createdTargets', [])
                               if t.get('tabId') != created['tabId']])[:8]
            peer['createRequest'] = None
        # 建窗证据只对当前活动任务有效；单活动任务模型下旧归属不得阻塞新任务。
        active_id, _ = active_task(task_file)
        peer['createdTargets'] = [t for t in peer.get('createdTargets', [])
                                  if t.get('taskId') == active_id]
        if 'commandResult' in message:
            reply = message['commandResult']
            require(type(reply) is dict and set(reply) in
                    ({'requestId', 'status', 'error'}, {'requestId', 'status', 'result'}),
                    '命令结果字段错误')
            canceled = peer.get('commandCanceled')
            accepted_reply = reply.get('requestId') in peer.get('commandSeen', [])
            if accepted_reply or (canceled and reply['requestId'] == canceled.get('commandId')) or reply['requestId'] == peer.get('commandExpired'):
                # 已取消命令的迟到结果不采信、不记为成功，但允许扩展清空待回传。
                if canceled and reply['requestId'] == canceled.get('commandId'):
                    peer['commandCanceled'] = {**canceled, 'lateResult': True}
            else:
                pending = peer.get('command')
                require(type(pending) is dict and reply['requestId'] == pending.get('requestId'),
                        '未被请求的命令结果')
                require(reply['status'] in ('ok', 'error'), '命令结果状态错误')
                latency = (round((now - pending['deliveredAt']) * 1000, 1)
                           if pending.get('deliveredAt') else None)
                assignment_now = current_assignment(data, task_file)
                event_now = (assignment_now or {}).get('event') or {}
                if reply['status'] == 'ok' and pending.get('documentId') and pending['operation'] in ('snapshot', 'screenshot') and (
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
                                0 < len(outcome['data']) <= 200000 and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', outcome['data']) and
                                outcome['tabId'] == pending['tabId'] and outcome['documentId'] == pending['documentId'], '截图身份或数据错误')
                        # Keep one image; do not grow the JSON store with old screenshots.
                        for previous in peer.get('commandResults', []):
                            if previous.get('operation') == 'screenshot':
                                previous['result'] = {'expiredImage': True}
                    elif pending['operation'] == 'diagnostics':
                        outcome = validate_diagnostics(reply['result'])
                    else:
                        outcome = validate_action_result(reply['result'], pending['operation'])
                        if outcome.get('children'):
                            require(assignment_now is not None, '子标签所属任务已失效')
                            owned = assignment_now.setdefault('ownedTabs', [{'tabId': assignment_now['tabId'],
                                'browserWindowId': assignment_now['browserWindowId']}])
                            for child in outcome['children']:
                                require(child['browserWindowId'] == assignment_now['browserWindowId'] and
                                        not any(other.get('tabId') == child['tabId'] for other in owned), '子标签必须属于固定任务窗口且不能重复')
                                require(len(owned) < 64, '任务子标签数量达到上限')
                                owned.append(copy.deepcopy(child))
                        # 只有真正执行过的动作才要求复核；失败或无效引用不制造虚假闭环。
                        peer['pendingVerify'] = ({'commandId': reply['requestId'],
                                                  'operation': pending['operation'],
                                                  'taskId': pending['taskId']}
                                                 if outcome['performed'] else None)
                    peer['commandResult'] = {'requestId': reply['requestId'], 'status': 'ok',
                                             'operation': pending['operation'],
                                             'latencyMs': latency,
                                             'result': outcome, 'at': now}
                else:
                    error = reply['error']
                    require(type(error) is str and 0 < len(error) <= 200, '命令错误信息不合法')
                    if pending['operation'] not in ('snapshot', 'diagnostics', 'screenshot'):
                        peer['pendingVerify'] = {'commandId': reply['requestId'], 'taskId': pending['taskId']}
                    peer['commandResult'] = {'requestId': reply['requestId'], 'status': 'error',
                                             'operation': pending['operation'],
                                             'latencyMs': latency,
                                             'error': error, 'at': now}
                peer['commandResults'] = (peer.get('commandResults', []) +
                                          [peer['commandResult']])[-MAX_RESULTS:]
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
        # 单活动任务模型：其他任务的浏览器分配一律淘汰，避免失效任务长期占用标签。
        active_id, _ = active_task(task_file)
        data['tasks'] = {k: v for k, v in data['tasks'].items() if k == active_id}
        record = current_assignment(data, task_file)
        task_id, task = active_task(task_file)
        assignment = copy.deepcopy(record) if record and record['origin'] == origin else None
        if assignment is not None:
            # WindowServer mapping evidence belongs to the host, not the observer.
            # Its read-only wire contract must not claim or inherit that attestation.
            assignment['mappingVerified'] = False
            assignment.pop('mapping', None)
        command = peer.get('command')
        if command and (not assignment or command.get('bindingId', assignment['bindingId']) != assignment['bindingId']):
            peer['commandCanceled'] = {'commandId': command['requestId'], 'delivered': bool(command.get('delivered'))}
            peer['command'] = None
            command = None
        if command and now > command.get('expiresAt', 0):
            peer['commandExpired'] = command['requestId']
            if command.get('delivered') and command['operation'] not in ('snapshot', 'diagnostics', 'screenshot'):
                peer['pendingVerify'] = {'commandId': command['requestId'], 'taskId': command['taskId']}
            peer['command'] = None
            command = None
        if command:
            # 记录已投递，取消时才能诚实区分「可能已执行」与「尚未下发」。
            command['delivered'] = True
            command.setdefault('deliveredAt', now)
            peer['command'] = command
        return {'ok': True, 'protocolVersion': 2, 'heartbeatAt': now,
                'activeTaskId': task_id,
                'requestCandidates': bool(peer.get('requestCandidates')),
                'createRequest': copy.deepcopy(peer.get('createRequest')),
                'command': copy.deepcopy(command),
                'taskAssignment': ('permission_missing' if not message['permission'] else
                    'disabled' if not message['enabled'] else 'paused' if assignment and assignment.get('paused') else
                    'assigned' if assignment else
                    'needs_target_selection' if task else 'no_assignment'),
                'assignment': assignment}


def internal_action(args, task_file=TASK_FILE, event_file=EVENT_FILE):
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
        # 单活动任务模型：其他任务的浏览器分配一律淘汰（assign 可能早于下一次 sync）。
        active_now, _ = active_task(task_file)
        data['tasks'] = {k: v for k, v in data.get('tasks', {}).items() if k == active_now}
        live = time.time() - peer.get('lastSeen', 0) < 20
        available = live and peer.get('enabled') and peer.get('permission')
        if action == 'candidates':
            if 'taskId' in args:
                require(type(args['taskId']) is str and args['taskId'] == active_now and active_now is not None,
                        '需要当前活动任务的 taskId')
            peer['requestCandidates'] = True
            data['peer'] = peer
            return {'nativeConnection': 'connected' if live else 'host_unavailable',
                    'globalPermission': bool(peer.get('permission')) if live else None,
                    'candidates': copy.deepcopy(peer.get('candidates', [])) if available and
                        time.time() - peer.get('candidatesAt', 0) < 20 else [],
                    'refreshPending': True, 'mappingVerified': False}
        require(available, 'host_unavailable 或 permission_missing')
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
                        'mappingVerified': False, 'nextAction': 'candidates_then_assign'}
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
            title = peer.get('assignmentTitle')
            require(type(title) is str and title, '扩展尚未回传标签标题')
            windows = args['windows']
            require(type(windows) is list and len(windows) <= 200, '窗口证据数量错误')
            for window in windows:
                require(type(window) is dict and set(window) == {'windowId', 'title'} and
                        valid_int(window['windowId'], 1) and type(window['title']) is str and
                        len(window['title']) <= 200, '窗口证据格式错误')
            hits = [w for w in windows if w['title'].strip() == title.strip()]
            if len(hits) == 1:
                record['mappingVerified'] = True
                record['mapping'] = {'windowId': hits[0]['windowId'], 'method': 'title',
                                     'title': title, 'at': time.time()}
                return {'verified': True, 'windowId': hits[0]['windowId'], 'method': 'title',
                        'candidates': 1}
            record['mappingVerified'] = False
            record['mapping'] = {'verified': False, 'method': 'title', 'title': title,
                                 'reason': 'no_match' if not hits else 'ambiguous',
                                 'candidates': len(hits), 'at': time.time()}
            return {'verified': False, 'method': 'title',
                    'reason': 'no_match' if not hits else 'ambiguous', 'candidates': len(hits)}
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
            require(mapping_conflict(record, task) is not True,
                    '映射窗口与任务绑定窗口不一致：先重新 verify_mapping')
            if args['operation'] not in ('snapshot', 'diagnostics', 'screenshot'):
                verify = peer.get('pendingVerify')
                require(not verify or verify.get('taskId') != task_id,
                        '上一个动作尚未用 snapshot 复核')
            event = record.get('event') or {}
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
            pending = peer.get('command')
            require(not pending or time.time() > pending.get('expiresAt', 0), '已有未完成的命令')
            request = {'requestId': secrets.token_hex(16), 'taskId': task_id,
                       'operation': args['operation'], 'params': params,
                       'bindingId': record['bindingId'], 'assignmentVersion': record['assignmentVersion'],
                       'tabId': record['tabId'], 'browserWindowId': record['browserWindowId'],
                       'documentId': event['documentId'], 'navigationEpoch': event['navigationEpoch'],
                       'issuedAt': time.time(), 'expiresAt': time.time() + COMMAND_TTL}
            peer.update(command=request, commandResult=None, commandExpired=None)
            return {'commandId': request['requestId'], 'status': 'queued',
                    'expiresInSeconds': COMMAND_TTL,
                    'target': {k: request[k] for k in ('tabId', 'browserWindowId', 'documentId', 'navigationEpoch')},
                    'route': 'background_browser',
                    'limitation': '输入只走固定标签的 CDP/DOM；不激活窗口，不回落桌面输入。performed 不代表业务成功，须后续 snapshot 复核'}
        if action == 'result':
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
                    reply['nextAction'] = ('snapshot' if peer.get('pendingVerify') else
                                           'observe' if done.get('operation') in ('snapshot', 'diagnostics')
                                           else 'done')
                else:
                    reply['error'] = copy.deepcopy(done['error'])
                    reply['nextAction'] = 'stop'
                return reply
            if args['commandId'] in peer.get('commandSeen', []):
                return {'commandId': args['commandId'], 'status': 'evicted',
                        'note': '结果已被后续命令挤出保留窗口'}
            return {'commandId': args['commandId'], 'status': 'unknown'}
        if action == 'cancel':
            command = peer.get('command')
            if command and command['requestId'] == args['commandId']:
                peer['commandCanceled'] = {'commandId': command['requestId'], 'at': time.time(),
                                           'delivered': bool(command.get('delivered'))}
                peer['command'] = None
                if command.get('delivered') and command['operation'] not in ('snapshot', 'diagnostics', 'screenshot'):
                    peer['pendingVerify'] = {'commandId': command['requestId'], 'taskId': command['taskId']}
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
        require(time.time() - peer.get('candidatesAt', 0) < 20, '扩展候选证据过期')
        candidate = next((c for c in peer.get('candidates', []) if c['tabId'] == args['tabId'] and
                          c['browserWindowId'] == args['browserWindowId']), None)
        require(candidate is not None, '缺少扩展目标证据；不能使用 CGWindowID 代替浏览器窗口 ID')
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
        needs_verify = peer.get('pendingVerify') or (old_command if old_command.get('delivered') else None)
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
    waiting = (reason if not live or not peer.get('permission') or not peer.get('enabled') else
               'extension_upgrade_required' if not capabilities else 'target_not_assigned' if not record else
               'target_paused' if record.get('paused') else 'document_pending' if not (record.get('event') or {}).get('documentId') else None)
    return public_status(record) | {'nativeConnection': 'connected' if live else 'host_unavailable',
        'lastHeartbeatAt': peer.get('lastSeen'), 'globalPermission': peer.get('permission') if live else None,
        'commandState': {'pendingOperation': pending.get('operation') if pending else None,
                         'verificationRequired': bool(verify),
                         'verificationOperation': verify.get('operation') if verify else None,
                         'lastResultStatus': (peer.get('commandResult') or {}).get('status')},
        'clientVersion': peer.get('clientVersion'),
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
    action = message.get('action')
    if action == 'bind':
        return {'ok': False, 'code': 'LEGACY_UNSUPPORTED',
                'error': 'legacy bind unsupported：旧手动 bind 已禁用；请由可信 agent 使用内部 assign 分配已核验任务和候选标签'}
    if action == 'sync':
        return sync_peer(message, origin, task_file, event_file)
    if action == 'ping':
        # 长期端口保活：只需确认 host 可达，不改动任何任务或观察状态。
        require(set(message) == {'action'}, 'ping 字段不合法')
        return {'ok': True, 'protocolVersion': 2, 'heartbeatAt': time.time()}
    shapes = {'status': {'action', 'taskId'},
              'unbind': {'action', 'taskId', 'token'},
              'event': {'action', 'taskId', 'token', 'event'}}
    require(type(action) is str and action in shapes and set(message) == shapes[action], '请求字段不合法')
    task_id = message['taskId']
    task_exists(task_id, task_file)
    if action == 'status':
        return {'ok': True, 'status': status(task_id, task_file, event_file)}
    with event_store(event_file) as data:
        # 在持有观察锁之后再次核验，不创建或改写任务记录。
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


def make_tool(task_file, validate_task=None):
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
            ('snapshot', (), ()), ('diagnostics', (), ()), ('screenshot', (), ()), ('keypress', ('key',), ()),
            ('click', ('ref', 'snapshotId'), ()), ('fill', ('ref', 'snapshotId'), ('value',)),
            ('select', ('ref', 'snapshotId'), ('value',)),
            ('scroll', ('snapshotId',), ('ref', 'deltaY')), ('navigate', ('url',), ())):
        shapes.append(shape('command', ('taskId', *required), optional, operation))

    def handler(args):
        require(type(args) is dict, '参数必须是对象')
        if args.get('action') in ('candidates', 'assign', 'create_target', 'command', 'result',
                                  'cancel', 'annotate', 'annotations', 'verify_mapping', 'tabs', 'select_tab'):
            if args.get('action') == 'assign' and validate_task:
                validate_task(args.get('taskId'))
            return json.dumps(internal_action(args, task_file=task_file()), ensure_ascii=False)
        require(set(args) == {'action', 'taskId'} and args['action'] == 'status', '状态查询需要 taskId')
        return json.dumps(status(args['taskId'], task_file=task_file()), ensure_ascii=False)
    return {'name': 'cua_browser_events', 'description': '通过已授权扩展观察及操作固定浏览器标签。优先 candidates（可带当前 taskId；轮询后再次查询），再以真实 browserWindowId/tabId 显式 assign；ID 不能用 CGWindowID 代替。绑定后持续复用同一标签，不按前台焦点换目标。create_target 仅用于尚无窗口的活动任务，可带 url，focused=false；同一任务重复申请复用原请求或结果，不因超时另开窗口。command 下发限定操作（snapshot/click/fill/select/scroll/navigate/diagnostics，单飞，8 秒过期），result 轮询（pending/ok/error/expired/canceled/unknown，不重放，附 latencyMs），cancel 如实报告 mayHaveExecuted。动作执行过之后必须先用 snapshot 复核才能继续动作。performed 不代表业务成功，浏览器分配不授权桌面输入或 wait_ready 判定。'
                                                          '前提：Chrome 已加载 WorkbuddyExtension 并授予站点权限、原生宿主已登记（可在 cua_permissions 或安装页核对）。后台网页任务仅走此通道，扩展未就绪时等待，禁止回落 AX 或桌面输入。snapshot 返回 snapshotId，click/fill/select/scroll 必须传该标识；screenshot 直接截取该标签。tabs 列出任务归属标签，select_tab 只改逻辑目标，不激活标签或窗口。keypress 只支持页面按键。',
            'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status', 'candidates', 'create_target', 'command', 'result', 'cancel', 'annotate', 'annotations', 'verify_mapping', 'assign', 'tabs', 'select_tab']},
                'taskId': {'type': 'string'}, 'browserWindowId': {'type': 'integer'}, 'tabId': {'type': 'integer'},
                'operation': {'type': 'string', 'enum': list(OPERATIONS)},
                'snapshotId': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                'key': {'type': 'string'},
                'ref': {'type': 'integer', 'minimum': 1, 'description': '快照中的元素引用'},
                'note': {'type': 'string', 'maxLength': 200, 'description': 'annotate 的标注文本'},
                'value': {'type': 'string', 'maxLength': 1000, 'description': 'fill/select 的目标值'},
                'deltaY': {'type': 'integer', 'minimum': -10, 'maximum': 10, 'description': 'scroll 行数'},
                'commandId': {'type': 'string', 'description': 'command 返回的标识，用于 result/cancel'},
                'url': {'type': 'string', 'description': 'create_target 起始地址或 navigate 目标，仅 http/https 且不允许凭据'}},
                'required': ['action'], 'additionalProperties': False, 'oneOf': shapes,
                'examples': [{'action': 'candidates', 'taskId': 'current-task'},
                             {'action': 'assign', 'taskId': 'current-task', 'browserWindowId': 2, 'tabId': 7},
                             {'action': 'command', 'taskId': 'current-task', 'operation': 'snapshot'}]},
            'handler': handler, 'readOnly': False}
