"""Exercise the standalone GUI and asynchronous exit with isolated synthetic data."""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk

from app import CapsLockChecker


def run():
    result = {}
    with tempfile.TemporaryDirectory(prefix="stt_standard_") as folder:
        directory = Path(folder)
        root = tk.Tk()
        root.withdraw()
        root.attributes("-alpha", 0.0)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(str(args))
        app = CapsLockChecker(root, directory, monitor=False)

        def pump(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError("GUI operation timed out")

        try:
            pump(lambda: app.store.ready)
            root.deiconify()
            root.update()
            assert not any(name.startswith("qwen") for name in sys.modules)
            assert not any(
                "qwen" in thread.name.lower() for thread in threading.enumerate()
            )
            config = (directory / "config.txt").read_text(encoding="utf-8")
            assert "qwen_" not in config and "千问" not in config
            assert not hasattr(app, "qwen_controller")
            menu = [
                app.right_click_menu.entrycget(i, "label")
                for i in range(app.right_click_menu.index("end") + 1)
                if app.right_click_menu.type(i) != "separator"
            ]
            assert menu == ["设置", "刷新", "统计", "关闭"], menu
            number_center = (
                app.timer_label.winfo_rootx() + app.timer_label.winfo_width() / 2
            )
            button_center = (
                app.timer_buttons_frame.winfo_rootx()
                + app.timer_buttons_frame.winfo_width() / 2
            )
            window_center = root.winfo_rootx() + root.winfo_width() / 2
            assert abs(number_center - button_center) <= 1
            assert abs(number_center - window_center) <= 2
            result["standalone_ui"] = (
                "centered timer; no model widgets, modules or workers"
            )
            app.show_titlebar()
            root.update()
            assert (
                app.timer_label.winfo_rooty()
                >= app.titlebar.winfo_rooty() + app.titlebar.winfo_height()
            )
            app.render_history_stream(
                history=[(str(i), f"App{i % 7}", 5) for i in range(720)]
            )
            # The test explicitly chooses one minute per cell.
            app.grid_second_per_block = 60
            app.render_history_stream(
                history=[(str(i), f"App{i % 7}", 5) for i in range(720)]
            )
            assert len(app.history_canvas.find_all()) == 60
            result["timeline"] = "one hour with five-second switches produces 60 cells"
            app.tracker.switch("Synthetic app")
            app.tracker.start -= 61
            app.tracker._start_elapsed -= 61
            app.tracker.capture()
            app.show_stats_window()
            root.update()
            app.render_stats_chart()
            items = app.stats_canvas.find_all()
            assert items
            before = app.store.read_queries
            for _ in range(100):
                app.render_stats_chart()
            assert app.stats_canvas.find_all() == items
            assert app.store.read_queries == before
            result["stats_cache"] = "100 renders reuse items and make no extra SELECT"
            app.start_timer()
            app.countdown.deadline -= 10
            app.pause_timer()
            remaining = app.countdown.remaining
            app.refresh_config()
            assert abs(app.countdown.remaining - remaining) <= 1
            app.start_timer()
            assert abs(app.countdown.remaining - remaining) <= 1
            app.reset_timer()
            assert app.countdown.remaining == app.timer_total_seconds
            result["countdown"] = "pause, resume, refresh and reset preserve semantics"
            pump(lambda: app.store.pending_writes == 0)
            blocker = sqlite3.connect(directory / "screen_time_history.db", timeout=0.1)
            blocker.execute("BEGIN IMMEDIATE")
            try:
                app.tracker.switch("Pending exit")
                app.tracker.start -= 2
                app.tracker._start_elapsed -= 2
                app.close_application()
                heartbeat = []
                root.after(30, lambda: heartbeat.append(True))
                pump(lambda: bool(heartbeat))
                assert not app._closing
                assert app.tracker.pending
            finally:
                blocker.rollback()
                blocker.close()
            pump(lambda: app._closing)
            assert not errors, errors
            result["async_exit"] = (
                "locked database retains pending data without blocking Tk; exit succeeds after unlock"
            )
            result["callback_errors"] = errors
            result["result"] = "passed"
        finally:
            if not app._closing:
                app.close_application()
                pump(lambda: app._closing)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if "--smoke-output" in sys.argv:
        Path(sys.argv[sys.argv.index("--smoke-output") + 1]).write_text(
            output, encoding="utf-8"
        )
    elif sys.stdout:
        print(output)


if __name__ == "__main__":
    run()
