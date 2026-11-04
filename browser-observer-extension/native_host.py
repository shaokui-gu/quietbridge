#!/usr/bin/env python3
"""原生端口多请求 framing；标准输出只承载协议，不操作浏览器。"""
import json
from pathlib import Path
import struct
import sys

BRIDGE = Path(__file__).resolve().parent.parent / 'cua-mcp-bridge'
if not (BRIDGE / 'browser_events.py').is_file():
    print('Missing companion component: ../cua-mcp-bridge/browser_events.py; '
          'keep it beside browser-observer-extension.', file=sys.stderr)
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
        except (ValueError, OSError, UnicodeError, TypeError, KeyError):
            result = {'ok': False, 'error': '请求被拒绝：请检查任务分配、权限、数据格式及本地文件状态'}
        write_message(target, result)


def main():
    try:
        config = read_file(Path(__file__).with_name('native-host-config.json'))
    except (ValueError, OSError):
        config = None
    serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[1] if len(sys.argv) > 1 else None, config)


if __name__ == '__main__':
    main()
