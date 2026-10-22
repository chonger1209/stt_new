"""Qwen-drafted single-worker controller, reviewed for Tk safety and bounded events."""
import queue
import threading
import time
import urllib.error


class QwenController:
    def __init__(self, backend, interval=3.0, autostart=False):
        self.backend, self.interval = backend, max(.01, float(interval))
        self._requests = queue.Queue(maxsize=1)
        self._results = queue.Queue(maxsize=1)
        self._latest_status = None
        self._lock = threading.Lock()
        self._pending = 'ensure_service' if autostart else None
        if autostart:
            self._requests.put_nowait('ensure_service')
        self._next_idle_check = 0.0
        self._poll_fast = False
        self._closed = threading.Event()
        self._generation = 0
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(0, self._cancelled, self._requests),
                                        daemon=True, name='qwen-ctl')
        self._thread.start()

    @property
    def operation(self):
        with self._lock:
            return self._pending if self._pending in ('start', 'stop', 'force_stop', 'unload', 'ensure_service', 'reset_idle') else None

    def request(self, action):
        if action == 'force_stop':
            return self._request_force()
        if action not in ('status', 'start', 'stop', 'unload', 'ensure_service', 'reset_idle') or self._closed.is_set():
            return False
        with self._lock:
            if self._pending is not None:
                return False
            self._pending = action
            self._requests.put_nowait(action)
        return True

    def _request_force(self):
        with self._lock:
            if self._closed.is_set() or self._pending == 'force_stop':
                return False
            self._cancelled.set()
            self._generation += 1
            generation = self._generation
            self._cancelled = threading.Event()
            self._requests = queue.Queue(maxsize=1)
            self._latest_status = None
            while not self._results.empty():
                self._results.get_nowait()
            self._pending = 'force_stop'
            threading.Thread(target=self._force, args=(generation, self._cancelled, self._requests),
                             daemon=True, name='qwen-force-exit').start()
            return True

    def _force(self, generation, cancelled, requests):
        try:
            result = self.backend.force_stop()
        except Exception as error:
            result = {'ok': False, 'message': self._error(error)}
        with self._lock:
            if self._closed.is_set() or generation != self._generation:
                return
            self._results.put_nowait({'kind': 'operation', 'action': 'force_stop', 'result': result})
            self._latest_status = {'state': 'off' if result.get('ok') else 'error',
                                   'detail': result.get('message', '')}
            self._thread = threading.Thread(target=self._loop, args=(generation, cancelled, requests),
                                            daemon=True, name='qwen-ctl')
            self._thread.start()

    def _invoke(self, action, cancelled):
        if cancelled.is_set():
            raise RuntimeError('操作已取消')
        if hasattr(self.backend, 'execute'):
            return self.backend.execute(action, cancelled)
        return getattr(self.backend, action)()

    @staticmethod
    def _error(error):
        if isinstance(error, TimeoutError):
            return '服务正在处理其他请求或等待超时，请稍后重试'
        if isinstance(error, urllib.error.HTTPError):
            return f'本地服务响应失败（HTTP {error.code}），未强制结束进程'
        if isinstance(error, FileNotFoundError):
            return '未找到用户级千问管理工具或配置的启动程序'
        if isinstance(error, PermissionError) or type(error).__name__ == 'AccessDenied':
            return '权限不足，操作未完成；未确认服务已退出'
        if isinstance(error, ValueError):
            return '无法核验千问服务身份或配置，未结束不明进程'
        return '操作未完成，请检查本地服务和全局千问配置'

    def _status(self, generation, cancelled):
        try:
            status = self._invoke('status', cancelled)
            if (not self._closed.is_set() and not cancelled.is_set() and status.get('state') == 'ready'
                    and time.monotonic() >= self._next_idle_check
                    and hasattr(self.backend, 'idle_shutdown')):
                self._next_idle_check = time.monotonic() + 10
                try:
                    result = self._invoke('idle_shutdown', cancelled)
                    if result and result.get('ok'):
                        status = self._invoke('status', cancelled)
                except Exception:
                    # Unknown activity / contended locks never justify shutting down.
                    # Keep displaying status and retry on the next bounded check.
                    pass
        except Exception as error:
            status = {'state': 'error', 'detail': self._error(error)}
        with self._lock:
            if generation == self._generation and not cancelled.is_set():
                self._latest_status = status
                self._poll_fast = status.get('state') == 'busy'

    def _loop(self, generation, cancelled, requests):
        self._status(generation, cancelled)
        while not self._closed.is_set() and not cancelled.is_set():
            try:
                action = requests.get(timeout=min(self.interval, 1.0) if self._poll_fast else self.interval)
            except queue.Empty:
                self._status(generation, cancelled)
                continue
            if action is None or self._closed.is_set() or cancelled.is_set():
                break
            if action == 'status':
                self._status(generation, cancelled)
                with self._lock:
                    if generation == self._generation:
                        self._pending = None
                continue
            try:
                result = self._invoke(action, cancelled)
            except Exception as error:
                result = {'ok': False, 'message': self._error(error)}
            # Preserve the operation result until the UI drains it; coalesce probes.
            with self._lock:
                if generation != self._generation or cancelled.is_set():
                    return
                self._results.put_nowait({'kind': 'operation', 'action': action, 'result': result})
            self._status(generation, cancelled)

    def drain_events(self):
        events = []
        with self._lock:
            try:
                events.append(self._results.get_nowait())
                self._pending = None
            except queue.Empty:
                pass
            if self._latest_status is not None:
                events.append({'kind': 'status', 'status': self._latest_status})
                self._latest_status = None
        return events

    def close(self):
        self._closed.set()
        self._cancelled.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        # Never join on the Tk thread; never stop the shared model on STT exit.
