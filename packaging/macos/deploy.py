#!/usr/bin/env python3
"""Deploy user components transactionally. Never change authorizationdb.

Run as the desktop user, after the root installer has published the signed helper.
The journal covers user files and the extension directory, not the PKG payload.
"""
import argparse
import ctypes
import datetime
import fcntl
import fnmatch
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import signal
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# One adapter implementation is used from source and from packaged support.
_module_root = Path(__file__).resolve().parents[2]
if (_module_root / 'services/mcp').is_dir():
    sys.path[:0] = [str(_module_root / 'services/mcp'), str(_module_root)]
from compatibility import (LEGACY_APP_NAMES, LEGACY_SERVER_NAMES, PUBLISHED_CONNECTOR_DIGESTS,
                           MENU_IDENTIFIER, HELPER_IDENTIFIER,
                           LEGACY_MENU_IDENTIFIER, LEGACY_HELPER_IDENTIFIER, runtime_root)
from adapters.workbuddy import configuration as workbuddy

# Keep the existing launchd storage key so upgrades can stop/restore the same job.
# It is independent of the new code-signing bundle identifier.
LABEL = LEGACY_MENU_IDENTIFIER
APP = Path('/Applications/QuietBridge.app')
LEGACY_APP = Path('/Applications/workbuddy-cua.app')
APP_PATHS = (APP, *(Path('/Applications') / name for name in LEGACY_APP_NAMES))
SUPPORT = Path('/Library/Application Support/QuietBridge')
PUBLIC = Path('/Library/Application Support/WorkBuddyCUAHelper/releases')
TEAM = '62RNP54C47'
EXTENSION_IGNORES = ('native-host-config.json', 'native-host-launcher.sh', '__pycache__',
                     '.DS_Store', '._*', '*.test.mjs')


