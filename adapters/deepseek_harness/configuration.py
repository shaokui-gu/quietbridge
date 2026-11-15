"""DeepSeek Harness bundle rendering and profile patch plans; mutations belong to deployment.

The adapter never edits a real profile: :func:`bundle_render` writes only the
explicit destination it is given, and :func:`profile_plan` returns rendered
bytes for a caller-controlled transaction. No network access, no pnpm and no
``~/.dsh`` writes happen here.
"""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

ROW_ID = 'quietbridge-dsh'
SERVER_NAME = 'quietbridge'
PACKAGE_JSON = 'package.json'
PATCH_FILE = 'cordis.patch.yml'
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
_PATH_PREFIXES = ('file:', 'link:', 'portal:', 'workspace:')
_SKIPPED_DIRECTORIES = ('test', '__pycache__', '.git')
_SKIPPED_NAMES = ('.DS_Store',)


def _read_json(path):
    return json.loads(Path(path).read_bytes().decode('utf-8'))


def _read_text(path):
    return Path(path).read_bytes().decode('utf-8')


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _package_name(bundle_path):
    """Return the bundle package name declared by its package.json, or None."""
    try:
        package = _read_json(Path(bundle_path) / PACKAGE_JSON)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    name = package.get('name') if isinstance(package, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def bundle_name(bundle_path):
    """Read the package name a bundle directory declares; refuse unreadable ones."""
    name = _package_name(bundle_path)
    if name is None:
        raise ValueError('bundle 的 package.json 缺少有效 name；未修改')
    return name


def _path_value(value):
    """Strip npm-style ``file:``/``link:`` prefixes from a row or dependency value."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    lowered = candidate.lower()
    for prefix in _PATH_PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    try:
        return Path(candidate).expanduser()
    except (OSError, ValueError):
        return None


def _points_to(value, bundle):
    path = _path_value(value)
    if path is None:
        return False
    candidates = [path] if path.is_absolute() else [bundle.parent / path, Path.cwd() / path]
    for candidate in candidates:
        try:
            if candidate.resolve() == bundle:
                return True
        except OSError:
            continue
    return False


def owned_row(entry, bundle_path):
    """Recognise a patch row (or dependency record) that belongs to this bundle.

    A row is ours when its ``name`` equals the bundle package name, when its
    ``name``/``value`` resolves to the bundle directory, or when the dependency
    key equals the package name.
    """
    if not isinstance(entry, dict):
        return False
    bundle = Path(bundle_path).resolve()
    package = _package_name(bundle)
    for key in ('name', 'value', 'key'):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if package and value.strip() == package:
            return True
        if _points_to(value, bundle):
            return True
    return False


def _absolute_path(value, label):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError('%s 必须是绝对路径' % label)
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ValueError('%s 必须是绝对路径' % label)
    return str(path)


def _python(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError('python 不能为空')
    text = str(value)
    separators = [os.sep] + ([os.altsep] if os.altsep else [])
    if any(part and part in text for part in separators):
        return _absolute_path(text, 'python')
    return text


def _config_values(python, connector, helper, config_dir, server_name):
    if not isinstance(server_name, str) or not _ID.fullmatch(server_name):
        raise ValueError('server_name 必须是 1-128 位字母、数字或 _.:- 字符')
    return {
        'python': _python(python),
        'connector': _absolute_path(connector, 'connector'),
        'helper': _absolute_path(helper, 'helper'),
        'configDir': _absolute_path(config_dir, 'config_dir'),
        'serverName': server_name,
    }


def _config_json(config):
    # A compact JSON flow mapping is valid YAML and keeps the rendered value on
    # one line, so every other template or profile line stays byte-identical.
    return json.dumps(config, ensure_ascii=False)


def _indent(line):
    return len(line) - len(line.lstrip(' \t'))


def _row_start(lines, row_id):
    pattern = re.compile(r'^[ \t]*-[ \t]+id:[ \t]*[\'"]?' + re.escape(row_id) + r'[\'"]?[ \t]*(?:#.*)?$')
    return [index for index, line in enumerate(lines) if pattern.match(line.rstrip('\r\n'))]


def _row_end(lines, start):
    item_indent = _indent(lines[start])
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if _indent(lines[index]) <= item_indent:
            return index
    return len(lines)


def _row_name(text):
    match = re.search(r'^[ \t]*name:[ \t]*(.*?)[ \t]*(?:#.*)?$', text, re.M)
    if match is None:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].replace("''", "'")
    return value or None


def _config_index(lines, start, end):
    for index in range(start, end):
        line = lines[index].lstrip(' \t')
        if line.startswith('config:'):
            return index
    return None


def _replace_row_config(text, start, end, config):
    """Replace exactly one row's config value, preserving every other byte."""
    lines = text.splitlines(keepends=True)
    item_indent = _indent(lines[start])
    rendered = 'config: ' + _config_json(config) + '\n'
    index = _config_index(lines, start, end)
    if index is None:
        # Keep the row usable when the template omits an otherwise defaulted key.
        key_indent = item_indent + 2
        for position in range(start + 1, end):
            if lines[position].strip():
                key_indent = _indent(lines[position])
                break
        head = lines[:end]
        if head and not head[-1].endswith('\n'):
            head[-1] += '\n'
        return ''.join(head + [' ' * key_indent + rendered] + lines[end:])
    config_indent = _indent(lines[index])
    value = lines[index].rstrip('\r\n').split(':', 1)[1].strip()
    if value:
        return ''.join(lines[:index] + [' ' * config_indent + rendered] + lines[index + 1:])
    stop = index + 1
    while stop < end:
        line = lines[stop]
        if not line.strip() or _indent(line) <= config_indent:
            break
        stop += 1
    return ''.join(lines[:index] + [' ' * config_indent + rendered] + lines[stop:])


def render_patch_text(text, config, row_id=ROW_ID):
    """Render one inserted row's config to concrete values; refuse other shapes."""
    if not isinstance(text, str):
        raise ValueError('cordis.patch.yml 内容无法识别；未渲染')
    lines = text.splitlines(keepends=True)
    starts = _row_start(lines, row_id)
    if not starts:
        raise ValueError('cordis.patch.yml 缺少 id: %s 的插入行；未渲染' % row_id)
    if len(starts) > 1:
        raise ValueError('cordis.patch.yml 含多个 id: %s 的插入行；未渲染' % row_id)
    return _replace_row_config(text, starts[0], _row_end(lines, starts[0]), config)


def _source_directory(source_bundle):
    source = Path(source_bundle) if source_bundle is not None else Path(__file__).resolve().parent / 'bundle'
    if not source.is_dir():
        raise ValueError('bundle 模板目录不存在：%s；未渲染' % source)
    for name in (PACKAGE_JSON, PATCH_FILE):
        if not (source / name).is_file():
            raise ValueError('bundle 模板缺少 %s；未渲染' % name)
    return source.resolve()


def _destination_path(destination):
    raw = Path(destination).expanduser()
    if raw.is_symlink():
        raise ValueError('bundle 目标目录不能是符号链接；未渲染')
    target = raw.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError('bundle 目标路径已存在且不是目录；未渲染')
    return target


def _destination_mode(target, name):
    if not target.exists():
        return 'create'
    entries = list(target.iterdir())
    if not entries:
        return 'create'
    if not (target / PACKAGE_JSON).is_file():
        raise ValueError('bundle 目标目录已存在且不属于 %s；未覆盖' % name)
    try:
        existing = _read_json(target / PACKAGE_JSON)
    except (OSError, ValueError, UnicodeDecodeError):
        raise ValueError('bundle 目标目录的 package.json 无法识别；未覆盖')
    if not isinstance(existing, dict) or existing.get('name') != name:
        raise ValueError('bundle 目标目录已被其他组件占用；未覆盖')
    return 'update'


def _template_files(source):
    files = []
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        parts = relative.parts
        if parts[0] in _SKIPPED_DIRECTORIES or any(part in _SKIPPED_DIRECTORIES for part in parts[:-1]):
            continue
        if path.name in _SKIPPED_NAMES:
            continue
        files.append(relative.as_posix())
    return files


def _write_bundle(source, target, patch_text, files):
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.quietbridge-dsh-', dir=str(target.parent)))
    payload = staging / 'payload'
    try:
        for relative in files:
            source_file = source / relative
            destination_file = payload / relative
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file)
        (payload / PATCH_FILE).write_text(patch_text, encoding='utf-8')
        if target.exists():
            previous = staging / 'previous'
            target.rename(previous)
            try:
                payload.rename(target)
            except BaseException:
                previous.rename(target)
                raise
            shutil.rmtree(previous, ignore_errors=True)
        else:
            payload.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def bundle_render(source_bundle=None, destination=None, *, python, connector, helper,
                  config_dir, server_name=SERVER_NAME, apply=False):
    """Validate and render the bundle template into a concrete install directory.

    ``apply=False`` (the default) is a pure preview: nothing is written and the
    returned plan describes what applying would do. ``apply=True`` copies the
    template (excluding tests) and writes the rendered ``cordis.patch.yml``.
    An existing destination that is not this exact bundle is refused, so a
    repeated install updates this product in place.
    """
    if destination is None:
        if source_bundle is None:
            raise ValueError('必须指定 bundle 目标目录；未渲染')
        source_bundle, destination = None, source_bundle
    source = _source_directory(source_bundle)
    target = _destination_path(destination)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('bundle 目标目录不能与模板目录重叠；未渲染')
    name = bundle_name(source)
    config = _config_values(python, connector, helper, config_dir, server_name)
    template_patch = _read_text(source / PATCH_FILE)
    template_lines = template_patch.splitlines(keepends=True)
    template_starts = _row_start(template_lines, ROW_ID)
    template_name = _row_name(''.join(template_lines[template_starts[0]:])) if template_starts else None
    if template_name is not None and template_name != name:
        raise ValueError('bundle 模板 patch 行的 name 与 package.json 不一致；未渲染')
    rendered_patch = render_patch_text(template_patch, config)
    mode = _destination_mode(target, name)
    files = _template_files(source)
    plan = {'mode': mode, 'bundlePath': str(target), 'patch': rendered_patch,
            'files': files, 'config': config, 'serverName': server_name, 'name': name}
    if not apply:
        return plan
    _write_bundle(source, target, rendered_patch, files)
    plan['applied'] = True
    return plan


