"""Bounded, nonblocking text logging; usage records use the database queue."""

import logging
from logging.handlers import QueueHandler, RotatingFileHandler
import queue
import threading


class _QueueHandler(QueueHandler):
    def __init__(self, records):
        super().__init__(records)
        self.dropped = 0
        self._drop_lock = threading.Lock()

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # A slow log disk must neither freeze Tk nor exhaust process memory.
            with self._drop_lock:
                self.dropped += 1

    def take_dropped(self):
        with self._drop_lock:
            dropped, self.dropped = self.dropped, 0
        return dropped


class AsyncFileLog:
    def __init__(self, logger, filename, capacity=1024):
        self.logger = logger
        self.records = queue.Queue(maxsize=capacity)
        self.handler = RotatingFileHandler(
            filename,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        self.queue_handler = _QueueHandler(self.records)
        self._stopping = threading.Event()
        self.stopped = threading.Event()
        self.last_error = ""
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="stt-log-writer"
        )
        logger.addHandler(self.queue_handler)
        self.thread.start()

    def _run(self):
        try:
            while True:
                try:
                    record = self.records.get(timeout=0.05)
                except queue.Empty:
                    if self._stopping.is_set():
                        return
                    continue
                try:
                    self.handler.handle(record)
                    dropped = self.queue_handler.take_dropped()
                    if dropped:
                        warning = logging.LogRecord(
                            self.logger.name,
                            logging.WARNING,
                            __file__,
                            0,
                            "日志队列繁忙，略过%s条文本日志；统计记录不受影响",
                            (dropped,),
                            None,
                        )
                        self.handler.handle(warning)
                except Exception as error:
                    # Do not log into our own queue recursively on sink failure.
                    self.last_error = str(error)
                finally:
                    self.records.task_done()
                if self._stopping.is_set() and self.records.empty():
                    return
        finally:
            try:
                self.handler.close()
            finally:
                self.stopped.set()

    def begin_close(self):
        """Detach producers and request a drain without waiting on the Tk thread."""
        self.logger.removeHandler(self.queue_handler)
        self._stopping.set()

    def close(self, timeout=3):
        """Blocking drain for non-GUI callers, e.g. command-line checks."""
        self.begin_close()
        self.thread.join(timeout)
        if self.stopped.is_set():
            self.queue_handler.close()
        return self.stopped.is_set()
