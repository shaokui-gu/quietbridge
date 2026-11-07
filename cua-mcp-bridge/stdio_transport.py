"""Read cancellation/EOF independently of serial tool execution; never replay."""
import json
import queue
import sys
import threading


def serve(dispatch, send, on_idle=lambda: None, on_close=lambda: None):
    inbox = queue.Queue(maxsize=64)
    tokens = {}
    lock = threading.Lock()
    disconnected = threading.Event()

    def key(value):
        # JSON-RPC IDs are typed: true must never cancel request 1.
        return (type(value), value) if type(value) in (str, int) else None

    def receive():
        def enqueue(item):
            inbox.put_nowait(item)
        try:
            for line in iter(lambda: sys.stdin.buffer.readline(1024 * 1024 + 1), b''):
                if len(line) > 1024 * 1024:
                    break
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except (ValueError, UnicodeError):
                    enqueue((None, None, 'parse'))
                    continue
                for message in parsed if isinstance(parsed, list) else [parsed]:
                    if not isinstance(message, dict):
                        enqueue((None, None, 'invalid'))
                        continue
                    if message.get('method') == 'notifications/cancelled':
                        params = message.get('params')
                        identifier = key(params.get('requestId')) if isinstance(params, dict) else None
                        with lock:
                            token = tokens.get(identifier)
                            if token is not None:
                                token.set()
                        continue
                    if not isinstance(message.get('method'), str) or ('params' in message and not isinstance(message['params'], dict)):
                        enqueue((None, None, 'invalid'))
                        continue
                    token = threading.Event()
                    identifier = key(message.get('id'))
                    if message.get('method') == 'tools/call' and identifier is None:
                        enqueue((None, None, 'invalid'))
                        continue
                    if message.get('method') == 'tools/call' and identifier is not None:
                        with lock:
                            if identifier in tokens:
                                enqueue((None, None, 'duplicate'))
                                continue
                            tokens[identifier] = token
                    enqueue((message, token, None))
        except (queue.Full, OSError):
            # A host flooding the serial queue cannot delay cancellation forever.
            # Disconnect and revoke rather than accepting unbounded pending input.
            pass
        finally:
            disconnected.set()
            with lock:
                for token in tokens.values():
                    token.set()
            # The main loop also checks disconnected when a saturated queue drains.
            try:
                inbox.put_nowait((None, None, 'eof'))
            except queue.Full:
                pass

    threading.Thread(target=receive, daemon=True).start()
    try:
        while True:
            on_idle()
            try:
                message, token, error = inbox.get(timeout=0.5)
            except queue.Empty:
                if disconnected.is_set():
                    break
                continue
            if error == 'eof':
                break
            if error:
                send({'jsonrpc': '2.0', 'id': None, 'error': {
                    'code': -32700 if error == 'parse' else -32600,
                    'message': 'Parse error' if error == 'parse' else 'Invalid Request'}})
                continue
            # Calls queued before EOF cannot start input after the host left.
            if disconnected.is_set() and message.get('method') == 'tools/call':
                continue
            try:
                reply = dispatch(message, token)
                if reply is not None:
                    send(reply)
            finally:
                identifier = key(message.get('id'))
                with lock:
                    if tokens.get(identifier) is token:
                        tokens.pop(identifier)
    finally:
        on_close()
