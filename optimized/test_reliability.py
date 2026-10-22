import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from qwen_config import migrate_existing, effective_config, toolkit_scripts
from qwen_backend import ToolkitBackend
from settings import DEFAULTS, read_settings, complete_config_text
from tracking import UsageTracker, active_seconds
from storage import AsyncHistoryStore
from process_identity import executable_name


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.wall = [dt.datetime(2026,10,22,12).timestamp()]
        self.active = [100.0]
        self.store = Mock()
        self.store.totals.return_value = {}
        self.store.history.return_value = []
        self.tracker = UsageTracker(self.store, lambda:self.wall[0], lambda:self.active[0])
        self.tracker.switch('A')

    def test_wall_jumps_never_change_elapsed_usage(self):
        for correction in (3600,-7200):
            self.wall[0] += correction+10
            self.active[0] += 10
            self.tracker.capture()
        self.assertEqual(sum(r[2] for r in self.tracker.pending),20)

    def test_sleep_excluded_and_foreground_reidentified(self):
        self.wall[0] += 7205
        self.active[0] += 5
        self.assertTrue(self.tracker.heartbeat())
        self.assertIsNone(self.tracker.app)
        self.assertEqual(sum(r[2] for r in self.tracker.pending),5)
        self.tracker.switch('B')
        self.wall[0] += 3; self.active[0] += 3
        self.assertEqual(self.tracker.snapshot()[0], {'A':5,'B':3})

    def test_dual_clock_midnight_conserves_duration(self):
        self.wall[0] = dt.datetime(2026,10,22,23,59,55).timestamp()
        self.tracker.capture(); self.tracker.pending.clear()
        self.wall[0] += 10; self.active[0] += 10
        self.tracker.capture()
        self.assertEqual([r[2] for r in self.tracker.pending], [5,5])
        self.assertEqual([r[0][:10] for r in self.tracker.pending], ['2026-10-22','2026-10-23'])

    def test_ack_mismatch_recovers_without_resubmitting_committed_rows(self):
        self.active[0] += 5; self.wall[0] += 5
        self.tracker.capture(); self.tracker.flush()
        old = self.tracker.pending[0]
        callback = self.store.submit_intervals.call_args.args[1]
        self.active[0] += 3; self.wall[0] += 3
        self.tracker.capture()
        new = self.tracker.pending[-1]
        self.tracker.pending.reverse()
        with self.assertLogs(level='ERROR'):
            callback(None)
        self.assertNotIn(old,self.tracker.pending)
        self.assertIs(self.tracker._inflight[0],new)
        self.assertEqual(self.store.submit_intervals.call_count,2)

    def test_failed_submission_does_not_stick_inflight(self):
        self.active[0] += 1; self.wall[0] += 1
        self.tracker.capture()
        self.store.submit_intervals.side_effect = RuntimeError('closed')
        with self.assertRaises(RuntimeError): self.tracker.flush()
        self.assertIsNone(self.tracker._inflight)
        self.assertEqual(len(self.tracker.pending),1)

    def test_ack_recovery_keeps_equal_valued_but_distinct_later_sample(self):
        self.active[0] += 1; self.wall[0] += 1
        self.tracker.capture(); self.tracker.flush()
        old=self.tracker.pending[0]
        callback=self.store.submit_intervals.call_args.args[1]
        new=tuple(list(old))
        self.assertIsNot(old,new)
        self.tracker.pending[:]=[new]
        with self.assertLogs(level='ERROR'): callback(None)
        self.assertIs(self.tracker._inflight[0],new)
        self.assertEqual(self.store.submit_intervals.call_count,2)

    def test_windows_awake_clock_advances(self):
        before = active_seconds()
        time.sleep(.01)
        self.assertGreater(active_seconds(),before)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scripts = self.root/'toolkit/scripts'; self.scripts.mkdir(parents=True)
        for n in ('manager.py','lifecycle.py'): (self.scripts/n).write_text('',encoding='utf-8')
        self.python = self.root/'python.exe'; self.python.touch()
        self.c = {'base_url':'http://127.0.0.1:8765/v1','model':'actual-model',
                  'launch':{'command':str(self.python),'cwd':str(self.root),'args':['serve.py','--lazy']},
                  'startup_timeout_sec':300,'api_key_required':False,'allow_subnets':['192.168.0.0/24'],
                  'lifecycle':{'enabled':False,'owner':'stt','shutdown_mode':'unload'}}
        (self.scripts.parent/'service.json').write_text(json.dumps(self.c),encoding='utf-8')
        self.path = self.root/'config.txt'

    def tearDown(self): self.temp.cleanup()

    def test_migration_preserves_user_fields_comments_and_actual_launch(self):
        original = '# 我的注释\nwindow_x = -1920\nqwen_model_id = keep-mine\n[LockApp.exe]\nshow_in_screen_time = false\n'
        self.path.write_text(original,encoding='utf-8')
        migrate_existing(self.path,self.scripts)
        settings = read_settings(self.path)
        self.assertEqual(settings['window_x'],-1920)
        self.assertEqual(settings['qwen_model_id'],'keep-mine')
        self.assertEqual(settings['qwen_base_url'],self.c['base_url'])
        self.assertEqual(json.loads(settings['qwen_launch_args']),self.c['launch']['args'])
        self.assertEqual(settings['qwen_startup_timeout_seconds'],300)
        self.assertFalse(settings['process_config']['LockApp.exe']['show_in_screen_time'])
        self.assertIn('# 我的注释',self.path.read_text(encoding='utf-8'))
        once = self.path.read_bytes(); migrate_existing(self.path,self.scripts); read_settings(self.path)
        self.assertEqual(self.path.read_bytes(),once)
        self.assertEqual((self.scripts.parent/'service.json').read_text(),json.dumps(self.c))

    def test_blank_overrides_preserve_network_and_auth_policy(self):
        c = effective_config(DEFAULTS,self.c)
        self.assertEqual(c['launch'],self.c['launch'])
        self.assertFalse(c['api_key_required'])
        self.assertEqual(c['allow_subnets'],self.c['allow_subnets'])

    def test_invalid_values_identify_the_field(self):
        for key,value in [('qwen_base_url','http://192.168.0.2:8765/v1'),('qwen_base_url','http://127.0.0.1:bad/v1'),
                          ('qwen_python_path',str(self.root/'missing')),('qwen_working_directory',str(self.python)),
                          ('qwen_launch_args','--bad string'),('qwen_launch_args','[1]'),('qwen_startup_timeout_seconds',0)]:
            with self.subTest(key=key,value=value), self.assertRaisesRegex(ValueError,key):
                effective_config({**DEFAULTS,key:value},self.c)

    def test_invalid_boolean_uses_default_and_warns(self):
        self.path.write_text('always_on_top=ture\n[foo.exe]\nshow_in_time_stream=flase\n',encoding='utf-8')
        with self.assertLogs(level='WARNING'):
            c = read_settings(self.path)
        self.assertTrue(c['always_on_top'])
        self.assertTrue(c['process_config']['foo.exe']['show_in_time_stream'])

    def test_old_documented_config_gets_missing_fields_before_processes(self):
        self.path.write_text('# 配置说明版本: 2\nwindow_width=345\n[foo.exe]\ndisplay_name=保留\n',encoding='utf-8')
        c=read_settings(self.path)
        self.assertEqual(c['window_width'],345)
        self.assertIn('qwen_launch_args',c)
        self.assertEqual(c['process_config']['foo.exe']['display_name'],'保留')
        self.assertLess(self.path.read_text(encoding='utf-8').index('qwen_launch_args'),self.path.read_text(encoding='utf-8').index('[foo.exe]'))

    def test_disabled_backend_performs_no_discovery_or_requests(self):
        backend=ToolkitBackend(); backend.configure({**DEFAULTS,'qwen_enabled':False})
        backend._load=Mock(side_effect=AssertionError('must not discover'))
        self.assertEqual(backend.status()['state'],'disabled')
        import threading
        self.assertFalse(backend.execute('start',threading.Event())['ok'])
        backend._load.assert_not_called()

    def test_refresh_stages_config_and_start_writes_single_shared_source(self):
        backend=ToolkitBackend(); manager=Mock(); lifecycle=Mock()
        manager.ROOT=self.scripts.parent
        manager.config.return_value=self.c
        backend._load=Mock(return_value=(manager,lifecycle))
        backend.configure({**DEFAULTS,'qwen_model_id':'next-model'})
        manager.atomic_json.assert_not_called()
        manager.port_busy.return_value=True
        with self.assertRaisesRegex(ValueError,'先退出原服务'):
            backend._activate_settings(manager,self.c)
        manager.atomic_json.assert_not_called()
        manager.port_busy.return_value=False
        result=backend._activate_settings(manager,self.c)
        self.assertEqual(result['model'],'next-model')
        manager.atomic_json.assert_called_once_with(self.scripts.parent/'service.json',result)
        self.assertTrue((self.scripts.parent/'service.json.before-stt-config.bak').exists())

    def test_toolkit_discovery_checks_both_required_files(self):
        self.assertEqual(toolkit_scripts(str(self.scripts.parent)),self.scripts)
        (self.scripts/'lifecycle.py').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'qwen_toolkit_path'): toolkit_scripts(str(self.scripts.parent))


