import datetime as dt
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from settings import CONFIG_MARKER, DEFAULTS, read_settings, save_position
from storage import HistoryStore
from tracking import Countdown, UsageTracker, split_interval, timeline_blocks


class Clock:
    def __init__(self, now=1000):
        self.now = now

    def __call__(self):
        return self.now


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = HistoryStore(self.directory)
        self.clock = Clock(dt.datetime(2026, 10, 10, 12).timestamp())
        self.tracker = UsageTracker(self.store, self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_switch_attributes_each_interval(self):
        self.tracker.switch('A')
        self.clock.now += 20
        self.tracker.switch('B')
        self.clock.now += 10
        self.tracker.capture()
        totals, history = self.tracker.snapshot()
        self.assertEqual(totals, {'A': 20, 'B': 10})
        self.assertEqual([row[1:] for row in history], [('A', 20), ('B', 10)])

    def test_live_tail_never_duplicates_captured_time(self):
        self.tracker.switch('A')
        self.clock.now += 30
        self.tracker.capture()
        self.clock.now += 10
        totals, history = self.tracker.snapshot()
        self.assertEqual(totals['A'], 40)
        self.assertEqual(sum(row[2] for row in history), 40)
        self.tracker.flush()
        self.assertEqual(self.tracker.snapshot(), (totals, history))

    def test_exit_captures_unsampled_tail_and_restart_does_not_reinsert(self):
        self.tracker.switch('A')
        self.clock.now += 17
        self.tracker.close()
        restarted = UsageTracker(self.store, self.clock)
        restarted.flush()
        self.assertEqual(restarted.snapshot()[0], {'A': 17})
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM stream.time_stream').fetchone()[0], 1)

    def test_cross_midnight_splits_both_databases(self):
        self.clock.now = dt.datetime(2026, 10, 10, 23, 50).timestamp()
        self.tracker.switch('A')
        self.clock.now += 1200
        self.tracker.close()
        self.assertEqual(self.store.totals('2026-10-10'), {'A': 600})
        self.assertEqual(self.store.totals('2026-10-11'), {'A': 600})
        self.assertEqual(sum(row[2] for row in self.store.history('2026-10-10')), 600)
        self.assertEqual(self.tracker.snapshot()[0], {'A': 600})

    def test_subsecond_switches_are_not_deduplicated_by_timestamp(self):
        self.tracker.switch('A')
        self.clock.now += 0.2
        self.tracker.switch('B')
        self.clock.now += 0.2
        self.tracker.close()
        rows = self.store.history('2026-10-10')
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(row[2] for row in rows), 0.4, places=5)

    def test_same_app_window_switch_does_not_fragment_records(self):
        self.tracker.switch('A')
        self.clock.now += 10
        self.tracker.switch('A')
        self.clock.now += 10
        self.tracker.capture()
        self.assertEqual(len(self.tracker.pending), 1)
        self.assertEqual(self.tracker.pending[0][2], 20)

    def test_failed_flush_rolls_back_both_databases_and_keeps_pending(self):
        self.tracker.switch('A')
        self.clock.now += 5
        self.tracker.capture()
        self.store.connection.execute("CREATE TRIGGER stream.fail BEFORE INSERT ON time_stream BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tracker.flush()
        self.assertEqual(len(self.tracker.pending), 1)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM screen_time').fetchone()[0], 0)
        self.store.connection.execute('DROP TRIGGER stream.fail')
        self.tracker.flush()
        self.assertFalse(self.tracker.pending)
        self.assertEqual(self.tracker.snapshot()[0], {'A': 5})

    def test_repeated_snapshot_queries_are_cached(self):
        queries = []
        self.store.connection.set_trace_callback(queries.append)
        for _ in range(100):
            self.tracker.snapshot()
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 2)

    def test_mixed_switch_sample_flush_sequence_conserves_time(self):
        rng = random.Random(42)
        expected = {}
        self.tracker.switch('A')
        for _ in range(1000):
            duration = rng.randrange(1, 10)
            expected[self.tracker.app] = expected.get(self.tracker.app, 0) + duration
            self.clock.now += duration
            action = rng.randrange(3)
            if action == 0:
                self.tracker.switch(rng.choice(['A', 'B', 'C']))
            elif action == 1:
                self.tracker.capture()
            else:
                self.tracker.flush()
        self.tracker.close()
        totals, history = self.tracker.snapshot()
        self.assertEqual(totals, expected)
        self.assertEqual(sum(row[2] for row in history), sum(expected.values()))

    def test_backward_wall_clock_does_not_create_negative_duration(self):
        self.tracker.switch('A')
        self.clock.now -= 100
        self.tracker.capture()
        self.assertEqual(self.tracker.pending, [])
        self.clock.now += 10
        self.assertEqual(self.tracker.snapshot()[0], {'A': 10})

    def test_daily_range_query_uses_index(self):
        plan = self.store.connection.execute("EXPLAIN QUERY PLAN SELECT * FROM screen_time WHERE timestamp >= ? AND timestamp < ?", ('2026-10-10', '2026-10-11')).fetchall()
        self.assertIn('INDEX', str(plan))

    def test_adjacent_samples_share_pixels(self):
        blocks = timeline_blocks([('1', 'A', 10), ('2', 'A', 20), ('3', 'B', 5)], 30, lambda app: True)
        self.assertEqual(blocks, [('A', 1)])


