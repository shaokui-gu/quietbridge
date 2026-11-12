"""Task surface selection, fixed channels, and native focus constraints."""
import json
import unittest
from unittest import mock

import server
import test_task_windows as fixtures


class BrowserRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def begin(self, surface):
        return self.fixture.payload(action='begin', targetSurface=surface)

    def bind(self, surface):
        task = self.begin(surface)
        return self.fixture.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)

    def test_web_content_selects_extension_and_preserves_all_native_input_guards(self):
        task = self.bind('web_content')
        self.assertEqual(task['interactionMode'], 'background_browser')
        self.assertEqual(task['routing']['channel'], 'chrome_extension')
        self.assertTrue(task['routing']['extensionTabRequired'])
        self.fixture.fake.request.reset_mock()
        for tool in ('quietbridge_click', 'quietbridge_hover', 'quietbridge_scroll', 'quietbridge_drag',
                     'quietbridge_type_text', 'quietbridge_paste', 'quietbridge_key', 'quietbridge_set_value'):
            with self.subTest(tool=tool):
                self.fixture.error('BACKGROUND_ONLY', tool, appHandle='a', windowId=7)
        self.fixture.fake.request.assert_not_called()

    def test_native_browser_ui_binds_real_window_without_browser_attach(self):
        task = self.bind('browser_ui')
        self.assertEqual(task['interactionMode'], 'desktop')
        self.assertEqual(task['routing']['channel'], 'native_window')
        self.assertFalse(task['routing']['automaticFallbackAllowed'])
        self.assertTrue(task['routing']['requiresWindowShare'])
        self.assertEqual(task['routing']['focusPolicy'], 'preserve')
        methods = [c.args[0] for c in self.fixture.fake.request.call_args_list]
        self.assertIn('windowIdentity', methods)
        self.assertNotIn('browserSession', methods)

    def test_native_dialog_and_desktop_app_use_native_channel(self):
        for surface in ('native_dialog', 'desktop_app'):
            with self.subTest(surface=surface):
                task = self.begin(surface)
                self.assertEqual(task['interactionMode'], 'desktop')
                self.assertFalse(task['routing']['extensionTabRequired'])
                self.assertEqual(task['routing']['targetSurface'], surface)
                self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)

    def test_scope_and_mode_conflicts_do_not_replace_active_task(self):
        self.bind('browser_ui')
        before = self.fixture.path.read_bytes()
        self.fixture.fake.request.reset_mock()
        for surface, mode in (('web_content', 'desktop'), ('browser_ui', 'background_browser'),
                              ('native_dialog', 'background_browser')):
            with self.subTest(surface=surface):
                self.fixture.error('INVALID_ARGUMENT', action='begin', targetSurface=surface, interactionMode=mode)
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.fixture.fake.request.assert_not_called()

    def test_invalid_surface_does_not_create_task_or_dispatch(self):
        for value in (None, False, [], {}, 'headless', 'workbuddy_browser'):
            with self.subTest(surface=value):
                self.fixture.error('INVALID_ARGUMENT', action='begin', targetSurface=value)
        self.assertFalse(self.fixture.path.exists())
        self.fixture.fake.request.assert_not_called()

    def test_surface_cannot_change_on_bind_status_or_end(self):
        self.bind('web_content')
        before = self.fixture.path.read_bytes()
        for action in ('bind', 'status', 'end'):
            with self.subTest(action=action):
                self.fixture.error('INVALID_ARGUMENT', action=action, targetSurface='browser_ui')
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_explicit_native_phase_rejects_extension_before_dispatch(self):
        for surface in ('browser_ui', 'native_dialog', 'desktop_app'):
            task = self.begin(surface)
            self.fixture.fake.request.reset_mock()
            with mock.patch.object(server, '_events_handler') as handler:
                for action in ('assign', 'command', 'create_target', 'select_tab', 'verify_mapping', 'tabs', 'annotate'):
                    with self.subTest(surface=surface, action=action):
                        result = self.fixture.rpc('quietbridge_browser_events', action=action, taskId=task['taskId'])
                        data = result['structuredContent']
                        self.assertEqual(data['errorCode'], 'TASK_ROUTE_MISMATCH')
                        self.assertEqual(data['execution']['status'], 'not_dispatched')
                        self.assertEqual(data['recovery']['action'], 'inspect_task_route')
                        self.assertFalse(data['recovery']['allowAlternativeTools'])
                        self.assertFalse(data['recovery']['replaySideEffects'])
                handler.assert_not_called()
            self.fixture.fake.request.assert_not_called()
            self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)

    def test_native_phase_keeps_existing_result_query_and_cancel_available(self):
        task = self.begin('browser_ui')
        for action in ('status', 'candidates', 'result', 'cancel', 'annotations'):
            args = {'action': action, 'taskId': task['taskId']}
            with self.subTest(action=action):
                self.assertEqual(server.task_guard('quietbridge_browser_events', args), (args, None))
        self.fixture.fake.request.assert_not_called()

    def test_native_browser_route_does_not_enable_app_wide_keyboard_or_activation(self):
        self.bind('browser_ui')
        for name, args in (('quietbridge_key', {'key': 'l'}),
                           ('quietbridge_type_text', {'text': 'fixture'}),
                           ('quietbridge_paste', {'text': 'fixture'}),
                           ('quietbridge_windows', {'action': 'activate'})):
            with self.subTest(tool=name):
                self.fixture.error('UNSAFE_WINDOW_DELIVERY', name, **args)
        methods = [c.args[0] for c in self.fixture.fake.request.call_args_list]
        for method in ('pressKey', 'typeText', 'paste', 'activate'):
            self.assertNotIn(method, methods)

    def test_native_browser_click_still_requires_shared_window_first_frame(self):
        self.bind('browser_ui')
        self.fixture.fake.request.reset_mock()
        with mock.patch.object(server, 'ensure_share', side_effect=server.HelperError('SHARE_NOT_READY', 'fixture')):
            self.fixture.error('SHARE_NOT_READY', 'quietbridge_click', windowId=7, x=10, y=20)
        self.assertNotIn('click', [c.args[0] for c in self.fixture.fake.request.call_args_list])

    def test_routing_survives_phase_lifecycle_but_not_stored_as_authority(self):
        bound = self.bind('native_dialog')
        status = self.fixture.payload(action='status')
        ended = self.fixture.payload(action='end', confirm=True)
        self.assertEqual(bound['routing'], status['routing'])
        self.assertEqual(bound['routing'], ended['routing'])
        stored = json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]
        self.assertEqual(stored['targetSurface'], 'native_dialog')
        self.assertNotIn('routing', stored)

    def test_legacy_calls_keep_mode_and_task_storage_compatibility(self):
        for args, mode in (({}, 'background_browser'), ({'interactionMode': 'desktop'}, 'desktop')):
            task = self.fixture.payload(action='begin', **args)
            self.assertEqual(task['interactionMode'], mode)
            self.assertEqual(task['routing']['scopeSource'], 'interaction_mode')
            stored = json.loads(self.fixture.path.read_text())['tasks'][task['taskId']]
            self.assertNotIn('targetSurface', stored)
            self.fixture.payload(action='end', confirm=True)


if __name__ == '__main__':
    unittest.main()
