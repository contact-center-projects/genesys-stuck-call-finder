#!/usr/bin/env python3
"""
ICON stuck-call finder - sqlcmd variant.

EXPERIMENTAL - see icon_stuck_calls.py for the proven, working version.
This file exists ONLY to test whether sqlcmd.exe is faster than shelling
out to PowerShell + System.Data.SqlClient (confirmed ~6.5s of every ~12s
refresh in the PowerShell version is process/CLR startup overhead, not SQL
time - see the TIMING log lines that version prints). sqlcmd skips loading
the .NET CLR entirely, so it MIGHT cut that overhead - unconfirmed, needs
testing on the actual target machine.

WHAT'S DIFFERENT FROM icon_stuck_calls.py (read before trusting this file)
---------------------------------------------------------------------
1. NO REAL PARAMETER BINDING. The PowerShell version binds @queue via
   SqlParameter - structurally injection-proof, the queue text can never
   be interpreted as SQL regardless of content. sqlcmd has no equivalent,
   so here @queue is built into the query text as an escaped string
   literal (DECLARE @queue nvarchar(200) = N'<escaped>';). Escaping only
   doubles embedded single quotes - the standard T-SQL literal defense,
   but a manual one, not a structural guarantee. Do not point this at a
   search box any less trusted than "whatever a contact-center analyst
   types as a VQ name" without re-examining this.
2. Output parsing is homemade. sqlcmd's text output is column-separated
   with "-s" here, not proper CSV - had to pick a separator character
   (see SQLCMD_COL_SEP below) and assume it never appears in real data
   (ConnID hex strings, ANI/DNIS digits, dates, ints - should be safe,
   but not guaranteed at every site). NULLs come back as the literal text
   "NULL" and are translated to "" here to match the PowerShell version's
   blank-cell behavior.
3. No connect/query timing split. The PowerShell version's stopwatch
   instrumentation (TIMING connect=Xms query=Yms) has no sqlcmd
   equivalent without extra plumbing - this file only reports total
   elapsed time, same as the PowerShell version's outer "took" figure.
4. Untested by the author. sqlcmd's column-width truncation and NULL
   rendering can vary by version/driver (this box has the ODBC 17.0
   client SDK's sqlcmd, confirmed via `where sqlcmd`). The -y 0 -Y 0
   flags are meant to disable truncation entirely - if long values
   (ConnID, ENDPOINTDN) come back cut short, that's the first thing to
   check.

Everything else (UI, clear_call, the actual DEFAULT_QUERY logic) is
unchanged from icon_stuck_calls.py - only run_query and its callers differ.
"""

import csv
import io
import os
import re
import subprocess
import threading
import tkinter as tk
from configparser import ConfigParser
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

INI_FILENAME = "icon_stuck_calls.ini"
SQLCMD_TIMEOUT_SEC = 90
SQLCMD_QUERY_TIMEOUT_SEC = 75
# Column separator for sqlcmd's -s flag. Must not appear in any real
# column value - ConnID/ANI/DNIS are digits/hex, dates and ints are safe,
# but ENDPOINTDN/VirtualQueue text values are worth a second look if this
# ever breaks on a site with unusual naming.
SQLCMD_COL_SEP = "|"

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
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            ok = proc.returncode == 0
            output = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            ok = False
            output = str(exc)
        results.append((f"{host}:{port}", ok, output.strip()))
    return results


