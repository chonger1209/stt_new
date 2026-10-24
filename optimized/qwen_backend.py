"""STT adapter to the installed Qwen toolkit; never owns a second switch/config file."""
import importlib.util
import ctypes
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request  # Required by the dynamically loaded toolkit in a frozen EXE.
from contextlib import contextmanager, ExitStack

import psutil  # Also makes the toolkit's process dependency available in the EXE.
from qwen_status import metrics_display
from qwen_errors import ServiceLockBusy
from qwen_config import effective_config, toolkit_scripts


@contextmanager
def startup_locks(manager):
    with ExitStack() as stack:
        try:
            stack.enter_context(manager.lock('model-request', timeout=.3))
            stack.enter_context(manager.lock('startup', timeout=.3))
        except TimeoutError as error:
            raise ServiceLockBusy('其他操作正在使用千问启动锁') from error
        # Do not classify transport timeouts raised by the body as lock contention.
        yield


def idle_remaining(activity, reset_at, now, timeout=7200):
    """None means unknown/busy, never permission to stop a service."""
    last = activity.get('last_request_at')
    busy = activity.get('in_flight')
    if (not isinstance(busy, (int, float)) or not math.isfinite(busy) or busy != 0
            or not isinstance(last, (int, float)) or isinstance(last, bool)
            or not math.isfinite(last) or last <= 0 or last > now):
        return None
    if not isinstance(reset_at, (int, float)) or isinstance(reset_at, bool) or not math.isfinite(reset_at):
        return None
    if reset_at > now:
        return None
    return max(0.0, timeout - (now - max(last, reset_at)))


def launch_external(command, **kwargs):
    """Do not leak the frozen app's DLL directory into Strata's Python."""
    if not (getattr(sys, 'frozen', False) and os.name == 'nt'):
        return subprocess.Popen(command, **kwargs)
    bundle = Path(sys._MEIPASS).resolve()
    environment = dict(kwargs.pop('env', os.environ))
    environment['PATH'] = os.pathsep.join(
        item for item in environment.get('PATH', '').split(os.pathsep)
        if item and not Path(item).resolve().is_relative_to(bundle))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    kernel.GetDllDirectoryW.restype = ctypes.c_uint32
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    length = kernel.GetDllDirectoryW(0, None)
    previous = ctypes.create_unicode_buffer(length) if length else None
    if previous is not None:
        kernel.GetDllDirectoryW(length, previous)
    if not kernel.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return subprocess.Popen(command, env=environment, **kwargs)
    finally:
        if not kernel.SetDllDirectoryW(previous.value if previous is not None else None):
            raise ctypes.WinError(ctypes.get_last_error())


