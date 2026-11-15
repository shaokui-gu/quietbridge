"""Three bounded execution slots and conservative cross-session target leases."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import time

from browser_events import event_store, lease_live, read_file
from lifecycle_store import checked, host_binding

LIMIT = 3
KEY = re.compile(r'[a-f0-9]{64}')


class TargetInUse(ValueError):
    """Expose only the proven lease owner, never infer identity from a title."""
    def __init__(self, slot, scope):
        path = Path(slot['taskFile'])
        task = read_file(path, {}).get('tasks', {}).get(slot['taskId'], {})
        config = path.parent.parent.parent if path.parent.parent.name == 'sessions' else path.parent
        try:
            binding = host_binding(task, config) or {}
        except (OSError, ValueError, TypeError):
            binding = {}  # Missing attribution never removes the target conflict.
        target = slot.get('target') or {}
        self.conflict = dict(taskId=slot['taskId'], scope=scope,
                             **{k: target.get(k) for k in ('pid', 'windowId', 'interactionMode')})
        if binding.get('sessionId'):
            self.conflict['hostSessionId'] = binding['sessionId']
        owner = '，WorkBuddy 会话 ' + binding['sessionId'] if binding.get('sessionId') else ''
        super().__init__('TARGET_IN_USE: 目标由任务 %s 持有（窗口 %s%s）。'
                         '请在 QuietBridge“任务与窗口”中按会话核对并由用户停止；'
                         '停止本地 HTTP 服务或其他会话不会释放此目标，不能自动接管。' %
                         (slot['taskId'], target.get('windowId'), owner))


def session_key(host, session=None, agent=''):
    """Stable private session channel, scoped to the trusted host identity.

    New hosts pass the host, session and agent explicitly so a sub-agent can
    never share another agent's channel. The historical single-argument call
    (WorkBuddy hooks) keeps the exact published key: existing session
    directories, running workers and adapter receipts stay valid.
    """
    if session is None:
        return hashlib.sha256(('workbuddy\0' + host).encode()).hexdigest()
    if not isinstance(host, str) or not isinstance(session, str) or not session or \
            not isinstance(agent, str) or not agent:
        # A host connection always names both the session and its agent; an
        # empty agent must not silently degrade to a session-wide channel.
        raise ValueError('SESSION_ROUTING_REQUIRED: host session/agent context missing')
    return hashlib.sha256((host + '\0' + session + '\0' + agent).encode()).hexdigest()


def session_directory(config, key, create=False):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError('SESSION_ROUTING_REQUIRED: invalid session channel')
    parent = checked(Path(config) / 'sessions')
    path = checked(parent / key)
    if create:
        parent.mkdir(mode=0o700, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=True)
    for node in (parent, path):
        info = node.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe session directory')
    return path


def live_slots(data):
    result = {}
    for key, slot in data.get('tasks', {}).items():
        if not isinstance(slot, dict):
            continue
        try:
            path = Path(slot['taskFile'])
            store = read_file(path, {})
            task = store.get('tasks', {}).get(slot.get('taskId'))
            if task is None and time.time() - slot.get('reservedAt', 0) < 5:
                task = slot.get('reservation')
            elif store.get('activeTaskId') != slot.get('taskId'):
                continue
            if task and task.get('state') not in ('ended', 'interrupted', 'invalidated') and lease_live(task, path.parent):
                result[key] = slot
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


@contextmanager
def registry(config):
    with event_store(Path(config) / 'session-registry.json') as data:
        data['tasks'] = live_slots(data)
        yield data['tasks']


def reserve(config, key, task_file, record):
    """Called while the caller holds its task file lock; no reverse lock order."""
    with registry(config) as slots:
        # The caller's new task is not written yet. Keep the reservation until
        # the task lock releases; competing admissions observe the live owner.
        others = {k: v for k, v in slots.items() if k != key}
        if len(others) >= LIMIT:
            raise ValueError('SESSION_CAPACITY_FULL: 最多同时运行 3 个会话；请结束自己的一项后重试')
        slots[key] = {'taskFile': str(task_file), 'taskId': record['taskId'],
                      'reservedAt': time.time(), 'reservation': record}


def release(config, key, task_id=None):
    with registry(config) as slots:
        if key in slots and (task_id is None or slots[key].get('taskId') == task_id):
            slots.pop(key)


def claim_target(config, key, record, task_file):
    with registry(config) as slots:
        for other_key, slot in slots.items():
            if other_key == key:
                continue
            target = slot.get('target') or {}
            # Separate native windows in one process can share application-wide
            # state. Until verified, arbitrate desktop operations at PID level.
            same_window = target.get('pid') == record.get('pid') and target.get('windowId') == record.get('windowId')
            same_native_app = target.get('pid') == record.get('pid') and (
                target.get('interactionMode') == 'desktop' or record.get('interactionMode') == 'desktop')
            if same_window or same_native_app:
                raise TargetInUse(slot, 'application_process' if same_native_app else 'window')
        if key not in slots and len(slots) >= LIMIT:
            raise ValueError('SESSION_CAPACITY_FULL: 最多同时运行 3 个会话')
        slots[key] = {'taskFile': str(task_file), 'taskId': record['taskId'],
                      'target': {k: record.get(k) for k in ('pid', 'windowId', 'interactionMode')}}


def channels(config):
    with registry(config) as slots:
        return [key for key, slot in slots.items() if KEY.fullmatch(key) and
                Path(slot['taskFile']).parent == session_directory(config, key)]
