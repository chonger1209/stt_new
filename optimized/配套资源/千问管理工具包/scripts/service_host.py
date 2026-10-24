"""Persistent task action. Owns the service, never watches Codex or STT."""
import os
import subprocess
import time
import urllib.error
from pathlib import Path
import manager
import lifecycle

# A task action always owns its sibling configuration, never an inherited client root.
manager.ROOT = Path(__file__).resolve().parents[1]


def main():
    runtime = manager.runtime()
    with manager.lock('service-host', timeout=.2):
        c = manager.config()
        with manager.lock('service-state', timeout=2):
            if runtime.state()['desired_state'] != 'enabled':
                runtime.event('host_skipped_manual_stop', 'task')
                return 0
            if manager.port_busy(c):
                runtime.event('host_port_occupied', 'task')
                return 2
            launch = c['launch']
            env = manager.service_environment(c)
            env['QWEN_LIFECYCLE_ROOT'] = str(manager.ROOT)
            with (manager.ROOT / 'runtime' / 'service.log').open('ab') as log:
                process = subprocess.Popen([launch['command'], *launch['args']], cwd=launch['cwd'],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            manager.atomic_json(manager.ROOT / 'runtime' / 'last-launch.json',
                                dict(pid=process.pid, at=manager.now(), model=c['model']))
            runtime.event('service_started', 'task', child_pid=process.pid, host_pid=os.getpid())
        last_error = None
        while process.poll() is None:
            if runtime.state()['desired_state'] == 'stopped':
                try:
                    # Admission rejects new work as soon as desired state is stopped.
                    # A busy unload is retried; no terminate while work is running.
                    result = manager.request_json(c['base_url'] + '/stt/prepare-exit', c,
                                                  {'model': c['model']}, timeout=5)
                    if result.get('status') in ('unloaded', 'not loaded'):
                        with manager.lock('service-state', timeout=2):
                            if runtime.state()['desired_state'] == 'stopped':
                                with manager.lock('model-request', timeout=.2), manager.lock('startup', timeout=.2):
                                    service = lifecycle.identify_service(c)
                                    if service['root']['pid'] != process.pid:
                                        raise RuntimeError('owned process identity mismatch')
                                    if lifecycle.stop_verified_service(c, service):
                                        runtime.event('service_stopped', 'task', child_pid=process.pid)
                except (OSError, ValueError, RuntimeError, TimeoutError) as e:
                    name = type(e).__name__
                    if name != last_error:
                        runtime.event('stop_pending', 'task', error_type=name)
                        last_error = name
            time.sleep(.2)
        runtime.event('service_exited', 'task', child_pid=process.pid, exit_code=process.returncode,
                      reason='requested_stop' if runtime.state()['desired_state'] == 'stopped' else 'unknown')
        return process.returncode or 0


if __name__ == '__main__':
    raise SystemExit(main())
