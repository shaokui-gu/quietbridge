"""Workbuddy's supported hooks revoke only the task returned to that turn.

No process scanning, host log parsing, UI automation or input replay. FinalStop
also runs when a turn is cancelled between MCP calls and its transport stays open.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

MAX_BYTES = 1024 * 1024
ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def config_directory():
    return Path(os.environ.get('CUA_CONFIG_DIR') or
                Path.home() / 'Library/Application Support/WorkbuddyExtension/cua-helper-config')


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
                    json.dump(data, stream)
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


def handle_hook(payload, config, request_id=None, now=None):
    now = time.time() if now is None else now
    event = payload.get('hook_event_name')
    session = payload.get('session_id')
    if not isinstance(session, str) or not ID.fullmatch(session):
        return
    request_id = request_id or ''
    if not isinstance(request_id, str) or request_id and not ID.fullmatch(request_id):
        return
    record = None
    if event == 'PostToolUse':
        if (not re.search(r'(?:^|__)cua_task$', str(payload.get('tool_name', ''))) or
                not isinstance(payload.get('tool_input'), dict) or
                payload['tool_input'].get('action') not in ('begin', 'bind')):
            return
        record = task_receipt(payload.get('tool_response'))
        if record is None:
            return
        tasks = read(Path(config) / 'task-windows.json', {})
        current = tasks.get('tasks', {}).get(tasks.get('activeTaskId'), {})
        if (current.get('taskId') != record['taskId'] or
                (current.get('lease') or {}).get('ownerId') != record['lease']['ownerId'] or
                current.get('state') not in ('awaiting_window', 'bound')):
            return
    elif event not in ('FinalStop', 'SessionEnd'):
        return
    elif not checked(Path(config) / 'host-lifecycle').is_dir():
        # A FinalStop without a recorded begin still needs a tombstone for
        # a racing PostToolUse, but never create missing host runtime folders.
        if not checked(config).is_dir():
            return
    key = hashlib.sha256((session + '\0' + request_id).encode()).hexdigest()
    with store(config, now) as data:
        turns = data['turns']
        if event == 'SessionEnd':
            for turn in turns.values():
                if turn.get('sessionId') == session:
                    turn.update(stoppedAt=now, reason='host_session_end', updatedAt=now)
            return
        turn = turns.setdefault(key, {'sessionId': session, 'requestId': request_id})
        turn['updatedAt'] = now
        if record is not None:
            lease = record['lease']
            started = lease.get('expiresAt', now) - lease.get('idleSeconds', 120)
            # Without a host request ID, a new explicit task after an old stop
            # belongs to a later turn. A late receipt from before that stop does not.
            if (not request_id and turn.get('taskId') != record['taskId'] and
                    started > turn.get('stoppedAt', float('inf'))):
                turn.pop('stoppedAt', None); turn.pop('reason', None)
            turn.update(taskId=record['taskId'], ownerId=lease['ownerId'])
        else:
            reason = str(payload.get('final_stop_reason', 'completed'))
            turn.update(stoppedAt=now, reason='host_final_stop_' + (
                reason if reason in ('completed', 'cancelled', 'interrupted', 'error') else 'ended'))


def stop_reason(record, config):
    data = read(Path(config) / 'host-lifecycle/state.json', {})
    for turn in data.get('turns', {}).values():
        if (turn.get('stoppedAt') is not None and turn.get('taskId') == record.get('taskId') and
                turn.get('ownerId') == (record.get('lease') or {}).get('ownerId')):
            return turn.get('reason', 'host_turn_ended')
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=config_directory())
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            # Workbuddy may expose a parent ID in a subagent's hook payload;
            # the contextual environment identifies the actual executing session.
            session = os.environ.get('CODEBUDDY_SESSION_ID')
            if session and ID.fullmatch(session):
                payload = dict(payload, session_id=session)
            handle_hook(payload, args.config_dir, os.environ.get('CODEBUDDY_CONVERSATION_REQUEST_ID'))
    except (OSError, ValueError, TypeError, KeyError):
        # Hooks never block a host's stop or trigger a continuation.
        print('[cua-lifecycle] Hook state unavailable; bounded task lease remains active.', file=sys.stderr)


if __name__ == '__main__':
    main()
