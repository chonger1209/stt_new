import contextlib
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import os
import sys
import tempfile
from settings import read_settings, complete_config_text

from qwen_backend import ToolkitBackend, launch_external, idle_remaining
from qwen_control import QwenController


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for worker')


class FakeBackend:
    def __init__(self):
        self.state = 'off'
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.active = 0
        self.maximum_active = 0

    @contextlib.contextmanager
    def record(self, action):
        self.calls.append((action, threading.get_ident()))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            yield
        finally:
            self.active -= 1

    def status(self):
        with self.record('status'):
            return {'state': self.state, 'detail': self.state}

    def start(self):
        with self.record('start'):
            self.release.wait(timeout=3)
            self.state = 'ready'
            return {'ok': True, 'message': 'started'}

    def stop(self):
        with self.record('stop'):
            if self.state == 'busy':
                return {'ok': False, 'message': 'busy; not stopped'}
            self.state = 'off'
            return {'ok': True, 'message': 'stopped'}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.controller = QwenController(self.backend, interval=.02)

    def tearDown(self):
        self.backend.release.set()
        self.controller.close()

    def test_monitor_never_starts_or_stops_service(self):
        wait_for(lambda: len(self.backend.calls) >= 3)
        self.assertEqual({action for action, _ in self.backend.calls}, {'status'})
        self.assertEqual(self.backend.maximum_active, 1)

    def test_slow_start_does_not_block_caller_or_accept_duplicate(self):
        self.backend.release.clear()
        started = time.monotonic()
        self.assertTrue(self.controller.request('start'))
        self.assertLess(time.monotonic() - started, .2)
        wait_for(lambda: any(action == 'start' for action, _ in self.backend.calls))
        self.assertFalse(self.controller.request('start'))
        self.assertFalse(self.controller.request('stop'))
        self.assertFalse(self.controller.request('status'))
        self.assertEqual(self.controller.operation, 'start')
        self.backend.release.set()
        wait_for(lambda: self.backend.state == 'ready')
        wait_for(lambda: not self.controller._results.empty())
        events = self.controller.drain_events()
        self.assertTrue(any(event.get('result', {}).get('ok') for event in events))
        self.assertIsNone(self.controller.operation)
        self.assertEqual(self.backend.maximum_active, 1)
        self.assertTrue(all(identity != threading.get_ident() for _, identity in self.backend.calls))

    def test_busy_refusal_remains_available_after_many_probes(self):
        self.backend.state = 'busy'
        self.assertTrue(self.controller.request('stop'))
        wait_for(lambda: not self.controller._results.empty())
        calls = len(self.backend.calls)
        wait_for(lambda: len(self.backend.calls) >= calls + 4)
        events = self.controller.drain_events()
        operations = [event for event in events if event['kind'] == 'operation']
        self.assertEqual(len(operations), 1)
        self.assertFalse(operations[0]['result']['ok'])
        self.assertEqual(self.backend.state, 'busy')

    def test_close_does_not_wait_for_backend_or_stop_model(self):
        self.backend.release.clear()
        self.controller.request('start')
        wait_for(lambda: any(action == 'start' for action, _ in self.backend.calls))
        started = time.monotonic()
        self.controller.close()
        self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(any(action == 'stop' for action, _ in self.backend.calls))
        self.assertFalse(self.controller.request('start'))

    def test_exception_is_reported_without_raw_secret_text(self):
        self.backend.start = Mock(side_effect=RuntimeError('secret-token-not-for-ui'))
        self.controller.request('start')
        wait_for(lambda: not self.controller._results.empty())
        events = self.controller.drain_events()
        self.assertNotIn('secret-token', str(events))
        self.assertFalse(events[0]['result']['ok'])

    def test_idle_checks_stay_on_worker_and_are_throttled(self):
        self.backend.state = 'ready'
        self.backend.idle_shutdown = Mock(return_value=None)
        self.controller._next_idle_check = 0
        wait_for(lambda: self.backend.idle_shutdown.call_count == 1)
        calls = len(self.backend.calls)
        wait_for(lambda: len(self.backend.calls) >= calls + 4)
        self.assertEqual(self.backend.idle_shutdown.call_count, 1)
        self.assertFalse(any(action == 'stop' for action, _ in self.backend.calls))

    def test_reset_is_queued_once_and_result_is_retained(self):
        self.backend.reset_idle = Mock(return_value={'ok': True, 'message': 'reset'})
        self.assertTrue(self.controller.request('reset_idle'))
        self.assertFalse(self.controller.request('reset_idle'))
        wait_for(lambda: not self.controller._results.empty())
        events = self.controller.drain_events()
        self.assertEqual(events[0]['action'], 'reset_idle')
        self.assertTrue(events[0]['result']['ok'])
        self.backend.reset_idle.assert_called_once()

    def test_startup_runs_once_on_worker_without_loading_unloaded_model(self):
        self.controller.close()
        self.backend.state = 'unloaded'
        calls = []
        def ensure():
            calls.append(threading.get_ident())
            return {'ok': True, 'message': 'listener reused'}
        self.backend.ensure_service = ensure
        self.controller = QwenController(self.backend, interval=.01, autostart=True)
        wait_for(lambda: not self.controller._results.empty())
        events = self.controller.drain_events()
        self.assertEqual(events[0]['action'], 'ensure_service')
        self.assertEqual(self.backend.state, 'unloaded')
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        self.assertFalse(any(action == 'start' for action, _ in self.backend.calls))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.backend = ToolkitBackend()
        self.manager, self.lifecycle = Mock(), Mock()
        self.http = self.manager.request_json
        self.backend._load = Mock(return_value=(self.manager, self.lifecycle))
        self.config = {'base_url': 'http://127.0.0.1:8080/v1', 'model': 'test', 'startup_timeout_sec': 1}
        self.manager.config.return_value = self.config
        self.manager.state.return_value = {'enabled': False, 'auto_start': False}
        self.manager.port_busy.return_value = True
        self.manager.probe.return_value = {'reachable': True, 'ready': True, 'service': 'strata'}
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 0}
        self.lock_order = []

        @contextlib.contextmanager
        def lock(name, timeout):
            self.lock_order.append(name)
            yield

        self.manager.lock.side_effect = lock

    def test_status_distinguishes_ready_busy_unloaded_off_and_mismatch(self):
        self.assertEqual(self.backend.status()['state'], 'ready')
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 1}
        self.assertEqual(self.backend.status()['state'], 'busy')
        self.lifecycle.service_activity.return_value = {'loaded': False, 'in_flight': 0}
        self.assertEqual(self.backend.status()['state'], 'unloaded')
        self.manager.probe.return_value = {'reachable': False}
        self.assertEqual(self.backend.status()['state'], 'error')
        self.manager.port_busy.return_value = False
        self.assertEqual(self.backend.status()['state'], 'off')
        self.manager.probe.return_value = {'reachable': True, 'error': 'mismatch'}
        self.assertEqual(self.backend.status()['state'], 'error')
        self.assertTrue(all(call.args[0].endswith('/metrics') and len(call.args) == 2
                            for call in self.http.call_args_list))
        self.manager.ensure.assert_not_called()

    def test_busy_stop_does_not_unload_or_terminate(self):
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 1}
        self.assertFalse(self.backend.stop()['ok'])
        self.http.assert_not_called()
        self.lifecycle.identify_service.assert_not_called()
        self.lifecycle.stop_verified_service.assert_not_called()
        self.assertEqual(self.lock_order, ['model-request', 'startup'])

    def test_identity_mismatch_rejected_before_unload(self):
        self.lifecycle.identify_service.side_effect = ValueError('identity mismatch')
        with self.assertRaises(ValueError):
            self.backend.stop()
        self.http.assert_not_called()
        self.lifecycle.stop_verified_service.assert_not_called()

    def test_reloaded_or_newly_busy_service_not_force_stopped(self):
        self.manager.request_json.return_value = {'status': 'unloaded'}
        self.lifecycle.stop_verified_service.return_value = False
        self.assertFalse(self.backend.stop()['ok'])
        self.lifecycle.event.assert_not_called()

    def test_confirmed_stop_uses_only_verified_tree_and_shared_locks(self):
        service = {'root': {'pid': 123}, 'tree': [{'pid': 123, 'created': 1}]}
        self.lifecycle.identify_service.return_value = service
        self.manager.request_json.return_value = {'status': 'unloaded'}
        self.lifecycle.stop_verified_service.return_value = True
        self.assertTrue(self.backend.stop()['ok'])
        self.lifecycle.stop_verified_service.assert_called_once_with(self.config, service)
        self.assertEqual(self.lock_order, ['model-request', 'startup'])
        self.manager.set_state.assert_not_called()

    def test_manual_start_reuses_loaded_service_without_changing_switches(self):
        self.assertTrue(self.backend.start()['ok'])
        self.manager.set_state.assert_not_called()
        self.manager.ensure.assert_not_called()
        self.http.assert_not_called()

    def test_startup_reuses_unloaded_service_without_loading(self):
        self.manager.probe.return_value = {'reachable': True, 'ready': False, 'service': 'strata'}
        self.assertTrue(self.backend.ensure_service()['ok'])
        self.http.assert_not_called()
        self.manager.set_state.assert_not_called()

    def test_startup_busy_service_is_read_only(self):
        self.config['stt_control'] = True
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 1}
        self.manager.request_json.return_value = {'version': 1, 'model': 'test', 'service': 'strata', 'exiting': False}
        self.assertTrue(self.backend.ensure_service()['ok'])
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args[:2], (self.config['base_url'] + '/stt/control', self.config))
        self.assertLessEqual(self.http.call_args.kwargs['timeout'], 3)
        self.lifecycle.stop_verified_service.assert_not_called()

    def test_atomic_idle_protocol_carries_configured_seconds(self):
        self.prepare_idle()
        self.backend._control = Mock(return_value={'version': 1})
        self.backend.set_idle_minutes(7)
        with patch('qwen_backend.time.time', return_value=1420):
            self.assertTrue(self.backend.idle_shutdown()['ok'])
        self.http.assert_called_once_with(
            self.config['base_url'] + '/stt/unload', self.config,
            {'model': 'test', 'idle_seconds': 420}, timeout=75)
        self.lifecycle.stop_verified_service.assert_not_called()

    def test_failed_exit_restores_request_admission(self):
        self.prepare_idle()
        self.backend._control = Mock(return_value={'version': 1})
        self.lifecycle.stop_verified_service.return_value = False
        self.assertFalse(self.backend.stop()['ok'])
        self.assertEqual(self.http.call_args_list[0].args[0],
                         self.config['base_url'] + '/stt/prepare-exit')
        self.assertEqual(self.http.call_args_list[-1].args[0],
                         self.config['base_url'] + '/stt/cancel-exit')

    def test_manual_sleep_keeps_listener_and_switches(self):
        self.prepare_idle()
        self.assertTrue(self.backend.unload()['ok'])
        self.lifecycle.stop_verified_service.assert_not_called()
        self.manager.set_state.assert_not_called()

    def test_unloaded_service_has_no_countdown_or_repeat_unload(self):
        self.prepare_idle()
        self.lifecycle.service_activity.return_value['loaded'] = False
        self.assertIsNone(self.backend.status()['idle_remaining_seconds'])
        self.assertIsNone(self.backend.idle_shutdown())
        self.http.assert_not_called()

    def test_changed_timeout_controls_boundary_and_status(self):
        self.prepare_idle()
        self.backend.set_idle_minutes(7)
        with patch('qwen_backend.time.time', return_value=1419):
            self.assertEqual(self.backend.status()['idle_remaining_seconds'], 1)
            self.assertIsNone(self.backend.idle_shutdown())
        with patch('qwen_backend.time.time', return_value=1420):
            self.assertTrue(self.backend.idle_shutdown()['ok'])
        self.lifecycle.stop_verified_service.assert_not_called()

    def test_busy_race_rejected_by_server_never_terminates(self):
        import urllib.error
        self.prepare_idle()
        self.manager.request_json.side_effect = urllib.error.HTTPError('local', 409, 'busy', {}, None)
        with patch('qwen_backend.time.time', return_value=8200):
            with self.assertRaises(urllib.error.HTTPError):
                self.backend.idle_shutdown()
        self.lifecycle.stop_verified_service.assert_not_called()

    def test_refresh_timeout_rejects_invalid_internal_values(self):
        for value in (0, -1, True, 1.5, '120'):
            with self.assertRaises(ValueError):
                self.backend.set_idle_minutes(value)

    def test_manual_load_does_not_change_disabled_auto_start(self):
        self.manager.probe.side_effect = [
            {'reachable': True, 'ready': False, 'service': 'strata'},
            {'reachable': True, 'ready': True, 'service': 'strata'}]
        self.backend._watchdog = Mock()
        self.assertTrue(self.backend.start()['ok'])
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args, (self.config['base_url'] + '/load', self.config, {'model': 'test'}))
        self.assertGreater(self.http.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(self.http.call_args.kwargs['timeout'], 1)
        self.manager.set_state.assert_not_called()

    def prepare_idle(self):
        self.config['lifecycle'] = {'enabled': False, 'owner': 'stt'}
        self.backend._reset_at = Mock(return_value=0)
        self.lifecycle.service_activity.return_value = {'loaded': True, 'in_flight': 0, 'last_request_at': 1000}
        self.lifecycle.identify_service.return_value = {'root': {'pid': 123}}
        self.manager.request_json.return_value = {'status': 'unloaded'}
        self.lifecycle.stop_verified_service.return_value = True

    def test_idle_unload_exact_boundary_keeps_service_alive(self):
        self.prepare_idle()
        with patch('qwen_backend.time.time', return_value=8200):
            self.assertTrue(self.backend.idle_shutdown()['ok'])
        self.lifecycle.stop_verified_service.assert_not_called()
        self.lifecycle.event.assert_called_once_with('stt_idle_unload', root_pid=123, model='test')

    def test_idle_stop_before_boundary_and_recent_manual_reset_are_kept(self):
        self.prepare_idle()
        with patch('qwen_backend.time.time', return_value=8199):
            self.assertIsNone(self.backend.idle_shutdown())
        self.backend._reset_at.return_value = 8199
        with patch('qwen_backend.time.time', return_value=8200):
            self.assertIsNone(self.backend.idle_shutdown())
        self.http.assert_not_called()

    def test_new_task_between_idle_check_and_unload_wins(self):
        self.prepare_idle()
        self.lifecycle.service_activity.side_effect = [
            {'loaded': True, 'in_flight': 0, 'last_request_at': 1000},
            {'loaded': True, 'in_flight': 0, 'last_request_at': 8199}]
        with patch('qwen_backend.time.time', return_value=8200):
            self.assertIsNone(self.backend.idle_shutdown())
        self.http.assert_not_called()

    def test_idle_busy_or_unknown_never_unloads(self):
        self.prepare_idle()
        for activity in ({'in_flight': 1, 'last_request_at': 1000},
                         {'in_flight': 0}, {'last_request_at': 1000}):
            self.lifecycle.service_activity.return_value = activity
            with patch('qwen_backend.time.time', return_value=2800):
                self.assertIsNone(self.backend.idle_shutdown())
        self.http.assert_not_called()

    def test_idle_owner_or_old_watchdog_enabled_refuses_management(self):
        self.prepare_idle()
        for policy in ({'enabled': False}, {'owner': 'stt', 'enabled': True}):
            self.config['lifecycle'] = policy
            self.assertIsNone(self.backend.idle_shutdown())
        self.lifecycle.service_activity.assert_not_called()

    def test_reset_persists_shared_timestamp_without_loading_model(self):
        self.prepare_idle()
        self.manager.ROOT = Path('toolkit')
        with patch('qwen_backend.time.time', return_value=2500):
            self.assertTrue(self.backend.reset_idle()['ok'])
        self.manager.atomic_json.assert_called_once_with(
            Path('toolkit/runtime/stt-idle-reset.json'), {'at': 2500})
        self.http.assert_not_called()
        self.manager.ensure.assert_not_called()

    def test_status_poll_does_not_write_idle_reset_clock(self):
        self.prepare_idle()
        with patch('qwen_backend.time.time', return_value=8150):
            first = self.backend.status()
        with patch('qwen_backend.time.time', return_value=8160):
            second = self.backend.status()
        self.assertEqual(first['idle_remaining_seconds'], 50)
        self.assertEqual(second['idle_remaining_seconds'], 40)
        self.assertEqual(first['idle_deadline'], second['idle_deadline'])
        self.manager.atomic_json.assert_not_called()


class IdleClockTests(unittest.TestCase):
    def test_boundaries_and_warning_minute(self):
        activity = {'in_flight': 0, 'last_request_at': 1000}
        for now, expected in ((1000, 7200), (8140, 60), (8199, 1), (8200, 0), (8201, 0)):
            self.assertEqual(idle_remaining(activity, 0, now), expected)

    def test_reset_and_new_task_both_restart_two_hours(self):
        self.assertEqual(idle_remaining({'in_flight': 0, 'last_request_at': 1000}, 2800, 2800), 7200)
        self.assertEqual(idle_remaining({'in_flight': 0, 'last_request_at': 3000}, 2800, 3000), 7200)

    def test_busy_invalid_or_future_timestamps_do_not_authorize_close(self):
        for last in (None, '1000', float('nan'), float('inf'), True, -1, 3001):
            self.assertIsNone(idle_remaining({'in_flight': 0, 'last_request_at': last}, 0, 3000))
        for busy in (None, 1, -1, '0', float('nan')):
            self.assertIsNone(idle_remaining({'in_flight': busy, 'last_request_at': 1000}, 0, 3000))
        for reset in (float('nan'), '2800', 3001):
            self.assertIsNone(idle_remaining({'in_flight': 0, 'last_request_at': 1000}, reset, 3000))


class SleepConfigTests(unittest.TestCase):
    def test_upgrade_preserves_values_sections_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.txt'
            text = complete_config_text().replace('qwen_idle_timeout_minutes = 120\n', '')
            text += '\n[custom.exe]\ndisplay_name = 我的应用\nshow_in_screen_time = false\n'
            path.write_text(text, encoding='utf-8')
            result = read_settings(path)
            once = path.read_bytes()
            self.assertEqual(result['qwen_idle_timeout_minutes'], 120)
            self.assertFalse(result['process_config']['custom.exe']['show_in_screen_time'])
            self.assertEqual(path.read_text(encoding='utf-8').count('qwen_idle_timeout_minutes = 120'), 1)
            read_settings(path)
            self.assertEqual(once, path.read_bytes())

    def test_custom_minutes_and_invalid_values_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.txt'
            for raw, expected in [('7', 7), ('180', 180), ('0', 120), ('-5', 120), ('2h', 120), ('1.5', 120)]:
                path.write_text('qwen_idle_timeout_minutes = ' + raw, encoding='utf-8')
                logger = Mock()
                self.assertEqual(read_settings(path, logger=logger)['qwen_idle_timeout_minutes'], expected)
                self.assertEqual(logger.warning.called, expected == 120)


class CredentialTests(unittest.TestCase):
    def test_disabled_auth_ignores_old_key_file_and_clears_inherited_service_key(self):
        manager, _ = ToolkitBackend()._load()
        config = {'api_key_required': False, 'api_key_file': 'missing-old-key'}
        with patch.dict(os.environ, {'STRATA_API_KEY': 'old-key', 'QWEN_LOCAL_API_KEY': 'old-key'}):
            self.assertEqual(manager.api_key(config), '')
            self.assertNotIn('STRATA_API_KEY', manager.service_environment(config))

    def test_same_file_key_used_by_client_and_child_environment(self):
        manager, _ = ToolkitBackend()._load()
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / 'test-key.txt'
            key_path.write_text('fixture-key\n', encoding='utf-8')
            config = {'api_key_file': str(key_path)}
            self.assertEqual(manager.api_key(config), 'fixture-key')
            self.assertEqual(manager.service_environment(config)['STRATA_API_KEY'], 'fixture-key')
            key_path.write_text('', encoding='utf-8')
            with self.assertRaises(ValueError):
                manager.service_environment(config)

    def test_missing_key_file_does_not_fall_back_to_unprotected_launch(self):
        manager, _ = ToolkitBackend()._load()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                manager.service_environment({'api_key_file': str(Path(directory) / 'absent')})


class FrozenLauncherTests(unittest.TestCase):
    def test_external_python_gets_clean_path_and_parent_dll_directory_is_restored(self):
        bundle = Path.cwd() / 'fake_meipass'
        kernel = Mock()
        kernel.GetDllDirectoryW.return_value = 0
        kernel.SetDllDirectoryW.return_value = 1
        original_path = str(bundle) + os.pathsep + r'C:\Windows\System32'
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(bundle), create=True), \
             patch.dict(os.environ, {'PATH': original_path}), \
             patch('qwen_backend.ctypes.WinDLL', return_value=kernel), \
             patch('qwen_backend.subprocess.Popen') as spawn:
            launch_external(['python.exe', 'server.py'])
            environment = spawn.call_args.kwargs['env']
            self.assertNotIn(str(bundle), environment['PATH'])
            self.assertIn(r'C:\Windows\System32', environment['PATH'])
            self.assertEqual(os.environ['PATH'], original_path)
            self.assertEqual(kernel.SetDllDirectoryW.call_count, 2)

    def test_parent_dll_directory_restored_when_process_creation_fails(self):
        kernel = Mock()
        kernel.GetDllDirectoryW.return_value = 0
        kernel.SetDllDirectoryW.return_value = 1
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, '_MEIPASS', str(Path.cwd() / 'fake_meipass'), create=True), \
             patch('qwen_backend.ctypes.WinDLL', return_value=kernel), \
             patch('qwen_backend.subprocess.Popen', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                launch_external(['python.exe'])
            self.assertEqual(kernel.SetDllDirectoryW.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
