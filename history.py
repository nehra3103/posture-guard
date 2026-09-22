"""Posture history: per-minute totals of good/slouched time and alerts, stored in a local SQLite file."""

import sqlite3
import threading
import time
from datetime import datetime, timedelta

STREAK_GAP_MINUTES = 5  # being away longer than this ends a streak
MIN_DISTANCE_SAMPLES = 150  # ~10 s of face tracking before an average distance means anything


def local_midnight(day=None):
    day = day or datetime.now()
    return datetime(day.year, day.month, day.day)


def good_minute(good, bad, alerts):
    """A minute counts toward a streak if you were mostly upright and never got an alert."""
    return alerts == 0 and good + bad >= 10 and bad <= good


def best_streak(rows):
    """Longest run of good minutes. rows: (minute, good, bad, alerts, ...) sorted by minute."""
    best = run = 0
    last = None
    for minute, good, bad, alerts, *_ in rows:
        if good + bad < 10:
            continue  # barely tracked; neither extends nor breaks a streak
        if last is not None and minute - last > STREAK_GAP_MINUTES:
            run = 0
        run = run + 1 if good_minute(good, bad, alerts) else 0
        best = max(best, run)
        last = minute
    return best


class History:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS minutes (
                               minute INTEGER PRIMARY KEY,  -- unix time // 60
                               good   REAL NOT NULL DEFAULT 0,
                               bad    REAL NOT NULL DEFAULT 0,
                               alerts INTEGER NOT NULL DEFAULT 0,
                               dist_sum REAL NOT NULL DEFAULT 0,  -- eye-to-screen distance samples (cm)
                               dist_n INTEGER NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(minutes)")}
        for col, kind in (("dist_sum", "REAL"), ("dist_n", "INTEGER")):  # databases from older versions
            if col not in columns:
                self.db.execute(f"ALTER TABLE minutes ADD COLUMN {col} {kind} NOT NULL DEFAULT 0")
        self.db.commit()
        self.lock = threading.Lock()
        self.pending = None  # [minute, good, bad, alerts, dist_sum, dist_n] not yet written

    # ---- writing
    def record(self, good=0.0, bad=0.0, alerts=0, distance=None):
        minute = int(time.time() // 60)
        with self.lock:
            if self.db is None:
                return  # closed (app quitting)
            if self.pending and self.pending[0] != minute:
                self._flush()
            if not self.pending:
                self.pending = [minute, 0.0, 0.0, 0, 0.0, 0]
            self.pending[1] += good
            self.pending[2] += bad
            self.pending[3] += alerts
            if distance is not None:
                self.pending[4] += distance
                self.pending[5] += 1

    def _flush(self):
        if not self.pending:
            return
        self.db.execute("""INSERT INTO minutes (minute, good, bad, alerts, dist_sum, dist_n)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(minute) DO UPDATE SET good = good + excluded.good,
                               bad = bad + excluded.bad, alerts = alerts + excluded.alerts,
                               dist_sum = dist_sum + excluded.dist_sum, dist_n = dist_n + excluded.dist_n""",
                        self.pending)
        self.db.commit()
        self.pending = None

    def close(self):
        with self.lock:
            if self.db is None:
                return
            self._flush()
            self.db.close()
            self.db = None

    # ---- reading
    def rows(self, start, end=None):
        """(minute, good, bad, alerts, dist_sum, dist_n) between two datetimes, including unsaved data."""
        lo = int(start.timestamp() // 60)
        hi = int((end or datetime.now() + timedelta(days=1)).timestamp() // 60)
        with self.lock:
            rows = self.db.execute("SELECT minute, good, bad, alerts, dist_sum, dist_n FROM minutes "
                                   "WHERE minute >= ? AND minute < ? ORDER BY minute", (lo, hi)).fetchall()
            if self.pending and lo <= self.pending[0] < hi:
                p = self.pending
                if rows and rows[-1][0] == p[0]:
                    last = rows.pop()
                    rows.append((p[0],) + tuple(x + y for x, y in zip(last[1:], p[1:])))
                else:
                    rows.append(tuple(p))
        return rows

    def today(self):
        """Totals for today: dict(good, bad, alerts, streak, percent, distance)."""
        rows = self.rows(local_midnight())
        return summarize(rows)


def summarize(rows):
    good = sum(r[1] for r in rows)
    bad = sum(r[2] for r in rows)
    total = good + bad
    dist_n = sum(r[5] for r in rows)
    return {
        "good": good,
        "bad": bad,
        "alerts": sum(r[3] for r in rows),
        "streak": best_streak(rows),
        "percent": None if total < 30 else 100 * good / total,
        "distance": sum(r[4] for r in rows) / dist_n if dist_n >= MIN_DISTANCE_SAMPLES else None,
    }


def fmt_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"
