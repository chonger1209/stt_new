import tkinter as tk
from tkinter import Menu
import win32api
import win32con
import win32gui
import win32process
import time
import logging
import os
import datetime
import sqlite3
from collections import deque
import gc

class CapsLockChecker:
    def __init__(self, root):
        self.root = root
        self.root.title("Caps Lock 状态检测")
        
        # 初始化日志功能
        self.setup_logging()
        
        # 设置默认颜色值
        self.color_caps_on = "#fa6666"
        self.color_caps_off = "#4CAF50"
        self.color_titlebar = "#2c3e50"
        self.root.resizable(True, True)
        self.root.overrideredirect(True)  # 永久无边框窗口，避免overrideredirect切换导致的卡顿
        
        # 标题栏状态和拖动控制
        self.titlebar_visible = False
        self.dragging = False
        self.drag_offset = (0, 0)
        self.leave_hide_timer = None  # 鼠标离开后延迟隐藏的计时器
        self.last_mouse_move_time = 0  # 记录上次鼠标移动时间
        

        # 创建自定义标题栏
        self.titlebar = tk.Frame(self.root, bg=self.color_titlebar, height=30)
        self.titlebar.pack_propagate(False)  # 防止标题栏高度被内部组件改变

        
        # 创建标题栏按钮
        self._create_titlebar_buttons()
        
        # 设置标题栏拖动功能
        self.titlebar.bind("<ButtonPress-1>", self.on_titlebar_drag_start)
        self.titlebar.bind("<B1-Motion>", self.on_titlebar_drag_motion)
        
        # 绑定窗口拖动事件（标题栏隐藏时可拖动整个窗口）
        self._bind_window_events()
        
        # 创建右键菜单
        self._create_right_click_menu()
        
        # 设置窗口初始大小和位置
        self.root.geometry("250x180+100+100")
        # 初始化Caps Lock状态
        self.caps_lock_on = win32api.GetKeyState(win32con.VK_CAPITAL) & 1 != 0
        self.last_hwnd = None
        self.usage_stats = {}
        
        # 初始化应用跟踪变量
        self.current_app_name = None
        self.current_start_time = time.time()
        self.last_flush_time = self.current_start_time
        self.time_stream_write_interval = 30
        self.stats_window = None
        self.stats_canvas = None
        
        # 创建主框架，填充整个窗口
        self.main_frame = tk.Frame(self.root, bg=self.color_caps_on if self.caps_lock_on else self.color_caps_off)
        self.main_frame.place(x=0, y=0, relwidth=1, relheight=1)
        
        # 初始化原始高度，使用当前窗口高度
        self.original_height = self.root.winfo_height()
        
        # 初始化颜色和渲染优化变量
        self._init_color_and_rendering_vars()
        
        # 创建UI组件
        self._create_ui_components()

        # 读取配置文件
        self.read_config()
        self.grid_second_per_block = self.config.get('grid_second_per_block', 60)  # 设置grid_second_per_block属性
        
        # 优化后的内存缓存机制 - 使用deque限制大小
        self.MAX_CACHE_SIZE = 1000  # 限制缓存最大记录数
        self.time_stream_cache = {}  # 存储时间流像素格数据 {app_name: deque([(timestamp, duration), ...], maxlen=100)}
        self.last_cache_flush_time = time.time()  # 上次缓存刷新时间
        self.cache_flush_interval = 60  # 60秒刷新一次缓存到数据库
        self.last_grid_update_time = time.time()  # 上次网格更新时间
        
        # 分离的定时器管理
        self.last_stats_update_time = time.time()
        self.last_history_render_time = time.time()
        self.last_window_check_time = time.time()
        
        # Canvas事件绑定缓存，避免重复绑定
        self.bound_canvas_tags = set()
        
        # 初始化窗口高度变化检测和倒计时功能
        self.window_height_change_timer = None  # 窗口高度变化后的倒计时器
        self.last_window_height = self.root.winfo_height()  # 记录上次窗口高度
        self.height_change_detected = False  # 是否检测到高度变化
        self.auto_restore_timer = None  # 自动恢复窗口高度的计时器
        
        # 初始化数据库
        self.init_db()
        
        # 设置初始化标志，用于像素格渲染优化
        self._is_initializing = True
        
        # 加载最近的缓存数据，防止程序重启后像素格丢失
        self.load_recent_cache_data()
        
        # 立即绘制历史流
        self.render_history_stream()
        # 绑定窗口大小变化事件，仅在宽度变化时重绘
        self.last_history_canvas_width = self.history_canvas.winfo_width()
        self.history_canvas.bind("<Configure>", self.on_history_canvas_configure)
        
        # 启动优化后的定时任务
        self.start_scheduled_tasks()
        
        # 应用配置
        self.apply_config()
        
        # 隐藏标题栏
        self.hide_titlebar()
        
        # 记录程序初始化完成日志
        self.logger.info("程序初始化完成 - UI组件已创建，配置已加载，数据库已初始化")
        
        # 延迟初始化完成，确保所有组件正确渲染
        self.root.after(100, self._finish_initialization)
    
    def _finish_initialization(self):
        """延迟初始化完成，确保所有组件正确渲染"""
        try:
            # 强制更新窗口
            self.root.update_idletasks()
            # 刷新主框架背景
            self.update_main_frame_bg()
            # 更新状态显示
            self.update_status()
            # 如果统计面板打开，重新渲染
            if self.stats_toggle_var.get():
                self.render_stats_chart()
                self.render_history_stream()
            self.logger.debug("延迟初始化完成，界面已刷新")
        except Exception as e:
            self.logger.error(f"延迟初始化时出错: {e}")
    
    def start_scheduled_tasks(self):
        """启动所有定时任务，分离各个功能的执行频率"""
        # 1. Caps Lock状态检查 - 200ms（已优化）
        self.check_caps_lock()
        
        # 2. 屏幕时间和UI刷新 - 基于screen_time_refresh_frequency
        self.schedule_screen_time_refresh()
        
        # 3. 时间流网格更新 - 基于grid_second_per_block
        self.schedule_grid_update()
        
        # 4. 缓存刷新 - 60秒
        self.schedule_cache_flush()
    
    def schedule_screen_time_refresh(self):
        """基于screen_time_refresh_frequency的刷新任务"""
        refresh_interval = self.config.get('screen_time_refresh_frequency', 10)
        interval_ms = max(1000, int(10000 / refresh_interval))  # 转换为毫秒
        
        # 执行刷新
        if self.stats_toggle_var.get():
            self.update_stats_window()
            self.render_stats_chart()
        
        # 检测窗口高度变化
        self.check_window_height_change()
        
        # 计划下次执行
        self.root.after(interval_ms, self.schedule_screen_time_refresh)
    
    def schedule_grid_update(self):
        """时间流网格更新任务"""
        interval_ms = self.grid_second_per_block * 1000
        
        # 更新时间流缓存
        self.update_time_stream_cache()
        
        # 每30秒更新一次历史流渲染
        current_time = time.time()
        if current_time - self.last_history_render_time >= 30:
            self.render_history_stream()
            self.last_history_render_time = current_time
        
        # 计划下次执行
        self.root.after(interval_ms, self.schedule_grid_update)
    
    def schedule_cache_flush(self):
        """缓存刷新任务"""
        # 刷新缓存到数据库
        self.flush_time_stream_cache()
        
        # 清理过大的缓存
        self.cleanup_oversized_cache()
        
        # 计划下次执行
        self.root.after(60000, self.schedule_cache_flush)  # 60秒
    
    def cleanup_oversized_cache(self):
        """清理过大的缓存，防止内存无限增长"""
        for app_name in list(self.time_stream_cache.keys()):
            if app_name in self.time_stream_cache:
                cache = self.time_stream_cache[app_name]
                if isinstance(cache, list) and len(cache) > 100:
                    # 保留最新的100条记录
                    self.time_stream_cache[app_name] = deque(cache[-100:], maxlen=100)
                elif isinstance(cache, deque) and len(cache) > 100:
                    # deque会自动限制大小，但这里做额外检查
                    pass
    
    def update_status(self):
        """更新界面显示的Caps Lock状态"""
        if self.caps_lock_on:
            status_text = "ON"
        else:
            status_text = "OFF"

        # 更新状态文本
        self.status_value_label.configure(text=status_text)

    def get_app_name_from_hwnd(self, hwnd):
        """获取窗口所属程序名，并应用配置的显示名称 - 修复win32api句柄泄漏"""
        # 验证窗口句柄有效性
        if not hwnd or not win32gui.IsWindow(hwnd):
            return "Unknown"
        
        try:
            # 特殊处理LockApp.exe
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            
            # 检查是否是LockApp窗口
            if class_name == "LockAppFrame" or (title == "" and "LockApp" in class_name):
                # 检查进程配置，返回显示名称
                if 'process_config' in self.config and 'LockApp.exe' in self.config['process_config']:
                    return self.config['process_config']['LockApp.exe']['display_name']
                return "LockApp.exe"
            
            # 特殊处理任务管理器窗口
            if class_name == "TaskManagerWindow" or "任务管理器" in title or "Task Manager" in title:
                # 检查进程配置，返回显示名称
                if 'process_config' in self.config and 'Taskmgr.exe' in self.config['process_config']:
                    return self.config['process_config']['Taskmgr.exe']['display_name']
                return "任务管理器"
            
            # 常规进程名称获取 - 修复句柄泄漏
            tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            
            # 修复：更严格的句柄管理
            hproc = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            if hproc and hproc != 0:  # 检查句柄有效性
                try:
                    modules = win32process.EnumProcessModules(hproc)
                    if modules and len(modules) > 0:
                        path = win32process.GetModuleFileNameEx(hproc, modules[0])
                        process_name = os.path.basename(path)
                        
                        # 检查进程配置，返回显示名称
                        if 'process_config' in self.config:
                            for proc_name, config in self.config['process_config'].items():
                                if process_name.lower() == proc_name.lower():
                                    return config['display_name']  # 返回配置的显示名称
                        
                        # 如果没有配置，返回原进程名
                        return process_name
                finally:
                    # 确保句柄被关闭
                    win32api.CloseHandle(hproc)
            else:
                # 如果无法打开进程（权限问题），尝试通过窗口类名和标题识别
                if class_name == "TaskManagerWindow" or "任务管理器" in title or "Task Manager" in title:
                    return "任务管理器"
                # 尝试通过窗口标题识别
                if title:
                    return title
            
            # 如果所有方法都失败，回退到窗口标题
            title = win32gui.GetWindowText(hwnd)
            return title or "Unknown"
        except Exception as e:
            # 捕获所有可能的异常，返回默认值
            # 尝试通过窗口类名和标题识别
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

    def db_file(self):
        """获取SQLite数据库文件名"""
        return "screen_time_history.db"
    
    def time_stream_db_file(self):
        """获取时间流SQLite数据库文件名"""
        return "time_stream_history.db"
    
    def init_db(self):
        """初始化SQLite数据库，创建表"""
        # 初始化屏幕使用时间数据库
        db_path = self.db_file()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS screen_time (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                app_name TEXT NOT NULL,
                duration REAL NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_screen_time_timestamp ON screen_time (timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_screen_time_app ON screen_time (app_name)')
        conn.commit()
        conn.close()
        
        # 初始化时间流数据库
        time_stream_db_path = self.time_stream_db_file()
        conn = sqlite3.connect(time_stream_db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS time_stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                app_name TEXT NOT NULL,
                duration REAL NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_time_stream_timestamp ON time_stream (timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_time_stream_app ON time_stream (app_name)')
        
        conn.commit()
        conn.close()
    
    def write_to_db(self, data):
        """将数据写入SQLite数据库"""
        with sqlite3.connect(self.db_file()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO screen_time (timestamp, app_name, duration) VALUES (?, ?, ?)',
                data
            )
            conn.commit()
    
    def write_to_time_stream_db(self, data):
        """将数据写入时间流SQLite数据库"""
        with sqlite3.connect(self.time_stream_db_file()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO time_stream (timestamp, app_name, duration) VALUES (?, ?, ?)',
                data
            )
            conn.commit()
    
    def get_today_time_stream_from_db(self):
        """从时间流数据库获取今天的全部历史记录"""
        # 获取今天的本地日期
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        
        with sqlite3.connect(self.time_stream_db_file()) as conn:
            cursor = conn.cursor()
            # 获取当天00:00到当前时间的全部数据，包含时间戳
            cursor.execute(
                f'SELECT timestamp, app_name, duration FROM time_stream WHERE DATE(timestamp) = "{today}" ORDER BY timestamp',
            )
            rows = cursor.fetchall()
            return rows
            
    def get_screen_time_from_db(self):
        """从数据库获取按应用分组的屏幕使用时间数据"""
        with sqlite3.connect(self.db_file()) as conn:
            cursor = conn.cursor()
            # 获取今天的本地日期
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            # 使用本地日期查询
            cursor.execute(
                f'SELECT app_name, SUM(duration) AS total_duration FROM screen_time WHERE DATE(timestamp) = "{today}" GROUP BY app_name'
            )
            rows = cursor.fetchall()
            
            # 将结果转换为字典格式
            screen_time_dict = {}
            for app_name, total_duration in rows:
                screen_time_dict[app_name] = total_duration
            
            return screen_time_dict
    
    def handle_window_switch(self, hwnd):
        """窗口切换时记录时长并切换当前程序"""
        # 验证窗口句柄有效性
        if not hwnd or not win32gui.IsWindow(hwnd):
            return
            
        now = time.time()
        if self.current_app_name:
            elapsed = now - self.current_start_time
            # 只记录屏幕使用时间（用于统计显示）
            self.record_screen_time(self.current_app_name, elapsed)
        
        try:
            self.current_app_name = self.get_app_name_from_hwnd(hwnd)
            self.current_start_time = now
            self.last_hwnd = hwnd
        except Exception as e:
            # 处理获取应用名称时的异常
            self.logger.error(f"Error handling window switch: {e}")
            # 保持当前应用不变，避免数据丢失
    
    def record_screen_time(self, app_name, duration):
        """记录屏幕使用时间（用于统计显示）"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.write_to_db((timestamp, app_name, duration))
    
    def record_time_stream(self, app_name, duration):
        """记录时间流数据（仅记录窗口切换时的原始数据）"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.write_to_time_stream_db((timestamp, app_name, duration))
        # 不再写入30分钟数据块，改为使用内存缓存机制
    
    def update_time_stream_cache(self):
        """优化的时间流缓存更新"""
        if self.current_app_name:
            current_time = time.time()
            # 计算自上次更新以来的时间
            elapsed = current_time - self.last_grid_update_time
            
            # 只记录有效的时间段（大于0）
            if elapsed > 0:
                # 更新缓存中的时间流数据
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if self.current_app_name not in self.time_stream_cache:
                    # 使用deque限制每个应用的缓存大小
                    self.time_stream_cache[self.current_app_name] = deque(maxlen=100)
                
                # 添加到缓存
                self.time_stream_cache[self.current_app_name].append((timestamp, elapsed))
            
            self.last_grid_update_time = current_time
    
    def load_recent_cache_data(self):
        """加载最近的缓存数据（优化版）"""
        try:
            # 获取当前时间前15分钟的时间点
            fifteen_minutes_ago = datetime.datetime.now() - datetime.timedelta(minutes=15)
            fifteen_minutes_ago_str = fifteen_minutes_ago.strftime('%Y-%m-%d %H:%M:%S')
            
            with sqlite3.connect(self.time_stream_db_file()) as conn:
                cursor = conn.cursor()
                # 只获取最近15分钟的数据
                cursor.execute(
                    'SELECT timestamp, app_name, duration FROM time_stream WHERE timestamp >= ? ORDER BY timestamp',
                    (fifteen_minutes_ago_str,)
                )
                rows = cursor.fetchall()
                
                # 记录已加载的数据时间戳范围，避免重复
                self.cache_data_timestamps = set()
                
                # 将数据加载到缓存中，限制每个应用最多100条
                for timestamp, app_name, duration in rows[-self.MAX_CACHE_SIZE:]:
                    if app_name not in self.time_stream_cache:
                        self.time_stream_cache[app_name] = deque(maxlen=100)
                    self.time_stream_cache[app_name].append((timestamp, duration))
                    # 记录已加载的时间戳
                    self.cache_data_timestamps.add(timestamp)
                
                if rows:
                    self.logger.info(f"加载了 {len(rows)} 条最近的缓存数据")
        except Exception as e:
            self.logger.error(f"加载缓存数据时出错: {e}")
    
    def flush_time_stream_cache(self):
        """优化的缓存刷新，减少数据库操作"""
        if not self.time_stream_cache:
            return
        
        # 减少缓存刷新日志，只在有大量数据时记录
        total_apps = len(self.time_stream_cache)
        total_records = sum(len(records) for records in self.time_stream_cache.values())
        if total_records > 50:  # 只在记录数较多时记录日志
            self.logger.info(f"缓存刷新 - 应用数: {total_apps}, 记录数: {total_records}")
        
        # 批量准备数据
        records_to_insert = []
        
        # 一次性获取已存在的时间戳
        with sqlite3.connect(self.time_stream_db_file()) as conn:
            cursor = conn.cursor()
            one_hour_ago = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('SELECT timestamp FROM time_stream WHERE timestamp >= ?', (one_hour_ago,))
            existing_timestamps = {row[0] for row in cursor.fetchall()}
        
        # 准备插入数据
        for app_name, records in self.time_stream_cache.items():
            if isinstance(records, deque):
                records = list(records)
            for timestamp, duration in records:
                if timestamp not in existing_timestamps:
                    records_to_insert.append((timestamp, app_name, duration))
                    existing_timestamps.add(timestamp)
        
        # 批量插入
        if records_to_insert:
            with sqlite3.connect(self.time_stream_db_file()) as conn:
                cursor = conn.cursor()
                cursor.executemany(
                    'INSERT INTO time_stream (timestamp, app_name, duration) VALUES (?, ?, ?)',
                    records_to_insert
                )
                conn.commit()
            
            # 减少日志输出，只在有大量数据时记录
            if len(records_to_insert) > 50:
                self.logger.info(f"缓存刷新完成 - 写入 {len(records_to_insert)} 条记录")
        
        # 清空缓存
        self.time_stream_cache.clear()
        
        # 强制垃圾回收
        gc.collect()
    


    def format_duration(self, seconds):
        """格式化时长"""
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def show_stats_window(self):
        """切换主UI中的统计显示/隐藏"""
        # 切换统计显示开关的状态
        current_state = self.stats_toggle_var.get()
        self.stats_toggle_var.set(not current_state)
        self.toggle_stats_visibility()
    
    def toggle_stats_visibility(self):
        """切换统计组件的可见性"""
        if self.stats_toggle_var.get():
            # 保存当前窗口高度作为原始高度
            self.original_height = self.root.winfo_height()
            
            # 计算所需高度（固定显示7个项目）
            bar_h = 24
            gap = 6
            visible_items = 7
            stats_height = (bar_h + gap) * (visible_items + 1) + gap  # 固定高度，增加底部历史流条
            
            # 计算新窗口高度
            new_window_height = self.original_height + stats_height + 10  # 加10是pady的间距
            
            # 调整窗口高度
            self.root.geometry(f"{self.root.winfo_width()}x{new_window_height}")
            
            # 显示统计组件
            self.stats_container_frame.pack(expand=False, fill=tk.BOTH, pady=10)
            # 立即绘制统计图表和历史流条
            self.render_stats_chart()
            self.render_history_stream()
            

        else:
            # 隐藏统计组件
            self.stats_container_frame.pack_forget()
            # 恢复原始窗口高度
            self.root.geometry(f"{self.root.winfo_width()}x{self.original_height}")

    def render_history_stream(self):
        """
        优化的历史流渲染，修复内存泄漏问题
        """
        # 获取数据
        db_history = self.get_today_time_stream_from_db()
        
        # 合并缓存数据
        cache_data = []
        cache_timestamps = set()
        for app_name, records in self.time_stream_cache.items():
            if isinstance(records, deque):
                records = list(records)
            for timestamp, duration in records:
                cache_timestamps.add(timestamp)
                cache_data.append((timestamp, app_name, duration))
        
        # 过滤重复
        filtered_db_history = []
        for timestamp, app_name, duration in db_history:
            if timestamp not in cache_timestamps:
                filtered_db_history.append((timestamp, app_name, duration))
        
        # 合并并排序
        all_history = filtered_db_history + cache_data
        all_history.sort(key=lambda x: x[0])
        
        # 添加当前应用
        if self.current_app_name:
            elapsed = time.time() - self.current_start_time
            if elapsed > 0:
                current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                all_history.append((current_time, self.current_app_name, elapsed))
        
        # 过滤
        valid_history = []
        for timestamp, app, duration in all_history:
            if duration > 0 and self.check_app_display_config(app, 'show_in_time_stream'):
                valid_history.append((app, duration))
        
        # 检查是否需要更新
        current_history_hash = hash(tuple(valid_history))
        if not self._is_initializing and current_history_hash == getattr(self, 'last_history_hash', None):
            return
        
        self.last_history_hash = current_history_hash
        self._is_initializing = False
        
        # 获取canvas尺寸
        w = self.history_canvas.winfo_width()
        if w <= 0:
            return
        
        # 清理旧的事件绑定（修复内存泄漏的关键）
        for tag in self.bound_canvas_tags:
            for event in ["<Enter>", "<Leave>", "<Motion>"]:
                try:
                    self.history_canvas.tag_unbind(tag, event)
                except:
                    pass
        self.bound_canvas_tags.clear()
        
        # 清空canvas
        self.history_canvas.delete("all")
        # 清除历史点击绑定
        if hasattr(self, 'history_click_bindings'):
            self.history_click_bindings.clear()
        
        if not valid_history:
            return
        
        # 确保颜色分配
        for app, dur in valid_history:
            if app not in self.app_color_map:
                color_index = len(self.app_color_map) % len(self.color_palette)
                self.app_color_map[app] = self.color_palette[color_index]
        
        # 绘制方块
        block_size = 5
        interval_seconds = self.grid_second_per_block
        
        block_counts = []
        for app, dur in valid_history:
            count = max(1, int(dur / interval_seconds)) if dur >= interval_seconds else 1
            block_counts.append(count)
        
        total_blocks_needed = sum(block_counts)
        
        current_col = 0
        current_row = 0
        max_cols = max(1, w // block_size)
        
        # 更新画布高度
        total_rows_needed = max(20, (total_blocks_needed + max_cols - 1) // max_cols)
        new_height = total_rows_needed * block_size
        if new_height != self.history_bar_h:
            self.history_bar_h = new_height
            self.history_canvas.configure(height=self.history_bar_h)
        
        # 批量绘制方块
        for i, (app, dur) in enumerate(valid_history):
            count = block_counts[i]
            color = self.app_color_map[app]
            
            # 绘制所有方块
            for _ in range(count):
                x = current_col * block_size
                y = current_row * block_size
                
                rect_id = self.history_canvas.create_rectangle(
                    x, y, 
                    x + block_size - 1, y + block_size - 1,
                    fill=color, 
                    outline="",
                    tags=(app,)
                )
                
                current_col += 1
                if current_col >= max_cols:
                    current_col = 0
                    current_row += 1
            
            # 只绑定一次事件（修复内存泄漏的关键）
            if app not in self.bound_canvas_tags:
                self.history_canvas.tag_bind(app, "<Enter>", lambda e, a=app: self.show_tooltip(e, a))
                self.history_canvas.tag_bind(app, "<Leave>", self.hide_tooltip)
                self.history_canvas.tag_bind(app, "<Motion>", lambda e, a=app: self.move_tooltip(e, a))
                self.bound_canvas_tags.add(app)

    def check_app_display_config(self, app_name, config_type):
        """
        检查应用的显示配置
        config_type: 'show_in_screen_time' 或 'show_in_time_stream'
        """
        if 'process_config' not in self.config:
            return True  # 默认显示
            
        for proc_name, config in self.config['process_config'].items():
            if app_name == config['display_name']:
                return config.get(config_type, True)  # 默认显示
                
        return True  # 未找到配置则默认显示

    def render_stats_chart(self):
        """绘制横向条形图，仅使用数据库数据"""
        if not self.stats_canvas:
            return
        self.stats_canvas.update_idletasks()
        w = self.stats_canvas.winfo_width()
        
        # 从数据库获取今天的屏幕使用时间数据
        stats = self.get_screen_time_from_db()
        
        # 添加当前应用的临时时间
        if self.current_app_name:
            stats[self.current_app_name] = stats.get(self.current_app_name, 0) + (time.time() - self.current_start_time)
            
        # 过滤掉不需要在屏幕时间中显示的应用
        filtered_stats = {}
        for app, sec in stats.items():
            if self.check_app_display_config(app, 'show_in_screen_time'):
                filtered_stats[app] = sec
                
        items = sorted(filtered_stats.items(), key=lambda x: x[1], reverse=True)
        
        # 移除哈希检查，确保按照screen_time_refresh_frequency刷新
        # 计算当前统计数据的哈希值，避免重复渲染
        # current_stats_hash = hash(tuple(items))
        # if current_stats_hash == self.last_stats_hash:
        #     return
        # self.last_stats_hash = current_stats_hash
        
        # 彻底清理画布内存
        self.cleanup_chart_memory()
        
        if not items:
            return
            
        max_val = max(v for _, v in items)
        bar_h = 24
        gap = 6
        label_w = 20  # 调整柱形图起始位置，使其居左
        y = gap
        
        # 计算所需高度（至少容纳7个进程）
        num_items = max(len(items[:50]), 7)
        required_h = (bar_h + gap) * num_items + gap
        
        # 设置canvas高度（固定显示7个项目 + 底部历史流条）
        visible_items = 7
        self.stats_canvas.configure(height=(bar_h + gap) * (visible_items + 1) + gap)
        
        # 绘制每个条形图
        for i, (app, sec) in enumerate(items[:50]):
            width = int((w - label_w - 10) * (sec / max_val)) if max_val > 0 else 1  # 减少右侧边距，增加条宽度
            # 使用与时间流一致的颜色映射
            if app not in self.app_color_map:
                self.app_color_map[app] = self.color_palette[len(self.app_color_map) % len(self.color_palette)]
            color = self.app_color_map[app]
            # 绘制条形图
            self.stats_canvas.create_rectangle(label_w, y, label_w + width, y + bar_h, fill=color, outline="")
            # 在条形图内部绘制应用名称（左对齐）
            self.stats_canvas.create_text(label_w + 8, y + bar_h/2, anchor="w", text=app, fill="#ffffff", font=('Arial', 10))
            # 在条形图上方绘制时长（居中）
            canvas_width = self.stats_canvas.winfo_width()
            self.stats_canvas.create_text(canvas_width - 10, y + bar_h/2, anchor="e", text=self.format_duration(sec), fill="#ffffff", font=('Arial', 10))
            y += bar_h + gap
        
        # 绘制历史流条到独立的canvas
        self.render_history_stream()
        
        # 设置滚动区域
        self.stats_canvas.config(scrollregion=self.stats_canvas.bbox("all"))


    

            
    
    def update_stats_window(self):
        """刷新统计窗口（只更新右侧时间显示，不更新直方图）"""
        if not self.stats_canvas or not self.stats_toggle_var.get():
            return
            
        # 获取当前的统计数据
        stats = self.get_screen_time_from_db()
        
        # 添加当前应用的临时时间
        if self.current_app_name:
            stats[self.current_app_name] = stats.get(self.current_app_name, 0) + (time.time() - self.current_start_time)
            
        # 过滤掉不需要在屏幕时间中显示的应用
        filtered_stats = {}
        for app, sec in stats.items():
            if self.check_app_display_config(app, 'show_in_screen_time'):
                filtered_stats[app] = sec
                
        items = sorted(filtered_stats.items(), key=lambda x: x[1], reverse=True)
        
        # 只更新右侧的时间文本，不重新绘制整个直方图
        canvas_width = self.stats_canvas.winfo_width()
        bar_h = 24
        gap = 6
        y = gap
        
        # 遍历现有的时间文本项并更新
        for i, (app, sec) in enumerate(items[:50]):
            # 查找并更新右侧的时间文本
            time_text = self.stats_canvas.find_overlapping(canvas_width - 100, y, canvas_width, y + bar_h)
            for item in time_text:
                # 检查是否是文本项
                if self.stats_canvas.type(item) == "text":
                    # 获取当前文本内容，如果是时间格式则更新
                    current_text = self.stats_canvas.itemcget(item, "text")
                    if ":" in current_text and len(current_text) <= 8:  # 简单判断是否是时间格式
                        self.stats_canvas.itemconfig(item, text=self.format_duration(sec))
                        break
            y += bar_h + gap
        

    def on_history_canvas_configure(self, event):
        prev_w = getattr(self, 'last_history_canvas_width', None)
        if prev_w is not None and event.width == prev_w:
            return
        self.last_history_canvas_width = event.width
        self.render_history_stream()


    
    def check_caps_lock(self):
        """优化后：仅检查Caps Lock状态和窗口切换，不做其他刷新操作"""
        # 获取当前状态
        current_status = win32api.GetKeyState(win32con.VK_CAPITAL) & 1 != 0
        hwnd = win32gui.GetForegroundWindow()
        
        # 检查窗口切换
        if hwnd != self.last_hwnd:
            self.handle_window_switch(hwnd)
            
            # 获取当前窗口的程序名（使用更准确的方法）
            try:
                app_name = self.get_app_name_from_hwnd(hwnd)
                window_title = win32gui.GetWindowText(hwnd)
                
                # 检查是否是需要大写的软件（通过程序名或窗口标题）
                software_list = self.config.get('software_list', ['CAXA', 'CAD', 'SOLIDWORKS'])
                is_caps_required_software = False
                
                # 检查程序名和窗口标题
                for software in software_list:
                    if (software.lower() in app_name.lower() or 
                        software.lower() in window_title.lower()):
                        is_caps_required_software = True
                        # 减少日志输出，只在DEBUG模式下记录
                        self.logger.debug(f"检测到需要大写的软件: {software} (程序: {app_name}, 标题: {window_title})")
                        break
                
                # 根据软件类型自动切换大小写
                if is_caps_required_software and not current_status:
                    # 需要大写但当前是小写，切换为大写
                    # 减少日志输出，改为DEBUG级别
                    self.logger.debug(f"切换到大写 - 检测到软件: {app_name}")
                    win32api.keybd_event(win32con.VK_CAPITAL, 0, 0, 0)
                    win32api.keybd_event(win32con.VK_CAPITAL, 0, win32con.KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)  # 等待系统响应
                    # 重新获取状态以确保同步
                    current_status = win32api.GetKeyState(win32con.VK_CAPITAL) & 1 != 0
                    self.caps_lock_on = current_status
                    self.update_main_frame_bg()
                    self.update_status()
                elif not is_caps_required_software and current_status:
                    # 不需要大写但当前是大写，切换为小写
                    # 减少日志输出，改为DEBUG级别
                    self.logger.debug(f"切换到小写 - 当前软件: {app_name}")
                    win32api.keybd_event(win32con.VK_CAPITAL, 0, 0, 0)
                    win32api.keybd_event(win32con.VK_CAPITAL, 0, win32con.KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)  # 等待系统响应
                    # 重新获取状态以确保同步
                    current_status = win32api.GetKeyState(win32con.VK_CAPITAL) & 1 != 0
                    self.caps_lock_on = current_status
                    self.update_main_frame_bg()
                    self.update_status()
                else:
                    # 状态已正确，只更新显示
                    if self.caps_lock_on != current_status:
                        self.caps_lock_on = current_status
                        self.update_main_frame_bg()
                        self.update_status()
            except Exception as e:
                self.logger.error(f"处理窗口切换时出错: {e}")
                # 即使出错也要更新状态
                if self.caps_lock_on != current_status:
                    self.caps_lock_on = current_status
                    self.update_main_frame_bg()
                    self.update_status()
        else:
            # 仅在状态真正改变时更新UI
            if self.caps_lock_on != current_status:
                self.caps_lock_on = current_status
                self.update_main_frame_bg()
                self.update_status()
                # 更新历史流背景颜色
                if hasattr(self, 'history_canvas'):
                    self.history_canvas.configure(bg=self.main_frame['bg'])
        
        # 继续下一次检查（增加检查间隔以减少CPU占用）
        self.root.after(200, self.check_caps_lock)
    



    
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
        
        # 添加时间戳检查，限制标题栏显示的频率
        if current_time - self.last_mouse_move_time > 0.05:  # 50ms间隔
            self.last_mouse_move_time = current_time
            
            # 鼠标靠近顶部时显示标题栏
            if not self.titlebar_visible and event.y < 30:
                self.show_titlebar()
        
        # 取消延迟隐藏计时器
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
            # 延迟隐藏标题栏，确保用户有足够时间操作
            self.leave_hide_timer = self.root.after(500, self.hide_titlebar)
    
    def get_mouse_screen_info(self):
        """获取鼠标所在屏幕的矩形信息 (left, top, right, bottom)"""
        point = win32api.GetCursorPos()
        monitors = win32api.EnumDisplayMonitors()
        for monitor in monitors:
            monitor_rect = monitor[2]  # 矩形区域 (left, top, right, bottom)
            if monitor_rect[0] <= point[0] < monitor_rect[2] and monitor_rect[1] <= point[1] < monitor_rect[3]:
                return monitor_rect
        # 如果没有找到，返回主屏幕的信息
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def show_tooltip(self, event, app_name):
        """显示tooltip，显示应用程序名称"""
        # 设置tooltip文本
        self.tooltip_label.configure(text=app_name)
        # 更新tooltip以获取正确的尺寸
        self.tooltip.update_idletasks()
        
        tooltip_width = self.tooltip.winfo_width()
        tooltip_height = self.tooltip.winfo_height()
        
        # 鼠标当前位置（全局坐标）
        mouse_x = event.x_root
        mouse_y = event.y_root
        
        # 获取鼠标所在屏幕的信息
        screen_left, screen_top, screen_right, screen_bottom = self.get_mouse_screen_info()
        
        # 修改：默认在鼠标上方显示tooltip，避免被软件遮盖
        x = mouse_x + 10
        y = mouse_y - tooltip_height - 10  # 显示在鼠标上方
        
        # 确保tooltip在屏幕内，不超出右边界
        if x + tooltip_width > screen_right:
            x = mouse_x - tooltip_width - 10
        # 确保不超出上边界（如果上方空间不足，则显示在下方）
        if y < screen_top + 5:
            y = mouse_y + 10  # 回退到下方显示
        
        # 确保不超出左边界和下边界
        x = max(x, screen_left + 5)
        y = min(y, screen_bottom - tooltip_height - 5)
        
        # 更新tooltip位置并显示
        self.tooltip.geometry(f"+{x}+{y}")
        # 确保tooltip始终在最上层
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
        # 如果当前tooltip显示的是这个应用，更新位置
        if self.current_tooltip_app == app_name:
            # 添加时间戳检查，限制tooltip移动的频率
            current_time = time.time()
            if not hasattr(self, 'last_tooltip_move_time'):
                self.last_tooltip_move_time = 0
                
            if current_time - self.last_tooltip_move_time > 0.05:  # 50ms间隔
                self.last_tooltip_move_time = current_time
                self.show_tooltip(event, app_name)
    
    def close_application(self):
        """关闭应用程序"""
        # 记录程序退出日志
        self.logger.info("程序退出 - 开始清理资源并关闭应用程序")
        
        # 在应用关闭时，将当前应用的使用时间写入数据库
        if self.current_app_name and self.current_start_time:
            elapsed = time.time() - self.current_start_time
            if elapsed > 0:
                # 记录当前应用使用时间
                self.logger.info(f"记录最后应用使用时间 - 应用: {self.current_app_name}, 时长: {self.format_duration(elapsed)}")
                # 只记录屏幕使用时间（用于统计显示）
                self.record_screen_time(self.current_app_name, elapsed)
        
        # 将缓存中的时间流数据写入数据库
        if hasattr(self, 'time_stream_cache') and self.time_stream_cache:
            self.logger.info("程序退出 - 将缓存中的时间流数据写入数据库")
            self.flush_time_stream_cache()
        
        # 清理Canvas资源
        if hasattr(self, 'history_canvas'):
            self.history_canvas.delete("all")
        if hasattr(self, 'stats_canvas'):
            self.stats_canvas.delete("all")
        
        self.save_window_position()
        
        # 记录程序退出完成日志
        self.logger.info("程序退出完成 - 资源已清理，窗口位置已保存")
        
        self.root.destroy()
        
    def on_close_click(self, event=None):
        """关闭按钮点击事件"""
        self.close_application()
    
    def update_main_frame_bg(self):
        """更新主框架背景色"""
        current_color = self.color_caps_on if self.caps_lock_on else self.color_caps_off
        self.main_frame.configure(bg=current_color)
        self.label_container.configure(bg=current_color)
        self.inner_frame.configure(bg=current_color)
        self.caps_text_label.configure(bg=current_color)
        self.status_value_label.configure(bg=current_color)
        # 更新统计组件的背景色
        if hasattr(self, 'stats_container_frame') and self.stats_container_frame:
            self.stats_container_frame.configure(bg=current_color)
        if hasattr(self, 'stats_canvas') and self.stats_canvas:
            self.stats_canvas.configure(bg=current_color)
        if hasattr(self, 'stats_scrollbar') and self.stats_scrollbar:
            self.stats_scrollbar.configure(bg=current_color, troughcolor=current_color, activebackground=current_color)
        # 更新历史流背景色
        if hasattr(self, 'history_canvas') and self.history_canvas:
            self.history_canvas.configure(bg=current_color)
    
    def center_window(self):
        """将窗口居中显示在屏幕上"""
        # 获取当前窗口尺寸
        window_width = self.config.get("window_width", 300)
        window_height = self.config.get("window_height", 240)
        
        # 获取屏幕尺寸
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        
        # 计算中心位置
        center_x = int((screen_width - window_width) / 2)
        center_y = int((screen_height - window_height) / 2)
        
        # 设置窗口位置和尺寸
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
    
    def show_settings_window(self):
        """显示设置窗口"""
        if os.path.exists('config.txt'):
            os.startfile('config.txt')
    
    def apply_config(self):
        # 设置窗口大小和位置
        width = self.config.get("window_width", 300)
        height = self.config.get("window_height", 240)
        x = self.config.get("window_x", -1)
        y = self.config.get("window_y", -1)
        
        # 如果位置是有效坐标，则合并大小和位置
        if x != -1 and y != -1:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        else:
            # 设置大小并居中显示
            self.root.geometry(f"{width}x{height}")
            self.center_window()
        
        # 更新原始高度为配置中的高度
        self.original_height = height
        # 设置统计canvas的宽度为窗口宽度（如果存在）
        if hasattr(self, 'stats_canvas'):
            self.stats_canvas.configure(width=width)
        # 设置历史流canvas的宽度为窗口宽度（如果存在）
        if hasattr(self, 'history_canvas'):
            self.history_canvas.configure(width=width)
        # 更新历史流显示
        self.render_history_stream()
        
        # 应用颜色设置
        self.color_caps_on = self.config.get("color_caps_on", "#fa6666")
        self.color_caps_off = self.config.get("color_caps_off", "#4CAF50")
        self.color_titlebar = self.config.get("color_titlebar", "#2c3e50")
        
        # 更新标题栏颜色
        self.titlebar.configure(bg=self.color_titlebar)
        self.close_button.configure(bg=self.color_titlebar)
        
        # 更新历史流背景颜色
        if hasattr(self, 'history_canvas'):
            self.history_canvas.configure(bg=self.main_frame['bg'])
        
        # 更新主框架颜色
        self.update_main_frame_bg()
        
        # 设置窗口总是在最前
        self.root.wm_attributes("-topmost", self.config.get("always_on_top", 0))
        
        # 重新绘制统计图表以应用新颜色
        self.update_stats_window()
    
    def read_config(self):
        """读取配置文件"""
        # 默认配置
        self.config = {
            'color_caps_on': '#fa6666',
            'color_caps_off': '#4CAF50',
            'color_titlebar': '#2c3e50',
            'window_width': 300,
            'window_height': 240,
            'window_x': -1,
            'window_y': -1,
            'always_on_top': 1,
            'software_list': ['CAXA', 'CAD', 'SOLIDWORKS'],

            'grid_second_per_block': 30,

            'screen_time_refresh_frequency': 10  # 屏幕显示时间刷新率，单位：次/10秒
        }
        
        if os.path.exists('config.txt'):
            with open('config.txt', 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            # 使用字典映射处理配置键值，提高效率
            config_handlers = {
                'color_caps_on': str,
                'color_caps_off': str,
                'color_titlebar': str,
                'window_width': int,
                'window_height': int,
                'window_x': int,
                'window_y': int,
                'always_on_top': lambda v: v.strip().lower() in ['true', '1', 'yes', 'on'],
                'software_list': lambda v: [item.strip() for item in v.split(',') if item.strip()],

                'grid_second_per_block': int,

                'screen_time_refresh_frequency': int
            }
            
            # 初始化进程配置字典
            self.config['process_config'] = {}
            current_section = None  # 当前章节名（进程名）
            
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 检查是否为章节头 [process.exe]
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].strip()
                    # 初始化进程配置
                    if current_section not in self.config['process_config']:
                        self.config['process_config'][current_section] = {
                            'display_name': current_section,
                            'show_in_screen_time': True,
                            'show_in_time_stream': True
                        }
                    continue
                
                # 处理键值对
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # 处理注释
                    if ';' in value:
                        value = value.split(';')[0].strip()
                    
                    # 区分全局配置和进程配置
                    if current_section is None:
                        # 全局配置
                        if key in config_handlers:
                            self.config[key] = config_handlers[key](value)
                    else:
                        # 进程配置
                        if key == 'display_name':
                            self.config['process_config'][current_section]['display_name'] = value
                        elif key == 'show_in_screen_time':
                            self.config['process_config'][current_section]['show_in_screen_time'] = \
                                value.strip().lower() in ['true', '1', 'yes', 'on']
                        elif key == 'show_in_time_stream':
                            self.config['process_config'][current_section]['show_in_time_stream'] = \
                                value.strip().lower() in ['true', '1', 'yes', 'on']
            # 保持 backward compatibility - 将 software_list 转换为新格式
            if 'software_list' not in self.config:
                self.config['software_list'] = []
        else:
            # 配置文件不存在，生成默认配置文件
            self.write_config_file()
    
    def save_window_state(self):
        """保存窗口状态到配置"""
        try:
            # 获取当前窗口位置和大小
            self.config['window_x'] = self.root.winfo_x()
            self.config['window_y'] = self.root.winfo_y()
            self.config['window_width'] = self.root.winfo_width()
            self.config['window_height'] = self.original_height  # 保存原始高度，不包含统计部分
            
            # 写入配置文件
            self.write_config_file(save_size=True)
            self.logger.info("窗口状态已保存")
        except Exception as e:
            self.logger.error(f"保存窗口状态时出错: {e}")
    
    def write_config_file(self, save_size=True):
        """将配置写入文件"""
        # 确保配置字典包含所有必要的键
        # 添加时间流和刷新率配置的默认值
        self.config.setdefault('grid_second_per_block', 30)
        self.config.setdefault('screen_time_refresh_frequency', 10)
        
        with open('config.txt', 'w', encoding='utf-8') as f:
            f.write('# Caps Lock 检测器配置文件\n')
            f.write('# 颜色设置\n')
            f.write('# color_caps_on: Caps Lock开启时的指示灯颜色\n')
            f.write(f"color_caps_on = {self.config['color_caps_on']}\n")
            f.write('# color_caps_off: Caps Lock关闭时的指示灯颜色\n')
            f.write(f"color_caps_off = {self.config['color_caps_off']}\n")
            f.write('# color_titlebar: 窗口标题栏颜色\n')
            f.write(f"color_titlebar = {self.config['color_titlebar']}\n")
            f.write('\n# 窗口设置\n')
            f.write('# window_width: 窗口宽度(像素)\n')
            if save_size:
                f.write(f"window_width = {self.config['window_width']}\n")
            f.write('# window_height: 窗口高度(像素)\n')
            if save_size:
                f.write(f"window_height = {self.config['window_height']}\n")
            f.write('# window_x: 窗口X坐标位置(-1表示居中)\n')
            f.write(f"window_x = {self.config['window_x']}\n")
            f.write('# window_y: 窗口Y坐标位置(-1表示居中)\n')
            f.write(f"window_y = {self.config['window_y']}\n")
            f.write('\n# 其他设置\n')
            f.write('# always_on_top: 窗口是否始终置顶(true/false)\n')
            f.write(f"always_on_top = {'true' if self.config['always_on_top'] else 'false'}\n")
            f.write('# software_list: 自动切换为大写锁定的软件列表(逗号分隔)，其他软件自动切换为小写\n')
            f.write(f"software_list = {','.join(self.config['software_list'])}\n")  # 写入软件列表
            f.write('# grid_second_per_block: 时间流可视化中每个方块代表的秒数(默认30秒)\n')
            f.write(f"grid_second_per_block = {self.config.get('grid_second_per_block', 30)}\n")

            f.write('# screen_time_refresh_frequency: 屏幕时间刷新频率，单位次/10秒(默认10次)\n')
            f.write(f"screen_time_refresh_frequency = {self.config.get('screen_time_refresh_frequency', 10)}\n")  # 写入屏幕显示时间刷新率（单位：次/10秒）
            
            # 写入进程配置（新格式，带进程头）
            f.write('\n# 进程配置\n')
            f.write('# 格式：[进程名称]\n')
            f.write('# display_name: 在界面中显示的名称\n')
            f.write('# show_in_screen_time: 是否在屏幕时间统计中显示(true/false)\n')
            f.write('# show_in_time_stream: 是否在时间流中显示(true/false)\n')
            
            # 获取所有进程配置
            process_config = self.config.get('process_config', {})
            
            # 写入默认的进程配置（包括Windows Explorer、Google Chrome等常见应用）
            default_processes = {
                'explorer.exe': {
                    'display_name': 'Windows Explorer',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'chrome.exe': {
                    'display_name': 'Google Chrome',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'wechat.exe': {
                    'display_name': '微信',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'code.exe': {
                    'display_name': 'Visual Studio Code',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'QQ.exe': {
                    'display_name': 'QQ',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'CAXA.exe': {
                    'display_name': 'CAXA',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'edge.exe': {
                    'display_name': 'Microsoft Edge',
                    'show_in_screen_time': True,
                    'show_in_time_stream': True
                },
                'LockApp.exe': {
                    'display_name': 'Lock Screen',
                    'show_in_screen_time': False,
                    'show_in_time_stream': True
                }
            }
            
            # 合并默认配置和现有的配置
            all_processes = {**default_processes, **process_config}
            
            # 写入每个进程的配置
            for process_name, config in all_processes.items():
                f.write(f'\n[{process_name}]\n')
                f.write(f"display_name = {config.get('display_name', process_name)}\n")
                f.write(f"show_in_screen_time = {'true' if config.get('show_in_screen_time', True) else 'false'}\n")
                f.write(f"show_in_time_stream = {'true' if config.get('show_in_time_stream', True) else 'false'}\n")
        # 默认配置文件已生成

    def refresh_config(self):
        """刷新配置"""
        # 记录配置刷新日志
        self.logger.info("配置刷新 - 开始重新加载配置文件")
        
        # 保存旧配置的关键参数用于比较
        old_window_width = self.config.get("window_width", 300)
        old_window_height = self.config.get("window_height", 240)
        old_color_caps_on = self.config.get("color_caps_on", "#fa6666")
        old_color_caps_off = self.config.get("color_caps_off", "#4CAF50")
        old_always_on_top = self.config.get("always_on_top", 0)
        
        # 重新读取配置
        self.read_config()
        self.apply_config()
        
        # 比较并记录变化的关键配置
        changes = []
        if old_window_width != self.config.get("window_width", 300):
            changes.append(f"窗口宽度: {old_window_width} -> {self.config.get('window_width', 300)}")
        if old_window_height != self.config.get("window_height", 240):
            changes.append(f"窗口高度: {old_window_height} -> {self.config.get('window_height', 240)}")
        if old_color_caps_on != self.config.get("color_caps_on", "#fa6666"):
            changes.append(f"Caps开启颜色: {old_color_caps_on} -> {self.config.get('color_caps_on', '#fa6666')}")
        if old_color_caps_off != self.config.get("color_caps_off", "#4CAF50"):
            changes.append(f"Caps关闭颜色: {old_color_caps_off} -> {self.config.get('color_caps_off', '#4CAF50')}")
        if old_always_on_top != self.config.get("always_on_top", 0):
            changes.append(f"窗口置顶: {old_always_on_top} -> {self.config.get('always_on_top', 0)}")
        
        if changes:
            self.logger.info(f"配置已更新 - 变更项: {', '.join(changes)}")
        else:
            self.logger.info("配置已刷新 - 未检测到配置变更")
    
    def save_window_position(self):
        """保存当前窗口位置到配置文件（不保存尺寸，尺寸仅通过设置修改）"""
        # 获取当前窗口位置和大小
        geo = self.root.geometry()
        if 'x' in geo and '+' in geo:
            # 解析宽度、高度和位置
            size_part, pos_part = geo.split('+', 1)
            x, y = map(int, pos_part.split('+', 1))
            
            # 只更新位置信息，不更新尺寸
            self.config['window_x'] = x
            self.config['window_y'] = y
            
            # 使用write_config_file方法重新写入整个配置文件，保存窗口尺寸（配置文件中始终需要尺寸设置）
            self.write_config_file(save_size=True)
    
    def setup_logging(self):
        """设置日志系统"""
        # 创建logs文件夹
        log_folder = 'logs'
        if not os.path.exists(log_folder):
            os.makedirs(log_folder)
        
        # 生成日志文件名
        today = datetime.date.today()
        log_filename = os.path.join(log_folder, 'log_{}.txt'.format(today.strftime('%Y-%m-%d')))
        
        # 配置日志 - 将日志级别从DEBUG改为INFO，减少日志输出
        logging.basicConfig(
            filename=log_filename,
            level=logging.INFO,  # 改为INFO级别，减少DEBUG级别的日志
            format='%(asctime)s - %(levelname)s - %(message)s',
            encoding='utf-8'
        )
        
        # Initialize logger instance
        self.logger = logging.getLogger(__name__)
        
        # 记录程序启动日志
        self.logger.info("程序启动 - 屏幕时间追踪器初始化开始")
    
    def _create_titlebar_buttons(self):
        """创建标题栏按钮"""
        # 添加关闭按钮
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
        
        # 添加刷新按钮
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
        
        # 添加设置按钮
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
        
        # 添加统计按钮
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
        # 绑定窗口拖动事件（标题栏隐藏时可拖动整个窗口）
        self.root.bind("<ButtonPress-1>", self.on_window_drag_start)
        self.root.bind("<B1-Motion>", self.on_window_drag_motion)
        self.root.bind("<ButtonRelease-1>", self.on_drag_stop)
        
        # 绑定鼠标事件
        self.root.bind("<Motion>", self.on_mouse_motion)
        self.root.bind("<Enter>", self.on_mouse_enter)
        self.root.bind("<Leave>", self.on_mouse_leave)
        
        # 绑定ESC键关闭窗口
        self.root.bind("<Escape>", self.on_escape)
        
        # 绑定窗口关闭事件（点击X按钮时触发）
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_click)
    
    def _create_right_click_menu(self):
        """创建右键菜单"""
        self.right_click_menu = Menu(self.root, tearoff=False)
        self.right_click_menu.add_command(label="设置", command=self.show_settings_window)
        self.right_click_menu.add_command(label="刷新", command=self.refresh_config)
        self.right_click_menu.add_command(label="统计", command=self.show_stats_window)
        self.right_click_menu.add_separator()
        self.right_click_menu.add_command(label="关闭", command=self.on_menu_close)
        self.root.bind("<Button-3>", self.show_right_click_menu)
    
    def _init_color_and_rendering_vars(self):
        """初始化颜色和渲染优化变量"""
        # 统计条形图颜色调色板
        self.color_palette = [
            "#4a90e2", "#35bc59", "#f39c12", "#e74c3c", "#9b59b6",
            "#1abc9c", "#f1c40f", "#e67e22", "#3498db", "#95a5a6"
        ]

        # 应用到颜色的映射，确保时间流和统计面板颜色一致
        self.app_color_map = {}
        # 应用最后使用时间跟踪
        self.app_last_used = {}
        # 历史流渲染优化：保存上一次渲染的历史数据哈希值
        self.last_history_hash = None
        # 历史流点击事件绑定存储
        self.history_click_bindings = {}
        # 统计图表渲染优化：保存上一次渲染的统计数据哈希值
        self.last_stats_hash = None
        
        # 创建历史流条的高度常量（增加高度以支持方块堆叠）
        self.history_bar_h = 120
        self.history_gap = 0  # 移除间隙，确保与底部对齐
    
    def _create_ui_components(self):
        """创建UI组件"""
        # 创建容器帧放置状态文本
        self.label_container = tk.Frame(self.main_frame, bg=self.main_frame['bg'])
        self.label_container.pack(expand=True, fill=tk.BOTH)

        # 创建内部容器用于居中显示
        self.inner_frame = tk.Frame(self.label_container, bg=self.main_frame['bg'])
        self.inner_frame.pack(expand=True)

        # 添加固定文本标签（"Caps Lock"）
        self.caps_text_label = tk.Label(
            self.inner_frame,
            text="Caps Lock",
            font=("Arial", 22, "bold"),
            fg="white",
            bg=self.main_frame['bg']
        )
        self.caps_text_label.pack(side=tk.LEFT, padx=(0, 5))  # 固定文本右侧留5px间距

        # 添加动态状态标签（"ON"/"OFF"）
        self.status_value_label = tk.Label(
            self.inner_frame,
            text="ON" if self.caps_lock_on else "OFF",
            font=("Arial", 22, "bold"),
            fg="white",
            bg=self.main_frame['bg'],
        )
        self.status_value_label.pack(side=tk.LEFT)  # 动态状态标签紧随其后

        # 设置状态标签的固定宽度，确保ON/OFF切换时Caps Lock文本不动
        self.status_value_label.configure(width=4)  # 设置固定宽度为4个字符，足够容纳"OFF"
        
        # 创建历史流条canvas，放置在CPAS区域底部
        self.history_canvas = tk.Canvas(self.main_frame, bg=self.main_frame['bg'], height=self.history_bar_h, borderwidth=0, highlightthickness=0)
        self.history_canvas.pack(fill=tk.X, expand=False, side=tk.BOTTOM)
        
        # 创建tooltip用于显示应用名称
        self._create_tooltip()
        
        # 创建统计组件
        self._create_stats_components()
    
    def _create_tooltip(self):
        """创建tooltip组件"""
        self.tooltip = tk.Toplevel(self.root)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry("+0+0")
        # 确保tooltip始终显示在最上层，不会被其他窗口遮盖
        self.tooltip.wm_attributes("-topmost", True)
        # 设置tooltip为工具窗口，减少任务栏显示
        self.tooltip.wm_attributes("-toolwindow", True)
        # 设置tooltip的透明度和背景
        self.tooltip.attributes("-alpha", 0.95)
        self.tooltip_label = tk.Label(self.tooltip, text="", bg="#333333", fg="white", font=("Arial", 10), padx=5, pady=2)
        self.tooltip_label.pack()
        self.tooltip.withdraw()
        self.current_tooltip_app = None
    
    def _create_stats_components(self):
        """创建统计组件"""
        # 创建统计显示/隐藏开关变量（用于内部逻辑）
        self.stats_toggle_var = tk.BooleanVar(value=False)
        
        # 创建统计组件容器
        self.stats_container_frame = tk.Frame(self.main_frame, bg=self.main_frame['bg'])
        # 初始隐藏统计组件
        self.stats_container_frame.pack(expand=False, fill=tk.BOTH)
        self.stats_container_frame.pack_forget()

        # 创建垂直滚动条
        self.stats_scrollbar = tk.Scrollbar(self.stats_container_frame, orient=tk.VERTICAL, bg="white", troughcolor="white", activebackground="white", borderwidth=0, highlightthickness=0)

        # 创建统计canvas
        self.stats_canvas = tk.Canvas(self.stats_container_frame, bg="white", height=160, yscrollcommand=self.stats_scrollbar.set, borderwidth=0, highlightthickness=0)
        self.stats_canvas.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(0, 3))  # 为滚动条预留空间

        # 配置滚动条
        self.stats_scrollbar.config(command=self.stats_canvas.yview)
        self.stats_scrollbar.place(relx=1, rely=0, relheight=1, x=-3, anchor='ne')  # 向左移动3像素

        # 为Canvas添加右侧内边距，避免内容被滚动条遮挡
        self.stats_canvas.configure(scrollregion=self.stats_canvas.bbox("all"), borderwidth=0, highlightthickness=0)

        # 绑定鼠标滚轮事件
        self.stats_canvas.bind_all("<MouseWheel>", lambda event: self.stats_canvas.yview_scroll(int(-1*(event.delta/120)), "units"))
    
    def check_window_height_change(self):
        """检测窗口高度变化，如果从window_height变大则启动30秒倒计时"""
        current_height = self.root.winfo_height()
        window_height = self.config.get('window_height', 240)
        
        # 检测窗口高度是否从window_height变大
        if current_height > window_height and self.last_window_height <= window_height:
            # 检测到高度变化，启动30秒倒计时
            self.start_auto_restore_timer()
            self.height_change_detected = True
        
        # 更新上次记录的窗口高度
        self.last_window_height = current_height
    
    def start_auto_restore_timer(self):
        """启动30秒倒计时，倒计时结束后自动恢复窗口高度"""
        # 取消之前的倒计时
        if self.auto_restore_timer:
            self.root.after_cancel(self.auto_restore_timer)
            self.auto_restore_timer = None
        
        # 启动新的30秒倒计时
        self.auto_restore_timer = self.root.after(30000, self.auto_restore_window)
    
    def auto_restore_window(self):
        """自动恢复窗口高度，清理直方图内存"""
        # 清理直方图内存
        self.cleanup_chart_memory()
        
        # 调用统计按钮的回调函数，模拟点击统计按钮的效果
        self.show_stats_window()
        
        # 重置状态
        self.height_change_detected = False
        self.auto_restore_timer = None
    
    def cleanup_chart_memory(self):
        """增强的内存清理方法"""
        # 清理Canvas事件绑定
        for canvas in [self.stats_canvas, self.history_canvas]:
            if canvas:
                # 解绑所有事件
                for tag in list(self.bound_canvas_tags):
                    for event in ["<Enter>", "<Leave>", "<Motion>", "<Button-1>", "<Button-3>"]:
                        try:
                            canvas.tag_unbind(tag, event)
                        except:
                            pass
                # 删除所有项
                canvas.delete("all")
        
        # 清空绑定记录
        self.bound_canvas_tags.clear()
        
        # 限制缓存大小
        for app_name in list(self.time_stream_cache.keys()):
            if app_name in self.time_stream_cache:
                cache = self.time_stream_cache[app_name]
                if isinstance(cache, deque) and len(cache) > 50:
                    # 创建新的更小的deque
                    self.time_stream_cache[app_name] = deque(list(cache)[-50:], maxlen=100)
                elif isinstance(cache, list) and len(cache) > 50:
                    # 转换为deque并限制大小
                    self.time_stream_cache[app_name] = deque(cache[-50:], maxlen=100)
        
        # 清理历史点击绑定
        if hasattr(self, 'history_click_bindings'):
            self.history_click_bindings.clear()
        
        # 强制垃圾回收
        gc.collect()
        
        # 重置哈希值
        self.last_stats_hash = None
        self.last_history_hash = None
    
    def cleanup_screen_time_memory(self):
        """清理屏幕时间相关内存"""
        # 清理时间流缓存
        if hasattr(self, 'time_stream_cache'):
            self.time_stream_cache.clear()
        
        # 重置历史流的哈希值，强制重新渲染
        if hasattr(self, 'last_history_hash'):
            self.last_history_hash = None
        
        # 清理历史流画布
        if hasattr(self, 'history_canvas') and self.history_canvas:
            self.history_canvas.delete("all")
    


if __name__ == "__main__":
    root = tk.Tk()
    app = CapsLockChecker(root)
    root.mainloop()