def _parse_json(original, label):
    try:
        return json.loads(original.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('%s 格式无法识别；未修改' % label)


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _owned_dependency(dependencies, bundle, name):
    if isinstance(dependencies.get(name), str):
        return name
    for key, value in dependencies.items():
        if isinstance(value, str) and _points_to(value, bundle):
            return key
    return None


def _package_plan(profile, bundle, name):
    path = Path(profile) / PACKAGE_JSON
    original = _read_bytes(path)
    if original is None:
        raise ValueError('profile 缺少 package.json；未修改')
    data = _parse_json(original, 'package.json')
    if not isinstance(data, dict):
        raise ValueError('package.json 格式无法识别；未修改')
    dependencies = data.setdefault('dependencies', {})
    if not isinstance(dependencies, dict):
        raise ValueError('package.json dependencies 格式无法识别；未修改')
    key = _owned_dependency(dependencies, bundle, name)
    if key is None:
        key = name
    dependencies[key] = 'link:' + str(bundle.resolve())
    return path, original, _json_bytes(data)


def _find_patch_rows(lines, bundle):
    """Return the owned insert row as (start, end, text), or None; validate owners."""
    starts = _row_start(lines, ROW_ID)
    owned = []
    for start in starts:
        end = _row_end(lines, start)
        row_text = ''.join(lines[start:end])
        name = _row_name(row_text)
        if name is not None and not owned_row({'name': name}, bundle):
            raise ValueError('cordis.patch.yml 中 quietbridge-dsh 行已被其他组件占用；未修改')
        if name is not None:
            owned.append((start, end, row_text))
    if len(owned) > 1:
        raise ValueError('cordis.patch.yml 含多个 quietbridge-dsh 插入行；未修改')
    return (owned[0] if owned else None)


def _yaml_string(value):
    return "'" + value.replace("'", "''") + "'"


def _append_insert(text, name, config):
    lines = []
    if text and not text.endswith('\n'):
        lines.append('\n')
    if text:
        lines.append('\n')
    lines.append('# QuietBridge for DeepSeek Harness：按 Agent 注入可信身份的后台界面操作。\n')
    lines.append('- insert:\n')
    lines.append('    - id: %s\n' % ROW_ID)
    lines.append('      name: %s\n' % _yaml_string(name))
    lines.append('      config: %s\n' % _config_json(config))
    return text + ''.join(lines)


def _profile_patch_plan(profile, bundle, name, config):
    path = Path(profile) / PATCH_FILE
    original = _read_bytes(path)
    text = ''
    if original is not None:
        try:
            text = original.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('cordis.patch.yml 不是 UTF-8 文本；未修改')
    lines = text.splitlines(keepends=True)
    row = _find_patch_rows(lines, bundle)
    if row is None:
        rendered = _append_insert(text, name, config)
    else:
        start, end, _ = row
        rendered = _replace_row_config(text, start, end, config)
    return path, original, rendered.encode('utf-8')


def profile_plan(profile, bundle_path, server_name=SERVER_NAME, config=None):
    """Return the profile package.json and cordis.patch.yml merge plan.

    Pure planning: nothing is written and no package manager runs. The bundle
    must already contain its rendered ``cordis.patch.yml`` (see
    :func:`bundle_render`) unless an explicit ``config`` mapping is passed.
    """
    profile = Path(profile)
    bundle = Path(bundle_path)
    if not bundle.is_dir():
        raise ValueError('bundle 目录不存在：%s；未修改' % bundle)
    name = bundle_name(bundle)
    if config is None:
        config = _rendered_bundle_config(bundle)
        if config is None:
            raise ValueError(
                '无法从 bundle 的 cordis.patch.yml 读取已渲染的 config；'
                '请先 bundle_render 或显式提供 config')
    if not isinstance(config, dict):
        raise ValueError('config 必须是对象；未修改')
    config = dict(config)
    config.setdefault('serverName', server_name)
    package = _package_plan(profile, bundle, name)
    patch = _profile_patch_plan(profile, bundle, name, config)
    return {'packageJson': package, 'patch': patch}


def _rendered_bundle_config(bundle):
    path = Path(bundle) / PATCH_FILE
    if not path.is_file():
        return None
    lines = _read_text(path).splitlines(keepends=True)
    starts = _row_start(lines, ROW_ID)
    if not starts:
        return None
    end = _row_end(lines, starts[0])
    index = _config_index(lines, starts[0], end)
    if index is None:
        return None
    value = lines[index].rstrip('\r\n').split(':', 1)[1].strip()
    if not value.startswith('{'):
        return None
    try:
        config = json.loads(value)
    except ValueError:
        return None
    return dict(config) if isinstance(config, dict) else None
