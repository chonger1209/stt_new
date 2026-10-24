"""Shared lifecycle coordinator. Clients may request a task, never spawn the server."""
import json
import os
import subprocess
import threading
import time


class Runtime:
    def __init__(self, manager):
        self.m = manager

    def state(self):
        try:
            s = self.m.read_json(self.m.ROOT / 'runtime' / 'service-state.json')
            if s['desired_state'] not in ('enabled', 'stopped') or type(s['generation']) is not int:
                raise ValueError('invalid lifecycle state')
            return s
        except FileNotFoundError:
            return {'desired_state': 'stopped', 'generation': 0}

    def event(self, action, source, **data):
        with self.m.lock('runtime-events', timeout=2):
            with (self.m.ROOT / 'runtime' / 'service-events.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(dict(at=self.m.now(), action=action, source=source,
                                        pid=os.getpid(), **data), ensure_ascii=False) + '\n')

    def desire(self, enabled, source):
        with self.m.lock('service-state', timeout=2):
            old = self.state()
            new = dict(desired_state='enabled' if enabled else 'stopped',
                       generation=old['generation'] + 1, updated_by=source, updated_at=self.m.now())
            self.m.atomic_json(self.m.ROOT / 'runtime' / 'service-state.json', new)
        self.event('start_requested' if enabled else 'stop_requested', source, generation=new['generation'])
        return new

    def check(self, generation, cancelled=None):
        if cancelled:
            cancelled()
        s = self.state()
        if s['desired_state'] != 'enabled' or s['generation'] != generation:
            raise RuntimeError('生命周期状态已改变，停止等待；不强制结束后台服务')

    def request_task(self, c, timeout):
        name = c.get('scheduled_task')
        if not name:
            raise ValueError('未配置独立计划任务；禁止退回客户端直接启动')
        result = subprocess.run(['schtasks.exe', '/Run', '/TN', name],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('计划任务启动失败；未直接启动模型。请检查任务注册和登录用户')

    def ensure(self, source='client', only_service=False, explicit=False, cancelled=None):
        c = self.m.config()
        wait = float(c.get('startup_timeout_sec', 900))
        if not 0 < wait <= 900:
            raise ValueError('启动预算必须在 (0, 900] 秒内')
        deadline = time.monotonic() + wait
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('启动总预算耗尽；后台可能仍在加载，不重启、不重复加载')
            return left
        # Explicit Start is the only operation that clears manual Stop.
        if explicit:
            with self.m.lock('service-state', timeout=min(2, remaining())):
                if self.state()['desired_state'] != 'enabled':
                    s = self.state()
                    self.m.atomic_json(self.m.ROOT / 'runtime' / 'service-state.json',
                                       dict(desired_state='enabled', generation=s['generation'] + 1,
                                            updated_by=source, updated_at=self.m.now()))
        generation = self.state()['generation']
        self.check(generation, cancelled)
        # Long operations run in daemon workers so timeout bounds the caller even
        # if an HTTP peer drip-feeds bytes. Timeout does not cancel server loading.
        def call(fn, *args, **kwargs):
            done, outcome = threading.Event(), []
            def work():
                try:
                    outcome.append((True, fn(*args, **kwargs)))
                except Exception as e:
                    outcome.append((False, e))
                finally:
                    done.set()
            threading.Thread(target=work, daemon=True).start()
            while not done.is_set():
                self.check(generation, cancelled)
                done.wait(min(.05, remaining()))
            self.check(generation, cancelled)
            remaining()
            ok, result = outcome[0]
            if not ok:
                raise result
            return result
        with self.m.lock('startup', timeout=remaining()):
            self.check(generation, cancelled)
            launched = False
            while True:
                remaining()
                p = call(self.m.probe, c)
                if p.get('reachable'):
                    if p.get('error') or p.get('service') != 'strata':
                        raise RuntimeError('端口服务身份不匹配；未启动或停止任何服务')
                    if only_service or p.get('ready'):
                        if p.get('ready'):
                            (self.m.ROOT / 'runtime' / 'load-pending.json').unlink(missing_ok=True)
                        self.event('ready', source, only_service=only_service)
                        return dict(ok=True, message='接收服务已就绪' if only_service else '模型已就绪', **p)
                    marker = self.m.ROOT / 'runtime' / 'load-pending.json'
                    pending = False
                    try:
                        record = self.m.read_json(marker)
                        control = call(self.m.request_json, c['base_url'] + '/stt/control', c)
                        pending = record.get('instance_id') == control.get('instance_id') and bool(control.get('instance_id'))
                    except FileNotFoundError:
                        control = call(self.m.request_json, c['base_url'] + '/stt/control', c)
                    if control.get('exiting'):
                        self.check(generation, cancelled)
                        call(self.m.request_json, c['base_url'] + '/stt/cancel-exit', c,
                             {'model': c['model']})
                    if not pending:
                        if not control.get('instance_id'):
                            raise RuntimeError('接收服务缺少实例标识，需先迁移新版接收服务')
                        self.check(generation, cancelled)
                        self.m.atomic_json(marker, {'instance_id': control['instance_id'], 'at': self.m.now()})
                        self.event('load_requested', source)
                        # On timeout keep the marker: a subsequent client only polls.
                        call(self.m.request_json, c['base_url'] + '/load', c,
                             {'model': c['model']}, timeout=remaining())
                        marker.unlink(missing_ok=True)
                elif not launched:
                    if call(self.m.port_busy, c):
                        raise RuntimeError('端口已被占用；禁止重复启动')
                    self.check(generation, cancelled)
                    call(self.request_task, c, min(10, remaining()))
                    self.event('task_requested', source)
                    launched = True
                time.sleep(min(.1, remaining()))

    def stop(self, source='client'):
        self.desire(False, source)
        return {'ok': True, 'pending': True, 'message': '已禁止自动唤醒，独立宿主将等待在途请求完成后退出服务'}
