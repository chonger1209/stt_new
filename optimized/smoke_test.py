"""Exercise real Tk widgets in an isolated temporary directory, without keyboard events."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import tkinter as tk
import threading
import time
import sqlite3
from unittest.mock import patch

from app import CapsLockChecker


class SimulatedQwen:
    def __init__(self):
        self.state = 'off'
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.stops = 0
        self.remaining = None
        self.resets = 0
        self.minutes = 120
        self.unloads = 0
        self.presentation = {}

    def set_idle_minutes(self, minutes):
        self.minutes = minutes

    def unload(self):
        self.unloads += 1
        self.state = 'unloaded'
        self.remaining = None
        return {'ok': True, 'message': '已休眠（模拟）'}

    def status(self):
        return {'state': self.state, 'detail': 'isolated Qwen UI test',
                'idle_remaining_seconds': self.remaining, 'idle_deadline': 12345, **self.presentation}

    def reset_idle(self):
        self.resets += 1
        self.remaining = self.minutes * 60
        return {'ok': True, 'message': '已重置（模拟）'}

    def start(self):
        self.started.set()
        self.release.wait(timeout=5)
        self.state = 'ready'
        return {'ok': True, 'message': '已启动（模拟）'}

    def stop(self):
        self.stops += 1
        self.state = 'off'
        return {'ok': True, 'message': '已停止（模拟）'}

    force_stop = stop


def run():
    directory = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    result = {}
    fresh_config = '--fresh-config' in sys.argv
    with tempfile.TemporaryDirectory(prefix='stt_smoke_') as folder:
        data = Path(folder)
        for name in ([] if fresh_config else ['config.txt', 'screen_time_tracker_new.ico']):
            shutil.copy2(directory / name, data / name)
        root = tk.Tk()
        root.withdraw()
        root.attributes('-alpha', 0.0)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(str(args))
        simulated_qwen = SimulatedQwen()
        families = tk.font.families(root)
        if '--fallback-font' in sys.argv:
            families = tuple(f for f in families if not f.casefold().startswith('misans'))
        with patch('app.tkfont.families', return_value=families):
            app = CapsLockChecker(root, data, monitor=False, qwen_backend=simulated_qwen)

        def pump_until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(.01)
            raise AssertionError('Timed out waiting for Qwen UI')

        def refresh_status():
            # A status request is pending work even though operation is None.
            # Wait for its UI acknowledgement before changing the next fixture.
            pump_until(lambda: app.qwen_controller._pending is None)
            assert app.qwen_controller.request('status')
            pump_until(lambda: app.qwen_controller._pending is None
                       and app.qwen_controller._latest_status is None)
        try:
            # A withdrawn, centered window can leave child geometry at 1x1.
            # Map it transparently so the real layout is exercised without showing it.
            root.wm_attributes('-topmost', False)
            root.deiconify()
            root.update()
            pump_until(lambda: '已停止' in app.qwen_label.cget('text'))
            assert app.qwen_button.cget('text') == '启动服务'
            simulated_qwen.release.clear()
            app.qwen_button.invoke()
            pump_until(simulated_qwen.started.is_set)
            assert '启动中' in app.qwen_label.cget('text')
            assert str(app.qwen_button.cget('state')) == 'disabled'
            heartbeat = []
            root.after(20, lambda: heartbeat.append(True))
            pump_until(lambda: bool(heartbeat))
            simulated_qwen.release.set()
            pump_until(lambda: '就绪' in app.qwen_label.cget('text'))
            app.qwen_button.invoke()
            pump_until(lambda: '待唤醒' in app.qwen_label.cget('text'))
            assert simulated_qwen.unloads == 1 and simulated_qwen.stops == 0
            assert app.qwen_button.cget('text') == '启动服务'
            app.qwen_button.invoke()
            pump_until(lambda: '就绪' in app.qwen_label.cget('text'))
            app.qwen_exit_button.invoke()
            assert simulated_qwen.stops == 0
            assert app.qwen_exit_button.cget('text') == '确认退出'
            app.qwen_exit_button.invoke()
            pump_until(lambda: '已停止' in app.qwen_label.cget('text'))
            assert simulated_qwen.stops == 1
            simulated_qwen.state = 'busy'
            refresh_status()
            pump_until(lambda: '千问工作' in app.qwen_label.cget('text'))
            assert str(app.qwen_button.cget('state')) == 'disabled'
            assert str(app.qwen_exit_button.cget('state')) == 'normal'
            app.qwen_button.invoke()
            app.qwen_exit_button.invoke()
            assert simulated_qwen.stops == 1
            app.qwen_exit_button.invoke()
            pump_until(lambda: simulated_qwen.stops == 2 and app.qwen_controller.operation is None)
            simulated_qwen.state = 'off'
            refresh_status()
            pump_until(lambda: '已停止' in app.qwen_label.cget('text'))
            result['qwen_controls'] = 'passed (sleep/start, forced busy exit, responsive UI)'
            assert app.qwen_exit_button.cget('text') == '强制退出'
            app.qwen_exit_button.invoke()
            assert app.qwen_exit_button.cget('text') == '确认退出'
            app._qwen_force_armed_until = time.monotonic() - 1
            app.qwen_exit_button.invoke()
            assert simulated_qwen.stops == 2, 'Expired confirmation executed force exit'
            app.cancel_force_confirmation()
            assert app.qwen_exit_button.cget('text') == '强制退出'
            assert app.qwen_label.cget('text').startswith('· ')
            result['force_confirmation'] = 'passed (first click arms, second confirms, expiry re-arms)'
            assert app.qwen_status_font.cget('size') == 12
            assert app.qwen_status_font.cget('weight') == 'normal'
            assert app.qwen_control_font.cget('size') == 9
            assert app.qwen_control_font.cget('weight') == 'normal'
            result['qwen_button_font'] = app.qwen_control_font.actual()
            root.update()
            assert app.qwen_button.winfo_width() == app.qwen_exit_button.winfo_width(), repr({
                'root': root.winfo_width(), 'left': app.timer_label.winfo_width(),
                'buttons': [(w.winfo_width(), w.winfo_reqwidth()) for w in (app.qwen_button, app.qwen_exit_button)],
                'font': app.qwen_control_font.actual()})
            result['qwen_font'] = app.qwen_status_font.actual()
            app.show_titlebar()
            root.update()
            for widget in (app.qwen_label, app.qwen_speed_label, app.qwen_button, app.qwen_exit_button):
                assert widget.winfo_rooty() >= app.titlebar.winfo_rooty() + app.titlebar.winfo_height()
                assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
            assert app.qwen_label.winfo_rootx() >= app.timer_label.winfo_rootx() + app.timer_label.winfo_width()
            number_center = app.timer_label.winfo_rootx() + app.timer_label.winfo_width() / 2
            buttons_center = app.timer_buttons_frame.winfo_rootx() + app.timer_buttons_frame.winfo_width() / 2
            assert abs(number_center - buttons_center) <= 1, 'Countdown not centered above its buttons'
            assert number_center < root.winfo_rootx() + root.winfo_width() / 2
            qwen_center = app.qwen_label.winfo_rootx() + app.qwen_label.winfo_width() / 2
            switch_center = app.qwen_buttons.winfo_rootx() + app.qwen_buttons.winfo_width() / 2
            assert abs(qwen_center - switch_center) <= 1, 'Qwen status not centered above its switch'
            assert abs((app.timer_label.winfo_rooty() + app.timer_label.winfo_height() / 2)
                       - (app.qwen_label.winfo_rooty() + app.qwen_label.winfo_height() / 2)) <= 1
            assert abs((app.timer_buttons_frame.winfo_rooty() + app.timer_buttons_frame.winfo_height() / 2)
                       - (app.qwen_button.winfo_rooty() + app.qwen_button.winfo_height() / 2)) <= 1
            result['qwen_layout'] = 'passed (two centered columns, shared status and button rows, below title bar)'
            assert app.qwen_label.winfo_width() > 1
            assert app.qwen_speed_label.winfo_y() >= app.qwen_label.winfo_y() + app.qwen_label.winfo_height()
            assert app.qwen_speed_label.winfo_y() + app.qwen_speed_label.winfo_height() <= app.qwen_buttons.winfo_y()
            assert app.qwen_speed_label.cget('font') == str(app.qwen_speed_font)
            assert app.qwen_speed_font.actual('size') == 11
            assert app.qwen_speed_font.actual('weight') == 'normal'
            assert app.qwen_status_font.actual('size') == 12
            assert app.qwen_status_font.actual('weight') == 'normal'
            result['qwen_speed_font'] = app.qwen_speed_font.actual()
            positions = None
            simulated_qwen.state = 'busy'
            for phase, name, speed, expected in (
                    ('reading', '预读', None, '0.0 T/s'),
                    ('generating', '生成', 57.6, '57.6 T/s'),
                    ('generating', '生成', 12345.6, '12345.6 T/s'),
                    ('generating', '生成', None, '— T/s'),
                    ('queued', '排队', 57.6, '0.0 T/s'),
                    ('loading', '加载中', None, '0.0 T/s')):
                simulated_qwen.presentation = {'display_phase': phase, 'generation_tps': speed,
                                               'metrics_available': True}
                pump_until(lambda: app.qwen_controller._pending is None)
                refresh_status()
                pump_until(lambda: name in app.qwen_label.cget('text') and app.qwen_speed_label.cget('text') == expected)
                assert str(app.qwen_button.cget('state')) == 'disabled'
                geometry = tuple((w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())
                                 for w in (app.timer_label, app.qwen_speed_label, app.qwen_buttons))
                if positions is None:
                    positions = geometry
                assert positions == geometry, 'Speed or phase moved other controls'
                for caps in (True, False):
                    app.caps_lock_on = caps
                    app.update_main_frame_bg()
            simulated_qwen.presentation = {}
            result['qwen_stage_and_speed'] = 'passed (reading/generating/queued/loading, zero/unknown, fixed geometry, both backgrounds)'
            simulated_qwen.state = 'ready'
            simulated_qwen.remaining = 60
            refresh_status()
            pump_until(lambda: getattr(app, '_qwen_reminder', None) is not None)
            reminder = app._qwen_reminder
            assert app._qwen_reminder_button.cget('text') == '重置 2 小时'
            app._paint_qwen_status()
            assert reminder is app._qwen_reminder, 'Duplicated reminder window'
            app._qwen_reminder_button.invoke()
            pump_until(lambda: simulated_qwen.resets == 1 and app.qwen_controller.operation is None)
            pump_until(lambda: app._qwen_reminder is None)
            assert app._qwen_status['idle_remaining_seconds'] == 7200
            config_path = data / 'config.txt'
            config_path.write_text(config_path.read_text(encoding='utf-8').replace(
                'qwen_idle_timeout_minutes = 120', 'qwen_idle_timeout_minutes = 7'), encoding='utf-8')
            app.refresh_config()
            assert simulated_qwen.minutes == 7
            pump_until(lambda: app.qwen_controller._pending is None and app.qwen_controller._latest_status is None)
            simulated_qwen.remaining = 30
            refresh_status()
            pump_until(lambda: app._qwen_reminder is not None)
            assert app._qwen_reminder_button.cget('text') == '重置 7 分钟'
            simulated_qwen.state = 'busy'
            refresh_status()
            pump_until(lambda: app._qwen_reminder is None)
            simulated_qwen.state = 'off'
            simulated_qwen.remaining = None
            refresh_status()
            pump_until(lambda: app._qwen_status['state'] == 'off')
            result['qwen_idle_reminder'] = 'passed (2h reset, live 7min config refresh, hide on new work)'
            text_config = (data / 'config.txt').read_text(encoding='utf-8')
            assert '# 配置说明版本: 2' in text_config
            assert '没有配置的程序也会正常记录' in text_config
            if fresh_config:
                lock = app.config['process_config']['LockApp.exe']
                assert not lock['show_in_screen_time'] and lock['show_in_time_stream']
                assert (data / 'screen_time_history.db').exists()
                assert (data / 'time_stream_history.db').exists()
                assert (data / 'logs').is_dir()
                result['fresh_config_generated'] = 'passed (Chinese help and process defaults)'
            assert app.timer_total_seconds == app.config['timer_duration_minutes'] * 60
            app.tracker.switch('Smoke App')
            app.tracker.start -= app.grid_second_per_block + 0.25
            app.tracker._start_elapsed -= app.grid_second_per_block + 0.25
            app.tracker.capture()
            app.render_history_stream()
            app.show_stats_window()
            root.update()
            app.render_stats_chart()
            root.update()
            ids = app.stats_canvas.find_all()
            assert ids
            queries_before = app.store.read_queries
            for _ in range(100):
                app.render_stats_chart()
            assert app.stats_canvas.find_all() == ids, 'Statistics recreated canvas items'
            assert app.store.read_queries == queries_before, 'Unexpected repeated SELECT'
            result['repeated_stats_redraws'] = 100
            result['repeated_stats_select_queries'] = 0
            app.root.update_idletasks()
            app.history_canvas.configure(width=300)
            # A withdrawn root still calculates child sizes during layout updates.
            root.update_idletasks()
            assert app.history_canvas.winfo_width() > 1
            assert app.history_canvas.find_all(), 'History canvas did not render'
            commands_before = len(root.tk.call('info', 'commands'))
            for _ in range(100):
                app._history_key = None
                app.render_history_stream()
            commands_after = len(root.tk.call('info', 'commands'))
            assert commands_after - commands_before <= 3, 'Tk callback commands leaked'
            result['history_callback_growth_after_100_redraws'] = commands_after - commands_before
            original_interval = app.grid_second_per_block
            app.grid_second_per_block = 60
            hour_history = [(str(index), f'App{index % 7}', 5) for index in range(720)]
            app.render_history_stream(history=hour_history)
            rectangles = app.history_canvas.find_all()
            assert len(rectangles) == 60, 'App switches inflated one-hour block count'
            expected_apps = [f'App{index % 7}' for index in range(11, 720, 12)]
            for rectangle, name in zip(rectangles, expected_apps):
                assert app.history_canvas.itemcget(rectangle, 'fill') == app._color(name)
            app.render_history_stream(history=[('1', 'A', 50), ('2', 'B', 9)])
            assert not app.history_canvas.find_all(), 'Partial block was drawn'
            app.render_history_stream(history=[('1', 'A', 50), ('2', 'B', 10)])
            rectangles = app.history_canvas.find_all()
            assert len(rectangles) == 1
            assert app.history_canvas.itemcget(rectangles[0], 'fill') == app._color('B')
            result['one_hour_with_5s_switches_at_60s_per_block'] = 'passed: 60 rectangles'
            result['block_closing_app_color'] = 'passed'
            app.grid_second_per_block = original_interval
            app.render_history_stream()
            app.show_stats_window()
            app.auto_restore_window()
            assert not app.stats_toggle_var.get(), 'Auto-restore reopened hidden statistics'
            app.start_timer()
            app.countdown.deadline -= 100
            app.pause_timer()
            remaining = app.countdown.remaining
            app.start_timer()
            assert abs(app.countdown.remaining - remaining) <= 1
            app.reset_timer()
            assert app.countdown.remaining == app.timer_total_seconds
            original_position = (app.config['window_x'], app.config['window_y'])
            app.config['window_x'], app.config['window_y'] = -321, -123
            app.apply_config()
            pump_until(lambda: (root.winfo_x(),root.winfo_y()) == (-321,-123))
            app.config['window_x'], app.config['window_y'] = original_position
            app.apply_config()
            result['negative_monitor_coordinates'] = 'passed (-321,-123 absolute position)'
            app.last_hwnd = 999
            app.refresh_config()
            assert app.last_hwnd is None
            result['refresh_reidentifies_foreground'] = 'passed'
            # Exercise Caps Lock rules without changing the real keyboard state.
            app.monitor = True
            app.last_hwnd = None
            app.config['software_list'] = ['CAD']
            with patch('app.win32api.GetKeyState', return_value=0), \
                 patch('app.win32gui.GetForegroundWindow', return_value=123), \
                 patch('app.win32gui.IsWindow', return_value=True), \
                 patch('app.win32gui.GetWindowText', return_value='Drawing CAD'), \
                 patch.object(app, 'get_app_name_from_hwnd', return_value='Drawing'), \
                 patch('app.win32api.keybd_event') as key_event:
                app.check_caps_lock()
                assert key_event.call_count == 2
                assert app.tracker.app == 'Drawing'
            app.last_hwnd = None
            with patch('app.win32api.GetKeyState', return_value=1), \
                 patch('app.win32gui.GetForegroundWindow', return_value=456), \
                 patch('app.win32gui.IsWindow', return_value=True), \
                 patch('app.win32gui.GetWindowText', return_value='Notes'), \
                 patch.object(app, 'get_app_name_from_hwnd', return_value='Notes'), \
                 patch('app.win32api.keybd_event') as key_event:
                app.check_caps_lock()
                assert key_event.call_count == 2
            app.monitor = False
            result['caps_lock_rules'] = 'passed (mocked keyboard events)'
            assert not errors, errors
            pump_until(lambda: app.store.ready and app.store.pending_writes == 0)
            with patch.object(app, '_show_storage_warning') as warning:
                app.store.failure_since = time.monotonic()-61
                app.poll_storage_events()
                app.poll_storage_events()
                warning.assert_called_once()
                app.store.failure_since = None
                app.poll_storage_events()
                assert not app._storage_alerted
            result['persistent_storage_warning'] = 'passed (one notice per continuous failure, reset on recovery)'
            blocker = sqlite3.connect(str(data / 'screen_time_history.db'))
            blocker.execute('BEGIN IMMEDIATE')
            try:
                heartbeat = []
                app.tracker.start -= 1
                app.tracker._start_elapsed -= 1
                app.close_application()
                root.after(30, lambda: heartbeat.append(True))
                pump_until(lambda: bool(heartbeat))
                assert not app._closing, 'Exited before locked database was saved'
                assert app.tracker.pending, 'Pending rows cleared before commit'
            finally:
                blocker.rollback()
                blocker.close()
            pump_until(lambda: app._closing)
            result['async_exit_save'] = 'passed (locked DB preserves rows, Tk heartbeat runs, unlock completes exit)'
            result['callback_errors'] = errors
            result['result'] = 'passed'
        finally:
            simulated_qwen.release.set()
            app.close_application()
            pump_until(lambda: app._closing)
            assert simulated_qwen.stops == 2, 'Closing STT stopped Qwen'
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if '--live-qwen-status' in sys.argv:
        from qwen_backend import ToolkitBackend
        result['live_qwen_status'] = ToolkitBackend().status()
        text = json.dumps(result, ensure_ascii=False, indent=2)
    if '--smoke-output' in sys.argv:
        path = Path(sys.argv[sys.argv.index('--smoke-output') + 1])
        path.write_text(text, encoding='utf-8')
    elif sys.stdout:
        print(text)


if __name__ == '__main__':
    run()
