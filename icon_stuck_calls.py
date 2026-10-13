#!/usr/bin/env python3
"""
ICON stuck-call finder - active calls in a Virtual Queue, straight from the
Genesys Interaction Concentrator (ICON) database.

WHY THIS INSTEAD OF PARSING LOGS
--------------------------------
Log scanning answers "what was happening" by re-reading files every time -
O(all files) per refresh, always slightly stale, and fragile against log
format changes. ICON already stores call detail in an indexed database, so
"which calls are still in this virtual queue right now" is a single cheap
query. Refresh becomes O(open calls), which is what makes real-time
polling actually viable.

NO-DEPENDENCY DB ACCESS
-----------------------
pyodbc/pymssql can't be installed on a locked-down box, so this shells out
to PowerShell and uses System.Data.SqlClient, which ships with the .NET
Framework present on every Windows machine. Nothing to install.

The connection string and the queue name are passed via ENVIRONMENT
VARIABLES, not interpolated into the script text - that keeps the password
out of the process command line, and the queue name is bound as a real SQL
parameter (@queue), so a queue name can't inject SQL.

SCHEMA (Icon_voice, confirmed via "Show ICON Tables" + live diagnostics)
-----------------------------------------------------------------------
G_VIRTUAL_QUEUE: NOT a "currently waiting" table at this site. Every row
closes 1-2 seconds after it opens (STATUS=13 Diverted, with a TARGETDN).
The VQ is a routing waypoint - a call that waits N minutes bounces through
it dozens of times on RONA, one short row each. VQDN is the queue name,
ORIGCALLID links to G_CALL(_ACTIVE).CALLID.

G_PARTY: one row per party on a call. TERMINATED IS NULL = the party is
live. AGENTID > 0 identifies an agent party. There is NEVER an open party
on a virtual-queue DN here, so G_PARTY can't tell you "in VQ X" either -
but it CAN tell you whether a call is currently talking to an agent.

G_CALL_ACTIVE: live-only mirror of G_CALL - CONNID, CALLANI, CALLDNIS,
CREATED (UTC). TERMINATED empty while the call is up.

Conclusion: ICON does not persist "who is in VQ X right now" - only
StatServer (CCPulse) has that. DEFAULT_QUERY instead finds the calls that
matter: active, touched this VQ, entered more than 5s ago, and NOT
currently connected to an agent = genuinely stuck.
"""

import csv
import io
import os
import subprocess
import threading
import tkinter as tk
from configparser import ConfigParser
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

INI_FILENAME = "icon_stuck_calls.ini"
PS_TIMEOUT_SEC = 90

# Clear-call targets, mirroring clearCall_BY_ID.bat - same 4 hosts/ports,
# same "gstuckcalls.exe -a clc -c <ConnID>" call. gstuckcalls.exe is
# expected to be on PATH, same as in that .bat (no path given there either).
CLEAR_CALL_TARGETS = [
    ("ntas106969a9n", "3002"),
    ("ntas106960a9n", "3002"),
    ("ntas106969a3a", "3001"),
    ("ntas106960a3a", "3001"),
]
CLEAR_CALL_TIMEOUT_SEC = 15


def clear_call(conn_id):
    """Run gstuckcalls.exe -a clc (clear call) against every target host.
    Returns a list of (host:port, ok, output) - one per target, so a
    partial failure (one TServer down) is still visible per-host."""
    results = []
    for host, port in CLEAR_CALL_TARGETS:
        cmd = ["gstuckcalls.exe", "-h", host, "-p", port, "-a", "clc", "-c", conn_id]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=CLEAR_CALL_TIMEOUT_SEC,
                                   # don't let gstuckcalls.exe flash its own
                                   # console window - main window stays put
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            ok = proc.returncode == 0
            output = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            ok = False
            output = str(exc)
        results.append((f"{host}:{port}", ok, output.strip()))
    return results

