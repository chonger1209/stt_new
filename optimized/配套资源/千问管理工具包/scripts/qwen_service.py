"""Small Strata adapter: subnet admission and atomic local lifecycle commands.

The original server and model implementation stay untouched. Importing this
module does not import Strata, start a listener, or load a model.
"""
import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import sys
import threading
import os
import uuid
from urllib.parse import urlsplit


class Admission:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.exiting = False

    def enter(self):
        with self.lock:
            if self.exiting:
                return False
            self.active += 1
            return True

    def leave(self):
        with self.lock:
            self.active -= 1

    def control(self, svc, action, idle_seconds=None):
        with self.lock:
            if action == 'cancel-exit':
                self.exiting = False
                return 200, {'status': 'accepting'}
            if self.active:
                return 409, {'status': 'busy'}
            # Native FIFO and activity timestamp checks run while admission is
            # locked: queued/just-completed LAN work cannot slip past this check.
            outcome = svc.unload(idle_for=idle_seconds)
            if outcome not in ('unloaded', 'not loaded'):
                return 409, {'status': outcome}
            if action == 'prepare-exit':
                self.exiting = True
            return 200, {'status': outcome, 'exiting': self.exiting}


def install(server, networks):
    allowed = [ipaddress.ip_network(value) for value in networks]
    original_verify = server.Server.verify_request

    def verify(instance, request, address):
        peer = ipaddress.ip_address(address[0])
        return (peer.is_loopback or any(peer in network for network in allowed)) and original_verify(
            instance, request, address)

    server.Server.verify_request = verify
    native_factory = server.make_handler

    def make_handler(svc):
        # This deployment intentionally uses subnet admission, not API keys.
        svc.api_key = None
        gate = Admission()
        instance_id = str(uuid.uuid4())
        native = native_factory(svc)

        class Handler(native):
            def do_GET(self):
                if urlsplit(self.path).path.rstrip('/') != '/v1/stt/control':
                    return super().do_GET()
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self._json(403, {'error': 'local lifecycle control only'})
                    return
                if not self._authorized():
                    return
                self._json(200, {'version': 1, 'exiting': gate.exiting, 'instance_id': instance_id,
                                 'model': svc.model, 'service': 'strata'})

            def do_POST(self):
                path = urlsplit(self.path).path.rstrip('/')
                if (path in ('/load', '/unload', '/v1/load', '/v1/unload', '/settings', '/config', '/v1/vram')
                        or path.startswith('/slots/')) and not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self._json(403, {'error': 'local lifecycle control only'})
                    return
                if path.startswith('/v1/stt/'):
                    if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                        self._json(403, {'error': 'local lifecycle control only'})
                        return
                    if not self._authorized():
                        return
                    action = path.rsplit('/', 1)[-1]
                    if action not in ('unload', 'prepare-exit', 'cancel-exit'):
                        self._json(404, {'error': 'unknown control action'})
                        return
                    try:
                        body = json.loads(self._body() or b'{}')
                        if not isinstance(body, dict) or body.get('model') != svc.model:
                            raise ValueError('model mismatch')
                        idle = body.get('idle_seconds')
                        if idle is not None and (type(idle) is not int or idle <= 0):
                            raise ValueError('idle_seconds must be a positive integer')
                        code, result = gate.control(svc, action, idle)
                        self._json(code, result)
                    except (ValueError, TypeError):
                        self._json(400, {'error': 'invalid lifecycle request'})
                    except Exception:
                        self._json(503, {'error': 'lifecycle operation failed'})
                    return
                # Fail closed on corrupt/missing state when managed by the task.
                state_root = os.environ.get('QWEN_LIFECYCLE_ROOT')
                if state_root:
                    try:
                        state = json.loads((Path(state_root) / 'runtime/service-state.json').read_text(encoding='utf-8'))
                        enabled = state.get('desired_state') == 'enabled'
                    except (OSError, ValueError):
                        enabled = False
                    if not enabled:
                        self._json(503, {'error': 'service manually stopped; request was not executed'})
                        return
                if not gate.enter():
                    self._json(503, {'error': 'service is exiting; request was not executed'})
                    return
                try:
                    return super().do_POST()
                finally:
                    gate.leave()

        return Handler

    server.make_handler = make_handler


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--strata-server', required=True)
    parser.add_argument('--allow-subnet', action='append', default=[])
    args, remaining = parser.parse_known_args()
    path = Path(args.strata_server).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('_stt_strata_server', path)
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
    install(server, args.allow_subnet)
    sys.argv = [str(path), *remaining]
    return server.main()


if __name__ == '__main__':
    raise SystemExit(main())
