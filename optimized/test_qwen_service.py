"""Exercise the real Strata HTTP routes with its scripted MockEngine, never Qwen."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

from qwen_service import Admission, install


class AdmissionTests(unittest.TestCase):
    def test_exit_excludes_new_work_until_cancel(self):
        from unittest.mock import Mock
        gate = Admission()
        svc = Mock()
        svc.unload.return_value = 'unloaded'
        self.assertTrue(gate.enter())
        self.assertEqual(gate.control(svc, 'prepare-exit')[0], 409)
        svc.unload.assert_not_called()
        gate.leave()
        self.assertEqual(gate.control(svc, 'prepare-exit')[0], 200)
        self.assertFalse(gate.enter())
        gate.control(svc, 'cancel-exit')
        self.assertTrue(gate.enter())
        gate.leave()


class StrataMockHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(r'D:\Program Files\Strata\serve\server.py')
        spec = importlib.util.spec_from_file_location('_stt_mock_strata', path)
        cls.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.server
        spec.loader.exec_module(cls.server)
        install(cls.server, ['192.168.0.0/24'])

    def setUp(self):
        server = self.server
        class SleepableMock(server.MockEngine):
            resident = True
            loads = 0
            def alive(self):
                return self.resident
            def unload(self):
                self.resident = False
            def restart(self):
                self.resident = True
                self.loads += 1
        tok = server.ByteTokenizer()
        self.engine = SleepableMock(tok, 'mock answer', delay_s=.005)
        self.svc = server.Service(self.engine, tok,
            server.ChatTemplate(server.ROOT / 'serve/chat_template.jinja'), model_name='test-only')
        self.svc.api_key = 'isolated-test-key'
        self.svc.start_telemetry = lambda: None
        self.httpd = server.serve(self.svc, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:' + str(self.httpd.server_address[1])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, body=None, key=''):
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        request = urllib.request.Request(self.url + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request, timeout=5) as response:
            return response.read()

    def test_no_key_required_even_with_stale_service_key(self):
        for key in ('', 'wrong'):
            data = json.loads(self.request('/v1/models', key=key))
            self.assertEqual(data['data'][0]['id'], 'test-only')
        info = json.loads(self.request('/v1/stt/control'))
        self.assertEqual(info['model'], 'test-only')
        self.assertEqual(info['version'], 1)

    def test_admission_excludes_vpn_and_other_subnets(self):
        for address, expected in [('127.0.0.1', True), ('192.168.0.50', True),
                                  ('198.18.0.1', False), ('192.168.1.50', False), ('8.8.8.8', False)]:
            self.assertEqual(self.httpd.verify_request(None, (address, 45000)), expected)

    def test_lan_cannot_call_management_routes_even_without_auth(self):
        from unittest.mock import Mock
        handler = object.__new__(self.server.make_handler(self.svc))
        handler.client_address = ('192.168.0.50', 40000)
        handler._json = Mock()
        for path in ('/load', '/unload/', '/v1/load', '/v1/unload/',
                     '/settings', '/config', '/v1/vram', '/slots/0',
                     '/v1/stt/unload', '/v1/stt/cancel-exit'):
            handler.path = path
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0], 403, path)
        handler.path = '/v1/stt/control/'
        handler.do_GET()
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_work_in_progress_cannot_be_unloaded_or_exited(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.generate
        def generate(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture release timed out')
            yield from original(*args, **kwargs)
        self.engine.generate = generate
        outcome = []
        def request():
            try:
                outcome.append(json.loads(self.request('/v1/chat/completions', {
                    'model': 'test-only', 'messages': [{'role': 'user', 'content': 'fixture'}],
                    'max_tokens': 32})))
            except Exception as error:
                outcome.append(error)
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            for path in ('/v1/stt/unload', '/v1/stt/prepare-exit'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(path, {'model': 'test-only'})
                self.assertEqual(caught.exception.code, 409)
            self.assertTrue(self.engine.alive())
        finally:
            release.set()
            worker.join(4)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertIn('choices', outcome[0])

    def test_unload_then_request_reloads_with_same_listener(self):
        address = self.httpd.server_address
        self.request('/v1/stt/unload', {'model': 'test-only'})
        self.assertFalse(self.engine.alive())
        body = {'model': 'test-only', 'messages': [{'role': 'user', 'content': 'fixture'}],
                'max_tokens': 32, 'stream': False}
        response = json.loads(self.request('/v1/chat/completions', body))
        self.assertIn('choices', response)
        self.assertTrue(self.engine.alive())
        self.assertEqual(self.engine.loads, 1)
        self.assertEqual(address, self.httpd.server_address)
        self.request('/v1/stt/unload', {'model': 'test-only'})
        body['stream'] = True
        response = self.request('/v1/chat/completions', body)
        self.assertIn(b'data:', response)
        self.assertEqual(self.engine.loads, 2)

    def test_recent_completed_task_prevents_idle_unload(self):
        self.svc.last_request_at = time.time()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/v1/stt/unload', {'model': 'test-only', 'idle_seconds': 7200})
        self.assertEqual(caught.exception.code, 409)
        self.assertTrue(self.engine.alive())

    def test_exit_rejects_new_generation_and_cancel_recovers(self):
        self.request('/v1/stt/prepare-exit', {'model': 'test-only'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/v1/chat/completions', {'model': 'test-only', 'messages': []})
        self.assertEqual(caught.exception.code, 503)
        self.assertFalse(self.engine.alive())
        self.request('/v1/stt/cancel-exit', {'model': 'test-only'})
        self.request('/v1/load', {'model': 'test-only'})
        self.assertTrue(self.engine.alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