# "Stuck around this virtual queue" query - v3, keyed off VQ_Name.
#
# v1 keyed off G_VIRTUAL_QUEUE: wrong, because a call only gets a row there
# once the strategy DIVERTS it somewhere. A call still waiting for its
# first routing decision (or one queued at a Routing Point shared by many
# named queues, e.g. every mortgage call funnels through one RP DN) has NO
# G_VIRTUAL_QUEUE row and was invisible - confirmed live: a call CCPulse
# showed waiting 5+ min had zero G_VIRTUAL_QUEUE rows at all.
#
# v3 keys off G_USERDATA_HISTORY, KEYNAME='VQ_Name': the IVR/strategy
# stamps this on the call as soon as it decides where the call is headed -
# before any RP/queue activity, and it's specific even when many calls
# share one RP DN. Confirmed live via a call's full G_USERDATA_HISTORY.
# G_USERDATA_HISTORY is a full history table (huge) - NOT indexed for a
# broad scan (a query joining it against ALL active calls timed out twice,
# even bounded to 2h). So this finds the small set of already-waiting
# calls FIRST (cheap, G_PARTY-based, proven fast), then looks up VQ_Name
# only for that handful via a small IN-list - not a broad scan.
#
# 'Queue_Status' (also in G_USERDATA_HISTORY) looked promising as a direct
# "is this call waiting" flag, but it's only set by some strategy families
# (MALMASH_*, BM_Team_*, GPB_INB_*) - blank for others (LeumiCall_*) that
# still matter. So "waiting" is still determined the same way as before:
# open G_PARTY rows on the call. STATE (G_DICTIONARY type 8): 2=alerting,
# 3=connected, 4=hold, 5=queued. On this switch a call WAITING for a
# reserved target agent shows STATE 4 (confirmed against CCPulse's count).
#   in acd queue  - raw ACD queue, no target chosen yet (ENDPOINTTYPE 4)
#   waiting       - target agent reserved, call held at switch (STATE 4)
#   ringing agent - alerting an agent who isn't picking up (STATE 2)
#   no agent leg  - in IVR/treatment/limbo with no agent leg at all
# Calls with an open agent party in STATE 3 (connected/talking) are
# excluded entirely - a connected call isn't "waiting in the queue".
#
# WaitingSec = time since the call was FIRST tagged with this VQ_Name.
# InStateSec = age of the current wait leg - how long in the CURRENT status.
# CREATED/ADDED columns are UTC. AT TIME ZONE needs SQL Server 2016+; on an
# older server replace it with DATEADD(hour, 3, ...).
#
# Scoped to only 'in acd queue' and 'no agent leg' - calls that have NOT
# reached an agent at all yet (formally queued, or in some other pre-agent
# state like IVR/treatment). 'waiting' (target reserved, STATE 4) and
# 'ringing agent' (STATE 2) are deliberately excluded here by request -
# those calls DO have an agent leg already, just not connected yet.
DEFAULT_QUERY = """\
WITH Parties AS (
    SELECT p.CALLID,
        MAX(CASE WHEN p.AGENTID > 0 THEN p.AGENTID END)        AS AgentID,
        MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END)          AS AgentState,
        MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END)    AS InAcdQueue,
        MAX(CASE WHEN p.AGENTID > 0 OR p.ENDPOINTTYPE = 4
                 THEN p.CREATED END)                           AS WaitLegCreated,
        -- DN the call is currently parked at with no agent attached - e.g.
        -- a Virtual Routing Point's ENDPOINTDN is often a full readable
        -- name (confirmed live: "LeumiCall_..._VRP", ENDPOINTTYPE=6, not
        -- the ACD-queue type 4). Free to add: same G_PARTY rows already
        -- being aggregated above, no extra scan.
        MAX(CASE WHEN p.AGENTID IS NULL OR p.AGENTID = 0
                 THEN p.ENDPOINTDN END)                        AS QueueEndpointDN
    FROM G_PARTY p WITH (NOLOCK)
    WHERE p.TERMINATED IS NULL
    GROUP BY p.CALLID
),
-- cheap first: the small set of active calls that are in the ACD queue or
-- have no agent leg at all right now (i.e. haven't reached an agent).
-- CallCreated carried through so VQFirst/ConnFromHistory can each bound
-- their G_USERDATA_HISTORY lookup to a tight window near call start,
-- instead of that call's entire accumulated history.
Candidates AS (
    SELECT c.CALLID, c.CALLANI, c.CALLDNIS, c.CREATED AS CallCreated, pt.AgentID,
           pt.AgentState, pt.InAcdQueue, pt.WaitLegCreated, pt.QueueEndpointDN
    FROM G_CALL_ACTIVE c
    JOIN Parties pt ON pt.CALLID = c.CALLID
    WHERE pt.InAcdQueue = 1 OR pt.AgentState IS NULL
),
-- small IN-list lookup against the (huge, unindexed-for-broad-scan)
-- history table - not a scan of every active call's history.
--
-- Matches on VALUE alone (no KEYNAME filter). Vector strategies stamp
-- KEYNAME='VQ_Name' as soon as they decide where the call is headed, so
-- typing the VQ name matches those immediately.
--
-- ORS Business Processes at this site don't attach an equivalent VQ_Name -
-- confirmed live: a stuck ORS call had zero rows anywhere in
-- G_USERDATA_HISTORY whose value was its VQ name, and no way to derive
-- that name from ICON for a call still on its first wait cycle (tried and
-- ruled out: G_ROUTE_RES_VQ_HIST -> G_VIRTUAL_QUEUE only resolves after a
-- divert; VQ-name-suffix conventions don't hold site-wide). What ORS DOES
-- attach immediately is KEYNAME='RP' (the routing point/DN number, e.g.
-- 646099) - so for an ORS-routed queue, search by that RP number instead
-- of the VQ name. Dropping KEYNAME lets the same @queue parameter match
-- either a VQ_Name value (vector calls) or an RP value (ORS calls).
--
-- WITH (NOLOCK): under load this query got MUCH slower and eventually
-- deadlocked with no table hints - WITH (NOLOCK), tested directly in
-- SSMS, helped but wasn't enough on its own.
--
-- The real cost driver, confirmed via SET STATISTICS IO: a per-CALLID
-- lookup here costs roughly 2000 logical reads - fine for a handful of
-- candidates, but with ~500+ calls active site-wide (a busy period) that
-- adds up to 1M+ reads, fast only when already cache-warm. An open-ended
-- ADDED>=CallCreated bound (tried earlier) didn't help because it still
-- includes a long-stuck call's ENTIRE accumulated reroute history - and
-- a long-stuck call is exactly what this app is looking for. VQ_Name/RP
-- is attached right when a routing decision is made ("as soon as it
-- decides where the call is headed, before any RP/queue activity"), so a
-- tight window works IF anchored to the right moment.
--
-- Anchored to WaitLegCreated (start of the CURRENT agent-reserved/ACD-
-- queue leg), falling back to CallCreated only when there isn't one yet
-- (still in IVR/treatment, pre-first-queue) - NOT plain CallCreated alone.
-- A call can be routed more than once in its life (customer -> agent ->
-- agent transfers to a different queue -> stuck there): each transfer is
-- its own routing decision with its own fresh VQ_Name/RP attach at that
-- moment, not necessarily anywhere near the call's original start.
-- WaitLegCreated updates to reflect exactly that moment for the call's
-- CURRENT wait cycle, so anchoring there (instead of CallCreated) still
-- finds a post-transfer attach even if it happened hours into the call.
VQFirst AS (
    SELECT h.CALLID, MIN(h.ADDED) AS FirstTagged
    FROM G_USERDATA_HISTORY h WITH (NOLOCK)
    JOIN Candidates cand ON cand.CALLID = h.CALLID
    WHERE h.VALUE = @queue
      AND h.ADDED >= COALESCE(cand.WaitLegCreated, cand.CallCreated)
      AND h.ADDED < DATEADD(MINUTE, 10, COALESCE(cand.WaitLegCreated, cand.CallCreated))
    GROUP BY h.CALLID
),
-- The final result set - matched either by VQFirst (attached data) or by
-- QueueEndpointDN (the DN it's parked at, e.g. a VRP). Usually a handful
-- of calls, specific to this one @queue - much smaller than all of
-- Candidates (every waiting call site-wide). Named here so ConnFromHistory
-- below can scope its lookup to just these, not the whole site.
MatchedCalls AS (
    SELECT cand.CALLID
    FROM Candidates cand
    LEFT JOIN VQFirst vf ON vf.CALLID = cand.CALLID
    WHERE vf.CALLID IS NOT NULL OR cand.QueueEndpointDN = @queue
),
-- ConnID comes from G_USERDATA_HISTORY (KEYNAME='ConnectionID') rather than
-- G_CALL_ACTIVE.CONNID. Scoped to MatchedCalls (not all of Candidates) -
-- KEYNAME='ConnectionID' is a near-universal, unselective predicate (every
-- call has one), so checking it against the small already-matched set
-- instead of every waiting call site-wide keeps the worst case small on a
-- busy day.
ConnFromHistory AS (
    SELECT h.CALLID, MIN(h.VALUE) AS ConnID
    FROM G_USERDATA_HISTORY h WITH (NOLOCK)
    WHERE h.CALLID IN (SELECT CALLID FROM MatchedCalls)
      AND h.KEYNAME = 'ConnectionID'
    GROUP BY h.CALLID
)
SELECT TOP (500)
    cand.CALLID                                AS CallID,
    conn.ConnID                                AS ConnID,
    cand.CALLANI                                AS ANI,
    @queue                                     AS VirtualQueue,
    CAST(COALESCE(vf.FirstTagged, cand.WaitLegCreated) AT TIME ZONE 'UTC'
                        AT TIME ZONE 'Israel Standard Time' AS datetime2(0)) AS QueueEntered,
    DATEDIFF(second, COALESCE(vf.FirstTagged, cand.WaitLegCreated), GETUTCDATE()) AS WaitingSec,
    DATEDIFF(second, cand.WaitLegCreated, GETUTCDATE()) AS InStateSec,
    cand.AgentID                               AS AgentID,
    CASE
        WHEN cand.InAcdQueue = 1        THEN 'in acd queue'
        ELSE 'no agent leg'
    END                                        AS Status
FROM Candidates cand
LEFT JOIN VQFirst vf ON vf.CALLID = cand.CALLID
LEFT JOIN ConnFromHistory conn ON conn.CALLID = cand.CALLID
WHERE vf.CALLID IS NOT NULL OR cand.QueueEndpointDN = @queue
ORDER BY COALESCE(vf.FirstTagged, cand.WaitLegCreated) ASC
OPTION (RECOMPILE)
"""

