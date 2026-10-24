import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parents[1]))
sys.path.insert(0, str(BASE/'scripts'))
from qwen_backend import ToolkitBackend
from qwen_control import QwenController
import manager
import service_runtime


class Contracts(unittest.TestCase):
    def test_stt_start_delegates_without_child(self):
        backend = ToolkitBackend()
        m = Mock()
        m.config.return_value = {'base_url':'http://127.0.0.1:19999/v1'}
        m.runtime.return_value.ensure.return_value = {'ok':True}
        backend._load = Mock(return_value=(m, Mock()))
        with patch('qwen_backend.launch_external', side_effect=AssertionError('direct launch forbidden')):
            self.assertTrue(backend.start()['ok'])
            m.runtime.return_value.ensure.assert_called_once_with(source='stt', only_service=False,
                explicit=True, cancelled=backend._check_cancelled)

    def test_stt_prepare_never_clears_manual_stop(self):
        backend = ToolkitBackend()
        m = Mock(); m.config.return_value = {}
        backend._load = Mock(return_value=(m, Mock()))
        backend.start(only_service=True)
        self.assertFalse(m.runtime.return_value.ensure.call_args.kwargs['explicit'])

    def test_stt_stop_only_sets_shared_request(self):
        backend = ToolkitBackend()
        m = Mock(); backend._load = Mock(return_value=(m, Mock()))
        backend.stop()
        m.runtime.return_value.stop.assert_called_once_with(source='stt')

    def test_stt_close_does_not_stop_backend(self):
        backend = Mock()
        controller = QwenController(backend, interval=100)
        controller.close()
        backend.stop.assert_not_called()
        backend.force_stop.assert_not_called()

    def test_failed_task_has_no_direct_launch_fallback(self):
        m = Mock()
        runtime = service_runtime.Runtime(m)
        with patch('service_runtime.subprocess.run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(RuntimeError):
                runtime.request_task({'scheduled_task':'nonexistent-fake-task'}, 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'schtasks.exe')

    def test_missing_task_is_fail_closed(self):
        with patch('service_runtime.subprocess.run') as run:
            with self.assertRaises(ValueError):
                service_runtime.Runtime(Mock()).request_task({}, 1)
            run.assert_not_called()

    def test_cancelled_client_never_requests_task(self):
        m = Mock(); m.config.return_value = {'startup_timeout_sec':900}
        m.ROOT = BASE / 'tests'
        m.read_json.return_value = {'desired_state':'enabled','generation':1}
        runtime = service_runtime.Runtime(m)
        runtime.request_task = Mock()
        def cancel(): raise RuntimeError('cancelled')
        with self.assertRaises(RuntimeError):
            runtime.ensure(cancelled=cancel)
        runtime.request_task.assert_not_called()

    def test_admission_busy_does_not_unload(self):
        spec = importlib.util.spec_from_file_location('candidate_wrapper', BASE/'scripts/qwen_service.py')
        wrapper = importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
        gate = wrapper.Admission(); svc = Mock()
        self.assertTrue(gate.enter())
        self.assertEqual(gate.control(svc, 'prepare-exit')[0], 409)
        svc.unload.assert_not_called()
        gate.leave(); svc.unload.return_value = 'unloaded'
        self.assertEqual(gate.control(svc, 'prepare-exit')[0], 200)
        self.assertFalse(gate.enter())

    def test_no_launch_at_mcp_module_entry(self):
        text = (BASE/'scripts/local_model_mcp.py').read_text(encoding='utf-8')
        entry = text.split("if __name__ == '__main__':")[1]
        self.assertNotIn('prewarm', entry)
        self.assertNotIn('ensure', entry)

    def test_wrapper_persistent_stop_rejects_new_work(self):
        spec = importlib.util.spec_from_file_location('wrapper_state_test', BASE/'scripts/qwen_service.py')
        wrapper = importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)
        native_post = Mock()
        class Native:
            do_POST = native_post
        class Server:
            verify_request = Mock(return_value=True)
        from types import SimpleNamespace
        server = SimpleNamespace(Server=Server, make_handler=lambda svc: Native)
        wrapper.install(server, ['192.168.0.0/24'])
        cls = server.make_handler(Mock())
        handler = object.__new__(cls)
        handler.path = '/v1/chat/completions'
        handler.client_address = ('127.0.0.1', 1)
        handler._json = Mock()
        with tempfile.TemporaryDirectory(dir=BASE/'tests') as folder:
            p = Path(folder)/'runtime'; p.mkdir()
            with patch.dict(os.environ, {'QWEN_LIFECYCLE_ROOT':folder}):
                for data in ('{"desired_state":"stopped"}', 'invalid-json'):
                    (p/'service-state.json').write_text(data, encoding='utf-8')
                    handler.do_POST()
                    self.assertEqual(handler._json.call_args.args[0], 503)
                native_post.assert_not_called()
                (p/'service-state.json').write_text('{"desired_state":"enabled"}', encoding='utf-8')
                handler.do_POST()
                native_post.assert_called_once()

    def test_enabled_service_can_cancel_pending_exit_before_load(self):
        with tempfile.TemporaryDirectory(dir=BASE/'tests') as folder:
            ready, calls = [False], []
            c = {'base_url':'http://127.0.0.1:19998/v1','model':'fake','startup_timeout_sec':2}
            def probe(c):
                return {'ready':ready[0], 'reachable':True, 'service':'strata', 'loaded':ready[0]}
            def request(url, *args, **kwargs):
                calls.append(url)
                if url.endswith('/stt/control'):
                    return {'instance_id':'fake-only', 'exiting':True}
                if url.endswith('/load'):
                    ready[0] = True
                return {}
            with patch.object(manager, 'ROOT', Path(folder)), patch.object(manager, 'config', return_value=c), \
                 patch.object(manager, 'probe', side_effect=probe), patch.object(manager, 'request_json', side_effect=request):
                runtime = manager.runtime()
                runtime.desire(True, 'test')
                self.assertTrue(runtime.ensure()['ok'])
            self.assertLess(calls.index(c['base_url']+'/stt/cancel-exit'), calls.index(c['base_url']+'/load'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