def checked(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('拒绝符号链接: ' + str(node))
    return path


def read(path):
    path = checked(path)
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('配置必须是本人普通文件: ' + str(path))
    return path.read_bytes()


def atomic(path, data, mode=0o600):
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.quietbridge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tree_digest(directory, ignore=()):
    checked(directory)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if any(fnmatch.fnmatchcase(part, pattern) for part in path.relative_to(directory).parts for pattern in ignore):
            continue
        checked(path)
        if path.is_file():
            digest.update(str(path.relative_to(directory)).encode() + b'\0')
            digest.update(path.read_bytes())
        elif not path.is_dir():
            raise ValueError('组件包含非普通文件: ' + str(path))
    return digest.hexdigest()


def owned(entry, home):
    return workbuddy.owned(entry, home, read=read, app_paths=APP_PATHS)


def config_plan(home, bridge, helper, stable=False):
    return workbuddy.mcp_plan(home, bridge, helper, stable, checked=checked, read=read, owns=owned)


def lifecycle_plan(home, connector, config):
    return workbuddy.lifecycle_plan(home, connector, config, checked=checked, read=read)


def preflight(home, upgrade=None):
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或更高版本')
    if sys.platform != 'darwin' or int(platform.mac_ver()[0].split('.')[0]) < 14 or platform.machine() != 'arm64':
        raise ValueError('此安装包需要 Apple Silicon 和 macOS 14 或更高版本；请使用原生 arm64 Python')
    home = checked(home)
    if home.stat().st_uid != os.getuid() or os.geteuid() == 0:
        raise ValueError('用户组件必须以桌面用户身份安装')
    config_plan(home, APP / 'Contents/Resources/bridge/server.py', APP)
    runtime = checked(runtime_root(home))
    pending = read_upgrade(home)
    if pending and pending['phase'] not in ('committed', 'rolled-back') and (
            not upgrade or pending['id'] != upgrade['id'] or pending['phase'] not in ('stopped', 'deploying')):
        raise ValueError('发现替换文件前已暂停的安装；请先运行：' + str(upgrade_recovery_command(home, pending)))
    for relative in ('extension/extension-stage', 'releases', 'transactions'):
        checked(runtime / relative)
    agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
    previous_agent = read(agent)
    if previous_agent is not None:
        value = plistlib.loads(previous_agent)
        args = value.get('ProgramArguments', [])
        if (value.get('Label') != LABEL or not isinstance(args, list) or not args or
                not (Path(args[0]) in tuple(app / 'Contents/MacOS' / name for app in APP_PATHS
                         for name in ('quietbridge-native', 'workbuddy-cua-native')) or
                     runtime / 'releases' in Path(args[0]).parents)):
            raise ValueError('登录启动项被其他组件占用；未覆盖')
    for journal in sorted((runtime / 'transactions').glob('*/journal.json')):
        if json.loads(read(journal))['phase'] not in ('committed', 'rolled-back'):
            raise ValueError('发现未完成安装；请先运行“恢复安装.command”：' + str(journal))
    return runtime


class Transaction:
    def __init__(self, directory, files, extension, source):
        self.directory = directory
        self.journal = directory / 'journal.json'
        self.values = {'phase': 'prepared', 'files': [], 'extension': str(extension),
                       'extensionDigest': tree_digest(source),
                       'hadExtension': extension.exists()}
        shutil.copytree(source, directory / 'extension-next')
        for index, (path, original, data, mode) in enumerate(files):
            if original == data:
                continue
            if original is not None:
                atomic(directory / ('backup-' + str(index)), original)
            self.values['files'].append({'path': str(path), 'backup': 'backup-' + str(index),
                                        'hadFile': original is not None,
                                        'oldMode': stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600,
                                        'sha256': hashlib.sha256(data).hexdigest()})
        self.files = files
        self.save()

    def save(self):
        atomic(self.journal, json.dumps(self.values, indent=2).encode())

    def apply(self):
        # Detect changes since planning before changing any user file.
        for path, original, _, _ in self.files:
            if read(path) != original:
                raise ValueError('配置被其他程序修改；未覆盖: ' + str(path))
        target = checked(self.values['extension'])
        self.values['phase'] = 'applying'
        self.save()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            target.rename(self.directory / 'extension-previous')
        (self.directory / 'extension-next').rename(target)
        for path, original, data, mode in self.files:
            if read(path) != original:
                raise ValueError('配置在提交时被修改: ' + str(path))
            if original != data:
                atomic(path, data, mode)

    def rollback(self):
        conflicts = []
        for item in reversed(self.values['files']):
            path = checked(item['path'])
            original = (self.directory / item['backup']).read_bytes() if item['hadFile'] else None
            current = read(path)
            if current == original:
                continue
            if current is None or hashlib.sha256(current).hexdigest() != item['sha256']:
                conflicts.append(str(path))
                continue
            if original is None:
                path.unlink()
            else:
                atomic(path, original, item['oldMode'])
        target = checked(self.values['extension'])
        previous = self.directory / 'extension-previous'
        # extension-next disappearing proves that our new directory was switched in.
        switched = not (self.directory / 'extension-next').exists() and not self.values.get('extensionRestored')
        if switched and target.exists():
            if tree_digest(target) == self.values['extensionDigest']:
                target.rename(self.directory / 'extension-failed')
            else:
                conflicts.append(str(target))
        if previous.exists() and not target.exists():
            previous.rename(target)
            self.values['extensionRestored'] = True
        elif not previous.exists() and (not switched or not target.exists()):
            self.values['extensionRestored'] = True
        self.values['phase'] = 'rollback-conflict' if conflicts else 'rolled-back'
        self.save()
        if conflicts:
            raise RuntimeError('恢复时发现外部修改，已保留备份: ' + ', '.join(conflicts))

    @classmethod
    def reopen(cls, directory):
        instance = cls.__new__(cls)
        instance.directory = checked(directory)
        instance.journal = instance.directory / 'journal.json'
        instance.values = json.loads(read(instance.journal))
        return instance


class MenuBarService:
    def __init__(self, home):
        self.domain = 'gui/' + str(os.getuid())
        self.name = self.domain + '/' + LABEL
        self.agent = home / 'Library/LaunchAgents' / (LABEL + '.plist')

    def is_running(self):
        return bool(self.processes())

    def processes(self):
        # Startup/wizard processes may not have opened the status port yet.
        # Enumerate only this user's signed menu executables, never helpers.
        found = []
        verified = set()
        for pid, executable in user_processes():
            if not self.owns_process(executable, str(os.getuid()), str(executable)):
                continue
            command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
            if command.returncode not in (0, 1):
                raise RuntimeError('无法核对菜单栏进程')
            if not command.stdout.strip() or not self.owns_process(executable, str(os.getuid()), command.stdout.strip()):
                continue
            app = checked(executable.parents[2])
            if app not in verified:
                identifier = plistlib.loads((app / 'Contents/Info.plist').read_bytes()).get('CFBundleIdentifier')
                if identifier not in (MENU_IDENTIFIER, LEGACY_MENU_IDENTIFIER):
                    raise ValueError('运行中应用标识无法识别；未终止进程')
                verify_code(app, identifier)
                verified.add(app)
            found.append((pid, executable))
        return found

    def owns_process(self, executable, owner, arguments):
        if owner != str(os.getuid()):
            return False
        system_binaries = {app / 'Contents/MacOS' / name for app in APP_PATHS
                           for name in ('quietbridge-native', 'workbuddy-cua-native')}
        releases = runtime_root(self.agent.parents[2]) / 'releases'
        if executable in system_binaries:
            # proc_pidpath follows the renamed bundle, while argv retains the
            # path used at launch. Only these two system paths are aliases.
            launch_paths = system_binaries
        elif (executable.parent.name == 'MacOS' and executable.parent.parent.name == 'Contents'
              and executable.name in ('quietbridge-native', 'workbuddy-cua-native') and releases in executable.parents):
            launch_paths = {executable}
        else:
            return False
        return any(arguments in (str(path), str(path) + ' --menubar') or
                   re.fullmatch(re.escape(str(path)) + r' -psn_[0-9_]+', arguments)
                   for path in launch_paths)

    def stop(self):
        owned = self.processes()
        pids = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:47631', '-sTCP:LISTEN', '-t'],
                              capture_output=True, text=True)
        if pids.returncode not in (0, 1) or pids.stderr.strip():
            raise RuntimeError('无法核对菜单栏状态端口: ' + pids.stderr.strip())
        if any(int(pid) not in {item[0] for item in owned} for pid in pids.stdout.split()):
            raise RuntimeError('状态端口被其他程序占用；未终止该进程')
        status = subprocess.run(['/bin/launchctl', 'print', self.name], capture_output=True)
        if status.returncode == 0:
            subprocess.run(['/bin/launchctl', 'bootout', self.name], check=True, capture_output=True)
        elif b'Could not find service' not in status.stderr:
            raise RuntimeError('无法读取菜单栏登录启动状态: ' + status.stderr.decode('utf-8', 'replace').strip())
        # bootout may return before the process exits. Include Finder/wizard
        # instances and revalidate identity before every signal and final wait.
        for pid, executable in owned:
            if (pid, executable) not in self.processes():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        until = time.monotonic() + 5
        while self.processes():
            if time.monotonic() >= until:
                raise RuntimeError('旧菜单栏程序未退出；未允许替换应用文件')
            time.sleep(0.1)

    def start(self):
        subprocess.run(['/bin/launchctl', 'bootstrap', self.domain, str(self.agent)], check=True, capture_output=True)
        subprocess.run(['/bin/launchctl', 'kickstart', self.name], check=True, capture_output=True)

    def wait_ready(self, executable, info):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            connection = http.client.HTTPConnection('127.0.0.1', 47631, timeout=0.5)
            try:
                connection.request('GET', '/')
                service = json.loads(connection.getresponse().read(8192)).get('_service', {})
                if (service.get('executable') == str(executable) and
                        service.get('version') == info['CFBundleShortVersionString'] and
                        service.get('build') == info['CFBundleVersion']):
                    loaded = subprocess.run(['/bin/launchctl', 'print', self.name], capture_output=True, text=True, check=True)
                    if re.search(r'\bpid = ' + str(service.get('pid')) + r'\b', loaded.stdout):
                        return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.2)
        raise RuntimeError('新版菜单栏未通过启动、版本和进程检查')


