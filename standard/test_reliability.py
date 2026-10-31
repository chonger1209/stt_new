import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from settings import DEFAULTS, read_settings, complete_config_text
from tracking import UsageTracker, active_seconds
from storage import AsyncHistoryStore
from process_identity import executable_name

class ClockTests(unittest.TestCase):

    def setUp(self):
        self.wall = [dt.datetime(2026, 10, 22, 12).timestamp()]
        self.active = [100.0]
        self.store = Mock()
        self.store.totals.return_value = {}
        self.store.history.return_value = []
        self.tracker = UsageTracker(self.store, lambda: self.wall[0], lambda: self.active[0])
        self.tracker.switch('A')

    def test_wall_jumps_never_change_elapsed_usage(self):
        for correction in (3600, -7200):
            self.wall[0] += correction + 10
            self.active[0] += 10
            self.tracker.capture()
        self.assertEqual(sum((r[2] for r in self.tracker.pending)), 20)

    def test_sleep_excluded_and_foreground_reidentified(self):
        self.wall[0] += 7205
        self.active[0] += 5
        self.assertTrue(self.tracker.heartbeat())
        self.assertIsNone(self.tracker.app)
        self.assertEqual(sum((r[2] for r in self.tracker.pending)), 5)
        self.tracker.switch('B')
        self.wall[0] += 3
        self.active[0] += 3
        self.assertEqual(self.tracker.snapshot()[0], {'A': 5, 'B': 3})

    def test_dual_clock_midnight_conserves_duration(self):
        self.wall[0] = dt.datetime(2026, 10, 22, 23, 59, 55).timestamp()
        self.tracker.capture()
        self.tracker.pending.clear()
        self.wall[0] += 10
        self.active[0] += 10
        self.tracker.capture()
        self.assertEqual([r[2] for r in self.tracker.pending], [5, 5])
        self.assertEqual([r[0][:10] for r in self.tracker.pending], ['2026-10-22', '2026-10-23'])

    def test_ack_mismatch_recovers_without_resubmitting_committed_rows(self):
        self.active[0] += 5
        self.wall[0] += 5
        self.tracker.capture()
        self.tracker.flush()
        old = self.tracker.pending[0]
        callback = self.store.submit_intervals.call_args.args[1]
        self.active[0] += 3
        self.wall[0] += 3
        self.tracker.capture()
        new = self.tracker.pending[-1]
        self.tracker.pending.reverse()
        with self.assertLogs(level='ERROR'):
            callback(None)
        self.assertNotIn(old, self.tracker.pending)
        self.assertIs(self.tracker._inflight[0], new)
        self.assertEqual(self.store.submit_intervals.call_count, 2)

    def test_failed_submission_does_not_stick_inflight(self):
        self.active[0] += 1
        self.wall[0] += 1
        self.tracker.capture()
        self.store.submit_intervals.side_effect = RuntimeError('closed')
        with self.assertRaises(RuntimeError):
            self.tracker.flush()
        self.assertIsNone(self.tracker._inflight)
        self.assertEqual(len(self.tracker.pending), 1)

    def test_ack_recovery_keeps_equal_valued_but_distinct_later_sample(self):
        self.active[0] += 1
        self.wall[0] += 1
        self.tracker.capture()
        self.tracker.flush()
        old = self.tracker.pending[0]
        callback = self.store.submit_intervals.call_args.args[1]
        new = tuple(list(old))
        self.assertIsNot(old, new)
        self.tracker.pending[:] = [new]
        with self.assertLogs(level='ERROR'):
            callback(None)
        self.assertIs(self.tracker._inflight[0], new)
        self.assertEqual(self.store.submit_intervals.call_count, 2)

    def test_windows_awake_clock_advances(self):
        before = active_seconds()
        time.sleep(0.01)
        self.assertGreater(active_seconds(), before)

