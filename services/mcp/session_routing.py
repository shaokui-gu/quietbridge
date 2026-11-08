"""Host-issued one-use call envelopes; one private bridge process per session.

Nothing is inferred from the foreground app or a previous hook. Unknown,
expired, altered and replayed calls fail before creating any native connection.
"""
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time

from browser_events import event_store
from lifecycle_store import ID, checked, read
from session_registry import session_key, session_directory

ROUTE_FIELD = '_quietbridgeRoute'
TOKEN_TTL = 90


def digest(name, arguments):
    return hashlib.sha256(json.dumps([name, arguments], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def issue(config, name, arguments, session, request='', call=''):
    if not all(isinstance(v, str) and (not v or ID.fullmatch(v)) for v in (session, request, call)) or not session:
        raise ValueError('SESSION_ROUTING_REQUIRED: host session context missing')
    if not isinstance(arguments, dict):
        raise ValueError('invalid tool arguments')
    arguments = {k: v for k, v in arguments.items() if k != ROUTE_FIELD}
    parent = checked(Path(config) / 'host-routing')
    parent.mkdir(mode=0o700, exist_ok=True)
    token = secrets.token_hex(32)
    now = time.time()
    with event_store(parent / 'calls.json') as data:
        data['tasks'] = {k: v for k, v in data['tasks'].items() if v.get('expiresAt', 0) > now}
        if len(data['tasks']) >= 128:
            raise ValueError('SESSION_ROUTING_BUSY: too many pending calls')
        data['tasks'][token] = {'sessionId': session, 'requestId': request,
            'callId': call, 'digest': digest(name, arguments), 'expiresAt': now + TOKEN_TTL}
    return dict(arguments, **{ROUTE_FIELD: token})


def consume(config, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('SESSION_ROUTING_REQUIRED: 缺少宿主调用上下文，请更新连接器及 hooks')
    clean = dict(arguments)
    token = clean.pop(ROUTE_FIELD, None)
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError('SESSION_ROUTING_REQUIRED: 缺少可信会话凭据；本次调用未执行，请重新加载 QuietBridge 连接器')
    with event_store(checked(Path(config) / 'host-routing/calls.json')) as data:
        record = data['tasks'].pop(token, None)
    if not record or record.get('expiresAt', 0) <= time.time() or record.get('digest') != digest(name, clean):
        raise ValueError('SESSION_ROUTING_INVALID: 会话凭据过期、已使用或参数已改变；本次调用未执行')
    turns = read(Path(config) / 'host-lifecycle/state.json', {}).get('turns', {})
    for turn in turns.values():
        if turn.get('sessionId') == record['sessionId'] and turn.get('stoppedAt') is not None and (
                turn.get('sessionEnded') or turn.get('requestId') == record['requestId']):
            raise ValueError('TASK_CANCELLED: 宿主会话已经停止；本次调用未执行')
    return clean, record


def failure(message):
    code = str(message).split(':', 1)[0]
    return {'isError': True, 'content': [{'type': 'text', 'text': str(message)}],
        'structuredContent': {'ok': False, 'errorCode': code, 'execution': {'status': 'not_dispatched'},
            'recovery': {'waitForUser': True, 'allowAlternativeTools': False, 'replaySideEffects': False,
                         'message': '通知用户处理 QuietBridge 会话或连接器状态；不要换通道绕过。'}}}


class SessionProcess:
    def __init__(self, bridge, config, key):
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self.retained = False
        self.responses = queue.Queue(maxsize=2)
        session_directory(config, key, create=True)
        env = dict(os.environ, QUIETBRIDGE_SESSION_KEY=key, QUIETBRIDGE_ROUTING='worker')
        self.process = subprocess.Popen([sys.executable, '-B', str(bridge), '--session-worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, env=env, close_fds=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(34 * 1024 * 1024), b''):
                if not line.endswith(b'\n'):
                    break
                self.responses.put(json.loads(line), timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                self.responses.put_nowait(None)
            except queue.Full:
                pass

    def call(self, message, context, cancelled):
        # Never queue a second action behind a long-running operation from the
        # same session; the host can observe its existing result first.
        if not self.lock.acquire(blocking=False):
            return failure('SESSION_BUSY: 本会话已有工具调用进行中；请串行调用并核实既有结果')
        try:
            self.context = context
            request = dict(message, params=dict(message.get('params', {}), _quietbridgeContext=context))
            if cancelled is not None and cancelled.is_set():
                return failure('TASK_CANCELLED: 调用已取消')
            self._send(request)
            deadline = time.monotonic() + 70
            sent_cancel = False
            while time.monotonic() < deadline:
                if cancelled is not None and cancelled.is_set() and not sent_cancel:
                    self._send({'method': 'notifications/cancelled', 'params': {'requestId': request['id']}})
                    sent_cancel = True
                try:
                    response = self.responses.get(timeout=0.1)
                except queue.Empty:
                    continue
                if response is None:
                    return failure('SESSION_WORKER_EXITED: 本会话执行进程已退出；其他会话继续运行，先核实结果再建立新阶段')
                if response.get('id') != request.get('id'):
                    self.close()
                    return failure('SESSION_PROTOCOL_ERROR: 执行进程回执不匹配')
                self.retained = response.get('_quietbridgeRetained', True) is True
                return response['result']
            self.close()
            return failure('SESSION_TIMEOUT: 本会话超时并已回收；执行结果未知，请先观察，不能重放输入')
        except (OSError, ValueError, KeyError):
            self.close()
            return failure('SESSION_WORKER_EXITED: 本会话执行进程不可用；请先核实既有结果')
        finally:
            self.last_used = time.monotonic()
            self.lock.release()

    def _send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
        self.process.stdin.flush()

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process.stdout.close()


class SessionRouter:
    def __init__(self, bridge, config):
        self.bridge, self.config = bridge, Path(config)
        self.workers = {}
        self.lock = threading.Lock()

    def call(self, message, cancelled=None):
        try:
            params = message.get('params') or {}
            clean, context = consume(self.config, params.get('name'), params.get('arguments', {}))
            key = session_key(context['sessionId'])
            with self.lock:
                worker = self.workers.get(key)
                if worker and worker.process.poll() is not None:
                    worker.close()
                    self.workers.pop(key)
                    return failure('SESSION_WORKER_EXITED: 本会话执行进程异常退出；本次未执行，请先核实既有结果')
                if worker is None:
                    if len(self.workers) >= 3:
                        return failure('SESSION_CAPACITY_FULL: 最多同时运行 3 个会话；不会接管其他会话')
                    worker = self.workers[key] = SessionProcess(self.bridge, self.config, key)
            request = dict(message, params=dict(params, arguments=clean))
            return worker.call(request, context, cancelled)
        except (OSError, ValueError, TypeError) as error:
            return failure(str(error) if isinstance(error, ValueError) else
                           'SESSION_ROUTING_REQUIRED: 可信调用上下文不可用，请更新 QuietBridge hooks')

    def close(self):
        with self.lock:
            workers, self.workers = self.workers, {}
        for worker in workers.values():
            worker.close()

    def reap(self):
        """Stops remove only the corresponding worker, including idle phases."""
        turns = read(self.config / 'host-lifecycle/state.json', {}).get('turns', {})
        closing = []
        with self.lock:
            for key, worker in list(self.workers.items()):
                context = getattr(worker, 'context', {})
                stopped = any(t.get('sessionId') == context.get('sessionId') and t.get('stoppedAt') is not None and
                    (t.get('sessionEnded') or t.get('requestId') == context.get('requestId')) for t in turns.values())
                idle_without_resources = not worker.retained and time.monotonic() - worker.last_used >= 120
                if (stopped or idle_without_resources) and not worker.lock.locked():
                    closing.append(self.workers.pop(key))
        for worker in closing:
            worker.close()
