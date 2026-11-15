#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quietbridge 浏览器扩展安装页（仅监听 127.0.0.1）。

为什么要这个页面：Chrome 137 起移除了 --load-extension，扩展无法再被程序
装进用户的日常配置（Chrome for Testing / Chromium 仍支持，但不使用默认配
置，对本人无意义）。所以本页只自动化「能自动的那一半」：

  1. 部署原生消息宿主并登记到 Chrome（完全自动，可重复执行）
  2. 展示扩展目录与固定扩展 ID，供复制与核对
  3. 「加载已解压的扩展程序」必须由本人点，页面只给精确路径与结果自检

安全边界：只绑 127.0.0.1；写操作要求启动时生成的随机令牌；动作固定，
不接受客户端传入的任何路径。
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
EXTENSION = BASE.parent
# 必须早于下面的 import：否则 import install_native_host 会在扩展目录里
# 生成 __pycache__，而 Chrome 拒载任何 "_" 开头的条目。
sys.dont_write_bytecode = True
sys.path.insert(0, str(EXTENSION))

import install_native_host as host  # noqa: E402  单一事实来源：扩展 ID 与宿主计划都来自它

TOKEN = secrets.token_urlsafe(24)
CHROME_APP = Path('/Applications/Google Chrome.app')
CHROME_PROFILES = Path.home() / 'Library/Application Support/Google/Chrome'


def chrome_version():
    binary = CHROME_APP / 'Contents/MacOS/Google Chrome'
    if not binary.is_file():
        return None
    try:
        out = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'(\d+\.\d+\.\d+\.\d+)', out.stdout)
    return match.group(1) if match else None


def chrome_running():
    try:
        out = subprocess.run(['/usr/bin/pgrep', '-x', 'Google Chrome'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.returncode == 0


ORIGINS = ('http://*/*', 'https://*/*')


def extension_state(extension_id):
    """从 Chrome 的配置目录读扩展的真实状态（只读，不写回）。

    按 Chrome 156 实测的存储形态判断，而不是猜：
      · 装了才会有 extensions.settings[id]；location=4 表示「已解压」。
      · state 键不存在即启用；只有停用时才写 state=0，或在 disable_reasons 里给出原因。
      · manifest 不再持久化，版本回退到读扩展目录里的 manifest.json。
      · 用户授予的可选站点权限记在 granted_permissions.explicit_host。

    这不是官方 API，最终判定仍以 chrome://extensions 为准，页面会写明。
    只读 Preferences 不会破坏 Chrome 的签名校验；写入才会。
    """
    results = []
    if not CHROME_PROFILES.is_dir():
        return results
    expected = str(EXTENSION.resolve())
    for entry in sorted(CHROME_PROFILES.iterdir()):
        if not entry.is_dir():
            continue
        record = None
        for name in ('Secure Preferences', 'Preferences'):
            candidate = entry / name
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding='utf-8', errors='replace'))
            except (OSError, ValueError):
                continue
            settings = ((data.get('extensions') or {}).get('settings') or {})
            found = settings.get(extension_id)
            # 卸载后会留下空字典当墓碑；没有 path/location 的一律不算安装，
            # 否则会把「已移除」误报成「已启用」。
            if isinstance(found, dict) and (found.get('path') or found.get('location') is not None):
                record = found
                break
        if record is None:
            continue
        permissions = record.get('granted_permissions') or {}
        hosts = list(permissions.get('explicit_host') or []) + list(permissions.get('scriptable_host') or [])
        hosts += list(permissions.get('origins') or [])  # 旧形态兼容
        granted = all(origin in hosts or '<all_urls>' in hosts for origin in ORIGINS)
        path = record.get('path')
        version = (record.get('manifest') or {}).get('version')
        if version is None and path:
            try:
                version = json.loads((Path(path) / 'manifest.json').read_text(encoding='utf-8')).get('version')
            except (OSError, ValueError):
                version = None
        disabled = record.get('state') == 0 or bool(record.get('disable_reasons'))
        results.append({
            'profile': entry.name,
            'installed': True,
            'enabled': not disabled,
            'state': record.get('state'),
            'disableReasons': record.get('disable_reasons') or [],
            'location': record.get('location'),
            'version': version,
            'path': path,
            'pathMatchesProject': str(path or '') == expected,
            'grantedHosts': hosts,
            'permissionGranted': granted,
        })
    return results


