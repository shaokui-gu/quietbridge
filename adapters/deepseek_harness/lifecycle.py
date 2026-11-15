"""Associate DeepSeek Harness turns, agent disposal and task receipts with the exact host run.

The bundle passes one event per invocation on stdin. Only identifiers and
timestamps are retained: tool arguments, tool results and transcript content
are inspected for the task lease receipt and then discarded, never stored.
A failing hint never blocks the host's stop or triggers a continuation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

# Source checkout and installed connector use the same adapter package layout.
_base = Path(__file__).resolve().parents[2]
_core = _base if (_base / 'lifecycle_store.py').is_file() else _base / 'services/mcp'
sys.path.insert(0, str(_core))
from lifecycle_store import (MAX_BYTES, ID, config_directory, host_binding, read, store,
                             task_receipt)

HOST = 'deepseek-harness'
STOP_REASONS = ('completed', 'cancelled', 'interrupted', 'error')
SESSION_END_REASON = 'host_session_end'
TASK_TOOL = 'quietbridge_task'
TASK_ACTIONS = ('begin', 'bind')
RECEIPT_LIMIT = 256


def _digest(*parts):
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()


def turn_key(session, agent, turn):
    """Key one host turn exactly; the host string keeps other hosts distinct."""
    return _digest(HOST, session, agent, turn)


def session_key(session, agent):
    # The empty extra segment cannot be produced by a valid turn id, so this
    # tombstone key never collides with a real turn key.
    return _digest(HOST, session, agent, '', 'session-end')


def _valid(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def _tombstone(turns, session, agent):
    fallback = None
    for turn in turns.values():
        if (turn.get('sessionId') != session or turn.get('agentId') != agent or
                not turn.get('sessionEnded')):
            continue
        # Prefer the session-wide marker over an individual stopped turn.
        if turn.get('requestId') is None and turn.get('stoppedAt') is not None:
            return turn
        if fallback is None:
            fallback = turn
    return fallback


def normalized_reason(reason):
    return reason if reason in STOP_REASONS else 'ended'


def _start(config, session, agent, turn, now):
    key = turn_key(session, agent, turn)
    with store(config, now) as data:
        turns = data['turns']
        existing = turns.get(key)
        if existing is not None:
            if existing.get('stoppedAt') is not None:
                # A late start for an already stopped turn must not revive it.
                return
            existing['updatedAt'] = now
            return
        tombstone = _tombstone(turns, session, agent)
        record = {'host': HOST, 'sessionId': session, 'agentId': agent,
                  'requestId': turn, 'startedAt': now, 'updatedAt': now,
                  'sessionEnded': False, 'tasks': {}}
        if tombstone is not None:
            # The agent was disposed before this event arrived; keep the turn
            # stopped so routing rejects it instead of guessing a live run.
            record.update(stoppedAt=tombstone.get('stoppedAt', now),
                          reason=SESSION_END_REASON, sessionEnded=True)
        turns[key] = record


def _end(config, session, agent, turn, reason, now):
    key = turn_key(session, agent, turn)
    with store(config, now) as data:
        turns = data['turns']
        existing = turns.get(key)
        if existing is None:
            record = {'host': HOST, 'sessionId': session, 'agentId': agent,
                      'requestId': turn, 'startedAt': now, 'updatedAt': now,
                      'stoppedAt': now, 'reason': normalized_reason(reason),
                      'sessionEnded': False, 'tasks': {}}
            tombstone = _tombstone(turns, session, agent)
            if tombstone is not None:
                record.update(sessionEnded=True, reason=SESSION_END_REASON)
            turns[key] = record
            return
        if existing.get('stoppedAt') is not None:
            # A late end for an old turn must not reorder the store ahead of a
            # newer live turn, and cannot erase the first stop cause.
            return
        existing.update(stoppedAt=now, reason=normalized_reason(reason), updatedAt=now)


def _dispose(config, session, agent, now):
    with store(config, now) as data:
        turns = data['turns']
        for turn in turns.values():
            if turn.get('sessionId') != session or turn.get('agentId') != agent:
                continue
            if turn.get('stoppedAt') is None:
                turn.update(stoppedAt=now, reason=SESSION_END_REASON)
            turn.update(sessionEnded=True, updatedAt=now)
        key = session_key(session, agent)
        existing = turns.get(key)
        if existing is None:
            turns[key] = {'host': HOST, 'sessionId': session, 'agentId': agent,
                          'requestId': None, 'sessionEnded': True, 'stoppedAt': now,
                          'updatedAt': now, 'reason': SESSION_END_REASON}
        else:
            # Preserve the first stop cause; the tombstone only extends it.
            existing.setdefault('stoppedAt', now)
            existing.setdefault('reason', SESSION_END_REASON)
            existing.update(host=HOST, sessionId=session, agentId=agent,
                            requestId=None, sessionEnded=True, updatedAt=now)


def binding(config, record):
    """Expose the host turn that acknowledged this exact task/connector pair."""
    return host_binding(record, config)


def tombstone(config, session, agent):
    """Expose the disposal tombstone for a session/agent pair, if recorded."""
    data = read(Path(config) / 'host-lifecycle/state.json', {})
    if not isinstance(data, dict):
        return None
    return _tombstone(data.get('turns', {}), session, agent)


def _tool_result(payload, config, session, agent, now):
    name = payload.get('toolName')
    if not isinstance(name, str) or not name:
        return
    # Hosts expose MCP tools as mcp__<serverName>__<rawName>; also accept raw names.
    # Only the task tool's begin/bind carries a lease receipt worth storing.
    base = name.rsplit('__', 1)[-1]
    if base != TASK_TOOL:
        return
    turn = payload.get('turnId')
    if not _valid(turn):
        return
    arguments = payload.get('toolInput')
    # Session events may hand tool arguments over as the raw JSON document while
    # other paths pass an object; normalize before the begin/bind check so a
    # valid task receipt is never dropped on shape alone.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (UnicodeDecodeError, ValueError):
            return
    if not isinstance(arguments, dict) or arguments.get('action') not in TASK_ACTIONS:
        return
    record = task_receipt(payload.get('toolResult'))
    if record is None:
        return
    key = turn_key(session, agent, turn)
    owner = record['lease']['ownerId']
    with store(config, now) as data:
        turns = data['turns']
        # A post-call receipt may acknowledge ownership, never transfer an
        # already acknowledged phase to a different session, agent or turn.
        for other_key, other in turns.items():
            if other_key == key:
                continue
            if ((other.get('tasks') or {}).get(record['taskId']) == owner or
                    (other.get('taskId') == record['taskId'] and other.get('ownerId') == owner)):
                return
        current = turns.get(key)
        if current is None:
            current = turns[key] = {'host': HOST, 'sessionId': session, 'agentId': agent,
                                    'requestId': turn, 'startedAt': now,
                                    'updatedAt': now, 'sessionEnded': False, 'tasks': {}}
        was_stopped = current.get('stoppedAt') is not None
        receipts = current.setdefault('tasks', {})
        if len(receipts) >= RECEIPT_LIMIT and record['taskId'] not in receipts:
            raise ValueError('too many interface phases in one host run')
        if current.get('taskId') and current.get('ownerId'):
            receipts.setdefault(current['taskId'], current['ownerId'])
        receipts[record['taskId']] = owner
        current.update(taskId=record['taskId'], ownerId=owner)
        if not was_stopped:
            current['updatedAt'] = now
        ended = _tombstone(turns, session, agent)
        if ended is not None and current is not ended:
            if current.get('stoppedAt') is None:
                current.update(stoppedAt=ended.get('stoppedAt', now),
                               reason=SESSION_END_REASON, updatedAt=now)
            current['sessionEnded'] = True
        if current.get('stoppedAt') is None:
            current['lastActivityAt'] = now


def _agent_created(config, session, agent, now):
    """Retire the previous Host run's end state for a resumed session.

    DSH restarts and profile reloads resume the same session/agent id in a new
    Host process. The old process's ``agent-disposed`` tombstone must not mark
    the resumed Agent as already ended: Agent creation precedes every turn of
    the new run, so any end marker for this exact pair belongs to an earlier
    run and is stale. The first turn of the new run then establishes presence.
    """
    state = read(Path(config) / 'host-lifecycle/state.json', {})
    turns = state.get('turns') if isinstance(state, dict) else None
    if not isinstance(turns, dict):
        return

    def matches(turn):
        return turn.get('sessionId') == session and turn.get('agentId') == agent

    stale = [turn for turn in turns.values() if matches(turn) and
             (turn.get('requestId') is None or turn.get('sessionEnded'))]
    if not stale:
        return
    with store(config, now) as data:
        current = data['turns']
        for key, turn in list(current.items()):
            if not matches(turn):
                continue
            if turn.get('requestId') is None:
                del current[key]
                continue
            if turn.get('sessionEnded'):
                turn['sessionEnded'] = False
                turn['updatedAt'] = now


def handle_event(payload, config, now=None):
    """Apply one host lifecycle event; unknown or malformed hints are ignored."""
    if not isinstance(payload, dict):
        return
    now = time.time() if now is None else now
    event = payload.get('event')
    session = payload.get('sessionId')
    agent = payload.get('agentId')
    if not _valid(session) or not _valid(agent):
        return
    if event == 'turn-start':
        turn = payload.get('turnId')
        if _valid(turn):
            _start(config, session, agent, turn, now)
    elif event == 'turn-end':
        turn = payload.get('turnId')
        if _valid(turn):
            _end(config, session, agent, turn, payload.get('reason'), now)
    elif event == 'agent-created':
        # Creation does not grant a turn, but it does prove the pair belongs to
        # a new Host run; stale end markers from the previous run are retired.
        _agent_created(config, session, agent, now)
    elif event == 'agent-disposed':
        _dispose(config, session, agent, now)
    elif event == 'tool-result':
        _tool_result(payload, config, session, agent, now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=config_directory())
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    try:
        handle_event(payload, args.config_dir)
    except (OSError, ValueError, TypeError, KeyError):
        # Host lifecycle hints never block a stop or trigger a continuation.
        print('[quietbridge-dsh-lifecycle] host lifecycle state unavailable; event ignored.',
              file=sys.stderr)


if __name__ == '__main__':
    main()
