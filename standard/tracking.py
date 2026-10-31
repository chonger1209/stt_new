"""Application intervals and a monotonic countdown, independent of the UI."""
import datetime as dt
import math
import time
import logging
import os


def active_seconds():
    """Windows awake time: unaffected by clock correction and excludes suspend."""
    if os.name == 'nt':
        import ctypes
        value = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
            raise ctypes.WinError()
        return value.value / 10_000_000
    return time.monotonic()


def split_interval(app, start, end):
    """Keep the legacy end-timestamp format, splitting intervals at local midnight."""
    rows = []
    while end > start:
        local = dt.datetime.fromtimestamp(start)
        midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time()).timestamp()
        stop = min(end, midnight)
        # An interval ending at midnight belongs to the day that just ended.
        stamp = dt.datetime.fromtimestamp(stop)
        if stop == midnight:
            stamp -= dt.timedelta(microseconds=1)
        rows.append((stamp.isoformat(sep=' ', timespec='microseconds'), app, stop - start))
        start = stop
    return rows


class UsageTracker:
    def __init__(self, store, clock=time.time, elapsed_clock=None, logger=None):
        self.store = store
        self.logger = logger or logging.getLogger(__name__)
        self.clock = clock
        # A supplied legacy test clock remains a single deterministic clock.
        self.elapsed_clock = elapsed_clock or (active_seconds if clock is time.time else clock)
        self.app = None
        self.start = clock()
        self._start_elapsed = self.elapsed_clock()
        self._observed = (self.start, self._start_elapsed)
        self.pending = []
        self._inflight = None
        self._flush_requested = False

    def _rows(self, now, elapsed):
        duration = max(0.0, elapsed - self._start_elapsed)
        if not self.app or not duration:
            return []
        # Calendar stamps still use local wall time; elapsed duration never does.
        start = self.start if abs((now - self.start) - duration) < 2 else now - duration
        return split_interval(self.app, start, start + duration)

    def heartbeat(self):
        wall, elapsed = self.clock(), self.elapsed_clock()
        old_wall, old_elapsed = self._observed
        self._observed = (wall, elapsed)
        if abs((wall - old_wall) - (elapsed - old_elapsed)) > 2:
            self.capture(wall, elapsed)
            self.app = None
            return True
        return False

    def capture(self, now=None, elapsed=None):
        now = self.clock() if now is None else now
        elapsed = self.elapsed_clock() if elapsed is None else elapsed
        self.pending.extend(self._rows(now, elapsed))
        self.start = now
        self._start_elapsed = elapsed
        # Bound pending memory by saving, never by discarding unsaved records.
        if len(self.pending) >= 1000:
            self.flush()

    def switch(self, app):
        if app != self.app:
            now = self.clock()
            self.capture(now)
            self.app = app

    def flush(self):
        if hasattr(self.store, 'submit_intervals'):
            self._flush_requested = True
            self._submit_pending()
            return
        if self.pending:
            self.store.insert_intervals(self.pending)
            self.pending.clear()  # Only after a successful transaction.

    def _submit_pending(self):
        if self._inflight is not None or not self.pending:
            return
        batch = tuple(self.pending)
        self._inflight = batch
        def committed(_):
            # Later samples may have been appended while this batch was saving.
            if len(self.pending) >= len(batch) and all(row is expected for row, expected in zip(self.pending, batch)):
                del self.pending[:len(batch)]
            else:
                # The DB already committed: never retry these rows or delete
                # equal-valued, later samples. Reconcile by object identity.
                identities = {id(row) for row in batch}
                self.pending[:] = [row for row in self.pending if id(row) not in identities]
                self.logger.error('保存回执前缀异常；已按批次身份核销，未重试已提交记录')
            self._inflight = None
            if self._flush_requested and self.pending:
                self._submit_pending()
            else:
                self._flush_requested = False
        try:
            self.store.submit_intervals(batch, committed)
        except Exception:
            self._inflight = None
            raise

    def live_rows(self, now=None):
        now = self.clock() if now is None else now
        return self._rows(now, self.elapsed_clock())

    def snapshot(self):
        now = self.clock()
        day = dt.datetime.fromtimestamp(now).date().isoformat()
        extra = [row for row in self.pending + self.live_rows(now) if row[0][:10] == day]
        totals = dict(self.store.totals(day))
        for _, app, duration in extra:
            totals[app] = totals.get(app, 0) + duration
        history = self.store.history(day) + extra
        history.sort(key=lambda row: row[0])
        return totals, history

    def close(self):
        self.capture()
        self.flush()


def timeline_blocks(history, seconds_per_block, visible):
    """Accumulate complete blocks; each block uses its closing segment's app."""
    block_ticks = round(seconds_per_block * 1_000_000)
    if block_ticks <= 0:
        raise ValueError('seconds_per_block must be positive')
    elapsed_ticks = 0
    completed = 0
    blocks = []
    for _, app, duration in history:
        if duration <= 0 or not visible(app):
            continue
        # Database timestamps have microsecond precision. Integer arithmetic keeps
        # repeated fractional samples from drifting across a block boundary.
        elapsed_ticks += round(duration * 1_000_000)
        new_completed = elapsed_ticks // block_ticks
        count = new_completed - completed
        if count:
            if blocks and blocks[-1][0] == app:
                blocks[-1] = (app, blocks[-1][1] + count)
            else:
                blocks.append((app, count))
        completed = new_completed
    return blocks


class Countdown:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.total = seconds
        self._remaining = float(seconds)
        self.deadline = None

    @property
    def running(self):
        return self.deadline is not None

    @property
    def remaining(self):
        value = self._remaining if self.deadline is None else self.deadline - self.clock()
        return max(0, math.ceil(value))

    def start(self):
        if not self.running:
            if self._remaining <= 0:
                self._remaining = float(self.total)
            self.deadline = self.clock() + self._remaining

    def pause(self):
        if self.running:
            self._remaining = max(0, self.deadline - self.clock())
            self.deadline = None

    def reset(self):
        self.deadline = None
        self._remaining = float(self.total)

    def configure(self, seconds):
        idle = not self.running and self._remaining == self.total
        self.total = seconds
        if idle:
            self.reset()
