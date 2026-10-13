#!/usr/bin/env python3
"""
Genesys stuck-call finder.

Scans Genesys T-Server and/or StatServer log files, reconstructs each call's
lifecycle by ConnID, and reports calls that are still "active" (queued /
in-progress with no matching terminal event) on a given Virtual Queue,
sorted by elapsed time so the stuck call surfaces at the top.

WHY BOTH LOG SOURCES
---------------------
StatServer tells you "VQ_Sales currently has 6 calls waiting" but by default
does not log per-call identity (ANI/DNIS) unless call-based stats are logged
at trace level. T-Server logs (EventQueued / EventDiverted / EventReleased /
EventAbandoned / EventRouted) already carry ConnID + ANI + DNIS at normal
log levels. This script therefore treats T-Server as the source of truth for
call identity, and StatServer (if supplied) only as a cross-check on the
VQ's current-call-count so you can sanity-check nothing was missed.

LOG FORMAT CAVEAT (READ THIS)
------------------------------
Genesys log line layout varies by app config (single-line vs. block style,
"AttributeX: [value]" vs "AttributeX=value", timestamp format, etc.).
The parser below is written against the two most common styles and is
DELIBERATELY built as small, isolated regexes (see PATTERNS below) so you
can adjust just the pattern that doesn't match your actual logs rather than
rewriting the whole script. Run with --debug-unmatched first to see which
lines the parser is silently skipping, and adjust PATTERNS accordingly.

USAGE
-----
    python find_stuck_calls.py --vq VQ_Sales --tserver tserver.log \
        [--statserver statserver.log] [--threshold-sec 300] [--debug-unmatched]

Requires only the Python standard library (3.8+).
"""

import argparse
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Tuple


class ScanCancelled(Exception):
    """Raised (and caught by the caller) when a cancel_event is set mid-scan."""


# How many lines between checks of cancel_event - frequent enough that a
# Stop click feels responsive, infrequent enough not to add real overhead.
CANCEL_CHECK_EVERY_LINES = 2000

# ---------------------------------------------------------------------------
# DEBUG LOGGING - a running log of what the tool itself did (file sizes,
# scan durations, lines processed, early-stops, skips) as opposed to the
# Genesys log content being scanned. Point people at this file when
# reporting "it's slow" or "it found nothing" - it shows why.
# ---------------------------------------------------------------------------
DEFAULT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "stuck_call_finder.log")

logger = logging.getLogger("stuck_call_finder")
logger.setLevel(logging.DEBUG)
logger.propagate = False


def configure_logging(log_path: str = DEFAULT_LOG_PATH) -> str:
    """(Re)point the debug logger at log_path. Safe to call more than once
    (e.g. GUI startup) - clears any previously attached handlers first."""
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return log_path


# Once a windowed scan has moved this many lines past the window's upper
# bound, stop reading the file - Genesys logs are append-only/ascending, so
# nothing relevant can appear later. The slack guards against the rare
# out-of-order line (multi-threaded loggers occasionally interleave a few
# lines) without reading gigabytes of irrelevant future log needlessly.
EARLY_STOP_SLACK_LINES = 5000

# Symmetric safety margin for the START of a windowed scan: after binary-
# searching for the byte offset of the window's lower bound, back up by
# this many bytes before starting to read, so a few out-of-order lines near
# the boundary aren't missed. Mirrors EARLY_STOP_SLACK_LINES on the other end.
SEEK_SAFETY_BYTES = 2 * 1024 * 1024

# ---------------------------------------------------------------------------
# PATTERNS - adjust these to match your actual log format.
# ---------------------------------------------------------------------------

# Matches an event header line. Genesys logs come in two flavors:
#   "2026-09-02T10:15:32.123 Std 25012 EventQueued Thread: 03"   (full date)
#   "10:15:32.1234 Std 25012 EventQueued Thread: 03"             (date-less,
#                                                                  common on
#                                                                  T-Server)
# The alternation tries the full-date form first, falling back to bare
# HH:MM:SS.ffff. Date-less timestamps get anchored to a calendar date
# elsewhere (see anchor_time()) since the line itself doesn't carry one.
EVENT_HEADER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+"
    r"|\d{2}:\d{2}:\d{2}[.,]\d+)"
    r".*?\b(?P<event>Event[A-Za-z]+)\b"
)

