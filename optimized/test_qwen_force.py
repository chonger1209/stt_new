"""Force-exit concurrency checks without real model requests or processes."""
import contextlib
import threading
import time
import unittest
from unittest.mock import Mock, patch

from qwen_backend import ToolkitBackend
from qwen_control import QwenController
from test_qwen import wait_for


class InterruptibleBackend:
    def __init__(self, blocked):
        self.blocked = blocked
        self.entered = threading.Event()
        self.release = threading.Event()
        self.force_entered = threading.Event()
        self.force_release = threading.Event()
        self.state = 'busy'
        self.forces = 0
        self.starts = 0

    def execute(self, action, cancelled):
        if action == self.blocked and not self.entered.is_set():
            self.entered.set()
            self.release.wait(3)
        if cancelled.is_set():
            raise RuntimeError('cancelled')
        return getattr(self, action)()

    def status(self):
        return {'state': self.state}

    def start(self):
        self.starts += 1
        self.state = 'ready'
        return {'ok': True}

    ensure_service = start

    def unload(self):
        self.state = 'unloaded'
        return {'ok': True}

    def force_stop(self):
        self.forces += 1
        self.force_entered.set()
        self.force_release.wait(3)
        self.state = 'off'
        return {'ok': True}


class ForceControllerTests(unittest.TestCase):
    def test_force_preempts_each_blocking_operation_and_discards_stale_result(self):
        for action in ('status', 'start', 'unload', 'ensure_service'):
            with self.subTest(action=action):
                backend = InterruptibleBackend(action)
                controller = QwenController(backend, interval=.02)
                try:
                    if action != 'status':
                        self.assertTrue(controller.request(action))
                    self.assertTrue(backend.entered.wait(1))
                    at = time.monotonic()
                    self.assertTrue(controller.request('force_stop'))
                    self.assertLess(time.monotonic() - at, .2)
                    self.assertTrue(backend.force_entered.wait(1))
                    self.assertFalse(controller.request('force_stop'))
                    backend.force_release.set()
                    wait_for(lambda: not controller._results.empty())
                    events = controller.drain_events()
                    self.assertEqual(events[0]['action'], 'force_stop')
                    self.assertTrue(events[0]['result']['ok'])
                    backend.release.set()
                    time.sleep(.06)
                    self.assertEqual(backend.state, 'off')
                    self.assertEqual(backend.starts, 0)
                    self.assertFalse(any(e.get('action') == action for e in controller.drain_events()))
                    self.assertTrue(controller.request('start'))
                    wait_for(lambda: backend.starts == 1)
                    self.assertEqual(backend.forces, 1)
                finally:
                    backend.force_release.set()
                    backend.release.set()
                    controller.close()

    def test_force_failure_never_reports_off(self):
        backend = InterruptibleBackend('unused')
        backend.force_stop = Mock(return_value={'ok': False, 'message': '权限不足'})
        controller = QwenController(backend, interval=1)
        try:
            controller.request('force_stop')
            wait_for(lambda: not controller._results.empty())
            events = controller.drain_events()
            self.assertFalse(events[0]['result']['ok'])
            self.assertFalse(any(e.get('status', {}).get('state') == 'off' for e in events))
        finally:
            controller.close()


class ForceBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = ToolkitBackend()
        self.manager, self.lifecycle = Mock(), Mock()
        self.backend.manager, self.backend.lifecycle = self.manager, self.lifecycle
        self.config = {'base_url': 'http://127.0.0.1:8080/v1'}
        self.manager.config.return_value = self.config
        self.root = {'pid': 100, 'created': 1.0}
        self.child = {'pid': 101, 'created': 2.0}
        self.lifecycle.identify_service.return_value = {'root': self.root}
        self.lifecycle.alive.return_value = True
        self.lifecycle.identity.side_effect = lambda pid: self.root if pid == 100 else self.child

    def test_force_kills_only_verified_tree_without_http_or_model_locks(self):
        root, child = Mock(), Mock()
        root.pid, child.pid = 100, 101
        root.children.return_value = [child]
        child.children.return_value = []
        self.manager.port_busy.side_effect = [True, False]
        with patch('qwen_backend.psutil.Process', side_effect=lambda pid: root if pid == 100 else child), \
                patch('qwen_backend.psutil.wait_procs', return_value=([], [])):
            self.assertTrue(self.backend.force_stop()['ok'])
        root.suspend.assert_called_once()
        child.suspend.assert_called_once()
        root.kill.assert_called_once()
        child.kill.assert_called_once()
        self.manager.lock.assert_not_called()
        self.manager.request_json.assert_not_called()
        self.lifecycle.service_activity.assert_not_called()

    def test_identity_mismatch_never_kills(self):
        self.manager.port_busy.return_value = True
        self.lifecycle.identify_service.side_effect = ValueError('mismatch')
        with patch('qwen_backend.psutil.Process') as process:
            with self.assertRaises(ValueError):
                self.backend.force_stop()
            process.assert_not_called()

    def test_cancelled_start_cannot_probe_or_spawn(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(RuntimeError):
            self.backend.execute('start', event)
        self.manager.probe.assert_not_called()

    def test_cancel_during_probe_cannot_spawn_after_forced_exit(self):
        event = threading.Event()
        self.manager.lock.side_effect = lambda *a, **k: contextlib.nullcontext()
        def probe(_):
            event.set()
            return {'reachable': False}
        self.manager.probe.side_effect = probe
        with patch('qwen_backend.launch_external') as launch:
            with self.assertRaises(RuntimeError):
                self.backend.execute('start', event)
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
