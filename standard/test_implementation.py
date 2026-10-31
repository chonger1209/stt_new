import datetime as dt
from contextlib import closing
import hashlib
import importlib.util
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from async_logging import AsyncFileLog
from storage import AsyncHistoryStore, HistoryStore
from tracking import UsageTracker, timeline_blocks


class QueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = AsyncHistoryStore(self.directory, retry_seconds=0.02)
        self.wait(lambda: self.store.ready and self.store.read_queries == 2)
        self.stamp = dt.datetime.now().isoformat(sep=" ", timespec="microseconds")

    def tearDown(self):
        self.store.close()
        self.assertTrue(self.store.stopped.wait(3))
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.store.poll()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("worker did not reach expected state")

    def test_auxiliary_failure_cannot_block_ordered_history_and_recovers_once(self):
        with closing(sqlite3.connect(self.directory / "time_stream_history.db")) as c:
            c.execute(
                "CREATE TRIGGER fail_aux BEFORE INSERT ON logged_pixels BEGIN SELECT RAISE(ABORT, 'aux only'); END"
            )
            c.commit()
        aux_callbacks, critical_callbacks = [], []
        self.store.submit_pixels(
            [(self.stamp, "Aux A", 1)], lambda _: aux_callbacks.append("A")
        )
        self.store.submit_pixels(
            [(self.stamp, "Aux B", 2)], lambda _: aux_callbacks.append("B")
        )
        self.wait(lambda: self.store.failure_since is not None)
        failed_since = self.store.failure_since
        self.store.submit_intervals(
            [(self.stamp, "A", 10)], lambda _: critical_callbacks.append("A")
        )
        self.store.submit_intervals(
            [(self.stamp, "B", 20)], lambda _: critical_callbacks.append("B")
        )
        self.wait(lambda: critical_callbacks == ["A", "B"])
        self.assertEqual(aux_callbacks, [])
        self.assertEqual(self.store.pending_writes, 2)
        self.assertEqual(self.store.failure_since, failed_since)
        with closing(sqlite3.connect(self.directory / "screen_time_history.db")) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*),SUM(duration) FROM screen_time").fetchone(),
                (2, 30),
            )
        with closing(sqlite3.connect(self.directory / "time_stream_history.db")) as c:
            c.execute("DROP TRIGGER fail_aux")
            c.commit()
        self.wait(lambda: self.store.pending_writes == 0)
        self.assertEqual(aux_callbacks, ["A", "B"])
        self.assertIsNone(self.store.failure_since)
        with closing(sqlite3.connect(self.directory / "time_stream_history.db")) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*),SUM(duration) FROM time_stream").fetchone(),
                (2, 30),
            )
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*),SUM(duration) FROM logged_pixels"
                ).fetchone(),
                (2, 3),
            )

    def test_bad_rows_leave_no_pending_acknowledgement(self):
        with self.assertRaises(TypeError):
            self.store.submit_intervals(None, lambda _: None)
        self.assertEqual(self.store.pending_writes, 0)
        self.assertTrue(self.store._jobs.empty())

    def test_failed_enqueue_restores_counter(self):
        with patch.object(
            self.store._jobs, "put", side_effect=RuntimeError("queue failure")
        ):
            with self.assertRaises(RuntimeError):
                self.store.submit_intervals([], lambda _: None)
        self.assertEqual(self.store.pending_writes, 0)