def verify_code(app, identifier):
    requirement = '=anchor apple generic and identifier "' + identifier + '" and certificate leaf[subject.OU] = "' + TEAM + '"'
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, str(app)], check=True, capture_output=True)


def upgrade_path(home):
    return checked(runtime_root(home) / 'upgrade-state.json')


def read_upgrade(home):
    raw = read(upgrade_path(home))
    if raw is None:
        return None
    if len(raw) > 262144:
        raise ValueError('安装暂停记录超限；未覆盖')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or
            not re.fullmatch(r'[0-9a-f]{32}', str(value.get('id', ''))) or
            value.get('phase') not in ('preparing', 'stopped', 'deploying', 'recovery-required', 'committed', 'rolled-back') or
            type(value.get('wasRunning')) is not bool):
        raise ValueError('安装暂停记录格式错误；未覆盖')
    return value


def save_upgrade(home, value):
    atomic(upgrade_path(home), json.dumps(value, ensure_ascii=False, indent=2).encode())


def upgrade_recovery_command(home, value):
    return checked(runtime_root(home) / 'upgrade-recovery' / value['id'] / '恢复安装.command')


def stage_upgrade_recovery(home, value):
    # PackageKit can fail before publishing the new support/deploy.py. Preserve
    # the new recovery implementation outside the payload before stopping.
    command = upgrade_recovery_command(home, value)
    directory = command.parent
    directory.mkdir(parents=True, mode=0o700)
    shutil.copy2(Path(__file__), directory / 'deploy.py')
    shutil.copy2(Path(sys.modules['compatibility'].__file__), directory / 'compatibility.py')
    shutil.copytree(Path(workbuddy.__file__).parents[1], directory / 'adapters',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests', 'test'))
    text = '#!/bin/bash\nset -euo pipefail\n' + shlex.quote(sys.executable) + ' -B ' + \
           shlex.quote(str(directory / 'deploy.py')) + ' --recover-latest\n' + \
           "read -r -p '按回车关闭。' ignored\n"
    atomic(command, text.encode(), 0o700)


def restore_upgrade(home, value, service=None):
    """Restore only the verified pre-payload menu copy, never system payloads."""
    service = service or MenuBarService(home)
    agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
    try:
        for journal in (checked(runtime_root(home)) / 'transactions').glob('*/journal.json'):
            if json.loads(read(journal))['phase'] not in ('committed', 'rolled-back'):
                raise ValueError('用户文件事务尚未恢复；请先运行“恢复安装.command”')
        if value['wasRunning']:
            original = bytes.fromhex(value['oldAgentHex']) if value['oldAgentHex'] is not None else None
            resume = bytes.fromhex(value['resumeAgentHex'])
            if read(agent) not in (original, resume):
                raise ValueError('启动项在安装期间被修改；已保留修改与旧版备份')
            binary = checked(value['restoreBinary'])
            releases = checked(runtime_root(home) / 'releases')
            if (releases not in binary.parents or binary.parent.name != 'MacOS' or
                    binary.parent.parent.name != 'Contents' or
                    binary.name not in ('quietbridge-native', 'workbuddy-cua-native')):
                raise ValueError('旧版恢复路径无效')
            app = binary.parents[2]
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            identifier = info.get('CFBundleIdentifier')
            if identifier not in (MENU_IDENTIFIER, LEGACY_MENU_IDENTIFIER) or info.get('CFBundleExecutable') != binary.name:
                raise ValueError('旧版恢复应用标识无效')
            verify_code(app, identifier)
            launch = plistlib.loads(resume)
            if launch.get('Label') != LABEL or launch.get('ProgramArguments') != [str(binary), '--menubar']:
                raise ValueError('旧版恢复启动项无效')
            service.stop()
            atomic(agent, resume, 0o644)
            service.start()
            service.wait_ready(binary, info)
        value['phase'] = 'rolled-back'
        save_upgrade(home, value)
    except Exception:
        value['phase'] = 'recovery-required'
        save_upgrade(home, value)
        raise


def prepare_install(home, version, build, service=None):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}', version) or not re.fullmatch(r'[0-9]+', build):
        raise ValueError('安装版本格式错误')
    runtime = preflight(home)
    service = service or MenuBarService(home)
    processes = service.processes()
    paths = {executable for _, executable in processes}
    if len(paths) > 1:
        raise ValueError('发现多个运行中的应用副本；未停止或替换，请保留一个后重试')
    value = {'schemaVersion': 1, 'id': uuid.uuid4().hex, 'phase': 'preparing',
             'version': version, 'build': build, 'wasRunning': bool(processes)}
    if processes:
        source = checked(next(iter(paths)).parents[2])
        info = plistlib.loads((source / 'Contents/Info.plist').read_bytes())
        identifier = info.get('CFBundleIdentifier')
        if identifier not in (MENU_IDENTIFIER, LEGACY_MENU_IDENTIFIER):
            raise ValueError('运行中应用身份无法验证')
        verify_code(source, identifier)
        # A directly launched /Applications copy will be replaced by PackageKit.
        # Keep an independent signed copy even when there was no LaunchAgent.
        backup = checked(runtime / 'releases' / ('rollback-' + value['id']) / 'QuietBridge.app')
        backup.parent.mkdir(parents=True, mode=0o700)
        shutil.copytree(source, backup, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if tree_digest(source, ('__pycache__', '*.pyc')) != tree_digest(backup):
            raise ValueError('旧版备份与运行应用不一致；未停止应用')
        verify_code(backup, identifier)
        executable = info.get('CFBundleExecutable')
        if executable not in ('quietbridge-native', 'workbuddy-cua-native') or not (backup / 'Contents/MacOS' / executable).is_file():
            raise ValueError('旧版恢复执行文件无效')
        binary = backup / 'Contents/MacOS' / executable
        agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
        original = read(agent)
        resume = plistlib.loads(original) if original else {'Label': LABEL, 'ProcessType': 'Interactive'}
        resume.update(ProgramArguments=[str(binary), '--menubar'], RunAtLoad=False, KeepAlive=False)
        value.update(oldAgentHex=original.hex() if original is not None else None,
                     resumeAgentHex=plistlib.dumps(resume).hex(), restoreBinary=str(binary))
    stage_upgrade_recovery(home, value)
    save_upgrade(home, value)
    try:
        service.stop()
        value['phase'] = 'stopped'
        save_upgrade(home, value)
    except Exception as error:
        try:
            restore_upgrade(home, value, service)
        except Exception as recovery:
            raise RuntimeError(str(error) + '\n旧版恢复未完成，请运行：' + str(upgrade_recovery_command(home, value)) + '\n' + str(recovery)) from error
        raise
    return value


def finish_install(home, helper, app=APP, support=SUPPORT, service=None):
    value = read_upgrade(home)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if (not value or value['phase'] != 'stopped' or
            (value.get('version'), value.get('build')) !=
            (info.get('CFBundleShortVersionString'), info.get('CFBundleVersion'))):
        raise ValueError('缺少与此安装包匹配的安装前暂停记录；未继续部署')
    value['phase'] = 'deploying'
    save_upgrade(home, value)
    try:
        result = deploy(home, helper, app, support, service, upgrade=value)
    except Exception as error:
        try:
            restore_upgrade(home, value, service)
        except Exception as recovery:
            raise RuntimeError(str(error) + '\n旧版恢复未完成，请运行：' + str(upgrade_recovery_command(home, value)) + '\n' + str(recovery)) from error
        raise RuntimeError(str(error) + '\n已恢复安装前的应用运行状态；系统安装文件不在用户事务回滚范围内。') from error
    value.update(phase='committed', deploymentJournal=result['journal'])
    save_upgrade(home, value)
    result['stoppedBeforePayload'] = True
    return result


def recover_latest(home, service=None):
    runtime = checked(runtime_root(home))
    records = [(path, json.loads(read(path))) for path in sorted((runtime / 'transactions').glob('*/journal.json'))]
    journals = [path for path, value in records if value['phase'] not in ('committed', 'rolled-back')]
    state = read_upgrade(home)
    pending_upgrade = state and state['phase'] not in ('committed', 'rolled-back')
    if pending_upgrade and not journals and any(value.get('upgradeId') == state['id'] and value['phase'] == 'committed'
                                                for _, value in records):
        # Deployment committed durably but the final stop receipt write was
        # interrupted. Do not roll a healthy new release back to the old copy.
        state['phase'] = 'committed'
        save_upgrade(home, state)
        return True
    if not journals and not pending_upgrade:
        return False
    service = service or MenuBarService(home)
    service.stop()
    for journal in reversed(journals):
        Transaction.reopen(journal.parent).rollback()
    if pending_upgrade:
        restore_upgrade(home, state, service)
    elif service.agent.is_file() and any(json.loads(read(path)).get('wasRunning') is True for path in journals):
        service.start()
    return True


def user_processes():
    """Read only PID/UID and executable paths; never dump host arguments/env."""
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,uid='], capture_output=True, text=True, check=True)
    library = ctypes.CDLL('/usr/lib/libproc.dylib')
    processes = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            continue
        pid, uid = map(int, fields)
        if uid != os.getuid() or pid == os.getpid():
            continue
        path = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, path, len(path)) > 0:
            processes.append((pid, Path(os.fsdecode(path.value))))
    return processes