class TimelineBlockTests(unittest.TestCase):
    def blocks(self, durations, interval=60, visible=lambda app: True):
        history = [(str(index), app, duration) for index, (app, duration) in enumerate(durations)]
        return timeline_blocks(history, interval, visible)

    def test_partial_block_does_not_draw(self):
        self.assertEqual(self.blocks([('A', 59.999999)]), [])
        self.assertEqual(self.blocks([('A', 60)]), [('A', 1)])

    def test_color_uses_the_app_closing_the_block(self):
        self.assertEqual(self.blocks([('A', 50), ('B', 10)]), [('B', 1)])
        self.assertEqual(self.blocks([('A', 60), ('B', 1)]), [('A', 1)])

    def test_remainder_carries_across_app_switches(self):
        self.assertEqual(self.blocks([('A', 40), ('B', 40), ('C', 40)]), [('B', 1), ('C', 1)])

    def test_one_hour_of_five_second_switches_has_sixty_blocks(self):
        values = [(f'App{index % 12}', 5) for index in range(720)]
        blocks = self.blocks(values)
        self.assertEqual(sum(count for _, count in blocks), 60)
        self.assertEqual(blocks, [('App11', 60)])

    def test_one_hour_of_subsecond_switches_has_sixty_blocks(self):
        values = [(f'App{index % 7}', 0.2) for index in range(18000)]
        self.assertEqual(sum(count for _, count in self.blocks(values)), 60)

    def test_long_segment_can_close_multiple_blocks(self):
        self.assertEqual(self.blocks([('A', 20), ('B', 170)]), [('B', 3)])

    def test_sampling_splits_do_not_change_count_or_color(self):
        whole = [('A', 70), ('B', 70)]
        samples = [('A', 30), ('A', 30), ('A', 10), ('B', 10), ('B', 30), ('B', 30)]
        self.assertEqual(self.blocks(whole), self.blocks(samples))

    def test_hidden_segments_do_not_produce_visible_blocks(self):
        values = [('A', 30), ('Hidden', 3600), ('B', 30)]
        self.assertEqual(self.blocks(values, visible=lambda app: app != 'Hidden'), [('B', 1)])

    def test_interval_change_recomputes_from_history(self):
        values = [('A', 30), ('B', 30)]
        self.assertEqual(self.blocks(values, 30), [('A', 1), ('B', 1)])
        self.assertEqual(self.blocks(values, 60), [('B', 1)])

    def test_zero_and_negative_durations_do_not_add_blocks(self):
        self.assertEqual(self.blocks([('A', 0), ('B', -10), ('C', 60)]), [('C', 1)])

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            self.blocks([('A', 60)], 0)

    def test_random_segments_conserve_complete_block_count(self):
        rng = random.Random(72)
        values = [(f'App{index % 9}', rng.randrange(1, 10000) / 1000) for index in range(2000)]
        expected = sum(round(duration * 1_000_000) for _, duration in values) // 60_000_000
        self.assertEqual(sum(count for _, count in self.blocks(values)), expected)


class CountdownTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer = Countdown(600, self.clock)

    def test_custom_duration_and_initial_second(self):
        self.timer.start()
        self.assertEqual(self.timer.remaining, 600)

    def test_pause_resume_preserves_fractional_remaining_time(self):
        self.timer.start()
        self.clock.now += 125.5
        self.timer.pause()
        self.clock.now += 999
        self.assertEqual(self.timer.remaining, 475)
        self.timer.start()
        self.clock.now += 0.5
        self.assertEqual(self.timer.remaining, 474)

    def test_delayed_ui_callback_does_not_slow_timer(self):
        self.timer.start()
        self.clock.now += 32
        self.assertEqual(self.timer.remaining, 568)

    def test_completion_and_restart(self):
        self.timer.start()
        self.clock.now += 700
        self.assertEqual(self.timer.remaining, 0)
        self.timer.pause()
        self.timer.start()
        self.assertEqual(self.timer.remaining, 600)

    def test_config_update_preserves_running_session_and_reset_uses_new_duration(self):
        self.timer.start()
        self.clock.now += 30
        self.timer.configure(1200)
        self.assertEqual(self.timer.remaining, 570)
        self.timer.reset()
        self.assertEqual(self.timer.remaining, 1200)

    def test_idle_config_update(self):
        self.timer.configure(900)
        self.assertEqual(self.timer.remaining, 900)


