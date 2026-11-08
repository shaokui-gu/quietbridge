"""Host-neutral receipt and revocation store; never controls user interfaces."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
from compatibility import browser_layout, environment

MAX_BYTES = 1024 * 1024
ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def config_directory():
    return Path(environment('QUIETBRIDGE_CONFIG_DIR') or browser_layout()[3])


def checked(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('lifecycle path contains symlink')
    return path


def read(path, default):
    path = checked(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o022):
            raise ValueError('unsafe lifecycle file')
        raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('lifecycle file too large')
        return json.loads(raw)


@contextmanager
def store(config, now=None):
    now = time.time() if now is None else now
    parent = checked(Path(config) / 'host-lifecycle')
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe lifecycle directory')
    fd = os.open(checked(parent / 'state.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe lifecycle lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = parent / 'state.json'
        data = read(path, {'version': 1, 'turns': {}})
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('turns'), dict):
            raise ValueError('invalid lifecycle state')
        before = json.dumps(data, sort_keys=True)
        yield data
        if before != json.dumps(data, sort_keys=True):
            # Retain recent stop tombstones so a delayed PostToolUse cannot
            # resurrect the task. No transcript or tool arguments are stored.
            turns = data['turns']
            for key in list(turns):
                if now - turns[key].get('updatedAt', 0) > 86400:
                    del turns[key]
            if len(turns) > 512:
                raise ValueError('too many lifecycle turns')
            temporary = parent / ('.state-' + uuid.uuid4().hex)
            target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(target, 'w') as stream:
                    encoded = json.dumps(data)
                    if len(encoded.encode('utf-8')) > MAX_BYTES:
                        raise ValueError('lifecycle file too large')
                    stream.write(encoded)
                os.replace(temporary, checked(path))
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        os.close(fd)


def task_receipt(value):
    """Accept the JSON task result inside standard MCP text/result wrappers."""
    pending = [value]
    for _ in range(80):
        if not pending:
            break
        item = pending.pop()
        if isinstance(item, str) and len(item) <= MAX_BYTES:
            try:
                pending.append(json.loads(item))
            except ValueError:
                pass
        elif isinstance(item, list):
            pending.extend(item[:32])
        elif isinstance(item, dict):
            if item.get('isError') or item.get('is_error'):
                continue
            lease = item.get('lease') or {}
            if (item.get('state') in ('awaiting_window', 'bound') and
                    isinstance(lease, dict) and isinstance(item.get('taskId'), str) and
                    isinstance(lease.get('ownerId'), str)):
                try:
                    uuid.UUID(item['taskId']); uuid.UUID(lease['ownerId'])
                except ValueError:
                    continue
                return item
            pending.extend(item[k] for k in ('result', 'content', 'text', 'structuredContent') if k in item)
    return None
def host_binding(record, config):
    """Find the host run that acknowledged this exact task/connector pair.

    Keep phase receipts after task end: a host stop must still revoke session
    protection while no interface task is active.
    """
    data = read(Path(config) / 'host-lifecycle/state.json', {})
    for turn in data.get('turns', {}).values():
        task_id = record.get('taskId')
        owner_id = (record.get('lease') or {}).get('ownerId')
        receipts = turn.get('tasks') or {}
        if (owner_id and ((turn.get('taskId') == task_id and turn.get('ownerId') == owner_id) or
                          receipts.get(task_id) == owner_id)):
            return turn
    return None


def stop_reason(record, config):
    turn = host_binding(record, config)
    if turn is not None and turn.get('stoppedAt') is not None:
        return turn.get('reason', 'host_turn_ended')
    return None