# Unchanged from icon_stuck_calls.py - see that file's comments above this
# query for the full history of why it's shaped this way (VQFirst,
# QueueEndpointDN, ConnFromHistory scoping, etc).
DEFAULT_QUERY = """\
WITH Parties AS (
    SELECT p.CALLID,
        MAX(CASE WHEN p.AGENTID > 0 THEN p.AGENTID END)        AS AgentID,
        MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END)          AS AgentState,
        MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END)    AS InAcdQueue,
        MAX(CASE WHEN p.AGENTID > 0 OR p.ENDPOINTTYPE = 4
                 THEN p.CREATED END)                           AS WaitLegCreated,
        MAX(CASE WHEN p.AGENTID IS NULL OR p.AGENTID = 0
                 THEN p.ENDPOINTDN END)                        AS QueueEndpointDN
    FROM G_PARTY p WITH (NOLOCK)
    WHERE p.TERMINATED IS NULL
    GROUP BY p.CALLID
),
-- CallCreated carried through so VQFirst can bound its G_USERDATA_HISTORY
-- lookup to a tight window near call start (see VQFirst comment).
Candidates AS (
    SELECT c.CALLID, c.CALLANI, c.CALLDNIS, c.CREATED AS CallCreated, pt.AgentID,
           pt.AgentState, pt.InAcdQueue, pt.WaitLegCreated, pt.QueueEndpointDN
    FROM G_CALL_ACTIVE c
    JOIN Parties pt ON pt.CALLID = c.CALLID
    WHERE pt.InAcdQueue = 1 OR pt.AgentState IS NULL
),
-- WITH (NOLOCK): under load this query got MUCH slower and eventually
-- deadlocked with no table hints - helped but wasn't enough on its own.
--
-- Real cost driver, confirmed via SET STATISTICS IO: a per-CALLID lookup
-- here costs ~2000 logical reads - fine for a handful of candidates, but
-- 500+ active calls site-wide (a busy period) means 1M+ reads, fast only
-- when cache-warm. An open-ended ADDED>=CallCreated bound (tried earlier)
-- didn't help because it still includes a long-stuck call's ENTIRE
-- accumulated reroute history - exactly the call this app looks for.
--
-- Anchored to WaitLegCreated (start of the CURRENT agent-reserved/ACD-
-- queue leg), falling back to CallCreated only when there isn't one yet
-- (still in IVR/treatment) - NOT plain CallCreated alone. A call can be
-- routed more than once in its life (customer -> agent -> agent transfers
-- to a different queue -> stuck there): each transfer is its own routing
-- decision with its own fresh VQ_Name/RP attach at that moment, not
-- necessarily near the call's original start. WaitLegCreated updates to
-- reflect exactly that moment for the call's CURRENT wait cycle, so
-- anchoring there still finds a post-transfer attach hours into the call.
VQFirst AS (
    SELECT h.CALLID, MIN(h.ADDED) AS FirstTagged
    FROM G_USERDATA_HISTORY h WITH (NOLOCK)
    JOIN Candidates cand ON cand.CALLID = h.CALLID
    WHERE h.VALUE = @queue
      AND h.ADDED >= COALESCE(cand.WaitLegCreated, cand.CallCreated)
      AND h.ADDED < DATEADD(MINUTE, 10, COALESCE(cand.WaitLegCreated, cand.CallCreated))
    GROUP BY h.CALLID
),
MatchedCalls AS (
    SELECT cand.CALLID
    FROM Candidates cand
    LEFT JOIN VQFirst vf ON vf.CALLID = cand.CALLID
    WHERE vf.CALLID IS NOT NULL OR cand.QueueEndpointDN = @queue
),
ConnFromHistory AS (
    SELECT h.CALLID, MIN(h.VALUE) AS ConnID
    FROM G_USERDATA_HISTORY h WITH (NOLOCK)
    WHERE h.CALLID IN (SELECT CALLID FROM MatchedCalls)
      AND h.KEYNAME = 'ConnectionID'
    GROUP BY h.CALLID
)
SELECT TOP (500)
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


def _sql_escape(value):
    """Double embedded single quotes - the standard T-SQL string-literal
    escape. This is the ONLY injection defense for @queue in this file
    (see module docstring) since sqlcmd has no real parameter binding."""
    return value.replace("'", "''")


def run_query(server, database, trusted, user, password, sql, queue=None,
              timeout=SQLCMD_TIMEOUT_SEC):
    """Execute sql via sqlcmd.exe. Returns (headers, rows, timing).
    timing is always "" here (see module docstring - no connect/query
    split like the PowerShell version has).
    Raises RuntimeError with the server's message on failure."""
    args = ["sqlcmd", "-S", server, "-d", database]
    env = dict(os.environ)
    if trusted:
        args += ["-E"]
    else:
        args += ["-U", user]
        # Password via env var, not -P on the command line, so it doesn't
        # show up in the process list - same property the PowerShell
        # version gets from passing the connection string via env var.
        env["SQLCMDPASSWORD"] = password

    # SET ARITHABORT ON matches SSMS's default (SqlClient/sqlcmd both
    # default it OFF) - SQL Server caches a separate plan per distinct SET
    # options combination, and for a heavily-skewed VQ value this
    # difference alone was confirmed live to be the gap between SSMS
    # finishing in ~10-27s and both apps timing out at 75s on identical
    # query text.
    full_sql = ("SET ARITHABORT ON;\n"
                "SET NOCOUNT ON;\n"
                "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;\n")
    if queue is not None:
        full_sql += f"DECLARE @queue nvarchar(200) = N'{_sql_escape(queue)}';\n"
    full_sql += sql

    args += [
        "-Q", full_sql,
        "-s", SQLCMD_COL_SEP,
        # 0 was meant as "unlimited width" but actually produces ZERO-WIDTH
        # header/separator formatting on this sqlcmd version - the header
        # line and dashes line both render as empty text and vanish
        # entirely once blank lines get filtered out below, which silently
        # shifted every row up by one (confirmed live: "headers" turned out
        # to be real data, "row[0]" was really the second data row).
        # Explicit max width instead of 0 fixes this.
        "-y", "8000", "-Y", "8000",
        # -W (trim trailing whitespace) is mutually exclusive with -y/-Y on
        # this sqlcmd version - not needed anyway since each cell already
        # gets .strip()'d in Python below.
        "-w", "8000",  # screen width - default 80 wraps a wide result row
        # (8 columns incl. a long VQ name + a ConnID hash) across MULTIPLE
        # output lines, which silently broke row/column parsing below
        # (blank ConnID, wrong row count). A wide-enough -w keeps one
        # logical row on one physical line.
        "-t", str(SQLCMD_QUERY_TIMEOUT_SEC),
        "-b",                   # exit nonzero on SQL error
    ]

    proc = subprocess.run(args, capture_output=True, text=True, env=env,
                           timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)

    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unknown error").strip())

    lines = [l for l in proc.stdout.splitlines() if l.strip() != ""]
    if not lines:
        return [], [], ""

    headers = [h.strip() for h in lines[0].split(SQLCMD_COL_SEP)]
    data_lines = lines[1:]
    # sqlcmd prints a dashes-and-spaces separator line under the header
    # row - drop it if present.
    if data_lines and re.fullmatch(r"[-\s" + re.escape(SQLCMD_COL_SEP) + r"]+", data_lines[0]):
        data_lines = data_lines[1:]

    rows = []
    for line in data_lines:
        cells = [c.strip() for c in line.split(SQLCMD_COL_SEP)]
        cells = ["" if c == "NULL" else c for c in cells]
        rows.append(cells)
    return headers, rows, ""


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
        self.title("ICON Stuck Call Finder (sqlcmd variant) - active calls in a Virtual Queue")
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
                return (0, float(v))
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

    def _conn_fields(self):
        """Validates and returns (server, database, trusted, user, password)
        for run_query - the sqlcmd variant needs the raw parts, not an
        assembled ADO connection string."""
        server = self.server_entry.get().strip()
        database = self.db_entry.get().strip()
        if not server or not database:
            raise ValueError("Server and Database are required.")
        trusted = self.trusted_var.get()
        user = self.user_entry.get().strip()
        password = self.pw_entry.get()
        if not trusted and not user:
            raise ValueError("User is required when not using Windows auth.")
        return server, database, trusted, user, password

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
            conn = self._conn_fields()
        except ValueError as exc:
            messagebox.showerror("Missing details", str(exc))
            return
        self.status_label.config(text="Testing connection...")

        def work():
            try:
                headers, rows, _timing = run_query(
                    *conn, "SELECT @@VERSION AS Version, DB_NAME() AS CurrentDb")
                msg = " | ".join(rows[0]) if rows else "(no rows)"
                self.after(0, lambda: self.log(f"[OK] Connected: {msg}\n"))
                self.after(0, lambda: self.status_label.config(text="Connection OK"))
                self.after(0, self._save_ini)
            except Exception as exc:
                self.after(0, lambda: self.log(f"[!] Connection failed: {exc}\n"))
                self.after(0, lambda: self.status_label.config(text="Connection failed"))
        self._run_bg(work)

    def show_tables(self):
        try:
            conn = self._conn_fields()
        except ValueError as exc:
            messagebox.showerror("Missing details", str(exc))
            return
        self.status_label.config(text="Reading schema...")

        def work():
            try:
                headers, rows, _timing = run_query(*conn, TABLES_QUERY)
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
            conn = self._conn_fields()
            parse_duration(self.threshold_entry.get().strip())
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        self.status_label.config(text="Querying ICON...")
        # repr() so any hidden/invisible character in the typed VQ value
        # (stray whitespace, a smart quote pasted from somewhere, etc.)
        # shows up instead of looking identical to a value that works.
        self.log(f"    DECLARE @queue = N'{_sql_escape(queue)}'  (repr: {queue!r})\n")

        def work():
            started = datetime.now()
            try:
                headers, rows, timing = run_query(*conn, self.query, queue=queue)
                took = (datetime.now() - started).total_seconds()
                self.after(0, lambda: self.log(f"    (total {took:.2f}s, sqlcmd)\n"))
                self.after(0, lambda: self.log(f"    headers: {headers!r}\n"))
                if rows:
                    self.after(0, lambda: self.log(f"    row[0]: {rows[0]!r}\n"))
                self.after(0, lambda: self._show(headers, rows, took))
            except Exception as exc:
                self.after(0, lambda: self.log(f"[!] Query failed: {exc}\n"))
                self.after(0, lambda: self.status_label.config(text="Query failed"))
        self._run_bg(work)

    def _show(self, headers, rows, took):
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
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
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
