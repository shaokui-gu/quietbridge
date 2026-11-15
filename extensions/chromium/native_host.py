#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生端口多请求 framing；标准输出只承载协议，不操作浏览器。"""
import json
from pathlib import Path
import re
import struct
import sys

# 扩展目录内不得出现 __pycache__：Chrome 会递归扫描，凡是 "_" 开头的
# 文件/目录一律拒载（Cannot load extension with file or directory name）。
sys.dont_write_bytecode = True

_base = Path(__file__).resolve().parent
BRIDGE = next((candidate for candidate in (
    _base.parents[1] / 'services/mcp', _base.parent / 'cua-mcp-bridge', _base,
) if (candidate / 'browser_events.py').is_file()), None)
if BRIDGE is None:
    print('Missing QuietBridge MCP companion; keep services/mcp in the project or installed runtime.', file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(BRIDGE))
from browser_events import MAX_BYTES, dispatch, read_file, require


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        piece = stream.read(size - len(chunks))
        require(bool(piece), '请求帧被截断')
        chunks.extend(piece)
    return bytes(chunks)


def read_message(stream):
    length = struct.unpack('<I', read_exact(stream, 4))[0]
    require(0 < length <= MAX_BYTES, '请求帧长度超出限制')
    return json.loads(read_exact(stream, length).decode('utf-8'))


def write_message(stream, message):
    raw = json.dumps(message, ensure_ascii=False).encode('utf-8')
    require(len(raw) <= MAX_BYTES, '响应超出限制')
    stream.write(struct.pack('<I', len(raw)) + raw)
    stream.flush()


def serve(source, target, origin, config, **paths):
    try:
        origins = config.get('allowed_origins') if type(config) is dict else None
        require(type(origins) is list and len(origins) == 1, 'host 配置未指定唯一扩展 origin')
        # 先检查 origin，再解析请求或接触任务存储。
        require(type(origin) is str and origin in origins, '扩展 origin 不在允许列表')
    except (ValueError, OSError, UnicodeError, TypeError, KeyError):
        write_message(target, {'ok': False, 'error': '扩展 origin 不在允许列表'})
        return
    while True:
        first = source.read(1)
        if not first:
            return
        try:
            header = first + read_exact(source, 3)
            length = struct.unpack('<I', header)[0]
            require(0 < length <= MAX_BYTES, '请求帧长度超出限制')
            message = json.loads(read_exact(source, length).decode('utf-8'))
        except (ValueError, OSError, UnicodeError, TypeError):
            write_message(target, {'ok': False, 'error': '请求帧无效或被截断'})
            return
        try:
            result = dispatch(message, origin, origins, **paths)
        except (ValueError, OSError, UnicodeError, TypeError, KeyError) as error:
            code = 'BROWSER_STORAGE_UNAVAILABLE' if isinstance(error, OSError) else 'BROWSER_REQUEST_REJECTED'
            # Never echo arbitrary exception text: it may contain page content,
            # URLs or local paths. These fields are sufficient to correlate a
            # transport-successful rejection without misreporting a host crash.
            detail = {'event': 'browser_native_rejected', 'code': code, 'errorType': type(error).__name__}
            if type(message) is dict:
                if message.get('action') in ('sync', 'event', 'ping', 'pause', 'unbind', 'status', 'channels'):
                    detail['action'] = message['action']
                reply = message.get('commandResult')
                if type(reply) is dict and type(reply.get('requestId')) is str and re.fullmatch('[a-f0-9]{32}', reply['requestId']):
                    detail['commandId'] = reply['requestId']
            print(json.dumps(detail), file=sys.stderr)
            result = {'ok': False, 'code': code,
                      'error': '本地存储暂不可用，请通知用户检查应用状态' if code == 'BROWSER_STORAGE_UNAVAILABLE' else
                               '本地请求被拒绝，未接受该证据；请检查任务分配、权限及协议版本'}
        write_message(target, result)


def main():
    try:
        config = read_file(Path(__file__).with_name('native-host-config.json'))
    except (ValueError, OSError):
        config = None
    serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[1] if len(sys.argv) > 1 else None, config)


if __name__ == '__main__':
    main()