def legacy_bridges(home):
    """A rewritten mcp.json does not replace a host's cached live transport."""
    releases = runtime_root(home) / 'releases'
    found = []
    for pid, executable in user_processes():
        if not executable.name.lower().startswith('python'):
            continue
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
        command = result.stdout.strip()
        for separator in (' -B ', ' '):
            prefix = str(executable) + separator
            if not command.startswith(prefix):
                continue
            script = Path(command[len(prefix):])
            if (script.parts[-4:] == ('Contents', 'Resources', 'bridge', 'server.py') and
                    (releases in script.parents or script in tuple(app / 'Contents/Resources/bridge/server.py' for app in APP_PATHS))):
                app = script.parents[3]
                identifier = plistlib.loads((app / 'Contents/Info.plist').read_bytes()).get('CFBundleIdentifier')
                if identifier not in (MENU_IDENTIFIER, LEGACY_MENU_IDENTIFIER):
                    raise ValueError('旧连接的应用标识无法识别')
                verify_code(app, identifier)
                found.append({'pid': pid, 'bridge': str(script)})
                break
    return found


def retire_previous_helpers(helper, build):
    """Upgrade only CUA workers; never signal Workbuddy or a target application."""
    candidates = []
    verified = set()
    for pid, executable in user_processes():
        if executable == helper or PUBLIC not in executable.parents or executable.parts[-3:-1] != ('Contents', 'MacOS') or executable.name not in ('quietbridge-native', 'workbuddy-cua-native'):
            continue
        app = checked(executable.parents[2])
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        identifier = info.get('CFBundleIdentifier')
        if (identifier not in (HELPER_IDENTIFIER, LEGACY_HELPER_IDENTIFIER) or
                not str(info.get('CFBundleVersion', '')).isdigit() or int(info['CFBundleVersion']) >= int(build)):
            continue
        if app not in verified:
            verify_code(app, identifier)
            verified.add(app)
        candidates.append((pid, executable))
    def alive(identity):
        return identity in user_processes()  # Revalidate UID/path before each signal.
    for identity in candidates:
        if alive(identity):
            try: os.kill(identity[0], signal.SIGTERM)
            except ProcessLookupError: pass
    until = time.monotonic() + 3
    while any(alive(identity) for identity in candidates) and time.monotonic() < until:
        time.sleep(0.1)
    for identity in candidates:
        if alive(identity):
            try: os.kill(identity[0], signal.SIGKILL)
            except ProcessLookupError: pass
    return [pid for pid, _ in candidates]