class SettingsTests(unittest.TestCase):
    def test_first_start_creates_documented_defaults_and_lock_screen_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            config = read_settings(path)
            text = path.read_text(encoding='utf-8')
            self.assertIn('没有配置的程序也会正常记录', text)
            self.assertIn('false 只隐藏对应视图，不停止记录', text)
            self.assertIn('修改并保存后', text)
            for key, value in DEFAULTS.items():
                self.assertEqual(config[key], value)
                self.assertIn(f'# {key}:', text)
            lock = config['process_config']['LockApp.exe']
            self.assertEqual(lock['display_name'], 'Lock Screen')
            self.assertFalse(lock['show_in_screen_time'])
            self.assertTrue(lock['show_in_time_stream'])
            self.assertEqual(config['process_config']['Taskmgr.exe']['display_name'], '任务管理器')

    def test_upgrade_minimal_config_keeps_values_and_backs_up_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            original = ('# 自己的备注\nwindow_x = 4271\nwindow_y = 353\n'
                        'timer_duration_minutes = 40\ncolor_caps_on = #abcdef\n'
                        'custom_setting = keep\n# 进程配置\n').encode('utf-8')
            path.write_bytes(original)
            config = read_settings(path)
            self.assertEqual(config['window_x'], 4271)
            self.assertEqual(config['window_y'], 353)
            self.assertEqual(config['timer_duration_minutes'], 40)
            self.assertEqual(config['color_caps_on'], '#abcdef')
            self.assertFalse(config['process_config']['LockApp.exe']['show_in_screen_time'])
            self.assertIn('# 自己的备注', path.read_text(encoding='utf-8'))
            self.assertIn('custom_setting = keep', path.read_text(encoding='utf-8'))
            self.assertEqual(path.with_name(path.name + '.before_config_upgrade.bak').read_bytes(), original)

    def test_existing_process_choices_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            path.write_text('[LockApp.exe]\ndisplay_name = 自定义锁屏\n'
                            'show_in_screen_time = true\nshow_in_time_stream = false\n'
                            '[my_app.exe]\ndisplay_name = 自己的软件\n', encoding='utf-8')
            config = read_settings(path)
            self.assertEqual(config['process_config']['LockApp.exe'], {
                'display_name': '自定义锁屏', 'show_in_screen_time': True, 'show_in_time_stream': False})
            self.assertEqual(config['process_config']['my_app.exe']['display_name'], '自己的软件')

    def test_repeated_start_keeps_file_unchanged_and_respects_deleted_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            read_settings(path)
            original = path.read_bytes()
            read_settings(path)
            self.assertEqual(path.read_bytes(), original)
            text = path.read_text(encoding='utf-8').split('[explorer.exe]', 1)[0]
            self.assertIn(CONFIG_MARKER, text)
            path.write_text(text, encoding='utf-8')
            edited = path.read_bytes()
            self.assertEqual(read_settings(path)['process_config'], {})
            self.assertEqual(path.read_bytes(), edited)

    def test_save_only_changes_coordinates_and_preserves_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            path.write_bytes(b'# Keep comment\r\nwindow_x = 100 ; left\r\nwindow_y = 200\r\nwindow_width = 333\r\ncustom = keep\r\n[app.exe]\r\ndisplay_name = App\r\nshow_in_time_stream = false\r\n')
            before = read_settings(path)
            save_position(path, -100, 300)
            after = read_settings(path)
            before.update(window_x=-100, window_y=300)
            self.assertEqual(after, before)
            self.assertIn('# Keep comment', path.read_text(encoding='utf-8'))
            self.assertIn('custom = keep', path.read_text(encoding='utf-8'))
            self.assertIn('; left', path.read_text(encoding='utf-8'))

    def test_invalid_numeric_config_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.txt'
            path.write_text('grid_second_per_block = 0\nscreen_time_refresh_frequency = nope\ntimer_duration_minutes = -3\n')
            config = read_settings(path)
            self.assertEqual(config['grid_second_per_block'], 30)
            self.assertEqual(config['screen_time_refresh_frequency'], 10)
            self.assertEqual(config['timer_duration_minutes'], 25)


if __name__ == '__main__':
    unittest.main(verbosity=2)
