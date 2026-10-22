"""Tkinter interface. Existing layout and controls are preserved."""
import datetime
import logging
import os
import time
import tkinter as tk
from tkinter import font as tkfont
import math
from tkinter import Menu
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process

from settings import DEFAULTS, read_settings, save_position
from storage import AsyncHistoryStore
from tracking import Countdown, UsageTracker, timeline_blocks
from qwen_backend import ToolkitBackend
from qwen_control import QwenController
from qwen_status import readable_color, rate_text


class CapsLockChecker:
    def __init__(self, root, directory=None, monitor=True, qwen_backend=None):
        self.root = root
        self.directory = Path(directory or Path(__file__).parent)
        self.config_path = self.directory / 'config.txt'
        self.monitor = monitor
        self._closing = False
        self._exit_requested = False
        self._last_storage_warning = 0
        self._jobs = {}
        self._qwen_status = {'state': 'checking', 'detail': '正在读取本地千问状态'}
        self._qwen_note = ''
        self._qwen_paint_key = None
        self._qwen_color_cache = {}
        self._qwen_force_armed_until = 0
        self._history_bindings = []
        self._history_key = None
        self._stats_key = None
        self._stats_items = []
        self._logged_through = datetime.datetime.now().isoformat(sep=' ', timespec='microseconds')
        self.setup_logging()
        self.config = read_settings(self.config_path, logger=self.logger)
        self._validate_colors()
        self.grid_second_per_block = self.config['grid_second_per_block']
        self.timer_total_seconds = self.config['timer_duration_minutes'] * 60
        self.countdown = Countdown(self.timer_total_seconds)
        self.store = AsyncHistoryStore(self.directory)
        self.tracker = UsageTracker(self.store)
        self.last_hwnd = None
        self.color_caps_on = self.config['color_caps_on']
        self.color_caps_off = self.config['color_caps_off']
        self.color_titlebar = self.config['color_titlebar']
        self.root.title('Caps Lock 状态检测')
        icon = self.directory / 'screen_time_tracker_new.ico'
        if icon.exists():
            self.root.iconbitmap(str(icon))
        self.root.resizable(True, True)
        self.root.overrideredirect(True)
        self.root.geometry('250x180+100+100')
        self.titlebar_visible = False
        self.dragging = False
        self.drag_offset = (0, 0)
        self.leave_hide_timer = None
        self.last_mouse_move_time = 0
        self.titlebar = tk.Frame(root, bg=self.color_titlebar, height=30)
        self.titlebar.pack_propagate(False)
        self._create_titlebar_buttons()
        self.titlebar.bind('<ButtonPress-1>', self.on_titlebar_drag_start)
        self.titlebar.bind('<B1-Motion>', self.on_titlebar_drag_motion)
        self._bind_window_events()
        self._create_right_click_menu()
        self.caps_lock_on = bool(win32api.GetKeyState(win32con.VK_CAPITAL) & 1)
        self.main_frame = tk.Frame(root, bg=self.color_caps_on if self.caps_lock_on else self.color_caps_off)
        self.main_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.original_height = self.config['window_height']
        self.color_palette = ['#4a90e2', '#35bc59', '#f39c12', '#e74c3c', '#9b59b6',
                              '#1abc9c', '#f1c40f', '#e67e22', '#3498db', '#95a5a6']
        self.app_color_map = {}
        self.history_bar_h = 120
        self.last_window_height = self.config['window_height']
        self.auto_restore_timer = None
        self.height_change_detected = False
        self._create_ui_components()
        self.history_canvas.bind('<Configure>', self.on_history_canvas_configure)
        self.apply_config()
        self.start_scheduled_tasks()
        self.qwen_controller = QwenController(
            qwen_backend or ToolkitBackend(idle_minutes=self.config['qwen_idle_timeout_minutes']),
            autostart=monitor)
        self._schedule('qwen', 100, self.poll_qwen_events)
        self._schedule('storage', 100, self.poll_storage_events)
        self._schedule('initial', 100, self._finish_initialization)
        self.logger.info('程序初始化完成')

    def setup_logging(self):
        folder = self.directory / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f'stt.{id(self)}')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self._log_handler = logging.FileHandler(folder / f'log_{datetime.date.today()}.txt', encoding='utf-8')
        self._log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(self._log_handler)

    def _schedule(self, name, milliseconds, callback):
        self._cancel(name)
        if self._closing:
            return
        def invoke():
            self._jobs.pop(name, None)
            if not self._closing:
                callback()
        self._jobs[name] = self.root.after(milliseconds, invoke)

    def _cancel(self, name):
        job = self._jobs.pop(name, None)
        if job:
            self.root.after_cancel(job)

    def _finish_initialization(self):
        self.root.update_idletasks()
        self.update_main_frame_bg()
        self.update_status()
        self.render_history_stream()

    def start_scheduled_tasks(self):
        self.check_caps_lock()
        self.schedule_screen_time_refresh()
        self.schedule_grid_update()
        self._schedule('flush', 60000, self.schedule_cache_flush)

    def schedule_screen_time_refresh(self):
        try:
            if self.stats_toggle_var.get():
                self.render_stats_chart()
            self.check_window_height_change()
        except Exception:
            self.logger.exception('刷新统计失败')
        finally:
            interval = max(1000, int(10000 / self.config['screen_time_refresh_frequency']))
            self._schedule('stats', interval, self.schedule_screen_time_refresh)

    def schedule_grid_update(self):
        try:
            self.tracker.capture()
            self.render_history_stream(log_new_pixels=True)
        except Exception:
            self.logger.exception('更新时间流失败')
        finally:
            self._schedule('grid', self.grid_second_per_block * 1000, self.schedule_grid_update)

    def schedule_cache_flush(self):
        try:
            self.tracker.capture()
            self.tracker.flush()
        except Exception:
            self.logger.exception('保存数据失败；保留未写入记录，稍后重试')
        finally:
            self._schedule('flush', 60000, self.schedule_cache_flush)

    def poll_storage_events(self):
        version = self.store.version
        failures = self.store.poll()
        if failures and time.monotonic() - self._last_storage_warning >= 5:
            self._last_storage_warning = time.monotonic()
            self.logger.error('后台保存暂未完成；保留数据并重试：%s', failures[-1])
        if self._exit_requested:
            if failures:
                self._exit_requested = False
                self.start_scheduled_tasks()
                from tkinter import messagebox
                messagebox.showerror('保存失败', '记录尚未保存，程序将保持运行并在后台重试。请稍后再退出。', parent=self.root)
            elif self.store.ready and not self.tracker.pending and not self.store.pending_writes:
                self.store.close()
                if self.store.stopped.is_set():
                    self._finish_close()
                    return
        if version != self.store.version and not self._exit_requested:
            self.render_history_stream()
        self._schedule('storage', 100, self.poll_storage_events)

    def check_caps_lock(self):
        try:
            current = bool(win32api.GetKeyState(win32con.VK_CAPITAL) & 1)
            if self.monitor:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and win32gui.IsWindow(hwnd) and hwnd != self.last_hwnd:
                    app = self.get_app_name_from_hwnd(hwnd)
                    self.tracker.switch(app)
                    self.last_hwnd = hwnd
                    title = win32gui.GetWindowText(hwnd)
                    required = any(name.lower() in app.lower() or name.lower() in title.lower()
                                   for name in self.config['software_list'])
                    if current != required:
                        win32api.keybd_event(win32con.VK_CAPITAL, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_CAPITAL, 0, win32con.KEYEVENTF_KEYUP, 0)
                        current = bool(win32api.GetKeyState(win32con.VK_CAPITAL) & 1)
            if current != self.caps_lock_on:
                self.caps_lock_on = current
                self.update_main_frame_bg()
                self.update_status()
        except Exception:
            self.logger.exception('检查前台窗口失败')
        finally:
            self._schedule('caps', 200, self.check_caps_lock)

    def check_app_display_config(self, app_name, config_type):
        for config in self.config.get('process_config', {}).values():
            if app_name == config['display_name']:
                return config.get(config_type, True)
        return True

    def _color(self, app):
        if app not in self.app_color_map:
            self.app_color_map[app] = self.color_palette[len(self.app_color_map) % len(self.color_palette)]
        return self.app_color_map[app]

    def _clear_history_bindings(self):
        for tag, event, function in self._history_bindings:
            self.history_canvas.tag_unbind(tag, event, function)
        self._history_bindings.clear()

    def render_history_stream(self, log_new_pixels=False, history=None):
        if history is None:
            _, history = self.tracker.snapshot()
        blocks = timeline_blocks(history, self.grid_second_per_block,
                                 lambda app: self.check_app_display_config(app, 'show_in_time_stream'))
        if log_new_pixels:
            # Only log completed records; the live tail will be captured by the next sample.
            day = datetime.datetime.fromtimestamp(self.tracker.clock()).date().isoformat()
            completed = self.store.history(day) + list(self.tracker.pending)
            fresh = [row for row in completed if row[0] > self._logged_through]
            if fresh:
                self.store.submit_pixels(fresh, self._pixels_saved)
                self._logged_through = max(row[0] for row in fresh)
        width = self.history_canvas.winfo_width()
        if width <= 1:
            return
        key = (width, tuple(blocks), tuple((app, self._color(app)) for app, _ in blocks))
        if key == self._history_key:
            return
        self._history_key = key
        self._clear_history_bindings()
        self.history_canvas.delete('all')
        max_cols = max(1, width // 5)
        total = sum(count for _, count in blocks)
        self.history_bar_h = max(20, (total + max_cols - 1) // max_cols) * 5
        self.history_canvas.configure(height=self.history_bar_h)
        index = 0
        tags = {}
        for app, count in blocks:
            tag = tags.setdefault(app, f'app_{len(tags)}')
            for _ in range(count):
                x, y = (index % max_cols) * 5, (index // max_cols) * 5
                self.history_canvas.create_rectangle(x, y, x + 4, y + 4, fill=self._color(app), outline='', tags=(tag,))
                index += 1
        for app, tag in tags.items():
            callbacks = {'<Enter>': lambda event, name=app: self.show_tooltip(event, name),
                         '<Leave>': self.hide_tooltip,
                         '<Motion>': lambda event, name=app: self.move_tooltip(event, name)}
            for event, callback in callbacks.items():
                function = self.history_canvas.tag_bind(tag, event, callback)
                self._history_bindings.append((tag, event, function))

    def _pixels_saved(self, inserted):
        summary = {}
        for _, app, duration in inserted:
            count, total = summary.get(app, (0, 0))
            summary[app] = (count + 1, total + duration)
        if summary:
            detail = '; '.join(f'{app}: {count}条区间, 总时长: {self.format_duration(total)}'
                               for app, (count, total) in summary.items())
            self.logger.info('后台保存时间流区间: %s; 总计: %s条', detail, len(inserted))

    def render_stats_chart(self):
        totals, history = self.tracker.snapshot()
        items = sorted(((app, duration) for app, duration in totals.items()
                        if self.check_app_display_config(app, 'show_in_screen_time')),
                       key=lambda pair: pair[1], reverse=True)[:50]
        width = self.stats_canvas.winfo_width()
        key = (width, tuple(app for app, _ in items))
        if key != self._stats_key:
            self._stats_key = key
            self.stats_canvas.delete('all')
            self._stats_items = []
            self.stats_canvas.configure(height=246)
            for index, (app, _) in enumerate(items):
                y = 6 + index * 30
                bar = self.stats_canvas.create_rectangle(20, y, 21, y + 24, fill=self._color(app), outline='')
                self.stats_canvas.create_text(28, y + 12, anchor='w', text=app, fill='#ffffff', font=('Arial', 10))
                label = self.stats_canvas.create_text(width - 10, y + 12, anchor='e', fill='#ffffff', font=('Arial', 10))
                self._stats_items.append((bar, label))
        maximum = max((duration for _, duration in items), default=0)
        for index, ((_, duration), (bar, label)) in enumerate(zip(items, self._stats_items)):
            y = 6 + index * 30
            bar_width = max(1, int((width - 30) * duration / maximum)) if maximum > 0 else 1
            self.stats_canvas.coords(bar, 20, y, 20 + bar_width, y + 24)
            self.stats_canvas.itemconfigure(label, text=self.format_duration(duration))
        self.stats_canvas.configure(scrollregion=self.stats_canvas.bbox('all') or (0, 0, width, 0))
        self.render_history_stream(history=history)

    def on_history_canvas_configure(self, event):
        self.render_history_stream()

    def _validate_colors(self):
        for key in ('color_caps_on', 'color_caps_off', 'color_titlebar'):
            try:
                self.root.winfo_rgb(self.config[key])
            except tk.TclError:
                self.logger.warning('无效颜色 %s=%s，使用默认值', key, self.config[key])
                self.config[key] = DEFAULTS[key]

    def apply_config(self):
        width, height = self.config['window_width'], self.config['window_height']
        x, y = self.config['window_x'], self.config['window_y']
        self.original_height = height
        shown_height = height + 256 if self.stats_toggle_var.get() else height
        self.root.geometry(f'{width}x{shown_height}')
        if x != -1 and y != -1:
            self.root.geometry(f'+{x}+{y}')
        else:
            self.center_window()
        self.color_caps_on = self.config['color_caps_on']
        self.color_caps_off = self.config['color_caps_off']
        self.color_titlebar = self.config['color_titlebar']
        for widget in (self.titlebar, self.close_button, self.refresh_button, self.settings_button, self.stats_button):
            widget.configure(bg=self.color_titlebar)
        self.qwen_button.configure(bg=self.color_titlebar)
        self.qwen_exit_button.configure(bg=self.color_titlebar)
        self.update_main_frame_bg()
        self.root.wm_attributes('-topmost', self.config['always_on_top'])
        self._history_key = self._stats_key = None
        self.render_history_stream()
        if self.stats_toggle_var.get():
            self.render_stats_chart()

    def refresh_config(self):
        self.tracker.capture()
        self.config = read_settings(self.config_path, logger=self.logger)
        if hasattr(self.qwen_controller.backend, 'set_idle_minutes'):
            self.qwen_controller.backend.set_idle_minutes(self.config['qwen_idle_timeout_minutes'])
        self.qwen_controller.request('status')
        self._validate_colors()
        self.grid_second_per_block = self.config['grid_second_per_block']
        self.timer_total_seconds = self.config['timer_duration_minutes'] * 60
        self.countdown.configure(self.timer_total_seconds)
        self.timer_label.configure(text=self.format_duration(self.countdown.remaining))
        self.apply_config()
        self._schedule('grid', self.grid_second_per_block * 1000, self.schedule_grid_update)
        self._schedule('stats', max(1000, int(10000 / self.config['screen_time_refresh_frequency'])),
                       self.schedule_screen_time_refresh)
        self.logger.info('配置已刷新')

    def show_settings_window(self):
        os.startfile(str(self.config_path))

    def save_window_position(self):
        save_position(self.config_path, self.root.winfo_x(), self.root.winfo_y())

    def _create_qwen_controls(self):
        families = {name.casefold(): name for name in tkfont.families(self.root)}
        family = next((families[name.casefold()] for name in ('MiSans', '等线', 'DengXian')
                       if name.casefold() in families), 'TkDefaultFont')
        self.qwen_status_font = tkfont.Font(root=self.root, family=family, size=11, weight='normal')
        button_family = next((families[name.casefold()] for name in ('等线', 'DengXian')
                              if name.casefold() in families), family)
        self.qwen_control_font = tkfont.Font(root=self.root, family=button_family, size=9, weight='normal')
        button_width = math.ceil(max(self.qwen_control_font.measure(text)
                                    for text in ('休眠服务', '启动服务', '强制退出'))
                                 / max(1, self.qwen_control_font.measure('0')))
        self.qwen_buttons = tk.Frame(self.timer_frame, bg=self.main_frame.cget('bg'))
        self.qwen_buttons.grid(row=2, column=1, pady=(1, 1))
        self.qwen_button = tk.Button(self.qwen_buttons, text='启动服务', width=button_width,
                                     font=self.qwen_control_font, relief=tk.FLAT, padx=5, pady=2,
                                     bg=self.color_titlebar, fg='white', state=tk.DISABLED,
                                     command=self.toggle_qwen)
        self.qwen_button.pack(side=tk.LEFT, padx=(0, 6))
        self.qwen_exit_button = tk.Button(
            self.qwen_buttons, text='强制退出', font=self.qwen_control_font, width=button_width, relief=tk.FLAT,
            padx=5, pady=2, fg='white', command=self.confirm_force_exit)
        self.qwen_exit_button.pack(side=tk.LEFT)
        self.qwen_label = tk.Label(self.timer_frame, text='· 千问检测中', font=self.qwen_status_font,
                                   fg='#333333', bg=self.main_frame['bg'])
        self.qwen_label.grid(row=0, column=1, pady=(0, 1), padx=4)
        self.qwen_speed_label = tk.Label(self.timer_frame, text='— T/s', width=12,
                                         font=self.qwen_control_font, anchor='center',
                                         bg=self.main_frame['bg'], fg='#333333')
        self.qwen_speed_label.grid(row=1, column=1, pady=(3, 6))
        self.timer_frame.grid_rowconfigure(1, minsize=self.qwen_control_font.metrics('linespace') + 9)
        for widget in (self.qwen_label, self.qwen_speed_label, self.qwen_button, self.qwen_exit_button):
            widget.bind('<Enter>', self.show_qwen_details)
            widget.bind('<Leave>', self.hide_tooltip)

    def confirm_force_exit(self):
        if self.qwen_controller.operation == 'force_stop':
            return
        if time.monotonic() < self._qwen_force_armed_until:
            self._qwen_force_armed_until = 0
            self._cancel('force-confirm')
            self.qwen_action('force_stop')
        else:
            self._qwen_force_armed_until = time.monotonic() + 5
            self._schedule('force-confirm', 5000, self.cancel_force_confirmation)
            self._paint_qwen_status()

    def cancel_force_confirmation(self):
        self._qwen_force_armed_until = 0
        self._paint_qwen_status()

    def show_qwen_details(self, event):
        detail = self._qwen_status.get('detail', '')
        if self._qwen_note:
            detail += '\n上次操作：' + self._qwen_note
        detail += '\n强制退出会中断当前任务；退出后可点击启动服务。'
        self.show_tooltip(event, detail)

    def _paint_qwen_status(self):
        labels = {'checking': ('检测中', '#333333', '启动服务'),
                  'off': ('已停止', '#333333', '启动服务'),
                  'unloaded': ('待唤醒', '#663c00', '启动服务'),
                  'ready': ('就绪', '#143d20', '休眠服务'),
                  'busy': ('工作', '#663c00', '休眠服务'),
                  'error': ('异常', '#7f1d1d', '启动服务'),
                  'unconfigured': ('未配置', '#333333', '启动服务')}
        state = self._qwen_status.get('state', 'error')
        name, color, button = labels.get(state, labels['error'])
        phase = 'idle' if state == 'ready' else state
        if state == 'busy':
            phase = self._qwen_status.get('display_phase', 'busy')
            name = {'reading': '预读', 'generating': '生成', 'queued': '排队',
                    'loading': '加载中'}.get(phase, '工作')
        operation = self.qwen_controller.operation
        if operation:
            name = {'start': '启动中', 'stop': '退出中', 'force_stop': '退出中', 'unload': '休眠中',
                    'ensure_service': '准备中', 'reset_idle': '延时中'}[operation]
            phase = 'loading'
        enabled = not operation and state not in ('checking', 'busy', 'unconfigured')
        background = self.main_frame.cget('bg')
        color_key = (phase, background)
        if color_key not in self._qwen_color_cache:
            rgb = tuple(value / 65535 for value in self.root.winfo_rgb(background))
            self._qwen_color_cache[color_key] = readable_color(phase, rgb)
        color = self._qwen_color_cache[color_key]
        rate = rate_text(self._qwen_status, operation)
        confirm = '确认退出' if time.monotonic() < self._qwen_force_armed_until else '强制退出'
        key = (name, color, button, enabled, confirm, operation, state, rate)
        if key != self._qwen_paint_key:
            self._qwen_paint_key = key
            self.qwen_label.configure(text='· 千问' + name, fg=color)
            self.qwen_speed_label.configure(text=rate, fg=color)
            self.qwen_button.configure(text=button, state=tk.NORMAL if enabled else tk.DISABLED)
            self.qwen_exit_button.configure(text=confirm,
                state=tk.DISABLED if operation == 'force_stop' else tk.NORMAL)
            self.right_click_menu.entryconfigure(self._qwen_start_menu,
                state=tk.NORMAL if not operation and state in ('off', 'unloaded', 'ready') else tk.DISABLED)
            self.right_click_menu.entryconfigure(self._qwen_stop_menu,
                state=tk.NORMAL if not operation and state in ('ready', 'unloaded') else tk.DISABLED)
        self._update_qwen_reminder()

    def _update_qwen_reminder(self):
        remaining = self._qwen_status.get('idle_remaining_seconds')
        deadline = self._qwen_status.get('idle_deadline')
        show = (self._qwen_status.get('state') == 'ready'
                and isinstance(remaining, (int, float)) and 0 < remaining <= 60
                and not self.qwen_controller.operation)
        window = getattr(self, '_qwen_reminder', None)
        if not show:
            if window is not None:
                window.destroy()
                self._qwen_reminder = None
            return
        if window is None:
            window = self._qwen_reminder = tk.Toplevel(self.root)
            window.withdraw()
            window.title('千问即将休眠')
            window.resizable(False, False)
            window.transient(self.root)
            window.attributes('-topmost', True)
            window.attributes('-alpha', self.root.attributes('-alpha'))
            self._qwen_reminder_label = tk.Label(window, padx=12, pady=12)
            self._qwen_reminder_label.pack()
            self._qwen_reminder_button = tk.Button(
                window, command=lambda: self.qwen_action('reset_idle'))
            self._qwen_reminder_button.pack(pady=(0, 10))
            monitor = win32api.MonitorFromWindow(self.root.winfo_id(), win32con.MONITOR_DEFAULTTONEAREST)
            left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
            x = min(max(left, self.root.winfo_x()), max(left, right - 300))
            y = min(max(top, self.root.winfo_y() + self.root.winfo_height()), max(top, bottom - 110))
            window.geometry(f'300x110+{x}+{y}')
            window.protocol('WM_DELETE_WINDOW', window.withdraw)
            self._qwen_reminder_deadline = deadline
            window.update_idletasks()
            handle = win32gui.GetParent(window.winfo_id()) or window.winfo_id()
            style = win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(handle, win32con.GWL_EXSTYLE, style | win32con.WS_EX_NOACTIVATE)
            # Windows must not activate this reminder over the user's current task.
            window.deiconify()
        minutes = self.config['qwen_idle_timeout_minutes']
        duration = f'{minutes // 60} 小时' if minutes % 60 == 0 else f'{minutes} 分钟'
        self._qwen_reminder_button.configure(text='重置 ' + duration)
        self._qwen_reminder_label.configure(text=f'千问将在 {max(1, int(remaining))} 秒后休眠')

    def toggle_qwen(self):
        state = self._qwen_status.get('state')
        action = 'start' if state in ('off', 'unloaded', 'error') else 'unload' if state == 'ready' else 'status'
        self.qwen_action(action)

    def qwen_action(self, action):
        if self.qwen_controller.request(action):
            if action == 'force_stop':
                self.logger.info('收到强制退出点击，已提交后台退出')
            self._qwen_note = ''
            self._paint_qwen_status()

    def poll_qwen_events(self):
        for event in self.qwen_controller.drain_events():
            if event['kind'] == 'status':
                self._qwen_status = event['status']
            else:
                result = event['result']
                self._qwen_note = result.get('message', '操作完成')
                self.logger.info('千问手动%s: %s', event['action'], self._qwen_note)
                # The worker's following status event is authoritative, including a
                # remote request that already reloaded the model after an unload.
                if not result.get('ok'):
                    from tkinter import messagebox
                    messagebox.showinfo('千问操作未完成', self._qwen_note, parent=self.root)
        self._paint_qwen_status()
        self._schedule('qwen', 100, self.poll_qwen_events)

    def start_timer(self):
        self.countdown.start()
        self.start_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.NORMAL)
        self.update_timer()

    def pause_timer(self):
        self.countdown.pause()
        self._cancel('timer')
        self.timer_label.configure(text=self.format_duration(self.countdown.remaining))
        self.start_button.configure(state=tk.NORMAL)
        self.pause_button.configure(state=tk.DISABLED)

    def reset_timer(self):
        self._cancel('timer')
        self.countdown.reset()
        self.timer_label.configure(text=self.format_duration(self.countdown.remaining))
        self.start_button.configure(state=tk.NORMAL)
        self.pause_button.configure(state=tk.DISABLED)

    def update_timer(self):
        remaining = self.countdown.remaining
        self.timer_label.configure(text=self.format_duration(remaining))
        if remaining > 0 and self.countdown.running:
            self._schedule('timer', 1000, self.update_timer)
        else:
            self.countdown.pause()
            self.start_button.configure(state=tk.NORMAL)
            self.pause_button.configure(state=tk.DISABLED)

    def show_stats_window(self):
        self.stats_toggle_var.set(not self.stats_toggle_var.get())
        self.toggle_stats_visibility()
        if self.stats_toggle_var.get():
            self.start_auto_restore_timer()
        else:
            self._cancel('restore')

    def check_window_height_change(self):
        height = self.root.winfo_height()
        if height > self.config['window_height'] and self.last_window_height <= self.config['window_height']:
            self.start_auto_restore_timer()
        self.last_window_height = height

    def start_auto_restore_timer(self):
        self._schedule('restore', 30000, self.auto_restore_window)

    def auto_restore_window(self):
        if self.stats_toggle_var.get():
            self.stats_toggle_var.set(False)
            self.toggle_stats_visibility()
        self.height_change_detected = False

    def close_application(self):
        if self._closing or self._exit_requested:
            return
        self._exit_requested = True
        for name in ('caps', 'grid', 'stats', 'flush', 'initial'):
            self._cancel(name)
        self.tracker.close()
        self._schedule('storage', 0, self.poll_storage_events)

    def _finish_close(self):
        self._closing = True
        self.qwen_controller.close()
        try:
            self.save_window_position()
        except Exception:
            self.logger.exception('保存窗口位置失败')
        for name in list(self._jobs):
            self._cancel(name)
        if self.leave_hide_timer:
            self.root.after_cancel(self.leave_hide_timer)
        self._clear_history_bindings()
        self.store.close()
        self.logger.info('程序退出完成')
        self._log_handler.close()
        self.logger.removeHandler(self._log_handler)
        self.root.destroy()

    def on_close_click(self, event=None):
        self.close_application()

    def update_status(self):
        """更新界面显示的Caps Lock状态"""
        if self.caps_lock_on:
            status_text = "ON"
        else:
            status_text = "OFF"

        self.status_value_label.configure(text=status_text)

    def get_app_name_from_hwnd(self, hwnd):
        """获取窗口所属程序名，并应用配置的显示名称 - 修复win32api句柄泄漏"""
        if not hwnd or not win32gui.IsWindow(hwnd):
            return "Unknown"

        try:
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)

            if class_name == "LockAppFrame" or (title == "" and "LockApp" in class_name):
                if 'process_config' in self.config and 'LockApp.exe' in self.config['process_config']:
                    return self.config['process_config']['LockApp.exe']['display_name']
                return "LockApp.exe"

            if class_name == "TaskManagerWindow" or "任务管理器" in title or "Task Manager" in title:
                if 'process_config' in self.config and 'Taskmgr.exe' in self.config['process_config']:
                    return self.config['process_config']['Taskmgr.exe']['display_name']
                return "任务管理器"

            tid, pid = win32process.GetWindowThreadProcessId(hwnd)

            hproc = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            if hproc and hproc != 0:  # 检查句柄有效性
                try:
                    modules = win32process.EnumProcessModules(hproc)
                    if modules and len(modules) > 0:
                        path = win32process.GetModuleFileNameEx(hproc, modules[0])
                        process_name = os.path.basename(path)

                        if 'process_config' in self.config:
                            for proc_name, config in self.config['process_config'].items():
                                if process_name.lower() == proc_name.lower():
                                    return config['display_name']  # 返回配置的显示名称

                        return process_name
                finally:
                    win32api.CloseHandle(hproc)
            else:
                if class_name == "TaskManagerWindow" or "任务管理器" in title or "Task Manager" in title:
                    return "任务管理器"
                if title:
                    return title

            title = win32gui.GetWindowText(hwnd)
            return title or "Unknown"
        except Exception as e:
            try:
                title = win32gui.GetWindowText(hwnd)
                class_name = win32gui.GetClassName(hwnd)
                if class_name == "TaskManagerWindow" or "任务管理器" in title or "Task Manager" in title:
                    return "任务管理器"
                if title:
                    return title
            except:
                pass
            return "Unknown"

    def format_duration(self, seconds):
        """格式化时长"""
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def toggle_stats_visibility(self):
        """切换统计组件的可见性"""
        if self.stats_toggle_var.get():
            self.original_height = self.root.winfo_height()

            bar_h = 24
            gap = 6
            visible_items = 7
            stats_height = (bar_h + gap) * (visible_items + 1) + gap  # 固定高度，增加底部历史流条

            new_window_height = self.original_height + stats_height + 10  # 加10是pady的间距

            self.root.geometry(f"{self.root.winfo_width()}x{new_window_height}")

            self.stats_container_frame.pack(expand=False, fill=tk.BOTH, pady=10)
            self.render_stats_chart()
            self.render_history_stream()


        else:
            self.stats_container_frame.pack_forget()
            self.root.geometry(f"{self.root.winfo_width()}x{self.original_height}")

    def hide_titlebar(self):
        """隐藏自定义标题栏"""
        if self.titlebar_visible:
            self.titlebar.place_forget()
            self.titlebar_visible = False
            self.update_main_frame_bg()  # 更新背景色

    def show_titlebar(self):
        """显示自定义标题栏"""
        if not self.titlebar_visible:
            self.titlebar.place(x=0, y=0, relwidth=1, height=30)
            self.titlebar.lift()  # 确保标题栏在主框架上方
            self.titlebar_visible = True
            self.update_main_frame_bg()  # 更新背景色

    def on_titlebar_drag_start(self, event):
        """开始拖动自定义标题栏"""
        self.dragging = True
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def on_titlebar_drag_motion(self, event):
        """拖动自定义标题栏"""
        if self.dragging:
            new_x = event.x_root - self.drag_offset[0]
            new_y = event.y_root - self.drag_offset[1]
            self.root.geometry(f"+{new_x}+{new_y}")

    def on_window_drag_start(self, event):
        """标题栏隐藏时拖动整个窗口"""
        if isinstance(event.widget, (tk.Button, tk.Scrollbar)):
            return
        if not self.titlebar_visible:
            self.dragging = True
            self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def on_window_drag_motion(self, event):
        """拖动整个窗口"""
        if self.dragging and not self.titlebar_visible:
            new_x = event.x_root - self.drag_offset[0]
            new_y = event.y_root - self.drag_offset[1]
            self.root.geometry(f"+{new_x}+{new_y}")

    def on_drag_stop(self, event):
        """停止拖动窗口"""
        self.dragging = False

    def on_mouse_motion(self, event):
        """鼠标移动事件"""
        current_time = time.time()

        if current_time - self.last_mouse_move_time > 0.05:  # 50ms间隔
            self.last_mouse_move_time = current_time

            if not self.titlebar_visible and event.y < 30:
                self.show_titlebar()

        if self.leave_hide_timer:
            self.root.after_cancel(self.leave_hide_timer)
            self.leave_hide_timer = None

    def on_mouse_enter(self, event):
        """鼠标进入窗口时显示标题栏"""
        if not self.dragging:
            self.show_titlebar()

    def on_mouse_leave(self, event):
        """鼠标离开窗口时隐藏标题栏"""
        if self.titlebar_visible and not self.dragging:
            self.leave_hide_timer = self.root.after(500, self.hide_titlebar)

    def get_mouse_screen_info(self):
        """获取鼠标所在屏幕的矩形信息 (left, top, right, bottom)"""
        point = win32api.GetCursorPos()
        monitors = win32api.EnumDisplayMonitors()
        for monitor in monitors:
            monitor_rect = monitor[2]  # 矩形区域 (left, top, right, bottom)
            if monitor_rect[0] <= point[0] < monitor_rect[2] and monitor_rect[1] <= point[1] < monitor_rect[3]:
                return monitor_rect
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def show_tooltip(self, event, app_name):
        """显示tooltip，显示应用程序名称"""
        self.tooltip_label.configure(text=app_name)
        self.tooltip.update_idletasks()

        tooltip_width = self.tooltip.winfo_width()
        tooltip_height = self.tooltip.winfo_height()

        mouse_x = event.x_root
        mouse_y = event.y_root

        screen_left, screen_top, screen_right, screen_bottom = self.get_mouse_screen_info()

        x = mouse_x + 10
        y = mouse_y - tooltip_height - 10  # 显示在鼠标上方

        if x + tooltip_width > screen_right:
            x = mouse_x - tooltip_width - 10
        if y < screen_top + 5:
            y = mouse_y + 10  # 回退到下方显示

        x = max(x, screen_left + 5)
        y = min(y, screen_bottom - tooltip_height - 5)

        self.tooltip.geometry(f"+{x}+{y}")
        self.tooltip.lift()
        self.tooltip.wm_attributes("-topmost", True)
        self.tooltip.deiconify()
        self.current_tooltip_app = app_name

    def hide_tooltip(self, event):
        """隐藏tooltip"""
        self.tooltip.withdraw()
        self.current_tooltip_app = None

    def move_tooltip(self, event, app_name):
        """移动tooltip跟随鼠标"""
        if self.current_tooltip_app == app_name:
            current_time = time.time()
            if not hasattr(self, 'last_tooltip_move_time'):
                self.last_tooltip_move_time = 0

            if current_time - self.last_tooltip_move_time > 0.05:  # 50ms间隔
                self.last_tooltip_move_time = current_time
                self.show_tooltip(event, app_name)

    def update_main_frame_bg(self):
        """更新主框架背景色"""
        current_color = self.color_caps_on if self.caps_lock_on else self.color_caps_off
        self.main_frame.configure(bg=current_color)
        self.label_container.configure(bg=current_color)
        if hasattr(self, 'caps_frame') and self.caps_frame:
            self.caps_frame.configure(bg=current_color)
        self.caps_text_label.configure(bg=current_color)
        self.status_value_label.configure(bg=current_color)
        if hasattr(self, 'qwen_label'):
            self.qwen_buttons.configure(bg=current_color)
            self.qwen_label.configure(bg=current_color)
            self.qwen_speed_label.configure(bg=current_color)
            if hasattr(self, 'qwen_controller'):
                self._paint_qwen_status()
        if hasattr(self, 'timer_frame') and self.timer_frame:
            self.timer_frame.configure(bg=current_color)
            self.timer_buttons_frame.configure(bg=current_color)
            self.timer_label.configure(bg=current_color)
        if hasattr(self, 'stats_container_frame') and self.stats_container_frame:
            self.stats_container_frame.configure(bg=current_color)
        if hasattr(self, 'stats_canvas') and self.stats_canvas:
            self.stats_canvas.configure(bg=current_color)
        if hasattr(self, 'stats_scrollbar') and self.stats_scrollbar:
            self.stats_scrollbar.configure(bg=current_color, troughcolor=current_color, activebackground=current_color)
        if hasattr(self, 'history_canvas') and self.history_canvas:
            self.history_canvas.configure(bg=current_color)

    def center_window(self):
        """将窗口居中显示在屏幕上"""
        window_width = self.config.get("window_width", 300)
        window_height = self.config.get("window_height", 240)

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        center_x = int((screen_width - window_width) / 2)
        center_y = int((screen_height - window_height) / 2)

        self.root.geometry(f"{window_width}x{window_height}+{center_x}+{center_y}")

    def on_escape(self, event):
        """ESC键关闭窗口"""
        self.close_application()

    def on_menu_close(self):
        """右键菜单关闭事件"""
        self.close_application()

    def show_right_click_menu(self, event):
        """显示右键菜单"""
        self.right_click_menu.post(event.x_root, event.y_root)

    def _create_titlebar_buttons(self):
        """创建标题栏按钮"""
        self.close_button = tk.Label(
            self.titlebar,
            text="×",
            font=("Arial", 12, "bold"),
            fg="white",
            bg=self.color_titlebar,
            cursor="hand2"
        )
        self.close_button.pack(side=tk.RIGHT, padx=5, pady=2)
        self.close_button.bind("<Button-1>", self.on_close_click)

        self.refresh_button = tk.Label(
            self.titlebar,
            text="⟳",
            font=("Arial", 10, "bold"),
            fg="white",
            bg=self.color_titlebar,
            cursor="hand2"
        )
        self.refresh_button.pack(side=tk.RIGHT, padx=5, pady=2)
        self.refresh_button.bind("<Button-1>", lambda e: self.refresh_config())

        self.settings_button = tk.Label(
            self.titlebar,
            text="⚙",
            font=("Arial", 10, "bold"),
            fg="white",
            bg=self.color_titlebar,
            cursor="hand2"
        )
        self.settings_button.pack(side=tk.RIGHT, padx=5, pady=2)
        self.settings_button.bind("<Button-1>", lambda e: self.show_settings_window())

        self.stats_button = tk.Label(
            self.titlebar,
            text="📊",
            font=("Arial", 10, "bold"),
            fg="white",
            bg=self.color_titlebar,
            cursor="hand2"
        )
        self.stats_button.pack(side=tk.RIGHT, padx=5, pady=2)
        self.stats_button.bind("<Button-1>", lambda e: self.show_stats_window())

    def _bind_window_events(self):
        """绑定窗口事件"""
        self.root.bind("<ButtonPress-1>", self.on_window_drag_start)
        self.root.bind("<B1-Motion>", self.on_window_drag_motion)
        self.root.bind("<ButtonRelease-1>", self.on_drag_stop)

        self.root.bind("<Motion>", self.on_mouse_motion)
        self.root.bind("<Enter>", self.on_mouse_enter)
        self.root.bind("<Leave>", self.on_mouse_leave)

        self.root.bind("<Escape>", self.on_escape)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_click)

    def _create_right_click_menu(self):
        """创建右键菜单"""
        self.right_click_menu = Menu(self.root, tearoff=False)
        self.right_click_menu.add_command(label="设置", command=self.show_settings_window)
        self.right_click_menu.add_command(label="刷新", command=self.refresh_config)
        self.right_click_menu.add_command(label="统计", command=self.show_stats_window)
        self.right_click_menu.add_separator()
        self.right_click_menu.add_command(label='启动/加载千问', command=lambda: self.qwen_action('start'))
        self._qwen_start_menu = self.right_click_menu.index('end')
        self.right_click_menu.add_command(label='完全关闭千问', command=lambda: self.qwen_action('stop'))
        self._qwen_stop_menu = self.right_click_menu.index('end')
        self.right_click_menu.add_command(label='刷新千问状态', command=lambda: self.qwen_action('status'))
        self.right_click_menu.add_separator()
        self.right_click_menu.add_command(label="关闭", command=self.on_menu_close)
        self.root.bind("<Button-3>", self.show_right_click_menu)

    def _create_ui_components(self):
        """创建UI组件"""
        self.label_container = tk.Frame(self.main_frame, bg=self.main_frame['bg'])
        self.label_container.pack(expand=True, fill=tk.BOTH)

        self.caps_frame = tk.Frame(self.label_container, bg=self.main_frame['bg'])
        self.caps_frame.pack(anchor='nw', fill=tk.X, padx=10, pady=10)

        self.caps_text_label = tk.Label(
            self.caps_frame,
            text="Caps Lock",
            font=("Arial", 10, "bold"),  # 进一步减小字体大小
            fg="white",
            bg=self.main_frame['bg']
        )
        self.caps_text_label.pack(side=tk.LEFT, padx=(0, 1))  # 进一步减小间距

        self.status_value_label = tk.Label(
            self.caps_frame,
            text="ON" if self.caps_lock_on else "OFF",
            font=("Arial", 10, "bold"),  # 进一步减小字体大小
            fg="white",
            bg=self.main_frame['bg'],
        )
        self.status_value_label.pack(side=tk.LEFT)  # 动态状态标签紧随其后
        self._create_timer_components()
        self._create_qwen_controls()

        self.history_canvas = tk.Canvas(self.main_frame, bg=self.main_frame['bg'], height=self.history_bar_h, borderwidth=0, highlightthickness=0)
        self.history_canvas.pack(fill=tk.X, expand=False, side=tk.BOTTOM)

        self._create_tooltip()

        self._create_stats_components()

    def _create_tooltip(self):
        """创建tooltip组件"""
        self.tooltip = tk.Toplevel(self.root)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry("+0+0")
        self.tooltip.wm_attributes("-topmost", True)
        self.tooltip.wm_attributes("-toolwindow", True)
        self.tooltip.attributes("-alpha", 0.95)
        self.tooltip_label = tk.Label(self.tooltip, text="", bg="#333333", fg="white", font=("Arial", 10), padx=5, pady=2)
        self.tooltip_label.pack()
        self.tooltip.withdraw()
        self.current_tooltip_app = None

    def _create_stats_components(self):
        """创建统计组件"""
        self.stats_toggle_var = tk.BooleanVar(value=False)

        self.stats_container_frame = tk.Frame(self.main_frame, bg=self.main_frame['bg'])
        self.stats_container_frame.pack(expand=False, fill=tk.BOTH)
        self.stats_container_frame.pack_forget()

        self.stats_scrollbar = tk.Scrollbar(self.stats_container_frame, orient=tk.VERTICAL, bg="white", troughcolor="white", activebackground="white", borderwidth=0, highlightthickness=0)

        self.stats_canvas = tk.Canvas(self.stats_container_frame, bg="white", height=160, yscrollcommand=self.stats_scrollbar.set, borderwidth=0, highlightthickness=0)
        self.stats_canvas.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(0, 3))  # 为滚动条预留空间

        self.stats_scrollbar.config(command=self.stats_canvas.yview)
        self.stats_scrollbar.place(relx=1, rely=0, relheight=1, x=-3, anchor='ne')  # 向左移动3像素

        self.stats_canvas.configure(scrollregion=self.stats_canvas.bbox("all"), borderwidth=0, highlightthickness=0)

        self.stats_canvas.bind_all("<MouseWheel>", lambda event: self.stats_canvas.yview_scroll(int(-1*(event.delta/120)), "units"))

    def _create_timer_components(self):
        """创建倒计时组件"""
        self.timer_frame = tk.Frame(self.label_container, bg=self.main_frame['bg'])
        self.timer_frame.pack(expand=True, fill=tk.BOTH, padx=12, pady=(0, 8))
        self.timer_frame.grid_rowconfigure(0, weight=1)
        self.timer_frame.grid_columnconfigure((0, 1), weight=1)

        self.timer_label = tk.Label(
            self.timer_frame,
            text=self.format_duration(self.timer_total_seconds),
            font=("Arial", 24, "bold"),  # 减小字体大小
            fg="white",
            bg=self.main_frame['bg']
        )
        self.timer_label.grid(row=0, column=0, pady=(0, 1))

        self.timer_buttons_frame = tk.Frame(self.timer_frame, bg=self.main_frame['bg'])
        self.timer_buttons_frame.grid(row=2, column=0, pady=(1, 1))

        self.start_button = tk.Button(
            self.timer_buttons_frame,
            text="▶",  # 播放符号
            command=self.start_timer,
            font=("Arial", 9),  # 字体更小
            fg="white",
            bg="#3498db",  # 蓝色
            relief=tk.FLAT,
            padx=2,  # 调整内边距
            pady=2,  # 调整内边距，使按钮为正方形
            width=2,  # 宽度
            height=1  # 高度，使按钮为正方形
        )
        self.start_button.pack(side=tk.LEFT, padx=2)  # 缩小间距

        self.pause_button = tk.Button(
            self.timer_buttons_frame,
            text="⏸",  # 暂停符号
            command=self.pause_timer,
            font=("Arial", 9),  # 字体更小
            fg="white",
            bg="#9b59b6",  # 紫色
            relief=tk.FLAT,
            padx=2,  # 调整内边距
            pady=2,  # 调整内边距，使按钮为正方形
            width=2,  # 宽度
            height=1,  # 高度，使按钮为正方形
            state=tk.DISABLED
        )
        self.pause_button.pack(side=tk.LEFT, padx=2)  # 缩小间距

        self.reset_button = tk.Button(
            self.timer_buttons_frame,
            text="⏹",  # 停止符号
            command=self.reset_timer,
            font=("Arial", 9),  # 字体更小
            fg="white",
            bg="#95a5a6",  # 灰色
            relief=tk.FLAT,
            padx=2,  # 调整内边距
            pady=2,  # 调整内边距，使按钮为正方形
            width=2,  # 宽度
            height=1  # 高度，使按钮为正方形
        )
        self.reset_button.pack(side=tk.LEFT, padx=2)  # 缩小间距