def handshake(bridge, helper):
    request = json.dumps({'id': 1, 'method': 'initialize', 'params': {}}) + '\n'
    reply = subprocess.run([sys.executable, '-B', str(bridge)], input=request, capture_output=True,
                           text=True, timeout=15, env=dict(os.environ, QUIETBRIDGE_HELPER_PATH=str(helper)))
    if reply.returncode or json.loads(reply.stdout)['result']['serverInfo']['name'] != 'quietbridge':
        raise ValueError('安装副本 MCP 握手失败；未提交用户配置')


def verify_helper(helper):
    helper = checked(helper)
    helper_app = helper.parents[2]
    if PUBLIC not in helper.parents:
        raise ValueError('helper 必须使用系统管理的独立签名版本')
    for path in (helper, *helper.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('helper 路径权限不安全: ' + str(path))
    verify_code(helper_app, HELPER_IDENTIFIER)


def deploy(home, helper, app=APP, support=SUPPORT, service=None, upgrade=None):
    runtime = preflight(home, upgrade)
    verify_helper(helper)
    verify_code(app, MENU_IDENTIFIER)
    menu_info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    helper_info = plistlib.loads((helper.parents[2] / 'Contents/Info.plist').read_bytes())
    if any(helper_info.get(key) != menu_info.get(key) for key in ('CFBundleShortVersionString', 'CFBundleVersion')):
        raise ValueError('独立 helper 与菜单栏版本不一致；停止安装')
    extension_source = support / 'extension/extension-stage'
    digest = hashlib.sha256((tree_digest(app, ('__pycache__', '*.pyc')) + tree_digest(extension_source, EXTENSION_IGNORES)).encode()).hexdigest()[:20]
    release = checked(runtime / 'releases' / digest)
    if not release.exists():
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.release-', dir=release.parent))
        try:
            shutil.copytree(app, stage / 'QuietBridge.app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copytree(extension_source, stage / 'extension', ignore=shutil.ignore_patterns(*EXTENSION_IGNORES))
            stage.rename(release)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if (tree_digest(release / 'QuietBridge.app', ('__pycache__', '*.pyc')) != tree_digest(app, ('__pycache__', '*.pyc')) or
            tree_digest(release / 'extension') != tree_digest(extension_source, EXTENSION_IGNORES)):
        raise ValueError('版本目录内容不一致；停止安装')
    menu_app = release / 'QuietBridge.app'
    verify_code(menu_app, MENU_IDENTIFIER)
    binary = menu_app / 'Contents/MacOS/quietbridge-native'
    bridge = menu_app / 'Contents/Resources/bridge/server.py'
    connector = checked(runtime / 'connector/connector.py')
    config, original, rendered = config_plan(home, connector, helper, stable=True)
    settings, old_settings, rendered_settings = lifecycle_plan(home, connector, rendered)
    handshake(bridge, helper)
    extension = checked(runtime / 'extension/extension-stage')
    sys.path.insert(0, str(release / 'extension'))
    spec = importlib.util.spec_from_file_location('package_native_host', release / 'extension/install_native_host.py')
    host = importlib.util.module_from_spec(spec)
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(host)
    finally:
        sys.dont_write_bytecode = previous_bytecode
    plan = host.plan(home=home)
    registration = Path(plan['manifestPath'])
    previous = read(registration)
    if previous is not None:
        value = json.loads(previous)
        if (not isinstance(value, dict) or value.get('name') != host.NAME or
                value.get('allowed_origins') != plan['manifest']['allowed_origins'] or
                Path(value.get('path', '')) != Path(plan['launcherPath'])):
            raise ValueError('原生宿主注册被其他组件占用；未覆盖')
    manifest = connector.with_name('active.json')
    generation = str(uuid.uuid4())
    active = json.dumps({'generation': generation, 'bridge': str(bridge), 'helper': str(helper),
                         'version': menu_info['CFBundleShortVersionString']}).encode()
    files = [(config, original, rendered, 0o600),
             (settings, old_settings, rendered_settings, 0o600)]
    stable_modules = ['connector.py', 'stdio_transport.py', 'compatibility.py', 'lifecycle_store.py',
                      'session_routing.py', 'session_registry.py', 'browser_events.py', 'browser_preview.py']
    stable_modules += [str(path.relative_to(bridge.parent)) for path in sorted((bridge.parent / 'adapters').rglob('*.py'))]
    transport_changed = False
    for relative in stable_modules:
        target = connector.parent / relative
        prior_module = read(target)
        replacement = (bridge.parent / relative).read_bytes()
        if relative == 'connector.py' and prior_module is not None:
            if hashlib.sha256(prior_module).hexdigest() in PUBLISHED_CONNECTOR_DIGESTS:
                replacement = prior_module
            transport_changed = prior_module != replacement
        files.append((target, prior_module, replacement, 0o600))
    extension_modules = [(release / 'extension/native_host.py', Path(plan['hostPath']))]
    extension_modules += [(release / 'extension' / name, Path(plan['bridgePath']).with_name(name))
                          for name in ('browser_events.py', 'compatibility.py', 'session_registry.py', 'lifecycle_store.py', 'browser_preview.py')]
    for source, target in extension_modules:
        files.append((target, read(target), source.read_bytes(), 0o600))
    launcher = Path(plan['launcherPath'])
    files.append((launcher, read(launcher), host.launcher_script(sys.executable, Path(plan['hostPath'])).encode(), 0o700))
    native_config = Path(plan['configPath'])
    files.append((native_config, read(native_config), json.dumps({'allowed_origins': plan['manifest']['allowed_origins']}).encode(), 0o600))
    files.append((registration, previous, json.dumps(plan['manifest']).encode(), 0o600))
    agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
    old_agent = read(agent)
    agent_data = plistlib.dumps({'Label': LABEL, 'ProgramArguments': [str(binary), '--menubar'],
                               'RunAtLoad': False, 'KeepAlive': False, 'ProcessType': 'Interactive',
                               'LimitLoadToSessionType': 'Aqua'})
    files.append((agent, old_agent, agent_data, 0o644))
    # Publish last: a running connector only sees a fully committed file set.
    files.append((manifest, read(manifest), active, 0o600))
    # Create the runtime state directory expected by the native host.
    checked(plan['statePath']).mkdir(parents=True, exist_ok=True, mode=0o700)
    transactions = runtime / 'transactions'
    transactions.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=transactions))
    transaction = Transaction(directory, files, extension, release / 'extension')
    service = service or MenuBarService(home)
    was_running = upgrade['wasRunning'] if upgrade is not None else service.is_running()
    transaction.values['wasRunning'] = was_running
    if upgrade is not None:
        transaction.values['upgradeId'] = upgrade['id']
    transaction.save()
    stopped = False
    try:
        stopped = True
        service.stop()
        transaction.apply()
        if was_running:
            service.start()
            service.wait_ready(binary, menu_info)
        transaction.values['phase'] = 'committed'
        transaction.save()
    except Exception as error:
        recovery_errors = []
        if stopped:
            try:
                service.stop()
            except Exception as failure:
                recovery_errors.append(str(failure))
        try:
            transaction.rollback()
        except Exception as failure:
            recovery_errors.append(str(failure))
        if upgrade is None and stopped and was_running and old_agent is not None and not recovery_errors:
            try:
                service.start()
            except Exception as failure:
                recovery_errors.append(str(failure))
        raise RuntimeError(str(error) + '\n用户文件恢复结果: ' + ('；'.join(recovery_errors) or '已恢复') +
                           '\n安装记录: ' + str(transaction.journal)) from error
    cleanup = {}
    try:
        cleanup['retiredHelperPids'] = retire_previous_helpers(helper, menu_info['CFBundleVersion'])
        running_legacy = legacy_bridges(home)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        cleanup['error'] = type(error).__name__
        running_legacy = None
    transaction.values['legacyCleanup'] = cleanup
    transaction.values['lifecycleHooks'] = {'installed': True,
        'events': ['PostToolUse', 'FinalStop', 'SessionEnd'], 'settings': str(settings)}
    previous_servers = json.loads(original).get('mcpServers', {}) if original else {}
    previous_entry = next((v for k, v in previous_servers.items() if k in ('quietbridge', *LEGACY_SERVER_NAMES) and owned(v, home)), None)
    stable_before = previous_entry is not None and str(connector) in previous_entry.get('args', [])
    connection = {'state': 'pending_connection' if stable_before else
                  'host_refresh_required' if previous_entry else 'first_install',
                  'automaticUpdates': True}
    if stable_before and transport_changed:
        connection.update(state='host_refresh_required', transportChanged=True)
    if running_legacy:
        connection.update(state='host_refresh_required', legacyBridgePids=[item['pid'] for item in running_legacy])
    elif running_legacy is None:
        connection['state'] = 'verification_unavailable'
    if connection['state'] == 'host_refresh_required':
        connection['message'] = ('Workbuddy 仍缓存旧连接的启动命令，普通重连不会重新读取配置。'
                                 '请保存当前工作后完整退出并重新打开 Workbuddy，再连接 quietbridge；'
                                 '迁移到固定入口后，后续覆盖安装和升级自动加载新版。')
        if connection.get('transportChanged'):
            connection['message'] = ('已更新无法识别的旧连接入口。请在 Workbuddy 中重新确认 QuietBridge 连接的信任与启用状态；'
                                     '确认后只读核实权限和目标，不能重放旧输入。')
    if stable_before and connection['state'] == 'pending_connection':
        until = time.monotonic() + 8
        while time.monotonic() < until:
            try:
                raw = read(connector.with_name('status.json'))
                status = json.loads(raw) if raw else {}
                if not isinstance(status, dict):
                    raise ValueError('连接器状态格式错误')
            except (OSError, ValueError):
                # Runtime acknowledgment is advisory after the file transaction
                # commits. A malformed/stale status file must not fail a PKG
                # whose verified components have already installed successfully.
                connection['state'] = 'verification_unavailable'
                break
            if status.get('generation') == generation and status.get('state') == 'ready':
                connection['state'] = 'reloaded'
                break
            # No connected host: do not launch a new connector behind its back.
            if not raw:
                break
            if type(status.get('pid')) is not int or status['pid'] <= 0:
                break
            try:
                os.kill(status['pid'], 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.2)
    transaction.values['connectorUpdate'] = connection
    transaction.save()
    return {'installed': True, 'version': menu_info['CFBundleShortVersionString'],
            'helper': str(helper), 'journal': str(transaction.journal), 'startupVerified': was_running,
            'connectorUpdate': connection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--prepare-install', action='store_true')
    actions.add_argument('--finish-install', action='store_true')
    actions.add_argument('--abort-install', action='store_true')
    actions.add_argument('--recover-latest', action='store_true')
    parser.add_argument('--package-version')
    parser.add_argument('--package-build')
    parser.add_argument('--helper', type=Path)
    args = parser.parse_args()
    home = Path.home()
    if args.check:
        preflight(home)
        print('环境与现有连接配置检查通过')
        return
    runtime = checked(runtime_root(home))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = checked(runtime / '.install.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare_install:
            if not args.package_version or not args.package_build:
                parser.error('暂停安装需要包版本与构建号')
            state = prepare_install(home, args.package_version, args.package_build)
            print(json.dumps({'prepared': True, 'wasRunning': state['wasRunning'], 'stoppedBeforePayload': True,
                              'recoveryCommand': str(upgrade_recovery_command(home, state))}, ensure_ascii=False))
        elif args.abort_install:
            state = read_upgrade(home)
            if (not state or (state.get('version'), state.get('build')) != (args.package_version, args.package_build) or
                    state['phase'] in ('committed', 'rolled-back')):
                raise ValueError('没有与此安装包匹配的待恢复暂停记录')
            restore_upgrade(home, state)
            print('已恢复安装前的应用运行状态')
        elif args.recover_latest:
            if not recover_latest(home):
                print('没有需要恢复的用户安装事务')
                return
            print('安装状态已恢复；请核对应用版本后重新运行安装包')
        else:
            if not args.helper:
                parser.error('需要 --helper')
            installer = finish_install if args.finish_install else deploy
            print(json.dumps(installer(home, args.helper), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('安装未完成: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
