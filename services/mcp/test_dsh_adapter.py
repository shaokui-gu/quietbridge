"""DeepSeek Harness adapter tests: isolated directories, no real profile, no network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from adapters.deepseek_harness import configuration, guidance, lifecycle
from lifecycle_store import MAX_BYTES

PACKAGE_NAME = '@quietbridge/dsh'
SERVER_NAME = 'quietbridge'

TEMPLATE_PATCH = """\
# QuietBridge for DeepSeek Harness bundle template.
- insert:
    - id: quietbridge-dsh
      name: '@quietbridge/dsh'
      config:
        python: !!js process.env.QUIETBRIDGE_DSH_PYTHON || 'python3'
        connector: !!js process.env.QUIETBRIDGE_DSH_CONNECTOR
        helper: !!js process.env.QUIETBRIDGE_DSH_HELPER
        configDir: !!js process.env.QUIETBRIDGE_DSH_CONFIG_DIR
        serverName: !!js process.env.QUIETBRIDGE_DSH_SERVER_NAME || 'quietbridge'
"""

# Captured shape of a real desktop profile written by `dsh plugin add` (paths
# anonymised): the CLI adds a dependency and selects the bundle in
# dsh.profile.bundles while keeping every existing dependency and selection.
# QuietBridge is not selected yet, so planning must append it last.
CLI_PROFILE_PACKAGE_JSON = """\
{
  "name": "dsh-profile-desktop",
  "private": true,
  "dependencies": {
    "@deepseek-ai/dsh-cua-bundle": "0.2.2-alpha.2",
    "@local/cua-native-helper-config": "link:/opt/fixtures/cua-helper-config",
    "dsh-figma-mcp": "file:/opt/fixtures/dsh-figma-mcp-0.1.0.tgz"
  },
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-web-app",
        "@deepseek-ai/dsh-experimental-auto-review",
        "@deepseek-ai/dsh-experimental-agent-team-profile",
        "@deepseek-ai/dsh-experimental-tool-worktree",
        "@deepseek-ai/dsh-experimental-cot-translation-bundle",
        "@deepseek-ai/dsh-experimental-session-titles-bundle",
        "@deepseek-ai/dsh-cua-bundle"
      ]
    }
  }
}
"""


def make_template(base):
    source = base / 'bundle'
    (source / 'locale').mkdir(parents=True)
    (source / 'test').mkdir()
    (source / 'package.json').write_text(json.dumps({
        'name': PACKAGE_NAME, 'version': '1.2.3', 'private': True, 'type': 'module',
        'exports': {'.': './index.js', './locale/*.json': './locale/*.json',
                    './icon': './icon.svg'},
        'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
    }, indent=2) + '\n')
    (source / 'index.js').write_text('export function apply() {}\n')
    (source / 'locale/zh.json').write_text('{"meta": {"title": "QuietBridge"}}\n')
    (source / 'icon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n')
    (source / 'cordis.patch.yml').write_text(TEMPLATE_PATCH)
    (source / 'test/connector.test.mjs').write_text('// not installed\n')
    return source


def make_installed_bundle(base, config):
    bundle = base / 'installed-bundle'
    bundle.mkdir()
    (bundle / 'package.json').write_text(json.dumps({
        'name': PACKAGE_NAME, 'version': '1.2.3', 'private': True, 'type': 'module',
        'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
    }, indent=2) + '\n')
    (bundle / 'index.js').write_text('export function apply() {}\n')
    (bundle / 'cordis.patch.yml').write_text(
        '- insert:\n'
        '    - id: quietbridge-dsh\n'
        "      name: '%s'\n"
        '      config: %s\n' % (PACKAGE_NAME, json.dumps(config, ensure_ascii=False)))
    return bundle


def parse_config(patch_text):
    for line in patch_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('config: {'):
            return json.loads(stripped.split(':', 1)[1].strip())
    raise AssertionError('rendered patch has no rendered config row')


class BundleRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = make_template(self.base)
        self.connector = str(self.base / 'bridge/server.py')
        self.helper = str(self.base / 'QuietBridge Helper.app/Contents/MacOS/helper')
        self.config_dir = str(self.base / 'QuietBridge Config')
        self.arguments = dict(python=sys.executable, connector=self.connector,
                              helper=self.helper, config_dir=self.config_dir)

    def render(self, destination=None, apply=True, source=None):
        return configuration.bundle_render(
            source or self.source, destination or (self.base / 'install'),
            apply=apply, **self.arguments)

    def test_preview_renders_plan_without_writing(self):
        destination = self.base / 'install'
        plan = self.render(destination, apply=False)
        self.assertFalse(destination.exists())
        self.assertEqual(plan['mode'], 'create')
        self.assertEqual(plan['bundlePath'], str(destination.resolve()))
        self.assertEqual(plan['name'], PACKAGE_NAME)
        self.assertEqual(plan['serverName'], SERVER_NAME)
        self.assertIn('package.json', plan['files'])
        self.assertIn('index.js', plan['files'])
        self.assertIn('cordis.patch.yml', plan['files'])
        self.assertIn('locale/zh.json', plan['files'])
        self.assertFalse([name for name in plan['files'] if name.startswith('test/')])
        self.assertEqual(plan['config'], {
            'python': sys.executable, 'connector': self.connector,
            'helper': self.helper, 'configDir': self.config_dir,
            'serverName': SERVER_NAME})
        self.assertEqual(parse_config(plan['patch']), plan['config'])
        self.assertIn("name: '@quietbridge/dsh'", plan['patch'])
        self.assertIn('- insert:', plan['patch'])
        self.assertNotIn('!!js', plan['patch'])

    def test_apply_creates_idempotently_and_updates_in_place(self):
        destination = self.base / 'install'
        plan = self.render(destination)
        self.assertEqual(plan['mode'], 'create')
        self.assertTrue(plan['applied'])
        self.assertEqual((destination / 'cordis.patch.yml').read_text(), plan['patch'])
        self.assertEqual(parse_config((destination / 'cordis.patch.yml').read_text()),
                         plan['config'])
        self.assertTrue((destination / 'index.js').is_file())
        self.assertFalse((destination / 'test').exists())
        self.assertFalse((destination / 'test').is_dir())

        (destination / 'index.js').write_text('stale')
        again = self.render(destination)
        self.assertEqual(again['mode'], 'update')
        self.assertEqual((destination / 'index.js').read_text(),
                         (self.source / 'index.js').read_text())
        self.assertEqual((destination / 'cordis.patch.yml').read_text(), again['patch'])
        self.assertEqual((destination / 'cordis.patch.yml').read_text().count('id: quietbridge-dsh'), 1)

    def test_existing_foreign_destination_is_refused(self):
        destination = self.base / 'install'
        destination.mkdir()
        (destination / 'package.json').write_text('{"name": "@other/plugin"}\n')
        (destination / 'keep.txt').write_text('keep')
        with self.assertRaises(ValueError):
            self.render(destination)
        with self.assertRaises(ValueError):
            self.render(destination, apply=False)
        self.assertEqual((destination / 'keep.txt').read_text(), 'keep')
        self.assertEqual((destination / 'package.json').read_text(),
                         '{"name": "@other/plugin"}\n')

    def test_empty_existing_destination_is_adopted(self):
        destination = self.base / 'install'
        destination.mkdir()
        plan = self.render(destination)
        self.assertEqual(plan['mode'], 'create')
        self.assertTrue((destination / 'package.json').is_file())

    def test_relative_paths_are_refused_before_writing(self):
        destination = self.base / 'install'
        for keyword, value in (('connector', 'bridge/server.py'),
                               ('helper', 'QuietBridge Helper'),
                               ('config_dir', 'config'),
                               ('python', 'venv/bin/python3')):
            arguments = dict(self.arguments, **{keyword: value})
            with self.assertRaises(ValueError):
                configuration.bundle_render(self.source, destination, apply=True, **arguments)
            self.assertFalse(destination.exists())
        # A bare interpreter name is a valid command, not a relative path.
        plan = configuration.bundle_render(
            self.source, destination, **dict(self.arguments, python='python3'))
        self.assertEqual(plan['config']['python'], 'python3')

    def test_invalid_server_name_is_refused(self):
        with self.assertRaises(ValueError):
            configuration.bundle_render(
                self.source, self.base / 'install', **dict(self.arguments, server_name='bad name'))

    def test_template_without_the_row_is_refused(self):
        broken = self.base / 'broken-bundle'
        broken.mkdir()
        (broken / 'package.json').write_text('{"name": "@quietbridge/dsh"}\n')
        (broken / 'cordis.patch.yml').write_text('- insert:\n    - id: other\n')
        with self.assertRaises(ValueError):
            self.render(self.base / 'install', source=broken)
        self.assertFalse((self.base / 'install').exists())

    def test_symlink_destination_is_refused(self):
        real = self.base / 'real-install'
        real.mkdir()
        (real / 'package.json').write_text('{"name": "@quietbridge/dsh"}\n')
        link = self.base / 'link-install'
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            self.render(link)

    def test_adjacent_bundle_template_is_the_default_source(self):
        template = Path(configuration.__file__).resolve().parent / 'bundle'
        if not ((template / 'package.json').is_file() and
                (template / 'cordis.patch.yml').is_file()):
            self.skipTest('bundle 模板由 dsh-bundle 提供，尚未生成')
        plan = configuration.bundle_render(
            self.base / 'default-install', **self.arguments)
        self.assertEqual(plan['mode'], 'create')
        self.assertIn('package.json', plan['files'])
        self.assertEqual(parse_config(plan['patch'])['connector'], self.connector)


class ProfilePlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.profile = self.base / 'profile'
        self.profile.mkdir()
        self.write_package({
            'name': 'dsh-profile-desktop', 'private': True,
            'dependencies': {'left-pad': '1.3.0', 'right-pad': 'file:/tmp/right-pad.tgz'},
            'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base']}},
        })
        self.write_patch(
            '# user patch layer\n'
            '- id: ui-theme\n'
            "  name: '@deepseek-ai/dsh-client-ui-theme'\n"
            '  config:\n'
            '    preference: dark\n')
        self.bundle_config = {
            'python': sys.executable, 'connector': '/opt/quietbridge/server.py',
            'helper': '/Applications/QuietBridge Helper.app/Contents/MacOS/helper',
            'configDir': '/Users/example/Library/Application Support/QuietBridge/config',
            'serverName': SERVER_NAME}
        self.bundle = make_installed_bundle(self.base, self.bundle_config)

    def write_package(self, data):
        (self.profile / 'package.json').write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + '\n')

    def write_patch(self, text):
        (self.profile / 'cordis.patch.yml').write_text(text)

    def read_file(self, name):
        return (self.profile / name).read_bytes()

    def test_adds_dependency_and_insert_row_without_touching_other_lines(self):
        package_before = self.read_file('package.json')
        patch_before = self.read_file('cordis.patch.yml')
        plan = configuration.profile_plan(self.profile, self.bundle)
        self.assertEqual(plan['packageJson'][0], self.profile / 'package.json')
        self.assertEqual(plan['packageJson'][1], package_before)
        self.assertEqual(plan['patch'][0], self.profile / 'cordis.patch.yml')
        self.assertEqual(plan['patch'][1], patch_before)
        # Pure planning never writes the profile.
        self.assertEqual(self.read_file('package.json'), package_before)
        self.assertEqual(self.read_file('cordis.patch.yml'), patch_before)

        data = json.loads(plan['packageJson'][2])
        self.assertEqual(data['dependencies'][PACKAGE_NAME],
                         'link:' + str(self.bundle.resolve()))
        self.assertEqual(data['dependencies']['left-pad'], '1.3.0')
        self.assertEqual(data['dsh'], {'profile': {'bundles': [
            '@deepseek-ai/dsh-base', PACKAGE_NAME]}})
        self.assertTrue(plan['packageJson'][2].endswith(b'\n'))

        rendered = plan['patch'][2].decode()
        self.assertTrue(rendered.startswith(patch_before.decode()))
        self.assertIn('- insert:', rendered)
        self.assertIn('id: quietbridge-dsh', rendered)
        self.assertIn("name: '@quietbridge/dsh'", rendered)
        self.assertEqual(parse_config(rendered), self.bundle_config)

    def test_existing_dependency_is_updated_in_place(self):
        self.write_package({
            'name': 'dsh-profile-desktop',
            'dependencies': {'alpha': '1.0.0', PACKAGE_NAME: 'link:/old/bundle',
                             'omega': '2.0.0'},
        })
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(list(data['dependencies']), ['alpha', PACKAGE_NAME, 'omega'])
        self.assertEqual(data['dependencies'][PACKAGE_NAME],
                         'link:' + str(self.bundle.resolve()))

    def test_missing_dsh_section_is_created(self):
        self.write_package({'name': 'dsh-profile-desktop',
                            'dependencies': {'left-pad': '1.3.0'}})
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(list(data['dependencies']), ['left-pad', PACKAGE_NAME])
        self.assertEqual(data['dsh'], {'profile': {'bundles': [PACKAGE_NAME]}})

    def test_missing_profile_section_keeps_other_dsh_keys(self):
        self.write_package({
            'name': 'dsh-profile-desktop', 'dependencies': {},
            'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
        })
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(data['dsh'], {
            'bundle': {'patch': './cordis.patch.yml'},
            'profile': {'bundles': [PACKAGE_NAME]},
        })

    def test_missing_bundles_list_is_created_beside_other_profile_keys(self):
        self.write_package({
            'name': 'dsh-profile-desktop', 'dependencies': {},
            'dsh': {'profile': {'theme': 'dark'}},
        })
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(data['dsh']['profile']['theme'], 'dark')
        self.assertEqual(data['dsh']['profile']['bundles'], [PACKAGE_NAME])

    def test_cli_shaped_profile_appends_bundle_selection_last(self):
        fixture_path = self.profile / 'package.json'
        fixture_path.write_text(CLI_PROFILE_PACKAGE_JSON)
        fixture = json.loads(CLI_PROFILE_PACKAGE_JSON)
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])

        self.assertEqual(list(data['dependencies']),
                         list(fixture['dependencies']) + [PACKAGE_NAME])
        for package, value in fixture['dependencies'].items():
            self.assertEqual(data['dependencies'][package], value)
        self.assertEqual(data['dependencies'][PACKAGE_NAME],
                         'link:' + str(self.bundle.resolve()))

        selected = data['dsh']['profile']['bundles']
        self.assertEqual(selected, fixture['dsh']['profile']['bundles'] + [PACKAGE_NAME])
        self.assertEqual(selected[:-1], fixture['dsh']['profile']['bundles'])
        self.assertEqual(data['dsh']['profile']['bundles'].count(PACKAGE_NAME), 1)
        self.assertEqual(data['dsh'], {'profile': {'bundles': selected}})
        # Planning stays pure: the fixture on disk is untouched.
        self.assertEqual(fixture_path.read_text(), CLI_PROFILE_PACKAGE_JSON)

    def test_existing_selection_keeps_its_position(self):
        self.write_package({
            'name': 'dsh-profile-desktop', 'dependencies': {},
            'dsh': {'profile': {'bundles': [
                '@deepseek-ai/dsh-base', PACKAGE_NAME, '@deepseek-ai/dsh-web-app']}},
        })
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(data['dsh']['profile']['bundles'],
                         ['@deepseek-ai/dsh-base', PACKAGE_NAME,
                          '@deepseek-ai/dsh-web-app'])

    def test_other_dsh_profile_keys_are_preserved(self):
        self.write_package({
            'name': 'dsh-profile-desktop', 'dependencies': {},
            'dsh': {'profile': {
                'bundles': ['@deepseek-ai/dsh-base'],
                'theme': 'dark',
                'extra': {'nested': [1, 2]},
            }},
        })
        plan = configuration.profile_plan(self.profile, self.bundle)
        data = json.loads(plan['packageJson'][2])
        self.assertEqual(data['dsh']['profile']['theme'], 'dark')
        self.assertEqual(data['dsh']['profile']['extra'], {'nested': [1, 2]})
        self.assertEqual(data['dsh']['profile']['bundles'],
                         ['@deepseek-ai/dsh-base', PACKAGE_NAME])

    def test_replanning_the_rendered_package_is_idempotent(self):
        (self.profile / 'package.json').write_text(CLI_PROFILE_PACKAGE_JSON)
        first = configuration.profile_plan(self.profile, self.bundle)
        repeat = configuration.profile_plan(self.profile, self.bundle)
        self.assertEqual(repeat['packageJson'][2], first['packageJson'][2])

        # Simulate deployment of exactly the planned bytes, then plan again.
        (self.profile / 'package.json').write_bytes(first['packageJson'][2])
        second = configuration.profile_plan(self.profile, self.bundle)
        self.assertEqual(second['packageJson'][1], first['packageJson'][2])
        self.assertEqual(second['packageJson'][2], first['packageJson'][2])
        data = json.loads(second['packageJson'][2])
        self.assertEqual(data['dsh']['profile']['bundles'].count(PACKAGE_NAME), 1)

    def test_invalid_dsh_selection_structures_are_refused(self):
        cases = [
            {'dsh': []},
            {'dsh': None},
            {'dsh': '@quietbridge/not-a-map'},
            {'dsh': {'profile': []}},
            {'dsh': {'profile': {'bundles': {}}}},
            {'dsh': {'profile': {'bundles': PACKAGE_NAME}}},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write_package(dict({'name': 'dsh-profile-desktop',
                                         'dependencies': {'left-pad': '1.3.0'}}, **case))
                before = self.read_file('package.json')
                with self.assertRaises(ValueError) as raised:
                    configuration.profile_plan(self.profile, self.bundle)
                self.assertIn('格式无法识别', str(raised.exception))
                self.assertIn('未修改', str(raised.exception))
                self.assertEqual(self.read_file('package.json'), before)

    def test_existing_owned_row_config_is_replaced_in_place(self):
        original = (
            '# user patch layer\n'
            '- id: ui-theme\n'
            "  name: '@deepseek-ai/dsh-client-ui-theme'\n"
            '  config:\n'
            '    preference: dark\n'
            '- insert:\n'
            '    - id: quietbridge-dsh\n'
            "      name: '@quietbridge/dsh'\n"
            '      config: {"python": "/old/python3", "connector": "/old/server.py", '
            '"helper": "/old/helper", "configDir": "/old/config", "serverName": "quietbridge"}\n'
            '- id: after-row\n'
            "  name: 'example/after'\n"
            '  config:\n'
            '    enabled: true\n')
        self.write_patch(original)
        plan = configuration.profile_plan(self.profile, self.bundle)
        rendered = plan['patch'][2].decode()
        self.assertEqual(rendered.count('id: quietbridge-dsh'), 1)
        self.assertEqual(rendered.count('- insert:'), original.count('- insert:'))
        self.assertEqual(parse_config(rendered), self.bundle_config)
        self.assertIn("      name: '@quietbridge/dsh'", rendered)
        for preserved in ("  name: '@deepseek-ai/dsh-client-ui-theme'\n", '    preference: dark\n',
                          '- id: after-row\n', '    enabled: true\n'):
            self.assertIn(preserved, rendered)

    def test_foreign_row_with_our_id_is_refused(self):
        original = (
            '- id: quietbridge-dsh\n'
            "  name: '@other/plugin'\n"
            '  config: {}\n'
            '- id: ui-theme\n'
            "  name: '@deepseek-ai/dsh-client-ui-theme'\n")
        self.write_patch(original)
        with self.assertRaises(ValueError):
            configuration.profile_plan(self.profile, self.bundle)
        self.assertEqual(self.read_file('cordis.patch.yml'), original.encode())

    def test_unowned_override_row_is_preserved_and_insert_is_appended(self):
        original = ('- id: quietbridge-dsh\n'
                    '  disabled: true\n'
                    '- id: ui-theme\n'
                    "  name: '@deepseek-ai/dsh-client-ui-theme'\n")
        self.write_patch(original)
        plan = configuration.profile_plan(self.profile, self.bundle)
        rendered = plan['patch'][2].decode()
        self.assertTrue(rendered.startswith(original))
        self.assertEqual(rendered.count('id: quietbridge-dsh'), 2)
        self.assertIn('disabled: true', rendered)
        self.assertEqual(parse_config(rendered.rsplit('- insert:', 1)[1]), self.bundle_config)

    def test_missing_patch_file_is_created(self):
        (self.profile / 'cordis.patch.yml').unlink()
        plan = configuration.profile_plan(self.profile, self.bundle)
        self.assertIsNone(plan['patch'][1])
        rendered = plan['patch'][2].decode()
        self.assertTrue(rendered.startswith('# QuietBridge'))
        self.assertIn('- insert:\n', rendered)
        self.assertEqual(parse_config(rendered), self.bundle_config)

    def test_missing_package_json_is_refused(self):
        (self.profile / 'package.json').unlink()
        with self.assertRaises(ValueError):
            configuration.profile_plan(self.profile, self.bundle)

    def test_unreadable_rendered_config_requires_explicit_config(self):
        template = make_template(self.base)
        with self.assertRaises(ValueError):
            configuration.profile_plan(self.profile, template)
        plan = configuration.profile_plan(self.profile, template, config=self.bundle_config)
        self.assertEqual(parse_config(plan['patch'][2].decode()), self.bundle_config)

    def test_owned_row_accepts_package_name_and_bundle_paths(self):
        bundle = self.bundle.resolve()
        self.assertTrue(configuration.owned_row({'name': PACKAGE_NAME}, bundle))
        self.assertTrue(configuration.owned_row({'name': str(bundle)}, bundle))
        self.assertTrue(configuration.owned_row({'name': 'link:' + str(bundle)}, bundle))
        self.assertTrue(configuration.owned_row({'value': 'file:' + str(bundle)}, bundle))
        self.assertFalse(configuration.owned_row({'name': 'unrelated'}, bundle))
        self.assertFalse(configuration.owned_row({'name': '@other/plugin'}, bundle))
        self.assertFalse(configuration.owned_row({}, bundle))
        self.assertFalse(configuration.owned_row(None, bundle))

    def test_duplicate_owned_insert_rows_are_refused(self):
        row = ('- insert:\n'
               '    - id: quietbridge-dsh\n'
               "      name: '@quietbridge/dsh'\n"
               '      config: {}\n')
        self.write_patch(row + row)
        with self.assertRaises(ValueError):
            configuration.profile_plan(self.profile, self.bundle)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name).resolve() / 'config'
        self.config.mkdir()
        self.session = 'session-' + str(uuid.uuid4())
        self.agent = 'agent-' + str(uuid.uuid4())
        self.turn = self.session + ':1'
        self.task = {'taskId': str(uuid.uuid4()), 'state': 'bound',
                     'lease': {'ownerId': str(uuid.uuid4()), 'expiresAt': 1120,
                               'idleSeconds': 120}}

    def send(self, event, now=1000, session=None, agent=None, **fields):
        payload = {'event': event, 'sessionId': session or self.session,
                   'agentId': agent or self.agent}
        payload.update(fields)
        return lifecycle.handle_event(payload, self.config, now=now)

    def state(self):
        return lifecycle.read(self.config / 'host-lifecycle/state.json', {})

    def turns(self):
        return self.state()['turns']

    def test_turn_key_is_host_scoped_and_deterministic(self):
        key = lifecycle.turn_key(self.session, self.agent, self.turn)
        expected = hashlib.sha256(
            ('deepseek-harness\0%s\0%s\0%s' % (self.session, self.agent, self.turn)).encode()
        ).hexdigest()
        self.assertEqual(key, expected)
        self.assertEqual(key, lifecycle.turn_key(self.session, self.agent, self.turn))
        self.assertNotEqual(key, lifecycle.turn_key(self.session, 'other-agent', self.turn))
        self.assertNotEqual(key, hashlib.sha256(
            (self.session + '\0' + self.turn).encode()).hexdigest())

    def test_turn_start_creates_a_host_tagged_turn(self):
        self.assertIsNone(self.send('turn-start', turnId=self.turn))
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual(turn['host'], 'deepseek-harness')
        self.assertEqual(turn['sessionId'], self.session)
        self.assertEqual(turn['agentId'], self.agent)
        self.assertEqual(turn['requestId'], self.turn)
        self.assertEqual((turn['startedAt'], turn['updatedAt']), (1000, 1000))
        self.assertFalse(turn['sessionEnded'])
        self.assertIsNone(turn.get('stoppedAt'))
        self.assertEqual(turn['tasks'], {})

    def test_turn_end_normalizes_reason_and_keeps_first_stop(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('turn-end', now=1010, turnId=self.turn, reason='cancelled')
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual((turn['stoppedAt'], turn['reason']), (1010, 'cancelled'))
        self.send('turn-end', now=1020, turnId=self.turn, reason='completed')
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual((turn['stoppedAt'], turn['reason']), (1010, 'cancelled'))
        self.assertEqual(turn['updatedAt'], 1010)

    def test_unknown_end_reason_becomes_ended(self):
        turn = self.session + ':2'
        self.send('turn-start', now=1000, turnId=turn)
        self.send('turn-end', now=1010, turnId=turn, reason='blocked')
        record = self.turns()[lifecycle.turn_key(self.session, self.agent, turn)]
        self.assertEqual(record['reason'], 'ended')

    def test_late_turn_start_does_not_revive_a_stopped_turn(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('turn-end', now=1010, turnId=self.turn, reason='cancelled')
        self.send('turn-start', now=1020, turnId=self.turn)
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual((turn['stoppedAt'], turn['reason']), (1010, 'cancelled'))

    def test_new_turn_after_a_stop_is_active(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('turn-end', now=1010, turnId=self.turn, reason='completed')
        fresh = self.session + ':2'
        self.send('turn-start', now=1020, turnId=fresh)
        turns = self.turns()
        self.assertEqual(turns[lifecycle.turn_key(self.session, self.agent, self.turn)]['stoppedAt'],
                         1010)
        self.assertIsNone(
            turns[lifecycle.turn_key(self.session, self.agent, fresh)].get('stoppedAt'))

    def test_late_old_events_do_not_reorder_ahead_of_a_newer_turn(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('turn-end', now=1010, turnId=self.turn, reason='completed')
        old_key = lifecycle.turn_key(self.session, self.agent, self.turn)
        fresh = self.session + ':2'
        self.send('turn-start', now=1020, turnId=fresh)
        # A delayed end and a delayed receipt for the old turn must not make the
        # stopped record look newer than the live turn.
        self.send('turn-end', now=1030, turnId=self.turn, reason='cancelled')
        self.send('tool-result', now=1040, turnId=self.turn,
                  toolName='quietbridge_task', toolInput={'action': 'begin'},
                  toolResult={'content': json.dumps(self.task)})
        turns = self.turns()
        old = turns[old_key]
        live = turns[lifecycle.turn_key(self.session, self.agent, fresh)]
        self.assertEqual((old['stoppedAt'], old['reason'], old['updatedAt']),
                         (1010, 'completed', 1010))
        self.assertEqual(live['updatedAt'], 1020)
        self.assertIsNone(live.get('stoppedAt'))
        self.assertIn(self.task['taskId'], old['tasks'])
        self.assertIs(max(turns.values(), key=lambda turn: turn['updatedAt']), live)

    def test_turn_end_without_start_still_records_a_stop(self):
        self.send('turn-end', now=1010, turnId=self.turn, reason='error')
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual((turn['stoppedAt'], turn['reason']), (1010, 'error'))
        self.assertEqual(turn['startedAt'], 1010)

    def test_agent_disposed_stops_its_runs_and_writes_a_tombstone(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('turn-end', now=1005, turnId=self.turn, reason='cancelled')
        second = self.session + ':2'
        self.send('turn-start', now=1006, turnId=second)
        other_session = 'session-' + str(uuid.uuid4())
        self.send('turn-start', now=1007, turnId=other_session + ':1', session=other_session)
        self.send('agent-disposed', now=1020)
        turns = self.turns()
        first = turns[lifecycle.turn_key(self.session, self.agent, self.turn)]
        second_record = turns[lifecycle.turn_key(self.session, self.agent, second)]
        self.assertTrue(first['sessionEnded'])
        self.assertTrue(second_record['sessionEnded'])
        self.assertEqual(first['reason'], 'cancelled')
        self.assertEqual((second_record['stoppedAt'], second_record['reason']),
                         (1020, 'host_session_end'))
        tombstone = lifecycle.tombstone(self.config, self.session, self.agent)
        self.assertIsNotNone(tombstone)
        self.assertEqual(turns[lifecycle.session_key(self.session, self.agent)], tombstone)
        self.assertTrue(tombstone['sessionEnded'])
        self.assertIsNone(tombstone['requestId'])
        self.assertEqual((tombstone['stoppedAt'], tombstone['reason']),
                         (1020, 'host_session_end'))
        untouched = turns[lifecycle.turn_key(other_session, self.agent, other_session + ':1')]
        self.assertIsNone(untouched.get('stoppedAt'))
        self.assertFalse(untouched['sessionEnded'])

    def test_agent_disposed_before_late_start_keeps_the_turn_stopped(self):
        self.send('agent-disposed', now=1010)
        fresh = self.session + ':2'
        self.send('turn-start', now=1020, turnId=fresh)
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, fresh)]
        self.assertEqual((turn['stoppedAt'], turn['reason'], turn['sessionEnded']),
                         (1010, 'host_session_end', True))

    def test_tool_result_records_a_task_receipt_only(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('tool-result', now=1001, turnId=self.turn,
                  toolName='mcp__quietbridge__quietbridge_task',
                  toolInput={'action': 'begin', 'targetSurface': 'desktop_app'},
                  toolResult={'content': [{'type': 'text', 'text': json.dumps(self.task)}]})
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual(turn['tasks'], {self.task['taskId']: self.task['lease']['ownerId']})
        self.assertEqual(turn['taskId'], self.task['taskId'])
        self.assertEqual(turn['ownerId'], self.task['lease']['ownerId'])
        self.assertEqual(turn['lastActivityAt'], 1001)
        self.assertEqual(lifecycle.binding(self.config, self.task), turn)

    def test_tool_result_without_usable_receipt_creates_nothing(self):
        receipts = [
            ('mcp__quietbridge__quietbridge_task', {'action': 'status'}, {'content': json.dumps(self.task)}),
            ('mcp__quietbridge__quietbridge_task', {'action': 'begin'},
             {'isError': True, 'content': json.dumps(self.task)}),
            ('mcp__quietbridge__other_tool', {'action': 'begin'}, json.dumps(self.task)),
            ('Read', {'action': 'begin'}, json.dumps(self.task)),
        ]
        for name, arguments, result in receipts:
            self.send('tool-result', now=1001, turnId=self.turn, toolName=name,
                      toolInput=arguments, toolResult=result)
        self.assertFalse((self.config / 'host-lifecycle').exists())

    def test_receipt_cannot_transfer_to_another_session_or_agent(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('tool-result', now=1001, turnId=self.turn,
                  toolName='mcp__quietbridge__quietbridge_task',
                  toolInput={'action': 'bind'},
                  toolResult={'content': [{'type': 'text', 'text': json.dumps(self.task)}]})
        other_session = 'session-' + str(uuid.uuid4())
        other_turn = other_session + ':1'
        self.send('turn-start', now=1002, turnId=other_turn, session=other_session)
        self.send('tool-result', now=1003, turnId=other_turn,
                  session=other_session, toolName='quietbridge_task',
                  toolInput={'action': 'bind'},
                  toolResult={'content': [{'type': 'text', 'text': json.dumps(self.task)}]})
        foreign = self.turns()[lifecycle.turn_key(other_session, self.agent, other_turn)]
        self.assertNotIn(self.task['taskId'], foreign.get('tasks') or {})
        self.assertIsNone(foreign.get('taskId'))

        other_agent = 'agent-' + str(uuid.uuid4())
        agent_turn = self.session + ':3'
        self.send('turn-start', now=1004, turnId=agent_turn, agent=other_agent)
        self.send('tool-result', now=1005, turnId=agent_turn, agent=other_agent,
                  toolName='quietbridge_task', toolInput={'action': 'begin'},
                  toolResult={'content': json.dumps(self.task)})
        sibling = self.turns()[lifecycle.turn_key(self.session, other_agent, agent_turn)]
        self.assertNotIn(self.task['taskId'], sibling.get('tasks') or {})

    def test_receipt_after_dispose_is_recorded_but_stopped(self):
        self.send('agent-disposed', now=1010)
        self.send('tool-result', now=1020, turnId=self.turn,
                  toolName='quietbridge_task', toolInput={'action': 'begin'},
                  toolResult={'content': json.dumps(self.task)})
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertEqual(turn['tasks'], {self.task['taskId']: self.task['lease']['ownerId']})
        self.assertEqual((turn['stoppedAt'], turn['reason']), (1010, 'host_session_end'))
        self.assertTrue(turn['sessionEnded'])

    def test_turn_stopped_state_file_contains_ids_only(self):
        self.send('turn-start', now=1000, turnId=self.turn)
        self.send('tool-result', now=1001, turnId=self.turn,
                  toolName='mcp__quietbridge__quietbridge_task',
                  toolInput={'action': 'begin', 'sensitiveInput': 'SENSITIVE-INPUT-TEXT'},
                  toolResult={'content': [{'type': 'text',
                                           'text': 'SENSITIVE-RESULT-TEXT ' + json.dumps(self.task)}]})
        self.send('turn-end', now=1010, turnId=self.turn, reason='completed')
        raw = (self.config / 'host-lifecycle/state.json').read_text()
        self.assertNotIn('SENSITIVE-INPUT-TEXT', raw)
        self.assertNotIn('SENSITIVE-RESULT-TEXT', raw)
        self.assertNotIn('targetSurface', raw)
        for forbidden in ('toolInput', 'toolResult', 'arguments', 'content', 'transcript'):
            self.assertNotIn(forbidden, raw)

    def test_malformed_payloads_are_ignored_without_creating_state(self):
        for payload in (None, [], {}, {'event': 'turn-start'},
                        {'event': 'turn-start', 'sessionId': 'bad session',
                         'agentId': self.agent, 'turnId': self.turn},
                        {'event': 'turn-start', 'sessionId': self.session,
                         'agentId': self.agent, 'turnId': 'bad turn'},
                        {'event': 'unknown', 'sessionId': self.session,
                         'agentId': self.agent}):
            self.assertIsNone(lifecycle.handle_event(payload, self.config, now=1000))
        self.send('agent-created', now=1000)
        self.send('agent-created', now=1000, turnId=self.turn)
        self.assertFalse((self.config / 'host-lifecycle').exists())

    def test_cli_consumes_one_event_silently(self):
        script = Path(lifecycle.__file__).resolve()
        def run(raw):
            result = subprocess.run(
                [sys.executable, '-B', str(script), '--config-dir', str(self.config)],
                input=raw, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
            return result
        run(json.dumps({'event': 'turn-start', 'sessionId': self.session,
                        'agentId': self.agent, 'turnId': self.turn}).encode())
        run(b'{not valid json')
        run(b'\xff\xfe')
        run(json.dumps({'event': 'agent-created', 'sessionId': self.session,
                        'agentId': self.agent}).encode())
        run(b'x' * (MAX_BYTES + 10))
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertIsNone(turn.get('stoppedAt'))
        run(json.dumps({'event': 'turn-end', 'sessionId': self.session,
                        'agentId': self.agent, 'turnId': self.turn,
                        'reason': 'completed'}).encode())
        turn = self.turns()[lifecycle.turn_key(self.session, self.agent, self.turn)]
        self.assertIsNotNone(turn['stoppedAt'])
        self.assertEqual(turn['reason'], 'completed')

    def test_cli_swallows_storage_failures(self):
        config_file = Path(self.config).parent / 'not-a-directory'
        config_file.write_text('blocked')
        result = subprocess.run(
            [sys.executable, '-B', str(Path(lifecycle.__file__).resolve()),
             '--config-dir', str(config_file)],
            input=json.dumps({'event': 'turn-start', 'sessionId': self.session,
                              'agentId': self.agent, 'turnId': self.turn}).encode(),
            capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (0, b''))


class GuidanceTests(unittest.TestCase):
    def test_description_is_chinese_and_states_unverified_limits(self):
        self.assertIsInstance(guidance.CONNECTOR_DESCRIPTION, str)
        self.assertTrue(guidance.CONNECTOR_DESCRIPTION.startswith('QuietBridge：'))
        self.assertIn('DeepSeek Harness', guidance.CONNECTOR_DESCRIPTION)
        self.assertIn('未', guidance.CONNECTOR_DESCRIPTION)
        self.assertGreater(len(guidance.CONNECTOR_DESCRIPTION), 80)
        self.assertLess(len(guidance.CONNECTOR_DESCRIPTION), 260)


if __name__ == '__main__':
    unittest.main()
