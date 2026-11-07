#!/usr/bin/env python3
"""Stable stdio endpoint; switch bridge releases only between complete requests.

No request is replayed. Each bridge generation owns its handles/helper client.
The immutable signed release stays intact. This entrypoint never drives UI.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time


def read_owned(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('connector path contains symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('connector file ownership/mode mismatch')
    return path.read_bytes()


def write_status(path, value):
    temporary = path.with_name('.status-' + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Connector:
    def __init__(self, manifest, send):
        self.manifest = Path(manifest)
        self.send = send
        self.bridge = None
        self.generation = None
        self.initialized = False
        self.revalidate = False
        self.last_failure = None

    def status(self, **fields):
        write_status(self.manifest.with_name('status.json'), dict(
            pid=os.getpid(), generation=self.generation, updatedAt=time.time(), **fields))

    def refresh(self):
        try:
            value = json.loads(read_owned(self.manifest))
            if value['generation'] == self.generation:
                if self.last_failure:
                    self.last_failure = None
                    self.status(state='ready', version=value.get('version'))
                return
            path = Path(value['bridge'])
            releases = self.manifest.parent.parent / 'releases'
            if path.name != 'server.py' or releases not in path.parents:
                raise ValueError('bridge is outside installed releases')
            read_owned(path)
            previous_env = os.environ.get('CUA_HELPER_PATH')
            previous_modules = {name: sys.modules.pop(name, None) for name in ('browser_events', 'recovery', 'task_lifecycle', 'host_lifecycle')}
            sys.path.insert(0, str(path.parent))
            try:
                os.environ['CUA_HELPER_PATH'] = value['helper']
                spec = importlib.util.spec_from_file_location('cua_active_bridge', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # Validate the protocol without launching a helper or any UI.
                module.handle({'id': 'upgrade-check', 'method': 'initialize', 'params': {}})
            except BaseException:
                for name, old in previous_modules.items():
                    sys.modules.pop(name, None)
                    if old is not None:
                        sys.modules[name] = old
                if previous_env is None:
                    os.environ.pop('CUA_HELPER_PATH', None)
                else:
                    os.environ['CUA_HELPER_PATH'] = previous_env
                raise
            finally:
                sys.path.pop(0)
            old = self.bridge
            self.bridge, self.generation = module, value['generation']
            self.last_failure = None
            if old:
                old.shutdown('connector_updated') if hasattr(old, 'shutdown') else old.HELPER._stop()
            if hasattr(module, 'start_task_watch'):
                module.start_task_watch()
            if self.initialized:
                self.revalidate = True
                self.send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
            self.status(state='ready', version=value.get('version'))
        except (Exception, SystemExit) as error:
            # A rejected/mid-install manifest cannot take down a healthy connection.
            failure = type(error).__name__ + ': ' + str(error)
            if failure != self.last_failure:
                print('[cua-connector] release refresh failed: ' + failure, file=sys.stderr)
                self.last_failure = failure
                self.status(state='update_failed', message='新版加载失败，保留当前连接')
            if self.bridge is None:
                raise

    def handle(self, message, cancel_event=None):
        method = message.get('method')
        if method == 'tools/call' and self.revalidate:
            self.revalidate = False
            text = ('CONNECTOR_UPDATED: 连接器已自动切换到新版，本次调用未执行。'
                    '请先只读重新核实权限、原任务/窗口/标签并显式确认绑定，恢复共享窗口；不能重放旧输入。')
            return {'jsonrpc': '2.0', 'id': message.get('id'), 'result': {
                'isError': True, 'content': [{'type': 'text', 'text': text}],
                'structuredContent': {'ok': False, 'errorCode': 'CONNECTOR_UPDATED',
                    'execution': {'status': 'not_dispatched'}, 'recovery': {
                        'status': 'revalidate_required', 'allowAlternativeTools': False,
                        'replaySideEffects': False, 'resumeAfter': 'revalidate', 'message': text}}}}
        reply = (self.bridge.handle(message, cancel_event=cancel_event) if hasattr(self.bridge, 'shutdown') else
                 self.bridge.handle(message))
        if method == 'initialize' and reply:
            reply['result']['capabilities']['tools']['listChanged'] = True
            self.initialized = True
        return reply

    def close(self):
        if self.bridge:
            self.bridge.shutdown() if hasattr(self.bridge, 'shutdown') else self.bridge.HELPER._stop()


def main():
    def send(message):
        print(json.dumps(message, ensure_ascii=False), flush=True)

    from stdio_transport import serve
    connector = Connector(Path(__file__).with_name('active.json'), send)
    serve(connector.handle, send, on_idle=connector.refresh, on_close=connector.close)


if __name__ == '__main__':
    main()
