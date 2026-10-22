import contextlib
import threading
import time
import unittest
from unittest.mock import Mock

from qwen_backend import ToolkitBackend, startup_locks
from qwen_control import QwenController
from qwen_errors import ServiceLockBusy


class PrepareBackendTests(unittest.TestCase):
    def test_existing_service_never_takes_start_locks_or_loads(self):
        for state in ('ready', 'busy', 'unloaded'):
            backend = ToolkitBackend()
            backend.status = Mock(return_value={'state': state})
            backend.start = Mock(side_effect=AssertionError('must not start'))
            self.assertTrue(backend.ensure_service()['ok'])
            backend.start.assert_not_called()

    def test_stopped_service_uses_original_locked_start(self):
        backend = ToolkitBackend()
        backend.status = Mock(return_value={'state':'off'})
        backend.start = Mock(return_value={'ok':True})
        self.assertTrue(backend.ensure_service()['ok'])
        backend.start.assert_called_once_with(only_service=True)

    def test_bad_configuration_or_unknown_identity_never_spawns(self):
        for state in ('error', 'unconfigured', 'disabled'):
            backend = ToolkitBackend()
            backend.status = Mock(return_value={'state':state,'detail':'specific reason'})
            backend.start = Mock()
            self.assertEqual(backend.ensure_service()['message'],'specific reason')
            backend.start.assert_not_called()

    def test_second_lock_failure_releases_first_and_is_retryable_type(self):
        manager = Mock(); released=[]
        @contextlib.contextmanager
        def lock(name, timeout):
            if name=='startup': raise TimeoutError('held')
            try: yield
            finally: released.append(name)
        manager.lock.side_effect=lock
        with self.assertRaises(ServiceLockBusy):
            with startup_locks(manager): self.fail('body ran')
        self.assertEqual(released,['model-request'])

    def test_http_timeout_inside_locks_is_not_lock_contention(self):
        manager=Mock();manager.lock.side_effect=lambda *a,**kw:contextlib.nullcontext()
        with self.assertRaises(TimeoutError):
            with startup_locks(manager): raise TimeoutError('HTTP stalled')
        self.assertNotEqual(QwenController._error(ServiceLockBusy()),QwenController._error(TimeoutError()))


class RetryBackend:
    def __init__(self):
        self.calls=0;self.state='off';self.error=ServiceLockBusy('held');self.forced=0;self.started=0
    def status(self): return {'state':self.state,'detail':self.state}
    def ensure_service(self):
        self.calls+=1
        raise self.error
    def force_stop(self):
        self.forced+=1
        return {'ok':True}
    def start(self):
        self.started+=1
        return {'ok':True}


class PrepareRetryTests(unittest.TestCase):
    def setUp(self):
        self.backend=RetryBackend()
        # Set delays before starting the worker, without changing production defaults.
        class FastController(QwenController):
            PREPARE_RETRY_DELAY=.04
        self.controller=FastController(self.backend,interval=.01,autostart=True)
        self.events=[]
    def tearDown(self):self.controller.close()
    def wait(self,predicate,timeout=2):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.events.extend(self.controller.drain_events())
            if predicate():return
            time.sleep(.003)
        self.fail('timed out')
    def test_lock_contention_retries_five_times_then_stops(self):
        self.wait(lambda:self.backend.calls==6 and self.controller._pending is None)
        time.sleep(.08);self.controller.drain_events()
        self.assertEqual(self.backend.calls,6)
        self.assertIsNone(self.controller._prepare_retry_at)
        self.assertIn('停止重试',self.controller._prepare_note)
    def test_http_timeout_is_not_automatically_retried(self):
        self.controller.close();self.backend=RetryBackend();self.backend.error=TimeoutError('HTTP')
        self.controller=QwenController(self.backend,interval=.01,autostart=True)
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.assertIsNone(self.controller._prepare_retry_at)
    def test_healthy_status_cancels_retry_and_clears_old_failure(self):
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.backend.state='busy'
        self.wait(lambda:self.controller._prepare_note=='')
        time.sleep(.07);self.assertEqual(self.backend.calls,1)
    def test_disabled_management_cancels_retry(self):
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.backend.state='disabled'
        self.wait(lambda:self.controller._prepare_retry_at is None)
        time.sleep(.07);self.assertEqual(self.backend.calls,1)
    def test_force_exit_during_backoff_prevents_restart(self):
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.assertTrue(self.controller.request('force_stop'))
        self.wait(lambda:self.backend.forced==1 and self.controller._pending is None)
        time.sleep(.07);self.assertEqual(self.backend.calls,1)
    def test_manual_action_cancels_automatic_retry(self):
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.assertTrue(self.controller.request('start'))
        self.wait(lambda:self.backend.started==1 and self.controller._pending is None)
        time.sleep(.07);self.assertEqual(self.backend.calls,1)
    def test_close_cancels_backoff(self):
        self.wait(lambda:self.backend.calls==1 and self.controller._pending is None)
        self.controller.close()
        time.sleep(.07);self.assertEqual(self.backend.calls,1)
        self.assertIsNone(self.controller._prepare_retry_at)


if __name__=='__main__':unittest.main()
