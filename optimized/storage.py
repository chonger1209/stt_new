"""Persistent SQLite connection; compatible with the original two database files."""
import datetime as dt
import sqlite3
import queue
import threading
import time
import logging


class HistoryStore:
    def __init__(self, directory):
        self.connection = sqlite3.connect(str(directory / 'screen_time_history.db'), timeout=5)
        try:
            self._initialize(directory)
        except Exception:
            self.connection.close()
            raise
        self._cache = {}

    def _initialize(self, directory):
        self.connection.execute('ATTACH DATABASE ? AS stream', (str(directory / 'time_stream_history.db'),))
        for schema, table in [('main', 'screen_time'), ('stream', 'time_stream')]:
            self.connection.execute(f'''CREATE TABLE IF NOT EXISTS {schema}.{table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                app_name TEXT NOT NULL, duration REAL NOT NULL)''')
            self.connection.execute(f'CREATE INDEX IF NOT EXISTS {schema}.idx_{table}_timestamp ON {table}(timestamp)')
            self.connection.execute(f'CREATE INDEX IF NOT EXISTS {schema}.idx_{table}_app ON {table}(app_name)')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS stream.logged_pixels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, app_name TEXT NOT NULL,
            time_range_start TEXT NOT NULL, time_range_end TEXT NOT NULL,
            duration REAL NOT NULL, log_timestamp TEXT NOT NULL,
            UNIQUE(app_name, time_range_start, time_range_end))''')
        self.connection.commit()

    def insert_intervals(self, rows):
        # One attached-database transaction commits both records together.
        with self.connection:
            for table in ['main.screen_time', 'stream.time_stream']:
                self.connection.executemany(
                    f'INSERT INTO {table}(timestamp, app_name, duration) VALUES (?, ?, ?)', rows)
        self._cache.clear()

    def _load(self, day):
        if day not in self._cache:
            tomorrow = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
            bounds = (day, tomorrow)
            totals = dict(self.connection.execute('''SELECT app_name, SUM(duration)
                FROM screen_time WHERE timestamp >= ? AND timestamp < ? GROUP BY app_name''', bounds))
            history = list(self.connection.execute('''SELECT timestamp, app_name, duration
                FROM stream.time_stream WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp, id''', bounds))
            self._cache = {day: (totals, history)}
        return self._cache[day]

    def totals(self, day):
        return self._load(day)[0]

    def history(self, day):
        return list(self._load(day)[1])

    def log_pixels(self, rows):
        stamp = dt.datetime.now().isoformat(sep=' ', timespec='seconds')
        inserted = []
        with self.connection:
            for timestamp, app, duration in rows:
                cursor = self.connection.execute('''INSERT OR IGNORE INTO stream.logged_pixels
                    (app_name, time_range_start, time_range_end, duration, log_timestamp)
                    VALUES (?, ?, ?, ?, ?)''', (app, timestamp, timestamp, duration, stamp))
                if cursor.rowcount:
                    inserted.append((timestamp, app, duration))
        return inserted

    def close(self):
        self.connection.close()


class AsyncHistoryStore:
    """One DB owner; the UI consumes ordered acknowledgements and cached reads.

    Failed jobs stay at the head of the worker queue and retry unchanged. A
    successful transaction is never retried because a UI callback failed.
    """
    def __init__(self, directory, retry_seconds=2, factory=HistoryStore, logger=None):
        self.directory = directory
        self.logger = logger or logging.getLogger(__name__)
        self.ready = False
        self.pending_writes = 0
        self.read_queries = 0
        self.version = 0
        self.failure_since = None
        self.last_error = ''
        self._cache = {}
        self._reading = set()
        self._jobs = queue.Queue()
        self._events = queue.Queue()
        self._closed = threading.Event()
        self.stopped = threading.Event()
        self._retry_seconds = retry_seconds
        self._factory = factory
        self._thread = threading.Thread(target=self._run, daemon=True, name='stt-database')
        self._thread.start()
        self._request_day(dt.date.today().isoformat())

    def _request_day(self, day):
        if day not in self._cache and day not in self._reading:
            self._reading.add(day)
            self._jobs.put(('read', day, None))

    def totals(self, day):
        self._request_day(day)
        return dict(self._cache.get(day, ({}, []))[0])

    def history(self, day):
        self._request_day(day)
        return list(self._cache.get(day, ({}, []))[1])

    def submit_intervals(self, rows, committed):
        self._submit('intervals', rows, committed)

    def submit_pixels(self, rows, committed):
        self._submit('pixels', rows, committed)

    def _submit(self, kind, rows, committed):
        if self._closed.is_set():
            raise RuntimeError('数据库工作线程已关闭')
        self.pending_writes += 1
        self._jobs.put((kind, tuple(rows), committed))

    def poll(self):
        """Called only by the UI; callback and cache change occur in one turn."""
        failures = []
        while True:
            try:
                kind, payload, callback, result, error = self._events.get_nowait()
            except queue.Empty:
                break
            if error is not None:
                if self.failure_since is None:
                    self.failure_since = time.monotonic()
                self.last_error = str(error)
                failures.append((kind, str(error)))
                continue
            self.failure_since = None
            self.last_error = ''
            if kind == 'ready':
                self.ready = True
            elif kind == 'read':
                self.version += 1
                self.read_queries += 2
                self._reading.discard(payload)
                self._cache = {payload: result}
            else:
                self.pending_writes -= 1
                if kind == 'intervals':
                    self.version += 1
                    for stamp, app, duration in payload:
                        cached = self._cache.get(stamp[:10])
                        if cached is not None:
                            totals, history = cached
                            totals[app] = totals.get(app, 0) + duration
                            history.append((stamp, app, duration))
                if callback is not None:
                    try:
                        callback(result)
                    except Exception as error:
                        self.logger.exception('数据库已提交，但保存回执处理异常；不重试已提交事务')
                        failures.append(('acknowledgement', str(error)))
        return failures

    def _run(self):
        store = None
        try:
            while not self._closed.is_set() and store is None:
                try:
                    store = self._factory(self.directory)
                    self._events.put(('ready', None, None, None, None))
                except Exception as error:
                    self._events.put(('initialize', None, None, None, error))
                    self._closed.wait(self._retry_seconds)
            while not self._closed.is_set():
                job = self._jobs.get()
                if job is None:
                    return
                kind, payload, callback = job
                while not self._closed.is_set():
                    try:
                        if kind == 'read':
                            result = (dict(store.totals(payload)), store.history(payload))
                        elif kind == 'intervals':
                            store.insert_intervals(payload)
                            result = None
                        else:
                            result = store.log_pixels(payload)
                    except Exception as error:
                        self._events.put((kind, payload, callback, None, error))
                        self._closed.wait(self._retry_seconds)
                    else:
                        self._events.put((kind, payload, callback, result, None))
                        break
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                self.stopped.set()

    def close(self):
        if self.pending_writes:
            raise RuntimeError('仍有未确认保存的数据，不能退出')
        if not self._closed.is_set():
            self._closed.set()
            self._jobs.put(None)