TABLES_QUERY = """\
SELECT t.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
FROM INFORMATION_SCHEMA.TABLES t
JOIN INFORMATION_SCHEMA.COLUMNS c ON c.TABLE_NAME = t.TABLE_NAME
WHERE t.TABLE_NAME LIKE 'G[_]%' OR t.TABLE_NAME LIKE 'GC[_]%'
   OR t.TABLE_NAME LIKE 'GX[_]%'
ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION
"""

# Runs a query via .NET's SqlClient and emits CSV on stdout. Connection
# string and SQL come from env vars (see docstring).
#
# A persistent-process variant (one PowerShell process + one open SQL
# connection reused across refreshes, instead of a fresh process+connection
# every time) was tried here for speed, but Popen with a redirected stdin
# fails with WinError 5 (Access is denied) in this locked-down environment -
# something here blocks process creation with a redirected stdin handle
# specifically. Reverted; this one-shot-per-query approach is what works.
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$connectMs = -1
$queryMs = -1
try {
    $conn = New-Object System.Data.SqlClient.SqlConnection $env:ICON_CONNSTR
    $swConnect = [System.Diagnostics.Stopwatch]::StartNew()
    $conn.Open()
    $swConnect.Stop()
    $connectMs = $swConnect.ElapsedMilliseconds
    $cmd = $conn.CreateCommand()
    $cmd.CommandTimeout = 75
    $cmd.CommandText = $env:ICON_SQL
    if ($env:ICON_QUEUE) {
        $null = $cmd.Parameters.AddWithValue('@queue', $env:ICON_QUEUE)
    }
    $da = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
    $dt = New-Object System.Data.DataTable
    $swQuery = [System.Diagnostics.Stopwatch]::StartNew()
    $null = $da.Fill($dt)
    $swQuery.Stop()
    $queryMs = $swQuery.ElapsedMilliseconds
    # Timing goes to stderr, never stdout - stdout is reserved for the CSV
    # result and must stay parseable on its own.
    [Console]::Error.WriteLine("TIMING connect=${connectMs}ms query=${queryMs}ms")
    $dt | ConvertTo-Csv -NoTypeInformation
    $conn.Close()
} catch {
    # Emit whatever timing we got before the failure (e.g. connect succeeded
    # but the query itself timed out) so a slow run is diagnosable, not just
    # a bare error. Write-Error would decorate the message with the whole
    # script body, burying the actual SQL/connection error, so use WriteLine.
    [Console]::Error.WriteLine("TIMING connect=${connectMs}ms query=${queryMs}ms")
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""

def run_query(conn_str, sql, queue=None, timeout=PS_TIMEOUT_SEC):
    """Execute sql via PowerShell/SqlClient. Returns (headers, rows, timing).
    timing is a "connect=Xms query=Yms" string, or "" if unavailable.
    Raises RuntimeError with the server's message (and any timing captured
    before the failure) on failure."""
    env = dict(os.environ)
    env["ICON_CONNSTR"] = conn_str
    # Read-only reporting against a busy OLTP database - don't take shared
    # locks (and don't become a deadlock victim, as the "ever visited"
    # diagnostic did).
    #
    # SET ARITHABORT ON matches SSMS's default (SqlClient/sqlcmd both
    # default it OFF) - SQL Server caches a separate plan per distinct SET
    # options combination, and for a heavily-skewed VQ value this
    # difference alone was confirmed live to be the gap between SSMS
    # finishing in ~10-27s and this app timing out at 75s on the identical
    # query text.
    env["ICON_SQL"] = ("SET ARITHABORT ON;\n"
                        "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;\n") + sql
    env["ICON_QUEUE"] = queue or ""

    proc = subprocess.run(
        ["powershell", "-NonInteractive", "-NoProfile", "-Command", PS_SCRIPT],
        capture_output=True, text=True, env=env, timeout=timeout)

    stderr_lines = (proc.stderr or "").splitlines()
    timing = next((l for l in stderr_lines if l.startswith("TIMING ")), "")
    error_lines = [l for l in stderr_lines if not l.startswith("TIMING ")]

    if proc.returncode != 0:
        msg = "\n".join(error_lines) or proc.stdout or "unknown error"
        if timing:
            msg = f"{msg.strip()} ({timing})"
        raise RuntimeError(msg.strip())

    reader = csv.reader(io.StringIO(proc.stdout))
    rows = [r for r in reader if r]
    if not rows:
        return [], [], timing
    return rows[0], rows[1:], timing


def parse_duration(text):
    """Accepts plain seconds ('90'), MM:SS, or HH:MM:SS. Empty -> 0."""
    text = text.strip()
    if not text:
        return 0
    parts = text.split(":")
    if len(parts) == 1:
        return int(parts[0])
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + int(s)
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + int(s)
    raise ValueError(f"Can't parse duration: {text!r} (use HH:MM:SS)")


class IconStuckCallApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ICON Stuck Call Finder - active calls in a Virtual Queue")
        self.geometry("1180x720")

        self.query = DEFAULT_QUERY
        self._busy = False
        self._refresh_job = None

        self._build_ui()
        self._load_ini()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 5, "pady": 3}

        conn_frame = ttk.LabelFrame(self, text="ICON database")
        conn_frame.pack(fill="x", **pad)

        ttk.Label(conn_frame, text="Server:").grid(row=0, column=0, sticky="w", padx=4)
        self.server_entry = ttk.Entry(conn_frame, width=28)
        self.server_entry.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(conn_frame, text="Database:").grid(row=0, column=2, sticky="w", padx=4)
        self.db_entry = ttk.Entry(conn_frame, width=20)
        self.db_entry.grid(row=0, column=3, sticky="w", padx=4)

        self.trusted_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(conn_frame, text="Windows auth", variable=self.trusted_var,
                         command=self._toggle_auth).grid(row=0, column=4, padx=8)

        ttk.Label(conn_frame, text="User:").grid(row=1, column=0, sticky="w", padx=4)
        self.user_entry = ttk.Entry(conn_frame, width=28, state="disabled")
        self.user_entry.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(conn_frame, text="Password:").grid(row=1, column=2, sticky="w", padx=4)
        self.pw_entry = ttk.Entry(conn_frame, width=20, show="*", state="disabled")
        self.pw_entry.grid(row=1, column=3, sticky="w", padx=4)

        tk.Button(conn_frame, text="Test Connection",
                  command=self.test_connection).grid(row=1, column=4, padx=8, pady=2)
        tk.Button(conn_frame, text="Show ICON Tables",
                  command=self.show_tables).grid(row=1, column=5, padx=4, pady=2)

        search = ttk.Frame(self)
        search.pack(fill="x", **pad)
        ttk.Label(search, text="Virtual Queue:").pack(side="left")
        self.queue_entry = ttk.Entry(search, width=32)
        self.queue_entry.pack(side="left", padx=(4, 12))

        ttk.Label(search, text="Min waiting (HH:MM:SS):").pack(side="left")
        self.threshold_entry = ttk.Entry(search, width=9)
        self.threshold_entry.insert(0, "0:00:00")
        self.threshold_entry.pack(side="left", padx=(4, 12))

        ttk.Label(search, text="Status:").pack(side="left")
        self.status_filter_var = tk.StringVar(value="All")
        self.status_filter_var.trace_add("write", lambda *a: self._rerender())
        ttk.Combobox(search, textvariable=self.status_filter_var, state="readonly", width=14,
                     values=["All", "in acd queue", "no agent leg"]
                     ).pack(side="left", padx=(4, 12))

        self.refresh_btn = tk.Button(search, text="Refresh Now",
                                      command=self.refresh, bg="#d0f0d0")
        self.refresh_btn.pack(side="left", padx=3)

        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search, text="Auto-refresh every", variable=self.auto_var,
                         command=self._toggle_auto).pack(side="left", padx=(12, 2))
        self.interval_entry = ttk.Entry(search, width=5)
        self.interval_entry.insert(0, "15")
        self.interval_entry.pack(side="left")
        ttk.Label(search, text="sec").pack(side="left", padx=(2, 0))

        self.status_label = ttk.Label(self, text="", foreground="blue")
        self.status_label.pack(fill="x", padx=8)

        res_frame = ttk.LabelFrame(self, text="Active calls in queue (click a heading to sort)")
        res_frame.pack(fill="both", expand=True, **pad)
        ttk.Label(res_frame, foreground="gray",
                  text="Click any cell to copy its value  ·  "
                       "select rows + Ctrl+C to copy them all"
                  ).pack(anchor="e", padx=4, pady=(2, 0))
        self.cols = ("waiting", "instate", "status", "agent", "entered",
                     "connid", "ani", "queue")
        headings = {"waiting": "Total in VQ", "instate": "In state",
                    "status": "Status", "agent": "Agent",
                    "entered": "Queue Entered", "connid": "ConnID",
                    "ani": "ANI", "queue": "Virtual Queue"}
        widths = {"waiting": 90, "instate": 80, "status": 100, "agent": 70,
                  "entered": 150, "connid": 170, "ani": 120, "queue": 160}
        self.tree = ttk.Treeview(res_frame, columns=self.cols, show="headings",
                                  selectmode="extended")
        for col in self.cols:
            self.tree.heading(col, text=headings[col],
                              command=lambda c=col: self._sort(c, False))
            self.tree.column(col, width=widths[col])
        self.tree.tag_configure("top_hit", background="#ffe0b2")
        self.tree.bind("<Button-1>", self._copy_cell_on_click)
        self.tree.bind("<Control-c>", self._copy_selected_rows)
        self.tree.bind("<Double-1>", self._on_row_double_click)
        self.tree.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="x", **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=9)
        self.log_text.pack(fill="both", expand=True)

    def _toggle_auth(self):
        state = "disabled" if self.trusted_var.get() else "normal"
        self.user_entry.config(state=state)
        self.pw_entry.config(state=state)

    def _sort(self, col, reverse):
        def key(item):
            v = self.tree.set(item, col)
            try:
                return (0, float(v))       # numeric columns sort numerically
            except ValueError:
                return (1, v.lower())
        items = sorted(self.tree.get_children(""), key=key, reverse=reverse)
        for i, item in enumerate(items):
            self.tree.move(item, "", i)
        self.tree.heading(col, command=lambda: self._sort(col, not reverse))

    def log(self, msg):
        self.log_text.insert(tk.END, msg)
        self.log_text.see(tk.END)

    # -------------------------------------------------------------- config
    def _load_ini(self):
        if not os.path.exists(INI_FILENAME):
            self.log(f"[i] No {INI_FILENAME} yet - fill in the connection details "
                     f"and they'll be saved (password is NOT stored).\n")
            return
        cfg = ConfigParser()
        cfg.read(INI_FILENAME)
        db = cfg["db"] if "db" in cfg else {}
        for entry, key in ((self.server_entry, "server"),
                            (self.db_entry, "database"),
                            (self.user_entry, "user")):
            if db.get(key):
                entry.delete(0, tk.END)
                entry.insert(0, db.get(key))
        self.trusted_var.set(db.get("trusted", "yes").lower() in ("yes", "true", "1"))
        self._toggle_auth()
        if cfg.has_option("search", "queue"):
            self.queue_entry.insert(0, cfg["search"]["queue"])

    def _save_ini(self):
        cfg = ConfigParser()
        cfg["db"] = {
            "server": self.server_entry.get().strip(),
            "database": self.db_entry.get().strip(),
            "user": self.user_entry.get().strip(),
            "trusted": "yes" if self.trusted_var.get() else "no",
        }
        cfg["search"] = {"queue": self.queue_entry.get().strip()}
        try:
            with open(INI_FILENAME, "w") as f:
                cfg.write(f)
        except OSError as exc:
            self.log(f"[!] Could not save INI: {exc}\n")

    def _conn_str(self):
        server = self.server_entry.get().strip()
        database = self.db_entry.get().strip()
        if not server or not database:
            raise ValueError("Server and Database are required.")
        base = f"Server={server};Database={database};"
        if self.trusted_var.get():
            return base + "Integrated Security=SSPI;"
        user = self.user_entry.get().strip()
        pw = self.pw_entry.get()
        if not user:
            raise ValueError("User is required when not using Windows auth.")
        return base + f"User ID={user};Password={pw};"

    # --------------------------------------------------------------- actions
    def _run_bg(self, fn):
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.config(state="disabled")

        def wrapper():
            try:
                fn()
            finally:
                self.after(0, self._done)
        threading.Thread(target=wrapper, daemon=True).start()

    def _done(self):
        self._busy = False
        self.refresh_btn.config(state="normal")

    def test_connection(self):
        try:
            conn_str = self._conn_str()
        except ValueError as exc:
            messagebox.showerror("Missing details", str(exc))
            return
        self.status_label.config(text="Testing connection...")

        def work():
            try:
                headers, rows, _timing = run_query(
                    conn_str, "SELECT @@VERSION AS Version, DB_NAME() AS CurrentDb")
                msg = " | ".join(rows[0]) if rows else "(no rows)"
                self.after(0, lambda: self.log(f"[OK] Connected: {msg}\n"))
                self.after(0, lambda: self.status_label.config(text="Connection OK"))
                self.after(0, self._save_ini)
            except Exception as exc:
                self.after(0, lambda: self.log(f"[!] Connection failed: {exc}\n"))
                self.after(0, lambda: self.status_label.config(text="Connection failed"))
        self._run_bg(work)

    def show_tables(self):
        """List ICON-ish tables and columns so the default query can be
        validated/corrected against this site's actual schema."""
        try:
            conn_str = self._conn_str()
        except ValueError as exc:
            messagebox.showerror("Missing details", str(exc))
            return
        self.status_label.config(text="Reading schema...")

        def work():
            try:
                headers, rows, _timing = run_query(conn_str, TABLES_QUERY)
                by_table = {}
                for r in rows:
                    by_table.setdefault(r[0], []).append(f"{r[1]} ({r[2]})")
                self.after(0, lambda: self.log(
                    f"\n--- {len(by_table)} ICON-like table(s) ---\n"))
                for table, cols in by_table.items():
                    self.after(0, lambda t=table, c=cols: self.log(
                        f"{t}: {', '.join(c)}\n"))
                self.after(0, lambda: self.status_label.config(text="Schema listed"))
            except Exception as exc:
                self.after(0, lambda: self.log(f"[!] Schema query failed: {exc}\n"))
                self.after(0, lambda: self.status_label.config(text="Schema query failed"))
        self._run_bg(work)

    def refresh(self):
        queue = self.queue_entry.get().strip()
        if not queue:
            messagebox.showerror("Missing queue", "Enter the Virtual Queue name.")
            return
        try:
            conn_str = self._conn_str()
            parse_duration(self.threshold_entry.get().strip())  # validate early
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        self.status_label.config(text="Querying ICON...")

        def work():
            started = datetime.now()
            try:
                headers, rows, timing = run_query(conn_str, self.query, queue=queue)
                took = (datetime.now() - started).total_seconds()
                if timing:
                    self.after(0, lambda: self.log(
                        f"    ({timing}, total incl. PowerShell startup {took:.2f}s)\n"))
                self.after(0, lambda: self._show(headers, rows, took))
            except Exception as exc:
                self.after(0, lambda: self.log(f"[!] Query failed: {exc}\n"))
                self.after(0, lambda: self.status_label.config(text="Query failed"))
        self._run_bg(work)

    def _show(self, headers, rows, took):
        """Cache the raw query result, then render it (threshold/status
        filters are applied at render time via _rerender so changing them
        doesn't need a fresh query)."""
        self._last_headers = headers
        self._last_rows = rows
        self._last_took = took
        self._rerender()

    def _rerender(self):
        if not hasattr(self, "_last_headers"):
            return
        headers, rows, took = self._last_headers, self._last_rows, self._last_took
        try:
            threshold = parse_duration(self.threshold_entry.get().strip())
        except ValueError:
            threshold = 0
        status_filter = self.status_filter_var.get()

        self.tree.delete(*self.tree.get_children())
        idx = {h.lower(): i for i, h in enumerate(headers)}

        def col(row, name):
            i = idx.get(name.lower())
            return row[i] if i is not None and i < len(row) else ""

        def fmt(secs_str):
            try:
                s = int(float(secs_str))
            except ValueError:
                return ""
            m, s = divmod(s, 60)
            return f"{m}m{s:02d}s"

        shown = 0
        for row in rows:
            try:
                waiting = int(float(col(row, "WaitingSec") or 0))
            except ValueError:
                waiting = 0
            if waiting < threshold:
                continue
            status = col(row, "Status")
            if status_filter != "All" and status != status_filter:
                continue
            mins, secs = divmod(waiting, 60)
            values = (f"{mins}m{secs:02d}s", fmt(col(row, "InStateSec")),
                      status, col(row, "AgentID"),
                      col(row, "QueueEntered"), col(row, "ConnID"),
                      col(row, "ANI"), col(row, "VirtualQueue"))
            self.tree.insert("", "end", values=values, tags=("top_hit",))
            shown += 1

        stamp = datetime.now().strftime("%H:%M:%S")
        self.status_label.config(
            text=f"{shown} active call(s) - refreshed {stamp} ({took:.2f}s)")
        self.log(f"[{stamp}] {len(rows)} row(s) from ICON, {shown} shown "
                 f"after filters ({took:.2f}s)\n")
        self._save_ini()

    def _copy_cell_on_click(self, event):
        """Click any cell -> copy just that value (e.g. one ConnID)."""
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # '#1', '#2', ...
        if not row_id or not col_id:
            return
        try:
            col_name = self.cols[int(col_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return
        value = self.tree.set(row_id, col_name)
        self.clipboard_clear()
        self.clipboard_append(value)
        self.status_label.config(text=f"Copied: {value}")

    def _copy_selected_rows(self, event=None):
        """Ctrl+C with rows selected -> copy them all, tab-separated,
        header row included."""
        items = self.tree.selection()
        if not items:
            return
        headings = [self.tree.heading(c)["text"] for c in self.cols]
        lines = ["\t".join(headings)]
        for item in items:
            lines.append("\t".join(str(self.tree.set(item, c)) for c in self.cols))
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_label.config(text=f"Copied {len(items)} row(s) to clipboard.")

    def _on_row_double_click(self, event):
        """Double-click a row -> confirm -> clear that call (gstuckcalls.exe
        -a clc) on every target host."""
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        conn_id = self.tree.set(row_id, "connid").strip()
        if not conn_id:
            return
        if not messagebox.askyesno(
                "Clear call",
                f"Are you sure you want to clear this call?\n\nConnID: {conn_id}"):
            return

        self.status_label.config(text=f"Clearing {conn_id}...")

        def work():
            results = clear_call(conn_id)

            def report():
                self.log(f"\n[Clear] ConnID {conn_id}\n")
                all_ok = True
                for target, ok, output in results:
                    self.log(f"  [{'OK' if ok else 'FAIL'}] {target}"
                             f"{': ' + output if output else ''}\n")
                    all_ok = all_ok and ok
                self.status_label.config(
                    text=f"Clear {'succeeded' if all_ok else 'had failures'} "
                         f"for {conn_id} - see Log")
            self.after(0, report)
        self._run_bg(work)

    # ---------------------------------------------------------- auto refresh
    def _toggle_auto(self):
        if self.auto_var.get():
            self._schedule_auto()
        elif self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None

    def _schedule_auto(self):
        try:
            interval = max(5, int(self.interval_entry.get().strip() or "15"))
        except ValueError:
            interval = 15
        self._refresh_job = self.after(interval * 1000, self._auto_tick)

    def _auto_tick(self):
        if not self.auto_var.get():
            return
        if not self._busy and self.queue_entry.get().strip():
            self.refresh()
        self._schedule_auto()


if __name__ == "__main__":
    IconStuckCallApp().mainloop()