def find_toolkit():
    for path in [Path.home() / '.agents/skills/qwen-collaboration',
                 Path.home() / '.codex/skills/qwen-collaboration']:
        scripts = path.resolve() / 'scripts'
        if (scripts / 'manager.py').is_file() and (scripts / 'lifecycle.py').is_file():
            return scripts
    raise FileNotFoundError('未找到用户级千问管理工具，请先安装千问协作技能')


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolkitBackend:
    def __init__(self, scripts=None, idle_minutes=120):
        self.scripts = scripts
        self.manager = self.lifecycle = None
        self._import_lock = threading.RLock()
        self._launch_gate = threading.Lock()
        self._context = threading.local()
        self._launched = None
        self._settings = None
        self.enabled = True
        self.set_idle_minutes(idle_minutes)

    def configure(self, settings):
        # Called on refresh: stage values only. Never change a live service here.
        self._settings = dict(settings)
        self.enabled = settings.get('qwen_enabled', True)
        self.set_idle_minutes(settings['qwen_idle_timeout_minutes'])

    def _desired(self, config):
        return effective_config(self._settings, config) if self._settings is not None else config

    def _activate_settings(self, manager, config):
        desired = self._desired(config)
        if desired == config:
            return config
        # Address/model/launch changes must not repoint management at a running
        # process. Stop the old service explicitly, then start with the new config.
        launch_changed = any(desired.get(key) != config.get(key) for key in ('base_url','model','launch'))
        if launch_changed and manager.port_busy(config):
            raise ValueError('千问配置已暂存：请先退出原服务，再点击启动服务使新路径/模型生效')
        path = manager.ROOT / 'service.json'
        backup = path.with_name('service.json.before-stt-config.bak')
        if not backup.exists() and path.exists():
            with backup.open('xb') as handle:
                handle.write(path.read_bytes())
        self._check_cancelled()
        manager.atomic_json(path, desired)
        return desired

    def execute(self, action, cancelled):
        self._context.cancelled = cancelled
        try:
            self._check_cancelled()
            if not self.enabled and action != 'status':
                return {'ok': False, 'message': 'qwen_enabled=false：千问管理已关闭'}
            return getattr(self, action)()
        finally:
            self._context.cancelled = None

    def _check_cancelled(self):
        event = getattr(self._context, 'cancelled', None)
        if event is not None and event.is_set():
            raise RuntimeError('操作已被强制退出取消')

    def set_idle_minutes(self, minutes):
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError('闲置时间必须是正整数分钟')
        self.idle_seconds = minutes * 60

    def _load(self):
        with self._import_lock:
            return self._load_locked()

    def _load_locked(self):
        if self.manager is None:
            requested = (self._settings or {}).get('qwen_toolkit_path', '')
            self.scripts = toolkit_scripts(requested) if requested else (Path(self.scripts) if self.scripts else find_toolkit())
            manager = _module('_stt_qwen_manager', self.scripts / 'manager.py')
            # lifecycle imports manager by its original name. Bind it only during
            # import, then restore the application's module namespace.
            previous = sys.modules.get('manager')
            sys.modules['manager'] = manager
            try:
                lifecycle = _module('_stt_qwen_lifecycle', self.scripts / 'lifecycle.py')
            finally:
                if previous is None:
                    sys.modules.pop('manager', None)
                else:
                    sys.modules['manager'] = previous
            self.manager, self.lifecycle = manager, lifecycle
        return self.manager, self.lifecycle

    def status(self):
        if not self.enabled:
            return {'state': 'disabled', 'detail': 'qwen_enabled=false：千问管理已关闭，不查询或控制服务'}
        try:
            manager, lifecycle = self._load()
            config = manager.config()
            try:
                if self._settings and self._settings.get('qwen_toolkit_path'):
                    toolkit_scripts(self._settings['qwen_toolkit_path'])
                desired = self._desired(config)
                path_changed = bool(self._settings and self._settings.get('qwen_toolkit_path') and toolkit_scripts(self._settings['qwen_toolkit_path']) != self.scripts)
                notice = '\n配置有待生效的修改，请在任务结束后退出原服务，再点击启动服务。' if desired != config or path_changed else ''
            except ValueError as error:
                return {'state':'error', 'detail': str(error)}
            switches = manager.state()
            probe = manager.probe(config)
            base = {'model': config['model'], 'enabled': switches['enabled'],
                    'auto_start': switches['auto_start']}
            if not probe.get('reachable'):
                if manager.port_busy(config):
                    return dict(base, state='error', detail='本地端口有服务但无法核验身份，暂不提供启停')
                return dict(base, state='off', detail='千问服务已停止' + notice)
            if probe.get('error') or probe.get('service') != 'strata':
                return dict(base, state='error', detail='服务身份或模型不匹配，未执行任何启停')
            activity = lifecycle.service_activity(config)
            presentation = {'display_phase': 'busy' if activity['in_flight'] > 0 else 'idle',
                            'generation_tps': None, 'metrics_available': False, 'metrics_active': False}
            if activity['loaded'] or activity['in_flight'] > 0:
                try:
                    from urllib.parse import urlsplit, urlunsplit
                    parts = urlsplit(config['base_url'])
                    url = urlunsplit((parts.scheme, parts.netloc, '/metrics', '', ''))
                    payload = manager.request_json(url, config, timeout=.8)
                    presentation = metrics_display(payload, activity['loaded'], activity['in_flight'])
                except (OSError, ValueError, TypeError):
                    pass  # Optional metrics must not make a healthy service unusable.
            base.update(presentation)
            if config.get('lifecycle', {}).get('owner') == 'stt' and not config['lifecycle'].get('enabled'):
                now = time.time()
                remaining = (idle_remaining(activity, self._reset_at(manager), now, self.idle_seconds)
                             if activity['loaded'] and not presentation['metrics_active'] else None)
                base.update(idle_remaining_seconds=remaining,
                            idle_deadline=now + remaining if remaining is not None else None)
            if activity['in_flight'] > 0 or presentation['metrics_active']:
                if presentation['display_phase'] in ('idle', 'unloaded'):
                    base['display_phase'] = 'busy'  # A non-atomic endpoint transition cannot declare it idle.
                detail = '千问正在处理或排队任务；强制退出会中断任务'
                if presentation.get('phase_detail'):
                    detail += '\n阶段：' + presentation['phase_detail']
                if presentation['metrics_available']:
                    detail += '\n执行：{}；排队：{}；引擎等待：{}'.format(
                        presentation['running_count'], presentation['queued_count'], presentation['waiting_count'])
                    if presentation.get('prompt_total') and presentation.get('prompt_read') is not None:
                        detail += '\n上下文进度：{} / {} token'.format(presentation['prompt_read'], presentation['prompt_total'])
                else:
                    detail += '\n细分指标暂不可用，保留忙碌保护'
                return dict(base, state='busy', detail=detail + notice)
            if not activity['loaded']:
                return dict(base, state='unloaded', detail='模型已卸载，接收服务保留；本地或局域网请求可自动唤醒' + notice)
            return dict(base, state='ready', detail='千问模型已就绪' + notice)
        except FileNotFoundError as error:
            return {'state': 'unconfigured', 'detail': '未找到用户级千问管理工具或服务配置：' + str(error)}
        except urllib.error.HTTPError as error:
            return {'state': 'error', 'detail': f'本地服务查询失败（HTTP {error.code}）'}
        except Exception:
            return {'state': 'error', 'detail': '千问状态查询失败，请检查全局管理工具和本地服务'}

    @staticmethod
    def _reset_at(manager):
        path = manager.ROOT / 'runtime' / 'stt-idle-reset.json'
        try:
            return manager.read_json(path)['at']
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError, KeyError, TypeError):
            return float('nan')  # Corrupt metadata is not permission to close.

    def reset_idle(self):
        manager, lifecycle = self._load()
        with manager.lock('model-request', timeout=.3), manager.lock('startup', timeout=.3):
            config = manager.config()
            if config.get('lifecycle', {}).get('owner') != 'stt' or config['lifecycle'].get('enabled'):
                return {'ok': False, 'message': '闲置关闭尚未由 STT 接管'}
            lifecycle.service_activity(config)  # Verify service identity before resetting.
            manager.atomic_json(manager.ROOT / 'runtime' / 'stt-idle-reset.json', {'at': time.time()})
            return {'ok': True, 'message': f'已重置：接下来{self.idle_seconds // 60}分钟内不会因闲置卸载模型'}

    def idle_shutdown(self):
        return self.unload(idle_seconds=self.idle_seconds)

    def ensure_service(self):
        self._check_cancelled()
        status = self.status()
        self._check_cancelled()
        if status.get('state') in ('ready', 'busy', 'unloaded'):
            return {'ok': True, 'message': '已检测到千问接收服务，保留当前模型状态'}
        if status.get('state') in ('disabled', 'unconfigured', 'error'):
            return {'ok': False, 'message': status.get('detail', '千问配置或状态需要检查'),
                    'retryable': False}
        return self.start(only_service=True)

    @staticmethod
    def _control(manager, config):
        if not config.get('stt_control'):
            return None
        try:
            result = manager.request_json(config['base_url'] + '/stt/control', config)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None  # A loopback-only old service can be closed for upgrade.
            raise
        if (result.get('version') != 1 or result.get('model') != config['model']
                or result.get('service') != 'strata'):
            raise ValueError('服务控制协议不匹配')
        return result

    def _watchdog(self, config):
        """Use the configured Python, never sys.executable when STT is frozen."""
        manager, lifecycle = self._load()
        if not config.get('lifecycle', {}).get('enabled'):
            return
        with manager.lock('watchdog-spawn', timeout=2):
            runtime = manager.ROOT / 'runtime'
            runtime.mkdir(exist_ok=True)
            record = runtime / 'watchdog.json'
            try:
                if lifecycle.alive(manager.read_json(record)):
                    return
            except (OSError, ValueError, KeyError):
                pass
            # The installed toolkit is Python; the same Strata interpreter runs its watcher.
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
            with (runtime / 'watchdog.log').open('ab') as log:
                process = launch_external(
                    [config['launch']['command'], '-X', 'utf8', '-B', str(self.scripts / 'lifecycle.py'), 'watch'],
                    cwd=str(manager.ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=flags)
            manager.atomic_json(record, dict(lifecycle.identity(process.pid), started_at=manager.now()))

    def start(self, only_service=False):
        self._check_cancelled()
        manager, lifecycle = self._load()
        config = manager.config()
        desired = self._desired(config)
        if any(desired.get(k) != config.get(k) for k in ('base_url', 'model', 'launch')):
            return {'ok': False, 'message': '启动配置已暂存；独立任务配置须一并迁移，未直接启动服务'}
        # Do not activate launch-path changes against an existing task here.
        # Deployment must update task/config together after migration approval.
        try:
            return manager.runtime().ensure(source='stt', only_service=only_service,
                explicit=not only_service, cancelled=self._check_cancelled)
        except Exception as e:
            return {'ok': False, 'message': str(e)}

    def force_stop(self):
        """Explicit user abort: OS identities only, no HTTP or model locks."""
        manager, lifecycle = self._load()
        manager.runtime().desire(False, 'stt-force-stop')
        config = manager.config()
        with self._launch_gate:
            roots = []
            if manager.port_busy(config):
                service = lifecycle.identify_service(config)
                roots.append(service['root'])
            if self._launched and lifecycle.alive(self._launched):
                process = psutil.Process(self._launched['pid'])
                if not lifecycle.server_matches(process, config):
                    raise ValueError('启动进程身份不匹配，未结束进程')
                if self._launched not in roots:
                    roots.append(self._launched)
            targets = {}
            suspended = []
            try:
                # Suspend parents before enumerating descendants so a loading
                # model cannot spawn an untracked child during the snapshot.
                def collect(process):
                    record = lifecycle.identity(process.pid)
                    key = (record['pid'], record['created'])
                    if key in targets:
                        return
                    process.suspend()
                    suspended.append((process, record))
                    targets[key] = (process, record)
                    for child in process.children():
                        try:
                            collect(child)
                        except psutil.NoSuchProcess:
                            pass
                for record in roots:
                    if lifecycle.alive(record):
                        collect(psutil.Process(record['pid']))
                for process, record in reversed(list(targets.values())):
                    if lifecycle.alive(record):
                        process.kill()
                _, pending = psutil.wait_procs([p for p, _ in targets.values()], timeout=5)
                if pending or manager.port_busy(config):
                    return {'ok': False, 'message': '强制退出未完成：仍有进程或端口占用，请查看状态'}
                self._launched = None
                return {'ok': True, 'message': '千问服务已强制退出，正在执行的任务已中断'}
            except psutil.AccessDenied:
                return {'ok': False, 'message': '权限不足，强制退出未完成'}
            finally:
                for process, record in suspended:
                    try:
                        if lifecycle.alive(record):
                            process.resume()
                    except psutil.NoSuchProcess:
                        pass

    def stop(self):
        manager, _ = self._load()
        return manager.runtime().stop(source='stt')

    def unload(self, idle_seconds=None):
        return self._shutdown(idle_seconds=idle_seconds, terminate=False)

    def _shutdown(self, idle_seconds=None, terminate=False):
        self._check_cancelled()
        manager, lifecycle = self._load()
        with manager.lock('model-request', timeout=.3), manager.lock('startup', timeout=.3):
            config = manager.config()
            automatic = idle_seconds is not None
            if automatic and (config.get('lifecycle', {}).get('owner') != 'stt'
                              or config['lifecycle'].get('enabled')):
                return None
            if not manager.port_busy(config):
                return {'ok': True, 'message': '千问服务已经停止'}
            activity = lifecycle.service_activity(config)
            if automatic and not activity.get('loaded'):
                return None
            if automatic and idle_remaining(activity, self._reset_at(manager), time.time(), idle_seconds) != 0:
                return None
            if activity['in_flight']:
                return {'ok': False, 'message': '千问正在处理或排队任务，未关闭；请空闲后重试'}
            service = lifecycle.identify_service(config)
            control = self._control(manager, config)
            if (terminate and not control and config.get('stt_control')
                    and any(address not in ('127.0.0.1', '::1')
                            for address in service.get('listener_addresses', []))):
                return {'ok': False, 'message': '旧版局域网服务缺少安全退出协议，未结束进程'}
            if automatic:
                # Re-read activity and manual extension after identifying the process tree.
                activity = lifecycle.service_activity(config)
                if idle_remaining(activity, self._reset_at(manager), time.time(), idle_seconds) != 0:
                    return None
            body = {'model': config['model']}
            if control and automatic:
                body['idle_seconds'] = idle_seconds
            endpoint = ('/stt/prepare-exit' if terminate else '/stt/unload') if control else '/unload'
            self._check_cancelled()
            result = manager.request_json(config['base_url'] + endpoint, config, body, timeout=75)
            if result.get('status') not in ('unloaded', 'not loaded'):
                return {'ok': False, 'message': '未确认模型已卸载，未结束任何服务进程'}
            if not terminate:
                lifecycle.event('stt_idle_unload' if automatic else 'stt_manual_unload',
                                root_pid=service['root']['pid'], model=config['model'])
                return {'ok': True, 'message': '模型已卸载，接收服务保留，可自动唤醒'}
            stopped = False
            try:
                stopped = lifecycle.stop_verified_service(config, service)
                if not stopped:
                    return {'ok': False, 'message': '服务状态已变化或尚未完全退出，请查看最新状态'}
            finally:
                if control and not stopped:
                    try:
                        manager.request_json(config['base_url'] + '/stt/cancel-exit', config,
                                             {'model': config['model']})
                    except Exception:
                        pass  # Explicit start recovers an interrupted exit on next use.
            lifecycle.event('stt_idle_stop' if automatic else 'stt_manual_stop',
                            root_pid=service['root']['pid'], model=config['model'])
            return {'ok': True, 'message': '千问服务已完全停止；后续任务仍可按全局规则自动启动'}
