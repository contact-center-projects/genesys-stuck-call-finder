#!/usr/bin/env python3
"""
Regression tests for find_stuck_calls.py.

Run with:
    py -m unittest discover -s tests -v
(from the genesys-stuck-call-finder directory), or just:
    py tests\\test_find_stuck_calls.py

Each test here was written to pin down a specific bug found and fixed
during development - see the docstring on each test for what it guards
against regressing.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from find_stuck_calls import (  # noqa: E402
    find_stuck_calls, find_seek_offset, is_snapshot_file, parse_at_time,
    peek_file_time_range, ScanCancelled, parse_ts, is_date_less,
)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def write_log(lines) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("\n".join(lines))
        return f.name


class TestSnapshotExclusion(unittest.TestCase):
    """Guards against re-introducing snapshot.log (a StatServer/T-Server
    state-checkpoint file, not an event log) into the scan."""

    def test_snapshot_variants_excluded(self):
        for name in ("snapshot.log", "Snapshot.LOG", "snapshot_2026.log"):
            self.assertTrue(is_snapshot_file(f"C:/logs/{name}"), name)

    def test_lookalike_names_not_excluded(self):
        for name in ("snapshotter.log", "tserver.log", "TServer_2026_09_02_10.log"):
            self.assertFalse(is_snapshot_file(f"C:/logs/{name}"), name)


class TestUnreadableFileResilience(unittest.TestCase):
    """Guards against a single unreadable/locked/missing file (permissions,
    AV lock, network hiccup) aborting the whole scan instead of being
    skipped with a message."""

    def test_missing_file_is_skipped_not_fatal(self):
        messages = []
        stuck = find_stuck_calls("VQ_Sales", ["/nonexistent/path.log"],
                                  debug_sink=messages.append)
        self.assertEqual(stuck, [])
        self.assertTrue(any("skipped" in m for m in messages))


class TestStartTimeProximitySearch(unittest.TestCase):
    """Guards the core search semantics: given a start time + radius, return
    every call that STARTED in that window - both already-ended and still-
    active - tagged with status, sorted by duration. This replaced an
    earlier, more confusing "overlap with an incident window" model."""

    def setUp(self):
        self.log = write_log([
            # started 10:05, ended 10:07 -> outside a 10:15-10:45 search
            f"{fmt(datetime(2026,9,2,10,5,0))} Std 1 EventQueued AttributeConnID: [callA] AttributeANI: [111] AttributeQueue: [VQ_Sales]",
            f"{fmt(datetime(2026,9,2,10,7,0))} Std 1 EventReleased AttributeConnID: [callA]",
            # started 10:20 (in window), ended 10:25 -> should show as "ended"
            f"{fmt(datetime(2026,9,2,10,20,0))} Std 1 EventQueued AttributeConnID: [callB] AttributeANI: [222] AttributeQueue: [VQ_Sales]",
            f"{fmt(datetime(2026,9,2,10,25,0))} Std 1 EventReleased AttributeConnID: [callB]",
            # started 10:35 (in window), never ends -> the stuck one
            f"{fmt(datetime(2026,9,2,10,35,0))} Std 1 EventQueued AttributeConnID: [callC] AttributeANI: [333] AttributeQueue: [VQ_Sales]",
            # later, unrelated event extends "now" past callC's start
            f"{fmt(datetime(2026,9,2,12,0,0))} Std 1 EventQueued AttributeConnID: [other] AttributeQueue: [VQ_Other]",
        ])

    def tearDown(self):
        os.unlink(self.log)

    def test_excludes_calls_outside_window(self):
        at = datetime(2026, 9, 2, 10, 30)
        window = (at - timedelta(minutes=15), at + timedelta(minutes=15))
        stuck = find_stuck_calls("VQ_Sales", [self.log], window=window)
        ids = [rec.conn_id for _, rec in stuck]
        self.assertNotIn("callA", ids)

    def test_includes_ended_and_active_with_correct_status(self):
        at = datetime(2026, 9, 2, 10, 30)
        window = (at - timedelta(minutes=15), at + timedelta(minutes=15))
        stuck = find_stuck_calls("VQ_Sales", [self.log], window=window)
        result = {rec.conn_id: rec.status for _, rec in stuck}
        self.assertEqual(result.get("callB"), "ended")
        self.assertEqual(result.get("callC"), "ACTIVE")

    def test_longest_duration_sorts_first(self):
        at = datetime(2026, 9, 2, 10, 30)
        window = (at - timedelta(minutes=15), at + timedelta(minutes=15))
        stuck = find_stuck_calls("VQ_Sales", [self.log], window=window)
        self.assertEqual(stuck[0][1].conn_id, "callC")

    def test_no_window_means_currently_stuck_only(self):
        """Blank start time = 'what's stuck right now': only calls with no
        end event at all, regardless of when they started."""
        stuck = find_stuck_calls("VQ_Sales", [self.log])
        ids = [rec.conn_id for _, rec in stuck]
        self.assertEqual(ids, ["callC"])


class TestParseAtTime(unittest.TestCase):
    """Guards the HH:MM-anchored-to-a-specific-day input parsing, added
    after 24h rolling logs made 'assume system today' unsafe."""

    def test_bare_time_anchors_to_given_day(self):
        today = datetime(2026, 9, 2, 3, 0, 0)
        parsed = parse_at_time("10:30", today=today)
        self.assertEqual(parsed, datetime(2026, 9, 2, 10, 30))

    def test_full_datetime_still_accepted(self):
        parsed = parse_at_time("2026-09-01 09:15")
        self.assertEqual(parsed, datetime(2026, 9, 1, 9, 15))

    def test_blank_returns_none(self):
        self.assertIsNone(parse_at_time(""))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_at_time("not a time")


class TestPeekFileTimeRange(unittest.TestCase):
    """Guards against relying on filesystem mtime to decide whether a file
    covers a given search time - mtime reflects when a file was last
    WRITTEN, not the calendar range of its CONTENT (e.g. a file opened at
    06:00 and still growing at 18:00 has mtime=18:00)."""

    def test_detects_real_content_range_regardless_of_mtime(self):
        lines = []
        t = datetime(2026, 9, 2, 6, 0, 0)
        while t <= datetime(2026, 9, 2, 18, 0, 0):
            lines.append(f"{fmt(t)} Std 1 EventQueued AttributeConnID: [c] AttributeQueue: [VQ_Sales]")
            t += timedelta(minutes=1)
        path = write_log(lines)
        try:
            time_range = peek_file_time_range(path)
            self.assertIsNotNone(time_range)
            lo, hi = time_range
            self.assertLessEqual(lo, datetime(2026, 9, 2, 6, 1))
            self.assertGreaterEqual(hi, datetime(2026, 9, 2, 17, 59))

            # A 10:30 search should overlap this file's real range, even
            # though the file's mtime (set by this test writing it) has
            # nothing to do with 06:00-18:00.
            search_lo = datetime(2026, 9, 2, 10, 15)
            search_hi = datetime(2026, 9, 2, 10, 45)
            self.assertFalse(hi < search_lo or lo > search_hi)
        finally:
            os.unlink(path)


class TestSeekAheadOptimization(unittest.TestCase):
    """Guards against 'scans from the start of a 24h file instead of
    jumping to the requested time' - find_seek_offset should land the read
    position close to the search window instead of always starting at 0,
    while find_stuck_calls must still find the right call afterward."""

    def test_seek_skips_most_of_a_24h_file_and_still_finds_the_call(self):
        day_start = datetime(2026, 9, 2, 0, 0, 0)
        lines = []
        for i in range(24 * 60 * 60):  # one line per second for 24h
            ts = day_start + timedelta(seconds=i)
            lines.append(f"{fmt(ts)} Std 1 EventQueued AttributeConnID: [filler{i}] AttributeQueue: [VQ_Sales]")
        target_idx = 23 * 3600 + 30 * 60  # target call starts at 23:30
        lines[target_idx] = (f"{fmt(day_start + timedelta(seconds=target_idx))} Std 1 EventQueued "
                              f"AttributeConnID: [callSTUCK] AttributeQueue: [VQ_Sales]")
        path = write_log(lines)
        try:
            target = day_start + timedelta(hours=23, minutes=30)
            window = (target - timedelta(minutes=15), target + timedelta(minutes=15))

            offset = find_seek_offset(path, window[0])
            size = os.path.getsize(path)
            self.assertGreater(offset, size * 0.5,
                                "seek-ahead should skip a large majority of a 24h file")

            stuck = find_stuck_calls("VQ_Sales", [path], window=window)
            ids = [rec.conn_id for _, rec in stuck]
            self.assertIn("callSTUCK", ids)
        finally:
            os.unlink(path)


class TestDatelessTimestamps(unittest.TestCase):
    """Guards support for logs with NO date in the timestamp at all - just
    'HH:MM:SS.xxxx' - which is the real format of the user's T-Server logs
    and was the actual root cause of 'search still scans from the start
    regardless of the time entered' (the parser couldn't read any
    timestamp at all, so none of the filtering/seeking could work)."""

    @staticmethod
    def fmt_dateless(dt: datetime) -> str:
        return dt.strftime("%H:%M:%S.%f")[:-2]  # 4-digit fractional

    def test_bare_time_parses_as_date_less(self):
        ts = parse_ts("10:30:15.1234")
        self.assertIsNotNone(ts)
        self.assertTrue(is_date_less(ts))
        self.assertEqual((ts.hour, ts.minute, ts.second), (10, 30, 15))

    def test_dateless_24h_log_with_hex_connid_is_searchable(self):
        day_start = datetime(2026, 9, 2, 0, 0, 0)
        lines = []
        for i in range(24 * 60 * 60):
            ts_i = day_start + timedelta(seconds=i)
            lines.append(f"{self.fmt_dateless(ts_i)} Std 1 EventQueued "
                         f"AttributeConnID: [0071a2b3c4d5{i:04x}] AttributeQueue: [VQ_Sales]")
        target_idx = 23 * 3600 + 30 * 60
        lines[target_idx] = (f"{self.fmt_dateless(day_start + timedelta(seconds=target_idx))} "
                              f"Std 1 EventQueued AttributeConnID: [deadbeef0001] AttributeQueue: [VQ_Sales]")
        path = write_log(lines)
        try:
            search_date = datetime(2026, 9, 2)
            target = datetime(2026, 9, 2, 23, 30)
            window = (target - timedelta(minutes=15), target + timedelta(minutes=15))

            time_range = peek_file_time_range(path, anchor_date=search_date)
            self.assertIsNotNone(time_range)
            self.assertEqual(time_range[0].date(), search_date.date())

            offset = find_seek_offset(path, window[0], anchor_date=search_date)
            size = os.path.getsize(path)
            self.assertGreater(offset, size * 0.5)

            stuck = find_stuck_calls("VQ_Sales", [path], window=window)
            ids = [rec.conn_id for _, rec in stuck]
            self.assertIn("deadbeef0001", ids)
        finally:
            os.unlink(path)

    def test_midnight_rollover_bumps_date_forward(self):
        lines = []
        t = datetime(2026, 9, 2, 23, 0, 0)
        end = datetime(2026, 9, 3, 1, 0, 0)
        while t <= end:
            lines.append(f"{self.fmt_dateless(t)} Std 1 EventQueued "
                         f"AttributeConnID: [c{t.strftime('%H%M%S')}] AttributeQueue: [VQ_Sales]")
            t += timedelta(seconds=30)
        lines.append(f"{self.fmt_dateless(datetime(2026,9,3,0,30,0))} Std 1 EventQueued "
                     f"AttributeConnID: [afterMidnight] AttributeQueue: [VQ_Sales]")
        path = write_log(lines)
        try:
            target = datetime(2026, 9, 3, 0, 30)  # search date correctly set to the 3rd
            window = (target - timedelta(minutes=10), target + timedelta(minutes=10))
            stuck = find_stuck_calls("VQ_Sales", [path], window=window)
            ids = [rec.conn_id for _, rec in stuck]
            self.assertIn("afterMidnight", ids)
        finally:
            os.unlink(path)


class TestCancellation(unittest.TestCase):
    """Guards the Stop button's mechanism: a cancel_event set mid-scan must
    actually interrupt a long scan quickly, not just be ignored."""

    def test_cancel_event_stops_a_long_scan_promptly(self):
        start = datetime(2026, 9, 2, 10, 0, 0)
        lines = [f"{fmt(start + timedelta(seconds=i))} Std 1 EventQueued AttributeConnID: [c{i}] AttributeQueue: [VQ_Sales]"
                 for i in range(500000)]
        path = write_log(lines)
        try:
            cancel_event = threading.Event()
            threading.Timer(0.05, cancel_event.set).start()

            t0 = time.time()
            with self.assertRaises(ScanCancelled):
                find_stuck_calls("VQ_Sales", [path], cancel_event=cancel_event)
            self.assertLess(time.time() - t0, 5,
                             "cancellation should take effect quickly, not run to completion")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