def registration_state(plan):
    registration = Path(plan['manifestPath'])
    if not registration.is_file():
        return {'registered': False, 'reason': '注册文件不存在'}
    try:
        current = json.loads(registration.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return {'registered': False, 'reason': '注册文件无法解析：%s' % error}
    if current == plan['manifest']:
        return {'registered': True, 'reason': '与本扩展计划一致'}
    return {'registered': False, 'reason': '内容不一致（路径或来源与当前扩展不符）'}


def runtime_state(plan):
    launcher = Path(plan['launcherPath'])
    stated = {
        'root': plan['runtimePath'],
        'launcher': str(launcher),
        'launcherReady': launcher.is_file() and os.access(launcher, os.X_OK),
        'hostPath': plan['hostPath'],
        'hostReady': Path(plan['hostPath']).is_file(),
        'pythonPath': plan['pythonPath'],
    }
    stated['ready'] = stated['launcherReady'] and stated['hostReady']
    return stated


def packaging_state():
    """Chrome 会递归扫描扩展目录：凡是以 "_" 开头的文件或目录一律拒载
    （Cannot load extension with file or directory name …）。典型来源是
    Python 的 __pycache__。这里提前查出来，避免用户点"加载已解压"才发现。"""
    reserved = []
    if EXTENSION.is_dir():
        for root, dirnames, filenames in os.walk(EXTENSION):
            for name in list(dirnames) + list(filenames):
                if name.startswith('_'):
                    reserved.append(str(Path(root, name).relative_to(EXTENSION)))
    manifest_ready = (EXTENSION / 'manifest.json').is_file()
    return {
        'manifestReady': manifest_ready,
        'reservedEntries': sorted(reserved),
        'ready': manifest_ready and not reserved,
    }


def status():
    plan = host.plan()
    registration = registration_state(plan)
    runtime = runtime_state(plan)
    packaging = packaging_state()
    installs = extension_state(plan['extensionId'])
    installed = any(item['enabled'] for item in installs) or bool(installs)
    enabled = any(item['enabled'] for item in installs)
    granted = any(item['permissionGranted'] for item in installs)
    blockers = []
    if not EXTENSION.is_dir():
        blockers.append('扩展源码目录不存在')
    elif not packaging['manifestReady']:
        blockers.append('扩展目录缺少 manifest.json')
    elif packaging['reservedEntries']:
        blockers.append('扩展目录含 "_" 前缀条目，Chrome 会整包拒载：%s'
                        % '、'.join(packaging['reservedEntries']))
    if not registration['registered']:
        blockers.append('原生宿主尚未登记：%s' % registration['reason'])
    if not runtime['ready']:
        blockers.append('原生宿主运行态未部署')
    if not installs:
        blockers.append('扩展尚未在 Chrome 中加载')
    elif not enabled:
        blockers.append('扩展已安装但被停用，请在扩展页启用')
    elif not granted:
        blockers.append('尚未授予普通 HTTP/HTTPS 站点权限')
    return {
        'extensionId': plan['extensionId'],
        'extensionDir': str(EXTENSION),
        'extensionPopup': 'chrome-extension://%s/popup.html' % plan['extensionId'],
        'manifestPath': str(EXTENSION / 'manifest.json'),
        'chromeVersion': chrome_version(),
        'chromeRunning': chrome_running(),
        'host': {'registrationPath': plan['manifestPath'], **registration},
        'runtime': runtime,
        'packaging': packaging,
        'extension': {'installed': installed, 'enabled': enabled,
                      'permissionGranted': granted, 'profiles': installs},
        'blockers': blockers,
        'ready': not blockers,
        'installed': installed and enabled,
        'authorized': granted,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'quietbridge-installer'

    def log_message(self, fmt, *args):  # 安静：只在启动时打印一次地址
        pass

    def _send(self, code, body, content_type):
        payload = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False), 'application/json; charset=utf-8')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            page = (BASE / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)
            return self._send(200, page, 'text/html; charset=utf-8')
        if path == '/api/status':
            try:
                return self._json(200, status())
            except (ValueError, OSError) as error:
                return self._json(500, {'error': str(error)})
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != '/api/install-host':
            return self._json(404, {'error': 'not found'})
        if (self.headers.get('X-QuietBridge-Token') or self.headers.get('X-Cua-Token')) != TOKEN:
            return self._json(403, {'error': '令牌不匹配'})
        try:
            plan = host.plan()
            outcome = host.install(plan, update=True)
            return self._json(200, {'outcome': outcome, 'status': status()})
        except (ValueError, OSError) as error:
            return self._json(500, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description='本机扩展安装页；只绑定 127.0.0.1')
    parser.add_argument('--port', type=int, default=0, help='默认随机端口')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()
    try:
        plan = host.plan()
    except (ValueError, OSError) as error:
        print('无法读取扩展清单：%s' % error, file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = 'http://127.0.0.1:%d/' % server.server_port
    print('安装页: %s' % url)
    print('扩展 ID: %s' % plan['extensionId'])
    print('扩展目录: %s' % EXTENSION)
    print('按 Ctrl+C 结束')
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已结束')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