class AsyncLogTests(unittest.TestCase):
    def test_slow_sink_does_not_block_producer_and_drains_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.getLogger("async-log-test-" + directory)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            pipe = AsyncFileLog(logger, Path(directory) / "log.txt")
            entered, release = threading.Event(), threading.Event()
            emit_threads = []
            original = pipe.handler.emit

            def slow(record):
                emit_threads.append(threading.get_ident())
                entered.set()
                release.wait(3)
                original(record)

            try:
                with patch.object(pipe.handler, "emit", side_effect=slow):
                    logger.info("first")
                    self.assertTrue(entered.wait(2))
                    start = time.perf_counter()
                    logger.info("second")
                    pipe.begin_close()
                    self.assertLess(time.perf_counter() - start, 0.1)
                    self.assertFalse(pipe.stopped.is_set())
                    release.set()
                    self.assertTrue(pipe.close())
                self.assertTrue(all(t != threading.get_ident() for t in emit_threads))
                text = (Path(directory) / "log.txt").read_text(encoding="utf-8")
                self.assertIn("first", text)
                self.assertIn("second", text)
            finally:
                release.set()
                pipe.close()

    def test_bounded_queue_reports_skipped_text_without_growing_unbounded(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.getLogger("bounded-log-test-" + directory)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            pipe = AsyncFileLog(logger, Path(directory) / "log.txt", capacity=2)
            entered, release = threading.Event(), threading.Event()
            original = pipe.handler.emit

            def slow(record):
                entered.set()
                release.wait(3)
                original(record)

            try:
                with patch.object(pipe.handler, "emit", side_effect=slow):
                    logger.info("first")
                    self.assertTrue(entered.wait(2))
                    for i in range(30):
                        logger.info("message %s", i)
                    self.assertLessEqual(pipe.records.qsize(), 2)
                    self.assertGreater(pipe.queue_handler.dropped, 0)
                    release.set()
                    self.assertTrue(pipe.close())
                self.assertIn(
                    "统计记录不受影响",
                    (Path(directory) / "log.txt").read_text(encoding="utf-8"),
                )
            finally:
                release.set()
                pipe.close()


class CachedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.temp.name))
        self.clock = [dt.datetime(2026, 10, 31, 12).timestamp()]
        self.tracker = UsageTracker(self.store, lambda: self.clock[0])
        self.tracker.switch("A")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_stable_history_loaded_once_while_live_tail_advances(self):
        self.clock[0] += 40
        self.tracker.capture()
        self.tracker.flush()
        with patch.object(self.store, "history", wraps=self.store.history) as history:
            for _ in range(100):
                self.clock[0] += 1
                totals, rows = self.tracker.snapshot()
                self.assertEqual(sum(r[2] for r in rows), totals["A"])
            self.assertEqual(history.call_count, 1)

    def test_totals_refresh_does_not_copy_history(self):
        with patch.object(
            self.store,
            "history",
            side_effect=AssertionError("unnecessary history copy"),
        ):
            self.clock[0] += 10
            self.assertEqual(self.tracker.totals_snapshot(), {"A": 10})

    def test_cached_timeline_matches_full_history_through_capture_commit_and_rule_change(
        self,
    ):
        visible = lambda app: app != "Hidden"
        for i in range(30):
            self.clock[0] += 7
            self.tracker.switch(["A", "B", "Hidden"][i % 3])
            if i % 4 == 0:
                self.tracker.capture()
            if i % 5 == 0:
                self.tracker.flush()
            self.clock[0] += 3
            for interval in (30, 60):
                actual = self.tracker.timeline_snapshot(
                    interval, visible, visibility_key="hidden-rule"
                )
                expected = timeline_blocks(
                    self.tracker.snapshot()[1], interval, visible
                )
                self.assertEqual(actual, expected)
        self.assertEqual(
            self.tracker.timeline_snapshot(60, lambda _: True, "all-rule"),
            timeline_blocks(self.tracker.snapshot()[1], 60, lambda _: True),
        )

    def test_day_change_invalidates_cached_prefix(self):
        self.clock[0] = dt.datetime(2026, 10, 31, 23, 59, 50).timestamp()
        self.tracker.capture()
        self.tracker.flush()
        self.tracker.snapshot()
        self.clock[0] += 20
        self.tracker.capture()
        self.assertEqual(self.tracker.snapshot()[0], {"A": 10})

    def test_clock_correction_retains_full_sorting_semantics(self):
        self.clock[0] += 100
        self.tracker.capture()
        self.tracker.flush()
        self.clock[0] -= 80
        self.tracker.capture()
        self.clock[0] += 10
        rows = self.tracker.snapshot()[1]
        self.assertEqual(rows, sorted(rows, key=lambda r: r[0]))
        self.assertEqual(
            self.tracker.timeline_snapshot(30, lambda _: True, "all"),
            timeline_blocks(rows, 30, lambda _: True),
        )


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "tools/package_release.py"
        spec = importlib.util.spec_from_file_location("stt_packaging", path)
        cls.packaging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.packaging)

    def test_standard_package_uses_public_defaults_and_cannot_mix_editions(self):
        source = Path(__file__).parent
        files = [
            "standard/" + p.name
            for p in source.iterdir()
            if p.is_file()
            and p.name != "config.txt"
            and p.suffix in (".py", ".md", ".txt", ".ico")
        ]
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "candidate.exe"
            exe.write_bytes(b"MZ-isolated-packaging-fixture")
            out = Path(folder) / "output"
            with patch.object(
                self.packaging.subprocess,
                "check_output",
                return_value=("\0".join(files) + "\0").encode("utf-8"),
            ):
                assets = self.packaging.package_release(
                    source.parent, "2026.10.31.1", exe, out, "standard"
                )
                with self.assertRaises(FileExistsError):
                    self.packaging.package_release(
                        source.parent, "2026.10.31.1", exe, out, "standard"
                    )
                with self.assertRaises(RuntimeError):
                    self.packaging.package_release(
                        source.parent,
                        "2026.10.31.1",
                        exe,
                        Path(folder) / "bad",
                        "optimized",
                    )
            with zipfile.ZipFile(assets[1]) as z:
                names = z.namelist()
                self.assertIn("config.txt", names)
                self.assertEqual(
                    z.read("config.txt"), (source / "config.default.txt").read_bytes()
                )
                self.assertFalse(any("qwen" in n or "optimized" in n for n in names))
            checks = assets[3].read_text(encoding="utf-8")
            for p in assets[:3]:
                self.assertIn(
                    hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name, checks
                )


if __name__ == "__main__":
    unittest.main()
