"""把原生 host 部署到用户目录并注册；默认只打印计划，--install 才写文件。

两个关键约束：
1) 扩展 ID 由 manifest.json 的 key 派生，不填空、不猜；目录移动、复制重载都不变。
2) 运行态必须放在非 TCC 目录。Chrome 自己能读 ~/Documents（解压扩展就从那里加载），
   但它派生的 sh/python3 子进程会被 macOS 隐私权限拒绝（Operation not permitted），
   所以 host、bridge 副本与任务记录统一部署到
   ~/Library/Application Support/WorkbuddyExtension，项目目录只留源码与扩展前端。
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys

# 见 native_host.py：被 serve.py 导入时不得把 __pycache__ 落到扩展目录。
sys.dont_write_bytecode = True

BRIDGE = Path(__file__).resolve().parent
if not (BRIDGE / 'browser_events.py').is_file():
    BRIDGE = BRIDGE.parent / 'cua-mcp-bridge'
if not (BRIDGE / 'browser_events.py').is_file():
    print('Missing companion component: ../cua-mcp-bridge/browser_events.py; '
          'keep it beside browser-observer-extension.', file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(BRIDGE))
from browser_events import atomic_write, directory, read_file, require

NAME = 'com.cua.browser_observer'
BASE = Path(__file__).resolve().parent
MANIFEST = BASE / 'manifest.json'
RUNTIME_NAME = 'WorkbuddyExtension'
STATE_FILES = ('task-windows.json', 'browser-events.json')

# 启动器会被 Chrome 长期直接执行，必须指向系统级解释器：不能用当前进程的
# sys.executable（可能是 IDE/工具自带的临时 Python），否则工具一升级就失效。
INTERPRETERS = ('/usr/bin/python3', '/opt/homebrew/bin/python3', '/usr/local/bin/python3')


def pick_interpreter():
    for path in INTERPRETERS:
        if os.access(path, os.X_OK):
            return Path(path)
    require(os.access(sys.executable, os.X_OK), '找不到可执行的 python3')
    return Path(sys.executable)


def extension_id_from_key(key):
    # Chrome 规则：SPKI DER 的 SHA-256 前 16 字节，十六进制 0-f 映射到 a-p。
    require(type(key) is str and key, 'manifest.json 缺少 key，无法派生稳定扩展 ID')
    try:
        der = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        raise ValueError('manifest.json 的 key 不是合法 base64')
    require(len(der) > 0, 'manifest.json 的 key 为空')
    digest = hashlib.sha256(der).hexdigest()[:32]
    return ''.join(chr(ord('a') + int(ch, 16)) for ch in digest)


def extension_id_from_manifest(manifest_path=MANIFEST):
    data = read_file(manifest_path)
    require(type(data) is dict, '无法读取扩展 manifest.json：' + str(manifest_path))
    return extension_id_from_key(data.get('key'))


def runtime_root(home=None):
    return (Path(home) if home else Path.home()) / 'Library/Application Support' / RUNTIME_NAME


def plan(extension_id=None, home=None):
    derived = extension_id_from_manifest()
    extension_id = derived if extension_id is None else extension_id
    require(type(extension_id) is str and re.fullmatch('[a-p]{32}', extension_id),
            '扩展 ID 必须为 32 位 a-p')
    require(extension_id == derived,
            '扩展 ID 与 manifest key 派生的 %s 不一致；请删除 --extension-id 使用派生值' % derived)
    origin = 'chrome-extension://' + extension_id + '/'
    home = Path(home) if home else Path.home()
    root = runtime_root(home)
    host_dir = root / 'browser-observer-extension'
    launcher = host_dir / 'native-host-launcher.sh'
    return {
        'extensionId': extension_id,
        'pythonPath': str(pick_interpreter()),
        'runtimePath': str(root),
        'hostPath': str(host_dir / 'native_host.py'),
        'bridgePath': str(root / 'cua-mcp-bridge' / 'browser_events.py'),
        'configPath': str(host_dir / 'native-host-config.json'),
        'statePath': str(root / 'cua-helper-config'),
        'launcherPath': str(launcher),
        'manifestPath': str(home / 'Library/Application Support/Google/Chrome'
                            '/NativeMessagingHosts' / (NAME + '.json')),
        'manifest': {'name': NAME, 'description': '任务浏览器只读观察器',
                     'path': str(launcher), 'type': 'stdio', 'allowed_origins': [origin]}}
        # 注册表必须写绝对路径：Chrome 不解析相对路径，也不跟随扩展目录。


def write_json(path, data):
    path = Path(path)
    with directory(path.parent) as fd:
        atomic_write(fd, path.name, data)


def read_bytes(path):
    """按字节读取，不做 JSON 解析；缺失返回 None。"""
    path = Path(path)
    with directory(path.parent) as fd:
        try:
            source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        with os.fdopen(source, 'rb') as stream:
            return stream.read()


def write_bytes(path, data):
    """原子替换；返回内容是否变化。"""
    path = Path(path)
    with directory(path.parent) as fd:
        if read_bytes(path) == data:
            return False
        temporary = '.cua-deploy-' + secrets.token_hex(8) + '.tmp'
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
        try:
            with os.fdopen(out, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=fd)
            except OSError:
                pass
            raise
    return True


def deploy_file(source, target):
    return write_bytes(target, Path(source).read_bytes())


def launcher_script(python_path, host_path):
    # 主机必须和启动器同目录；仅解释器采用安装时检测到的绝对路径。
    host_name = Path(host_path).name
    require(host_name not in ('', '.', '..'), '主机文件名无效')
    command = shlex.quote(str(python_path)) + ' -B "$host" "$@"'
    # 第一行日志必须在解析任何路径之前落盘：调用方（Chrome）根本没执行到这里时，
    # 日志里就不会有 launch 行，可直接区分“没被拉起”和“拉起来后退出”。
    return ('#!/bin/sh\n'
            'umask 077\n'
            'log=""\n'
            'if [ -n "$HOME" ] && [ -d "$HOME/Library/Logs" ]; then\n'
            '  log="$HOME/Library/Logs/WorkbuddyExtension-native-host.log"\n'
            'fi\n'
            'if [ -z "$log" ] && [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then\n'
            '  log="$TMPDIR/workbuddy-extension-native-host.log"\n'
            'fi\n'
            'if [ -n "$log" ] && { exec 3>> "$log"; } 2>/dev/null; then :; else exec 3>/dev/null; fi\n'
            'printf "%s launch pid=%s argc=%s arg1=%s home=%s\\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$#" "${1:-none}" "${HOME:-unset}" >&3\n'
            'script_dir=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd -P) || {\n'
            '  printf "%s script-dir-failed\\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" >&3\n'
            '  exit 1\n'
            '}\n'
            'host="$script_dir"/' + shlex.quote(host_name) + '\n'
            'if [ ! -f "$host" ]; then\n'
            '  printf "%s host-missing path=%s\\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$host" >&3\n'
            '  printf "%s\\n" "Native host missing beside launcher: $host" >&2\n'
            '  exit 1\n'
            'fi\n'
            'printf "%s host-start pid=%s python=%s\\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" ' + shlex.quote(str(python_path)) + ' >&3\n'
            + command + ' 2>&3\n'
            'status=$?\n'
            'printf "%s host-exit pid=%s code=%s\\n" "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$status" >&3\n'
            'exit "$status"\n')


def ensure_dirs(target, home=None):
    current = Path(home) if home else Path.home()
    target = Path(target)
    require(str(target).startswith(str(current) + os.sep), '安装目标必须在用户目录内')
    for part in target.relative_to(current).parts:
        with directory(current) as fd:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        current = current / part
        with directory(current):
            pass


def write_launcher(launcher, replace=False):
    script = launcher_script(pick_interpreter(), Path(launcher).with_name('native_host.py'))
    launcher = Path(launcher)
    if launcher.is_file():
        require(not launcher.is_symlink() and launcher.lstat().st_uid == os.getuid(),
                '既有启动器不是本人普通文件，拒绝覆盖：' + str(launcher))
        if launcher.read_text(encoding='utf-8') == script:
            os.chmod(launcher, 0o700)
            return False
    with directory(launcher.parent) as fd:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
        flags |= os.O_TRUNC if replace or launcher.is_file() else os.O_EXCL
        out = os.open(launcher.name, flags, 0o700, dir_fd=fd)
        with os.fdopen(out, 'w') as stream:
            stream.write(script)
    os.chmod(launcher, 0o700)
    return True


def migrate_state(source_dir, target_dir):
    """首次部署时把项目内的任务记录搬到运行态；已存在的一律不覆盖。"""
    copied = []
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    for name in STATE_FILES:
        source = source_dir / name
        target = target_dir / name
        if not source.is_file() or target.exists():
            continue
        write_bytes(target, source.read_bytes())
        copied.append(name)
    return copied


def install(result, update=False, home=None):
    home = Path(home) if home else Path.home()
    root = Path(result['runtimePath'])
    host_dir = root / 'browser-observer-extension'
    for path in (root, host_dir, root / 'cua-mcp-bridge', root / 'cua-helper-config'):
        ensure_dirs(path, home=home)
    registration = Path(result['manifestPath'])
    ensure_dirs(registration.parent, home=home)
    previous = read_file(registration)
    if previous is not None:
        require(type(previous) is dict and previous.get('name') == NAME,
                '既有注册不是本扩展，拒绝覆盖：' + str(registration))
    origin = result['manifest']['allowed_origins']
    copied = []
    for source, target in ((BASE / 'native_host.py', Path(result['hostPath'])),
                           (BRIDGE / 'browser_events.py', Path(result['bridgePath']))):
        if deploy_file(source, target):
            copied.append(Path(target).name)
    config = {'allowed_origins': origin}
    changed_config = read_file(result['configPath']) != config
    write_json(result['configPath'], config)
    # 项目内副本只是手工调试用；host 实际读取运行态那一份。
    write_json(BASE / 'native-host-config.json', config)
    launcher_changed = write_launcher(result['launcherPath'])
    state = migrate_state(BASE.parent / 'cua-helper-config', result['statePath'])
    write_json(registration, result['manifest'])
    return {'mode': '已更新' if previous is not None else '已安装',
            'changed': bool(copied or launcher_changed or changed_config or state),
            'launcherUpdated': launcher_changed, 'deployed': copied, 'migratedState': state}


def main():
    parser = argparse.ArgumentParser(description='默认 dryrun；安装须用户确认后明确提供 --install')
    parser.add_argument('--extension-id', help='调试用；必须与 manifest key 派生值一致')
    parser.add_argument('--install', action='store_true', help='部署运行态并写入注册')
    parser.add_argument('--update', action='store_true', help='刷新已存在的安装（配合 --install）')
    args = parser.parse_args()
    try:
        require(not args.update or args.install, '--update 必须与 --install 一起使用')
        result = plan(args.extension_id)
        outcome = install(result, update=args.update) if args.install else {'mode': '仅预览，不写文件'}
        print(json.dumps({**outcome, **result}, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