# Matches attribute lines that can appear either inline on the header line
# or on following indented lines, in either of these two common styles:
#   AttributeConnID: [0071a2b3c4d5b0e1]
#   ConnID=0071a2b3c4d5b0e1
ATTR_RE = re.compile(
    r"Attribute(?P<name1>[A-Za-z]+)\s*:\s*\[(?P<val1>[^\]]*)\]"
    r"|(?P<name2>\bConnID\b|\bANI\b|\bDNIS\b|\bQueue\b|\bVirtualQueue\b|\bThisQueue\b)"
    r"\s*=\s*(?P<val2>[^\s,;]+)"
)

TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
              "%Y-%m-%dT%H:%M:%S,%f", "%Y-%m-%d %H:%M:%S,%f")

# Date-less "time only" formats, e.g. "10:15:32.1234". Parsed timestamps use
# a sentinel date (year 1) to mark "no real date known yet" - see
# is_date_less()/anchor_time(), which combine these with an actual calendar
# date (from the search's --date/Date field, or a filename, or today).
TIME_ONLY_FORMATS = ("%H:%M:%S.%f", "%H:%M:%S,%f")


def is_date_less(dt: datetime) -> bool:
    """True if dt came from a bare HH:MM:SS timestamp with no real date -
    parse_ts() marks these with the sentinel date 0001-01-01."""
    return dt.year == 1 and dt.month == 1 and dt.day == 1


def compute_file_start_date(reference: datetime, day_start_time=None):
    """Infer the calendar date of a date-less log file's FIRST line.

    `reference` is a datetime we know the true date of - typically the
    user's search time (e.g. "2026-09-02 10:30"). If the file's own
    starting time-of-day (day_start_time) is LATER than the reference's
    time-of-day, the file must have started the day before the reference
    (e.g. reference is 00:30, file started at 23:00 - that 23:00 was
    yesterday relative to a 00:30 search). Otherwise the file started the
    same day as the reference. Assumes at most one midnight rollover in
    the file, true for any log covering a rolling window of 24h or less.
    """
    if day_start_time is None:
        return reference.date()
    if reference.time() < day_start_time:
        return reference.date() - timedelta(days=1)
    return reference.date()


def anchor_time(dt: datetime, file_start_date, day_start_time=None) -> datetime:
    """Combine a date-less timestamp (see is_date_less) with a real
    calendar date. If dt already has a real date, returned unchanged.

    file_start_date: calendar date of the file's FIRST line (see
    compute_file_start_date). day_start_time: that first line's
    time-of-day - a LATER line whose time-of-day is EARLIER than this
    means the clock wrapped past midnight since then, so it belongs to
    file_start_date + 1 day.
    """
    if not is_date_less(dt):
        return dt
    day_offset = 1 if (day_start_time is not None and dt.time() < day_start_time) else 0
    return datetime.combine(file_start_date + timedelta(days=day_offset), dt.time())

# Genesys apps periodically dump their in-memory state to a file literally
# named "snapshot.log" (or similar) for crash recovery. It's rewritten in
# place and held under an exclusive OS lock the whole time the app runs, so
# it's both unreadable while the app is up and irrelevant anyway - it has no
# per-call ConnID/ANI/DNIS event stream. Always skip it.
SNAPSHOT_FILENAME_RE = re.compile(r"^snapshot(?![a-z]).*\.log$", re.IGNORECASE)


def is_snapshot_file(path: str) -> bool:
    return bool(SNAPSHOT_FILENAME_RE.match(os.path.basename(path)))


# Event classification - adjust names to taste / your switch type.
QUEUE_START_EVENTS = {"EventQueued", "EventQueueCall", "EventDiverted"}
TERMINAL_EVENTS = {"EventReleased", "EventAbandoned", "EventDropped",
                    "EventPartyDeleted"}
# An EventRouted / EventDelivered off the VQ also ends its "waiting" phase,
# but the call may still be live (just no longer stuck IN the queue).
ROUTED_AWAY_EVENTS = {"EventRouted", "EventDelivered"}


