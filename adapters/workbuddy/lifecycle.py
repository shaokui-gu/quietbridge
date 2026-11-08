"""Associate interface phases and bounded protection with the exact host run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

# Source checkout and installed connector use the same adapter package layout.
_base = Path(__file__).resolve().parents[2]
_core = _base if (_base / 'lifecycle_store.py').is_file() else _base / 'services/mcp'
sys.path.insert(0, str(_core))
from lifecycle_store import MAX_BYTES, ID, checked, config_directory, read, store, task_receipt, stop_reason

def handle_hook(payload, config, request_id=None, now=None):
    now = time.time() if now is None else now
    event = payload.get('hook_event_name')
    session = payload.get('session_id')
    if not isinstance(session, str) or not ID.fullmatch(session):
        return
    request_id = request_id or ''
    if not isinstance(request_id, str) or request_id and not ID.fullmatch(request_id):
        return
    if event == 'PreToolUse':
        name = str(payload.get('tool_name', ''))
        if not re.search(r'(?:^|__)(?:quietbridge_|cua_)', name):
            return
        from session_routing import issue
        # Workbuddy versions expose modifiedInput for command hooks and
        # updatedInput for functional hooks. Identical copies support both.
        arguments = issue(config, name.rsplit('__', 1)[-1], payload.get('tool_input') or {},
                          session, request_id, payload.get('tool_use_id') or '')
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'modifiedInput': arguments, 'updatedInput': arguments}}
    record = None
    if event == 'PostToolUse':
        task_call = re.search(r'(?:^|__)(?:quietbridge_task|cua_task)$', str(payload.get('tool_name', '')))
        arguments = payload.get('tool_input') or {}
        if not isinstance(arguments, dict):
            return
        if task_call and isinstance(arguments, dict) and arguments.get('action') in ('begin', 'bind'):
            record = task_receipt(payload.get('tool_response'))
            if record is None:
                return
            from session_registry import session_directory, session_key
            scoped = Path(config) / 'sessions' / session_key(session)
            task_file = scoped / 'task-windows.json' if scoped.exists() else Path(config) / 'task-windows.json'
            tasks = read(task_file, {})
            current = tasks.get('tasks', {}).get(tasks.get('activeTaskId'), {})
            if (current.get('taskId') != record['taskId'] or
                    (current.get('lease') or {}).get('ownerId') != record['lease']['ownerId'] or
                    current.get('state') not in ('awaiting_window', 'bound')):
                return
        else:
            # Other real tools can be another phase of this run. Only update a
            # previously acknowledged run; never start protection from a hook.
            if not checked(Path(config) / 'host-lifecycle').is_dir():
                return
            if re.search(r'(?:^|__)(?:quietbridge_|cua_)(?:permissions|task|browser_events)$',
                         str(payload.get('tool_name', ''))) and arguments.get('action', 'status') in (
                             'status', 'candidates', 'annotations'):
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
            # Preserve a session-wide tombstone even if its first tool receipt
            # is delayed until after SessionEnd.
            end_key = hashlib.sha256((session + '\0\0SessionEnd').encode()).hexdigest()
            turns[end_key] = dict(sessionId=session, requestId=None, sessionEnded=True,
                                  stoppedAt=now, updatedAt=now, reason='host_session_end')
            return
        if event == 'PostToolUse' and record is None:
            turn = turns.get(key)
            if turn and turn.get('taskId') and turn.get('stoppedAt') is None:
                turn.update(lastActivityAt=now, updatedAt=now)
            return
        if record is not None:
            # A post-call receipt may acknowledge ownership, never transfer an
            # already acknowledged phase to a different session or turn.
            for other_key, other in turns.items():
                if other_key != key and ((other.get('tasks') or {}).get(record['taskId']) == record['lease']['ownerId'] or
                        (other.get('taskId') == record['taskId'] and other.get('ownerId') == record['lease']['ownerId'])):
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
                # Keep the old generation stopped for protection anchored to
                # any earlier phase; the new run must not revive it.
                archive_key = hashlib.sha256((key + '\0' + turn['taskId']).encode()).hexdigest()
                turns[archive_key] = dict(turn)
                turn = {'sessionId': session, 'requestId': request_id, 'updatedAt': now}
                turns[key] = turn
            receipts = turn.setdefault('tasks', {})
            if turn.get('taskId') and turn.get('ownerId'):
                receipts.setdefault(turn['taskId'], turn['ownerId'])
            if len(receipts) >= 256 and record['taskId'] not in receipts:
                raise ValueError('too many interface phases in one host run')
            receipts[record['taskId']] = lease['ownerId']
            turn.update(taskId=record['taskId'], ownerId=lease['ownerId'])
            ended = next((item for item in turns.values()
                          if item.get('sessionId') == session and item.get('sessionEnded')), None)
            if ended is not None:
                turn.update(stoppedAt=ended['stoppedAt'], reason='host_session_end')
            if turn.get('stoppedAt') is None:
                turn['lastActivityAt'] = now
        else:
            reason = str(payload.get('final_stop_reason', 'completed'))
            turn.update(stoppedAt=now, reason='host_final_stop_' + (
                reason if reason in ('completed', 'cancelled', 'interrupted', 'error') else 'ended'))


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
            result = handle_hook(payload, args.config_dir, os.environ.get('CODEBUDDY_CONVERSATION_REQUEST_ID'))
            if result is not None:
                print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError):
        # Hooks never block a host's stop or trigger a continuation.
        print('[cua-lifecycle] Hook state unavailable; bounded task lease remains active.', file=sys.stderr)


if __name__ == '__main__':
    main()
