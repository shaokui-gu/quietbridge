"""Workbuddy MCP settings and hook merge plans; mutations belong to deployment."""
import json
from pathlib import Path
import shlex
import sys
from compatibility import LEGACY_SERVER_NAMES, browser_layout, runtime_root
from adapters.workbuddy.guidance import CONNECTOR_DESCRIPTION

def owned(entry, home, *, read, app_paths):
    if not isinstance(entry, dict) or not isinstance(entry.get('args'), list):
        return False
    runtime = runtime_root(home) / 'releases'
    connector = runtime.parent / 'connector/connector.py'
    for arg in entry['args']:
        if not isinstance(arg, str):
            continue
        path = Path(arg)
        if (path == connector or path in tuple(app / 'Contents/Resources/bridge/server.py' for app in app_paths) or
                (path.name == 'server.py' and runtime in path.parents)):
            return True
        # Recognise source installs created by install.py without baking one
        # developer's checkout location into the distributed installer.
        if (path.name == 'server.py' and (path.parent.name == 'cua-mcp-bridge' or path.parts[-3:] == ('services', 'mcp', 'server.py')) and home in path.parents
                and isinstance(entry.get('description'), str) and entry['description'].startswith(('QuietBridge：', 'quietbridge：', 'workbuddy-cua：'))):
            source = read(path)
            if source is not None and any(marker in source for marker in (b'SERVER_NAME = "quietbridge"', b'SERVER_NAME = "workbuddy-cua"')):
                return True
    return False


def mcp_plan(home, bridge, helper, stable=False, *, checked, read, owns):
    config = checked(home / '.workbuddy/mcp.json')
    original = read(config)
    data = json.loads(original) if original is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('mcp.json 格式无法识别；未修改')
    servers = data.setdefault('mcpServers', {})
    key = 'quietbridge'
    if key in servers and not owns(servers[key], home):
        raise ValueError('quietbridge 配置键已被其他组件占用；未覆盖')
    if key not in servers:
        key = next((alias for alias in LEGACY_SERVER_NAMES if alias in servers and owns(servers[alias], home)), key)
    entry = dict(servers.get(key, {}))
    if not isinstance(entry.get('env', {}), dict):
        raise ValueError('现有连接 env 格式错误；未修改')
    env = dict(entry.get('env', {}))
    # One runtime for Workbuddy and DeepSeek Harness: browser events, task state
    # and lifecycle hooks all point at the QuietBridge runtime root.
    for legacy_key in ('QUIETBRIDGE_CONFIG_DIR', 'CUA_CONFIG_DIR'):
        env.pop(legacy_key, None)
    env['QUIETBRIDGE_CONFIG_DIR'] = str(browser_layout(home)[3])
    if stable:
        # The active manifest owns the helper version; keep the transport hash
        # fixed across upgrades so the host retains trust and enabled state.
        env.pop('QUIETBRIDGE_HELPER_PATH', None)
        env.pop('CUA_HELPER_PATH', None)
    else:
        env['QUIETBRIDGE_HELPER_PATH'] = str(helper)
    entry.update(command=sys.executable, args=['-B', str(bridge)], env=env)
    # Supply discovery text only when absent; custom host descriptions remain the person's choice.
    if 'description' not in entry:
        entry['description'] = CONNECTOR_DESCRIPTION
    servers[key] = entry
    return config, original, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()


def lifecycle_plan(home, connector, config, *, checked, read):
    """Merge supported Workbuddy hooks without replacing the person's hooks."""
    settings = checked(home / '.workbuddy/settings.json')
    original = read(settings)
    data = json.loads(original) if original else {}
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError('settings.json hooks 格式无法识别；未修改')
    entry = next(value for value in json.loads(config)['mcpServers'].values()
                 if str(connector) in value.get('args', []))
    runtime = Path(entry.get('env', {}).get('QUIETBRIDGE_CONFIG_DIR') or
                   entry.get('env', {}).get('CUA_CONFIG_DIR') or browser_layout(home)[3])
    if not runtime.is_absolute():
        raise ValueError('QUIETBRIDGE_CONFIG_DIR 必须是绝对路径')
    script = connector.parent / 'adapters/workbuddy/lifecycle.py'
    legacy_script = connector.with_name('host_lifecycle.py')
    command = ' '.join(shlex.quote(str(part)) for part in
                       (sys.executable, '-B', script, '--config-dir', runtime))
    hooks = data.setdefault('hooks', {})
    for event in ('PreToolUse', 'PostToolUse', 'FinalStop', 'SessionEnd'):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError('settings.json hook 事件必须是数组')
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('settings.json hook 组格式无法识别')
            remaining = []
            for hook in group['hooks']:
                if not isinstance(hook, dict):
                    raise ValueError('settings.json hook 格式无法识别')
                try:
                    parts = shlex.split(hook.get('command', ''))
                except ValueError:
                    parts = []
                ours = False
                if hook.get('type') == 'command' and len(parts) >= 3 and parts[1] == '-B':
                    target = Path(parts[2])
                    runtime_parts = {part for part in target.parts if part in ('QuietBridge', 'workbuddy-cua')}
                    ours = (target in (script, legacy_script) or
                            (bool(runtime_parts) and target.name in ('host_lifecycle.py', 'lifecycle.py') and
                             (target.name == 'host_lifecycle.py' or 'adapters' in target.parts)))
                if not ours:
                    remaining.append(hook)
            if remaining:
                retained.append(dict(group, hooks=remaining))
        group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
        # PostToolUse also observes activity between QuietBridge phases. The
        # adapter stores only timestamps for an already-associated run, never
        # tool arguments, responses or transcript content.
        retained.append(group)
        hooks[event] = retained
    return settings, original, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