def parse_ts(raw: str) -> Optional[datetime]:
    """Parse a timestamp string. Full date-time formats return a normal
    datetime; bare HH:MM:SS formats return one with the sentinel date
    0001-01-01 (see is_date_less/anchor_time) since there's no real date to
    attach yet - the caller anchors it to an actual calendar date."""
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    for fmt in TIME_ONLY_FORMATS:
        try:
            t = datetime.strptime(raw, fmt).time()
            return datetime.combine(datetime(1, 1, 1).date(), t)
        except ValueError:
            continue
    return None


def peek_file_time_range(path: str, head_bytes: int = 65536, tail_bytes: int = 65536,
                          anchor_date: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
    """Read just the first and last chunk of a log file to find the actual
    calendar time range its content covers, WITHOUT reading the whole file.

    This exists because filesystem modified-time is not a reliable proxy for
    "what time period does this file's content cover" - e.g. a log file
    opened at 06:00 that's still being appended to at 18:00 has an mtime of
    18:00, not 06:00. A call that started at 10:30 lives in that file, but
    a search filtered by mtime-near-10:30 would wrongly skip it. Reading the
    real first/last timestamps in the file is the only reliable way to know
    what it actually contains.

    anchor_date: calendar date to attach to date-less "HH:MM:SS" timestamps
    (some Genesys logs have no date at all in the line, just time-of-day).
    Defaults to today if not given. The tail is checked against the head's
    time-of-day to detect a midnight rollover within the file.

    Returns (earliest_ts, latest_ts) found, or None if neither chunk yielded
    a parseable timestamp (e.g. an empty or unrecognized-format file).
    """
    anchor_date = anchor_date or datetime.now()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head_raw = f.read(head_bytes)
            if size > tail_bytes:
                f.seek(max(0, size - tail_bytes))
                tail_raw = f.read()
            else:
                tail_raw = head_raw
    except OSError:
        return None

    def raw_ts_in(raw_bytes: bytes, from_end: bool) -> Optional[datetime]:
        """Parsed timestamp, NOT yet anchored to a calendar date."""
        lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
        if from_end:
            lines = reversed(lines)
        for line in lines:
            m = EVENT_HEADER_RE.search(line)
            if m:
                ts = parse_ts(m.group("ts"))
                if ts:
                    return ts
        return None

    raw_first = raw_ts_in(head_raw, from_end=False)
    raw_last = raw_ts_in(tail_raw, from_end=True)
    day_start_time = raw_first.time() if raw_first else None
    file_start_date = compute_file_start_date(anchor_date, day_start_time)

    first_ts = anchor_time(raw_first, file_start_date, day_start_time) if raw_first else None
    last_ts = anchor_time(raw_last, file_start_date, day_start_time) if raw_last else None
    candidates = [t for t in (first_ts, last_ts) if t]
    if not candidates:
        return None
    return (min(candidates), max(candidates))


def find_seek_offset(path: str, target: datetime, max_iterations: int = 30,
                      anchor_date: Optional[datetime] = None) -> int:
    """Binary-search an ascending-timestamp log file for a byte offset safe
    to start reading from when only interested in timestamps >= target.

    This is what actually fixes "the scan reads from the start of the file
    (hours before the search time) instead of jumping to it": a windowed
    scan previously read every line sequentially from byte 0, relying only
    on skipping non-matching events - for a large file where the search
    time is hours into it, that means reading and discarding most of the
    file. This finds roughly where the target time begins and returns a
    byte offset to seek to first.

    anchor_date: calendar date to attach to date-less "HH:MM:SS" timestamps
    (see anchor_time()). Defaults to today if not given.

    Best-effort and conservative: any trouble parsing a timestamp at a
    candidate position aborts the search and returns 0 (scan from the
    start) rather than risk returning a position that could skip real data.
    """
    anchor_date = anchor_date or datetime.now()
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if size == 0:
        return 0

    # Reference point for midnight-rollover detection: the file's own
    # starting time-of-day, read once from byte 0.
    day_start_time = None
    first_ts = None
    try:
        with open(path, "rb") as f:
            head_chunk = f.read(16384)
        for raw_line in head_chunk.split(b"\n"):
            m = EVENT_HEADER_RE.search(raw_line.decode("utf-8", errors="replace"))
            if m:
                first_ts = parse_ts(m.group("ts"))
                if first_ts:
                    day_start_time = first_ts.time()
                    break
    except OSError:
        pass

    file_start_date = compute_file_start_date(anchor_date, day_start_time)

    def ts_at_or_after(offset: int) -> Optional[datetime]:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                if offset != 0:
                    f.readline()  # discard partial line from landing mid-line
                chunk = f.read(16384)
        except OSError:
            return None
        for raw_line in chunk.split(b"\n"):
            m = EVENT_HEADER_RE.search(raw_line.decode("utf-8", errors="replace"))
            if m:
                ts = parse_ts(m.group("ts"))
                if ts:
                    return anchor_time(ts, file_start_date, day_start_time)
        return None

    lo, hi, best = 0, size, 0
    for _ in range(max_iterations):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        ts = ts_at_or_after(mid)
        if ts is None:
            break  # couldn't confirm a timestamp here - safer to give up
        if ts < target:
            best = mid
            lo = mid + 1
        else:
            hi = mid

    return max(0, best - SEEK_SAFETY_BYTES)


# "Call start time" input formats, tried in order. "HH:MM" (today's date,
# the normal/expected input) is tried first; full date-times are still
# accepted for analyzing a call from a previous day.
AT_TIME_FORMATS_TIME_ONLY = ("%H:%M", "%H:%M:%S")
AT_TIME_FORMATS_FULL = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S")


def parse_at_time(raw: str, today: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a user-entered call start time. Bare 'HH:MM' is anchored to
    `today` (defaults to the real current date). Returns None for blank
    input; raises ValueError for anything unparseable."""
    raw = raw.strip()
    if not raw:
        return None
    today = today or datetime.now()
    for fmt in AT_TIME_FORMATS_TIME_ONLY:
        try:
            t = datetime.strptime(raw, fmt)
            return today.replace(hour=t.hour, minute=t.minute,
                                  second=t.second, microsecond=0)
        except ValueError:
            continue
    for fmt in AT_TIME_FORMATS_FULL:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("Time must look like '10:30' (today), or "
                      "'2026-09-02 10:30' for a different day")


@dataclass
class CallRecord:
    conn_id: str
    ani: Optional[str] = None
    dnis: Optional[str] = None
    queue: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    ended: bool = False
    ended_at: Optional[datetime] = None
    routed_away: bool = False
    routed_away_at: Optional[datetime] = None
    events: list = field(default_factory=list)
    status: str = "ACTIVE"  # set by find_stuck_calls: "ACTIVE" or "ended"

    @property
    def effective_end(self) -> Optional[datetime]:
        """Timestamp the call stopped being 'in the VQ', or None if still open."""
        candidates = [t for t in (self.ended_at, self.routed_away_at) if t]
        return min(candidates) if candidates else None


def scan_file(path: str, debug_unmatched: bool, debug_sink=None,
              time_bound: Optional[Tuple[datetime, datetime]] = None,
              cancel_event: Optional[threading.Event] = None,
              anchor_date: Optional[datetime] = None) -> dict:
    """time_bound: optional (low, high) datetimes. Events outside this range
    are skipped entirely - used to cheaply narrow a scan to "around the hour
    the stuck call happened" instead of processing an entire day's log.

    When time_bound is set, scanning also stops early once the log has moved
    EARLY_STOP_SLACK_LINES past time_bound[1] - see that constant's comment.

    anchor_date: calendar date to attach to date-less "HH:MM:SS" timestamps
    (some Genesys logs have no date at all in the line, just time-of-day).
    Defaults to today if not given. A midnight rollover partway through the
    file (detected against the file's own starting time-of-day) bumps the
    date forward by a day from that point on.

    cancel_event: optional threading.Event, checked every
    CANCEL_CHECK_EVERY_LINES lines; raises ScanCancelled if set.
    """
    anchor_date = anchor_date or datetime.now()
    start_time = time.time()
    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = -1
    logger.debug("scan_file start: %s (size=%s bytes, time_bound=%s)",
                 path, file_size, time_bound)

    # Reference point for midnight-rollover detection, read once from the
    # file's true start (byte 0) regardless of where we later seek to.
    day_start_time = None
    try:
        with open(path, "rb") as f:
            head_chunk = f.read(16384)
        for raw_line in head_chunk.split(b"\n"):
            m = EVENT_HEADER_RE.search(raw_line.decode("utf-8", errors="replace"))
            if m:
                first_ts = parse_ts(m.group("ts"))
                if first_ts:
                    day_start_time = first_ts.time()
                    break
    except OSError:
        pass

    file_start_date = compute_file_start_date(anchor_date, day_start_time)

    calls: dict[str, CallRecord] = {}
    pending_attrs = {}
    pending_event = None
    pending_ts = None

    def flush_pending():
        nonlocal pending_attrs, pending_event, pending_ts
        in_bound = True
        if time_bound and pending_ts:
            in_bound = time_bound[0] <= pending_ts <= time_bound[1]
        if pending_event and "ConnID" in pending_attrs and in_bound:
            cid = pending_attrs["ConnID"]
            rec = calls.setdefault(cid, CallRecord(conn_id=cid))
            rec.events.append((pending_ts, pending_event))
            if pending_ts:
                if rec.first_seen is None or pending_ts < rec.first_seen:
                    rec.first_seen = pending_ts
                if rec.last_seen is None or pending_ts > rec.last_seen:
                    rec.last_seen = pending_ts
            for key, attr in (("ANI", "ani"), ("DNIS", "dnis")):
                if key in pending_attrs and not getattr(rec, attr):
                    setattr(rec, attr, pending_attrs[key])
            for key in ("Queue", "VirtualQueue", "ThisQueue"):
                if key in pending_attrs and not rec.queue:
                    rec.queue = pending_attrs[key]
            if pending_event in TERMINAL_EVENTS:
                rec.ended = True
                if pending_ts and (rec.ended_at is None or pending_ts > rec.ended_at):
                    rec.ended_at = pending_ts
            if pending_event in ROUTED_AWAY_EVENTS:
                rec.routed_away = True
                if pending_ts and (rec.routed_away_at is None or pending_ts > rec.routed_away_at):
                    rec.routed_away_at = pending_ts
        pending_attrs = {}
        pending_event = None
        pending_ts = None

    lines_read = 0
    lines_past_window = 0
    stopped_early = False
    seek_offset = 0

    if time_bound is not None:
        seek_offset = find_seek_offset(path, time_bound[0], anchor_date=anchor_date)
        if seek_offset > 0:
            logger.debug("scan_file seek-ahead: %s -> byte %d of %s (skipping data before the window)",
                         path, seek_offset, file_size)

    # Binary mode so seeking to an arbitrary byte offset (from the search
    # above) is safe - text-mode seeks are only guaranteed valid at
    # positions obtained from that same handle's tell().
    with open(path, "rb") as fb:
        if seek_offset > 0:
            fb.seek(seek_offset)
            fb.readline()  # discard the partial line we likely landed inside
        for raw_line in fb:
            line = raw_line.decode("utf-8", errors="replace")
            lines_read += 1
            if cancel_event is not None and lines_read % CANCEL_CHECK_EVERY_LINES == 0 \
                    and cancel_event.is_set():
                logger.info("scan_file cancelled: %s (after %d lines)", path, lines_read)
                raise ScanCancelled(f"Cancelled while scanning '{path}'")
            header = EVENT_HEADER_RE.search(line)
            if header:
                flush_pending()  # previous block is done
                pending_event = header.group("event")
                pending_ts = parse_ts(header.group("ts"))
                if pending_ts:
                    pending_ts = anchor_time(pending_ts, file_start_date, day_start_time)

                if time_bound and pending_ts and pending_ts > time_bound[1]:
                    lines_past_window += 1
                    if lines_past_window > EARLY_STOP_SLACK_LINES:
                        stopped_early = True
                        break
                else:
                    lines_past_window = 0  # only count *consecutive* overshoot

            found_attr = False
            for m in ATTR_RE.finditer(line):
                found_attr = True
                name = m.group("name1") or m.group("name2")
                val = m.group("val1") if m.group("name1") else m.group("val2")
                pending_attrs[name] = val

            if not header and not found_attr and debug_unmatched and line.strip():
                msg = f"[unmatched] {path}: {line.rstrip()}"
                if debug_sink:
                    debug_sink(msg)
                else:
                    print(msg, file=sys.stderr)

    flush_pending()  # harmless even after an early stop - flush_pending's own
                      # in_bound check will drop it if it's past the window

    elapsed = time.time() - start_time
    rate = lines_read / elapsed if elapsed > 0 else 0
    logger.debug(
        "scan_file done: %s (lines=%d, calls_found=%d, elapsed=%.2fs, "
        "%.0f lines/sec, stopped_early=%s)",
        path, lines_read, len(calls), elapsed, rate, stopped_early)
    if stopped_early and debug_sink:
        debug_sink(f"[info] '{path}': stopped early after {lines_read} lines "
                    f"- past the requested time window (ascending-log optimization).")

    return calls


def find_stuck_calls(vq: str, log_paths: list, threshold_sec: int = 0,
                      debug_unmatched: bool = False, debug_sink=None,
                      window: Optional[Tuple[datetime, datetime]] = None,
                      window_buffer_sec: int = 7200,
                      cancel_event: Optional[threading.Event] = None) -> list:
    """Core reusable logic: parse log_paths, return [(elapsed_sec, CallRecord), ...]
    sorted descending by elapsed time, for calls on `vq`.

    window: optional (start, end) datetimes = "call started around this
    time" +/- a tolerance, e.g. (10:15, 10:45) for "started near 10:30".
    When given, EVERY call whose start time (first_seen) falls in that
    range is returned - active or already ended - so you can eyeball which
    one is stuck by its duration/status rather than the tool silently
    filtering out calls that already resolved.
    elapsed is the call's real duration: end_time - start_time if it ended,
    or latest_seen_time - start_time if it's still active (i.e. this run's
    best approximation of "now").
    When window is omitted, behaves as a "what's stuck right now" live
    check: only calls with no terminal/routed-away event at all are
    returned, measured against the latest timestamp seen in the logs.

    window_buffer_sec: how far past the window's end to keep scanning, so a
    call that started right at the edge of the window but takes a while to
    resolve still gets its real end time (and isn't cut short). Defaults to
    2 hours; narrow this to speed up scanning huge log files if your calls
    never take that long.

    debug_sink: optional callable(str) to receive unmatched-line diagnostics
    instead of printing to stderr (used by the GUI).

    cancel_event: optional threading.Event; if set mid-scan, raises
    ScanCancelled (caller decides how to present that - the GUI shows
    "Cancelled" instead of treating it as an error).
    """
    scan_start = time.time()
    logger.info("find_stuck_calls start: vq=%r, %d file(s), threshold=%ds, window=%s",
                vq, len(log_paths), threshold_sec, window)

    time_bound = None
    if window:
        buf = timedelta(seconds=window_buffer_sec)
        time_bound = (window[0] - buf, window[1] + buf)

    # Date-less "HH:MM:SS" log lines get anchored to the search date when
    # one is known (windowed search), otherwise today - see anchor_time().
    anchor_date = window[0] if window else datetime.now()

    all_calls: dict[str, CallRecord] = {}
    for path in log_paths:
        if is_snapshot_file(path):
            msg = f"[skipped] '{path}' is a snapshot/state file, not an event log - ignoring."
            if debug_sink:
                debug_sink(msg)
            else:
                print(msg, file=sys.stderr)
            continue
        try:
            file_calls = scan_file(path, debug_unmatched, debug_sink, time_bound, cancel_event,
                                    anchor_date=anchor_date)
        except OSError as exc:
            # A single unreadable/locked file (permissions, AV lock, etc.)
            # shouldn't abort the whole scan - report it and keep going.
            msg = f"[skipped] Could not read '{path}': {exc}"
            if debug_sink:
                debug_sink(msg)
            else:
                print(msg, file=sys.stderr)
            continue
        for cid, rec in file_calls.items():
            if cid in all_calls:
                existing = all_calls[cid]
                existing.events.extend(rec.events)
                existing.ani = existing.ani or rec.ani
                existing.dnis = existing.dnis or rec.dnis
                existing.queue = existing.queue or rec.queue
                existing.ended = existing.ended or rec.ended
                existing.routed_away = existing.routed_away or rec.routed_away
                if rec.ended_at and (existing.ended_at is None or rec.ended_at > existing.ended_at):
                    existing.ended_at = rec.ended_at
                if rec.routed_away_at and (existing.routed_away_at is None or rec.routed_away_at > existing.routed_away_at):
                    existing.routed_away_at = rec.routed_away_at
                if rec.first_seen and (existing.first_seen is None or rec.first_seen < existing.first_seen):
                    existing.first_seen = rec.first_seen
                if rec.last_seen and (existing.last_seen is None or rec.last_seen > existing.last_seen):
                    existing.last_seen = rec.last_seen
            else:
                all_calls[cid] = rec

    # "now" = latest timestamp seen anywhere in the logs (works for both
    # historical analysis and a log file that's being tailed live).
    all_ts = [r.last_seen for r in all_calls.values() if r.last_seen]
    now = max(all_ts) if all_ts else datetime.utcnow()

    stuck = []
    for rec in all_calls.values():
        if rec.queue and vq.lower() not in rec.queue.lower():
            continue
        if not rec.first_seen:
            continue

        end_point = rec.effective_end
        if window:
            w_start, w_end = window
            if not (w_start <= rec.first_seen <= w_end):
                continue  # didn't start around the requested time
            rec.status = "ended" if end_point is not None else "ACTIVE"
            elapsed = ((end_point or now) - rec.first_seen).total_seconds()
        else:
            if end_point is not None:
                continue  # already resolved, not currently stuck
            rec.status = "ACTIVE"
            elapsed = (now - rec.first_seen).total_seconds()

        if elapsed >= threshold_sec:
            stuck.append((elapsed, rec))

    stuck.sort(key=lambda x: x[0], reverse=True)
    logger.info("find_stuck_calls done: %d candidate(s) in %.2fs",
                len(stuck), time.time() - scan_start)
    return stuck


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vq", required=True, help="Virtual Queue / queue DN name to filter on")
    ap.add_argument("--tserver", action="append", default=[], help="T-Server log file (repeatable)")
    ap.add_argument("--statserver", action="append", default=[], help="StatServer log file (repeatable, optional cross-check)")
    ap.add_argument("--threshold-sec", type=int, default=0,
                     help="Only show calls active longer than this many seconds (0 = show all)")
    ap.add_argument("--debug-unmatched", action="store_true",
                     help="Print log lines the parser couldn't classify, to help tune PATTERNS")
    ap.add_argument("--at", help="Call start time to search around, e.g. '10:30'. Bare HH:MM is "
                                  "anchored to --date (default: today). Finds every call that "
                                  "started near this time (active or ended), instead of only "
                                  "what's stuck right now.")
    ap.add_argument("--date", help="Calendar day --at applies to, e.g. '2026-09-02' (default: "
                                    "today). Use this to search yesterday's log instead of "
                                    "relying on the system clock's 'today' - important since a "
                                    "24h rolling log can span into yesterday.")
    ap.add_argument("--window-min", type=int, default=15,
                     help="Minutes before/after --at to search (default 15)")
    ap.add_argument("--log-file", default=DEFAULT_LOG_PATH,
                     help=f"Where to write the tool's own debug/perf log (default: {DEFAULT_LOG_PATH})")
    args = ap.parse_args()

    configure_logging(args.log_file)
    print(f"(debug log: {args.log_file})")

    if not args.tserver and not args.statserver:
        ap.error("Provide at least one --tserver and/or --statserver log file")

    window = None
    if args.at:
        anchor_day = None
        if args.date:
            try:
                anchor_day = datetime.strptime(args.date, "%Y-%m-%d")
            except ValueError:
                ap.error("--date must look like '2026-09-02'")
        try:
            at = parse_at_time(args.at, today=anchor_day)
        except ValueError as exc:
            ap.error(str(exc))
        delta = timedelta(minutes=args.window_min)
        window = (at - delta, at + delta)

    stuck = find_stuck_calls(args.vq, args.tserver + args.statserver,
                              args.threshold_sec, args.debug_unmatched, window=window)

    if not stuck:
        print(f"No calls found on VQ '{args.vq}' matching the search "
              f"(above {args.threshold_sec}s threshold).")
        return

    print(f"{'ConnID':<20} {'Elapsed':>10}  {'Status':<7} {'ANI':<15} {'DNIS':<12} {'Queue'}")
    print("-" * 90)
    for elapsed, rec in stuck:
        mins, secs = divmod(int(elapsed), 60)
        print(f"{rec.conn_id:<20} {mins:>3}m{secs:02d}s   {rec.status:<7} {rec.ani or '-':<15} "
              f"{rec.dnis or '-':<12} {rec.queue or '-'}")

    print(f"\n{len(stuck)} candidate call(s) on '{args.vq}'. "
          f"Top row = longest duration = most likely your stuck call.")


if __name__ == "__main__":
    main()
