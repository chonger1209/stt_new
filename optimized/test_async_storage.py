import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing

from storage import HistoryStore, AsyncHistoryStore
from tracking import UsageTracker


class AsyncStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.threads = []
        def factory(directory):
            self.threads.append(threading.get_ident())
            store = HistoryStore(directory)
            store.connection.execute('PRAGMA busy_timeout=50')
            return store
        self.store = AsyncHistoryStore(self.directory, retry_seconds=.03, factory=factory)
        self.failures = []
        self.wait(lambda: self.store.ready and self.store.read_queries == 2)
        self.day = dt.date.today().isoformat()
        self.clock = [dt.datetime.combine(dt.date.today(), dt.time(12)).timestamp()]
        self.tracker = UsageTracker(self.store, clock=lambda: self.clock[0])
        self.tracker.switch('A')

    def wait(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.failures.extend(self.store.poll())
            if condition():
                return
            time.sleep(.005)
        self.fail('asynchronous operation timed out')

    def tearDown(self):
        self.wait(lambda: self.store.pending_writes == 0)
        self.store.close()
        self.store._thread.join(1)
        self.temp.cleanup()

    def rows(self, name, table):
        with closing(sqlite3.connect(str(self.directory / name))) as connection:
            return connection.execute('SELECT COUNT(*), COALESCE(SUM(duration),0) FROM ' + table).fetchone()

    def test_commit_ack_keeps_snapshot_exact_and_clears_only_batch_prefix(self):
        self.clock[0] += 10
        self.tracker.capture()
        self.tracker.flush()
        self.assertEqual(len(self.tracker.pending), 1)
        self.clock[0] += 5
        self.tracker.capture()
        self.assertEqual(self.tracker.snapshot()[0], {'A': 15})
        self.wait(lambda: not self.tracker.pending)
        self.assertEqual(self.tracker.snapshot()[0], {'A': 15})
        self.assertEqual(self.rows('screen_time_history.db', 'screen_time'), (2, 15))
        self.assertEqual(self.rows('time_stream_history.db', 'time_stream'), (2, 15))
        self.assertNotEqual(self.threads[0], threading.get_ident())

    def test_locked_db_does_not_block_ui_and_both_writes_retry_once(self):
        blocker = sqlite3.connect(str(self.directory / 'screen_time_history.db'))
        blocker.execute('BEGIN IMMEDIATE')
        try:
            self.clock[0] += 10
            self.tracker.capture()
            start = time.perf_counter()
            self.tracker.flush()
            self.store.submit_pixels(tuple(self.tracker.pending), lambda rows: None)
            self.assertLess(time.perf_counter() - start, .1)
            self.wait(lambda: bool(self.failures))
            self.assertEqual(len(self.tracker.pending), 1)
            self.assertEqual(self.tracker.snapshot()[0], {'A': 10})
            with self.assertRaises(RuntimeError):
                self.store.close()
        finally:
            blocker.rollback()
            blocker.close()
        self.wait(lambda: self.store.pending_writes == 0)
        self.assertEqual(self.rows('screen_time_history.db', 'screen_time'), (1, 10))
        self.assertEqual(self.rows('time_stream_history.db', 'time_stream'), (1, 10))
        self.assertEqual(self.rows('time_stream_history.db', 'logged_pixels'), (1, 10))

    def test_failed_second_database_rolls_back_first_and_retains_pending(self):
        connection = sqlite3.connect(str(self.directory / 'time_stream_history.db'))
        connection.execute("CREATE TRIGGER fail BEFORE INSERT ON time_stream BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        connection.commit()
        try:
            self.clock[0] += 7
            self.tracker.close()
            self.wait(lambda: bool(self.failures))
            self.assertEqual(len(self.tracker.pending), 1)
            self.assertEqual(self.rows('screen_time_history.db', 'screen_time'), (0, 0))
        finally:
            connection.execute('DROP TRIGGER fail')
            connection.commit()
            connection.close()
        self.wait(lambda: not self.tracker.pending)
        self.assertEqual(self.rows('screen_time_history.db', 'screen_time'), (1, 7))

    def test_cross_day_read_and_write_ack_order_never_double_counts(self):
        tomorrow = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time()).timestamp()
        self.tracker.start = tomorrow - 10
        self.tracker._start_elapsed = tomorrow - 10
        self.clock[0] = tomorrow + 5
        self.tracker.capture()
        self.tracker.flush()
        new_day = dt.datetime.fromtimestamp(tomorrow).date().isoformat()
        self.tracker.snapshot()  # Queue new-day read behind the write.
        self.wait(lambda: not self.tracker.pending and new_day in self.store._cache)
        self.assertEqual(self.tracker.snapshot()[0], {'A': 5})
        self.assertEqual(self.rows('screen_time_history.db', 'screen_time'), (2, 15))

    def test_snapshot_poll_does_not_query_database_repeatedly(self):
        count = self.store.read_queries
        for _ in range(100):
            self.tracker.snapshot()
            self.store.poll()
        self.assertEqual(count, self.store.read_queries)


if __name__ == '__main__':
    unittest.main()