class BudgetAndProcessTests(unittest.TestCase):
    def test_blocked_http_cannot_hold_start_past_total_deadline(self):
        import threading
        backend=ToolkitBackend();manager=Mock();lifecycle=Mock()
        backend._load=Mock(return_value=(manager,lifecycle))
        manager.config.return_value={'base_url':'http://127.0.0.1:8765/v1','model':'fixture','startup_timeout_sec':1}
        manager.lock.side_effect=lambda *a,**kw:contextlib.nullcontext()
        release=threading.Event()
        def request(*args,**kwargs):
            release.wait(4)
            return {}
        manager.request_json.side_effect=request
        manager.probe.side_effect=lambda c:manager.request_json('fixture',c)
        started=time.monotonic()
        try:
            with self.assertRaises(TimeoutError): backend.start()
            self.assertLess(time.monotonic()-started,1.5)
        finally:
            release.set()

    def test_load_uses_remaining_total_budget(self):
        backend=ToolkitBackend(); manager=Mock(); lifecycle=Mock()
        backend._load=Mock(return_value=(manager,lifecycle))
        config={'base_url':'http://127.0.0.1:8765/v1','model':'fixture','startup_timeout_sec':1}
        manager.config.return_value=config
        clock=[0.0]; budgets=[]
        def http(url,c,body=None,timeout=3):
            budgets.append(timeout)
            duration=min(.4,timeout);clock[0]+=duration
            if timeout <= .4: raise TimeoutError('fixture deadline')
            return {}
        manager.request_json.side_effect=http
        def probe(c):
            manager.request_json('models',c)
            manager.request_json('health',c)
            return {'reachable':True,'ready':False,'service':'strata'}
        manager.probe.side_effect=probe
        manager.lock.side_effect=lambda *a,**kw:contextlib.nullcontext()
        with patch('qwen_backend.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(TimeoutError): backend.start()
        self.assertAlmostEqual(clock[0],1)
        self.assertEqual(len(budgets),3)
        self.assertLess(budgets[-1],.21)
        # Outside the start thread's budget the shared HTTP client remains usable.
        manager.request_json('normal',config,timeout=3)
        self.assertEqual(budgets[-1],3)

    def test_limited_process_query_identifies_current_process(self):
        import sys
        self.assertEqual(executable_name(os.getpid()).casefold(),Path(sys.executable).name.casefold())
        self.assertIsNone(executable_name(0))

    def test_process_handle_closed_on_query_failure(self):
        kernel=Mock(); kernel.OpenProcess.return_value=123
        kernel.QueryFullProcessImageNameW.return_value=False
        with patch('process_identity.ctypes.WinDLL',return_value=kernel):
            self.assertIsNone(executable_name(42))
        kernel.CloseHandle.assert_called_once_with(123)

    def test_process_access_denied_never_reads_memory_or_leaks_handle(self):
        kernel=Mock(); kernel.OpenProcess.return_value=0
        with patch('process_identity.ctypes.WinDLL',return_value=kernel):
            self.assertIsNone(executable_name(42))
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_process_lookup_prefers_executable_over_changing_title(self):
        from app import CapsLockChecker
        app=CapsLockChecker.__new__(CapsLockChecker)
        app.config={'process_config':{'sample.exe':{'display_name':'稳定名称'}}}
        with patch('app.win32gui.IsWindow',return_value=True), patch('app.win32gui.GetWindowText',return_value='文档标题'), \
             patch('app.win32gui.GetClassName',return_value='Normal'), patch('app.win32process.GetWindowThreadProcessId',return_value=(1,42)), \
             patch('app.executable_name',return_value='SAMPLE.EXE'),patch('app.win32api.OpenProcess') as memory:
            self.assertEqual(app.get_app_name_from_hwnd(1),'稳定名称')
            memory.assert_not_called()

    def test_process_lookup_failure_boundaries_keep_title_fallback_and_close(self):
        from app import CapsLockChecker
        app=CapsLockChecker.__new__(CapsLockChecker);app.config={}
        with patch('app.win32gui.IsWindow',return_value=True), patch('app.win32gui.GetWindowText',return_value='文档标题'), \
             patch('app.win32gui.GetClassName',return_value='Normal'),patch('app.executable_name',return_value=None), \
             patch('app.win32process.GetWindowThreadProcessId',return_value=(1,42)) as pid, \
             patch('app.win32api.OpenProcess',side_effect=PermissionError('denied')) as open_process, \
             patch('app.win32process.EnumProcessModules',return_value=[]), patch('app.win32api.CloseHandle') as close:
            self.assertEqual(app.get_app_name_from_hwnd(1),'文档标题')
            close.assert_not_called()
            open_process.side_effect=None;open_process.return_value=123
            self.assertEqual(app.get_app_name_from_hwnd(1),'文档标题')
            close.assert_called_once_with(123)
            pid.side_effect=RuntimeError('window disappeared')
            self.assertEqual(app.get_app_name_from_hwnd(1),'文档标题')

    def test_callback_failure_does_not_break_event_drain_or_retry_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            store=AsyncHistoryStore(Path(temp))
            try:
                deadline=time.monotonic()+3
                while not store.ready and time.monotonic()<deadline:
                    store.poll();time.sleep(.005)
                callback=Mock(side_effect=RuntimeError('callback failed'))
                store.submit_intervals([('2026-10-22 12:00:00','A',1)],callback)
                errors=[]
                with self.assertLogs(level='ERROR'):
                    while store.pending_writes and time.monotonic()<deadline:
                        errors.extend(store.poll());time.sleep(.005)
                self.assertEqual(errors[0][0],'acknowledgement')
                self.assertEqual(store.pending_writes,0)
                callback.assert_called_once()
                self.assertEqual(store.poll(),[])
            finally:
                store.close();store.stopped.wait(3)

    def test_log_rotation_bounds_new_log_files(self):
        from app import CapsLockChecker
        with tempfile.TemporaryDirectory() as temp:
            app=CapsLockChecker.__new__(CapsLockChecker);app.directory=Path(temp)
            app.setup_logging()
            try:
                for i in range(8): app.logger.info('%s %s',i,'x'*700000)
                files=list((Path(temp)/'logs').glob('stt.log*'))
                self.assertLessEqual(len(files),6)
                self.assertTrue(any(p.name=='stt.log.1' for p in files))
                self.assertTrue(all(p.stat().st_size<=2*1024*1024 for p in files))
            finally:
                app._log_handler.close();app.logger.removeHandler(app._log_handler)


if __name__=='__main__': unittest.main()
