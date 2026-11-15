"""One source list for native packaging and the source installer."""
from pathlib import Path
import shutil


def bridge_sources(root):
    root = Path(root)
    core = root / 'services/mcp'
    files = {Path(name): core / name for name in (
        'server.py', 'browser_events.py', 'recovery.py', 'connector.py',
        'task_lifecycle.py', 'stdio_transport.py', 'lifecycle_store.py', 'compatibility.py',
        'session_routing.py', 'session_registry.py', 'preview_metadata.py', 'browser_preview.py',
        'assets/logo-64.png')}
    for path in sorted((root / 'adapters').rglob('*.py')):
        if '__pycache__' not in path.parts and not any(part.startswith('test') for part in path.relative_to(root / 'adapters').parts):
            files[path.relative_to(root)] = path
    # Host bundle payloads (DeepSeek Harness) ship next to the adapter modules so
    # an installed bridge can render an installable bundle without the source
    # checkout. Runtime files only: tests, caches and editor droppings stay out.
    bundle = root / 'adapters/deepseek_harness/bundle'
    if bundle.is_dir():
        for suffix in ('.js', '.json', '.yml', '.yaml', '.svg', '.md'):
            for path in sorted(bundle.rglob('*' + suffix)):
                relative = path.relative_to(bundle)
                if '__pycache__' in path.parts or any(part == 'test' or part == 'tests'
                                                      for part in relative.parts):
                    continue
                files[path.relative_to(root)] = path
    return files


def copy_bridge(root, destination):
    for relative, source in bridge_sources(root).items():
        target = Path(destination) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