class BudgetAndProcessTests(unittest.TestCase):

    def test_limited_process_query_identifies_current_process(self):
        import sys
        self.assertEqual(executable_name(os.getpid()).casefold(), Path(sys.executable).name.casefold())
        self.assertIsNone(executable_name(0))

    def test_process_handle_closed_on_query_failure(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.QueryFullProcessImageNameW.return_value = False
        with patch('process_identity.ctypes.WinDLL', return_value=kernel):
            self.assertIsNone(executable_name(42))
        kernel.CloseHandle.assert_called_once_with(123)

    def test_process_access_denied_never_reads_memory_or_leaks_handle(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch('process_identity.ctypes.WinDLL', return_value=kernel):
            self.assertIsNone(executable_name(42))
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_process_lookup_prefers_executable_over_changing_title(self):
        from app import CapsLockChecker
        app = CapsLockChecker.__new__(CapsLockChecker)
        app.config = {'process_config': {'sample.exe': {'display_name': '稳定名称'}}}
        with patch('app.win32gui.IsWindow', return_value=True), patch('app.win32gui.GetWindowText', return_value='文档标题'), patch('app.win32gui.GetClassName', return_value='Normal'), patch('app.win32process.GetWindowThreadProcessId', return_value=(1, 42)), patch('app.executable_name', return_value='SAMPLE.EXE'), patch('app.win32api.OpenProcess') as memory:
            self.assertEqual(app.get_app_name_from_hwnd(1), '稳定名称')
            memory.assert_not_called()

    def test_process_lookup_failure_boundaries_keep_title_fallback_and_close(self):
        from app import CapsLockChecker
        app = CapsLockChecker.__new__(CapsLockChecker)
        app.config = {}
        with patch('app.win32gui.IsWindow', return_value=True), patch('app.win32gui.GetWindowText', return_value='文档标题'), patch('app.win32gui.GetClassName', return_value='Normal'), patch('app.executable_name', return_value=None), patch('app.win32process.GetWindowThreadProcessId', return_value=(1, 42)) as pid, patch('app.win32api.OpenProcess', side_effect=PermissionError('denied')) as open_process, patch('app.win32process.EnumProcessModules', return_value=[]), patch('app.win32api.CloseHandle') as close:
            self.assertEqual(app.get_app_name_from_hwnd(1), '文档标题')
            close.assert_not_called()
            open_process.side_effect = None
            open_process.return_value = 123
            self.assertEqual(app.get_app_name_from_hwnd(1), '文档标题')
            close.assert_called_once_with(123)
            pid.side_effect = RuntimeError('window disappeared')
            self.assertEqual(app.get_app_name_from_hwnd(1), '文档标题')

    def test_callback_failure_does_not_break_event_drain_or_retry_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AsyncHistoryStore(Path(temp))
            try:
                deadline = time.monotonic() + 3
                while not store.ready and time.monotonic() < deadline:
                    store.poll()
                    time.sleep(0.005)
                callback = Mock(side_effect=RuntimeError('callback failed'))
                store.submit_intervals([('2026-10-22 12:00:00', 'A', 1)], callback)
                errors = []
                with self.assertLogs(level='ERROR'):
                    while store.pending_writes and time.monotonic() < deadline:
                        errors.extend(store.poll())
                        time.sleep(0.005)
                self.assertEqual(errors[0][0], 'acknowledgement')
                self.assertEqual(store.pending_writes, 0)
                callback.assert_called_once()
                self.assertEqual(store.poll(), [])
            finally:
                store.close()
                store.stopped.wait(3)

    def test_log_rotation_bounds_new_log_files(self):
        from app import CapsLockChecker
        with tempfile.TemporaryDirectory() as temp:
            app = CapsLockChecker.__new__(CapsLockChecker)
            app.directory = Path(temp)
            app.setup_logging()
            try:
                for i in range(8):
                    app.logger.info('%s %s', i, 'x' * 700000)
                files = list((Path(temp) / 'logs').glob('stt.log*'))
                self.assertLessEqual(len(files), 6)
                self.assertTrue(any((p.name == 'stt.log.1' for p in files)))
                self.assertTrue(all((p.stat().st_size <= 2 * 1024 * 1024 for p in files)))
            finally:
                app._log_handler.close()
                app.logger.removeHandler(app._log_handler)
if __name__ == '__main__':
    unittest.main()
