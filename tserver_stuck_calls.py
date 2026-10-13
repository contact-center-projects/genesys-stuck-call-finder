#!/usr/bin/env python3
"""
T-Server stuck-call finder - Virtual Queue, active calls only.

Modeled on the existing StuckCallTabFrame tool, reduced to a single
T-Server tab, searching by Virtual Queue instead of campaign, and showing
ONLY calls that never terminated (i.e. still stuck).

HOW IT FINDS CALLS
------------------
1. File selection is by FILENAME timestamp - Genesys rotates logs as
   "...<name>.YYYYMMDD_HHMMSS.log". Files whose filename time falls within
   +/- N minutes of the stuck time are auto-selected and highlighted.
   (No reading of file contents or filesystem mtime is involved - the
   filename is authoritative for which time period a file covers.)

2. Within each selected file, a proximity search:
     - the last "StartTime" value seen is remembered as we scan
     - when a line contains the Virtual Queue name, we "arm" a window
     - inside the next PROXIMITY_LINES lines, any ConnectionID /
       AttributeConnID is captured as a candidate call, tagged with the
       StartTime that was in effect
   This deliberately does NOT try to model the call lifecycle - it just
   finds connection IDs mentioned near the queue.

3. Active-only filter: a second pass scans the same files for each
   candidate ConnID appearing on a line alongside a terminating event
   (EventReleased, EventAbandoned, ...). Any ConnID found terminated is
   dropped, so only still-active (stuck) calls remain.

REGEX CAVEAT
------------
The attribute patterns below (StartTime / ConnectionID / AttributeConnID)
are written to be permissive about quoting and separators, but if your
T-Server writes them differently, adjust the three *_RE patterns - they
are isolated at the top for exactly that reason. Use the log panel: it
reports how many candidates were found before and after the active
filter, which tells you which stage isn't matching.
"""

import os
import re
import subprocess
import threading
import tkinter as tk
from configparser import ConfigParser
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, scrolledtext, ttk

INI_FILENAME = "tserver_stuck_calls.ini"

# Rotated Genesys log filename stamp, e.g. "TServer.20260903_142200.log"
FILE_TS_RE = re.compile(r"\.(\d{8})_(\d{6})")

# Attribute patterns - permissive about ':' '=' quotes and brackets.
START_TIME_RE = re.compile(r"StartTime\s*[:=]?\s*[\['\"(]?\s*([0-9][0-9:.\-/ ]*)",
                            re.IGNORECASE)
CONNID_RE = re.compile(r"\bConnectionID\s*[:=]?\s*[\['\"(]?\s*([0-9A-Za-z]+)",
                        re.IGNORECASE)
ATTR_CONNID_RE = re.compile(r"\bAttributeConnID\s*[:=]?\s*[\['\"(]?\s*([0-9A-Za-z]+)",
                             re.IGNORECASE)

# How many lines after a queue mention still count as "near" it.
PROXIMITY_LINES = 15

# A StartTime is only attributed to a ConnID if it was logged within this
# many lines of it. Without this bound, StartTime is just "the last one seen
# anywhere in the file", so a call with no StartTime of its own silently
# inherits an unrelated earlier call's start time.
START_TIME_LOOKBACK_LINES = 15

# A ConnID seen on a line containing any of these is treated as finished,
# so it won't be reported as an active/stuck call.
DEFAULT_TERMINAL_KEYWORDS = (
    "EventReleased, EventAbandoned, EventPartyDeleted, "
    "EventDiverted, EventRouteUsed, EventCallDeleted"
)


def sort_column(tree, col, reverse):
    rows = [(tree.set(k, col), k) for k in tree.get_children("")]
    rows.sort(reverse=reverse)
    for index, (_, k) in enumerate(rows):
        tree.move(k, "", index)
    tree.heading(col, command=lambda: sort_column(tree, col, not reverse))


class TServerStuckCallApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("T-Server Stuck Call Finder - Virtual Queue (active calls only)")
        self.geometry("1250x780")

        self.ini_filename = INI_FILENAME
        self.path_display_names = {}     # real path -> friendly name
        self.friendly_to_real_path = {}  # friendly name -> real path
        self.path_vars = {}              # real path -> BooleanVar
        self.cmd_template = ""           # optional, from INI [cmd] command

        self._search_thread = None
        self._cancel = threading.Event()

        self._build_ui()
        self._load_ini_data()
        self._load_last_search()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side="top", fill="x", padx=5, pady=2)

        ttk.Label(top, text="Call stuck for:").pack(side="left")
        self.stuck_entry = ttk.Entry(top, width=10)
        self.stuck_entry.pack(side="left", padx=(4, 2))
        ttk.Label(top, text="(h m s)", foreground="#777").pack(side="left")

        self.stuck_label = ttk.Label(top, text="Stuck Time: ", foreground="blue")
        self.stuck_label.pack(side="left", padx=10)

        ttk.Label(top, text="Specific Time:").pack(side="left")
        self.specific_time_entry = ttk.Entry(top, width=20)
        self.specific_time_entry.pack(side="left", padx=(4, 10))
        ttk.Label(top, text="YYYY-MM-DD HH:MM:SS or HH:MM:SS",
                  foreground="#777").pack(side="left")

        row2 = ttk.Frame(self)
        row2.pack(side="top", fill="x", padx=5, pady=2)

        ttk.Label(row2, text="Virtual Queue:").pack(side="left")
        self.queue_entry = ttk.Entry(row2, width=32)
        self.queue_entry.pack(side="left", padx=(4, 10))

        ttk.Label(row2, text="File window (± min):").pack(side="left")
        self.window_entry = ttk.Entry(row2, width=5)
        self.window_entry.insert(0, "5")
        self.window_entry.pack(side="left", padx=(4, 10))

        ttk.Label(row2, text="Terminal keywords:").pack(side="left")
        self.terminal_entry = ttk.Entry(row2, width=52)
        self.terminal_entry.insert(0, DEFAULT_TERMINAL_KEYWORDS)
        self.terminal_entry.pack(side="left", fill="x", expand=True, padx=4)

        btns = ttk.Frame(self)
        btns.pack(side="top", fill="x", padx=5, pady=4)
        self.find_btn = tk.Button(btns, text="1. Find Log Files",
                                   command=self.start_find_files)
        self.find_btn.pack(side="left", padx=3)
        self.search_btn = tk.Button(btns, text="2. Search Active Calls",
                                     command=self.start_search, bg="#d0f0d0")
        self.search_btn.pack(side="left", padx=3)
        self.stop_btn = tk.Button(btns, text="Stop", command=self.stop_search,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=3)
        self.select_all_btn = tk.Button(btns, text="Select All Files",
                                         command=self.select_all_files)
        self.select_all_btn.pack(side="left", padx=3)
        tk.Button(btns, text="Add Server Path...",
                  command=self.add_path).pack(side="left", padx=3)

        self.progress_bar = ttk.Progressbar(btns, length=220)
        self.progress_bar.pack(side="right", padx=6)

        main_paned = tk.PanedWindow(self, orient="horizontal",
                                     sashrelief="raised", sashwidth=4)
        main_paned.pack(fill="both", expand=True, padx=5, pady=5)

        # Left: server paths + matched files
        left = tk.PanedWindow(main_paned, orient="vertical",
                               sashrelief="raised", sashwidth=4)
        path_frame = ttk.LabelFrame(left, text="T-Server Paths")
        self.path_inner = ttk.Frame(path_frame)
        self.path_inner.pack(fill="both", expand=True)
        left.add(path_frame, height=140)

        tree_frame = ttk.LabelFrame(left, text="Log Files (matched are highlighted + selected)")
        self.file_tree = ttk.Treeview(tree_frame)
        self.file_tree.tag_configure("matched", background="#40ffd0")
        self.file_tree.pack(fill="both", expand=True)
        left.add(tree_frame)
        main_paned.add(left, width=430)

        # Right: results + log
        right = tk.PanedWindow(main_paned, orient="vertical",
                                sashrelief="raised", sashwidth=4)
        conn_frame = ttk.LabelFrame(
            right, text="Active (stuck) calls on queue - click a heading to sort")
        cols = ("starttime", "connid", "file")
        self.connection_tree = ttk.Treeview(conn_frame, columns=cols, show="headings")
        for col, title, width in (("starttime", "StartTime", 170),
                                   ("connid", "ConnectionID", 190),
                                   ("file", "Found In", 300)):
            self.connection_tree.heading(
                col, text=title,
                command=lambda c=col: sort_column(self.connection_tree, c, False))
            self.connection_tree.column(col, width=width)
        self.connection_tree.pack(fill="both", expand=True)
        self.connection_tree.bind("<Double-1>", self.run_cmd_for_selected_item)
        right.add(conn_frame, height=380)

        log_frame = ttk.LabelFrame(right, text="Log")
        self.result_text = scrolledtext.ScrolledText(log_frame, height=12)
        self.result_text.pack(fill="both", expand=True)
        right.add(log_frame)
        main_paned.add(right)

    # -------------------------------------------------------------- config
    def _load_ini_data(self):
        if not os.path.exists(self.ini_filename):
            self.log(f"[i] No {self.ini_filename} found - use 'Add Server Path...' "
                     f"to add T-Server log folders.\n")
            return
        config = ConfigParser()
        config.read(self.ini_filename)

        paths = config["paths"] if "paths" in config else {}
        for friendly, real in paths.items():
            self._register_path(real.strip(), friendly)

        if "cmd" in config and "command" in config["cmd"]:
            self.cmd_template = config["cmd"]["command"]

    def _register_path(self, real_path, friendly=None):
        if real_path in self.path_vars:
            return
        friendly = friendly or os.path.basename(real_path.rstrip("\\/")) or real_path
        self.path_display_names[real_path] = friendly
        self.friendly_to_real_path[friendly] = real_path
        var = tk.BooleanVar(value=True)
        self.path_vars[real_path] = var
        ttk.Checkbutton(self.path_inner, text=friendly, variable=var).pack(anchor="w")

    def add_path(self):
        folder = filedialog.askdirectory(title="Select T-Server log folder")
        if folder:
            self._register_path(folder)
            self._save_last_search()

    def _save_last_search(self):
        config = ConfigParser()
        config["paths"] = {self.path_display_names[p]: p for p in self.path_vars}
        config["search"] = {
            "stuck_duration": self.stuck_entry.get(),
            "specific_time": self.specific_time_entry.get(),
            "queue": self.queue_entry.get(),
            "window_min": self.window_entry.get(),
            "terminal_keywords": self.terminal_entry.get(),
        }
        if self.cmd_template:
            config["cmd"] = {"command": self.cmd_template}
        try:
            with open(self.ini_filename, "w") as f:
                config.write(f)
        except OSError as exc:
            self.log(f"[!] Could not save {self.ini_filename}: {exc}\n")

    def _load_last_search(self):
        if not os.path.exists(self.ini_filename):
            return
        config = ConfigParser()
        config.read(self.ini_filename)
        if "search" not in config:
            return
        s = config["search"]
        for entry, key in ((self.stuck_entry, "stuck_duration"),
                            (self.specific_time_entry, "specific_time"),
                            (self.queue_entry, "queue")):
            if s.get(key):
                entry.delete(0, tk.END)
                entry.insert(0, s.get(key))
        for entry, key, default in ((self.window_entry, "window_min", "5"),
                                     (self.terminal_entry, "terminal_keywords",
                                      DEFAULT_TERMINAL_KEYWORDS)):
            entry.delete(0, tk.END)
            entry.insert(0, s.get(key) or default)

    # ----------------------------------------------------------- utilities
    def log(self, msg):
        self.result_text.insert(tk.END, msg)
        self.result_text.see(tk.END)

    def log_threadsafe(self, msg):
        self.after(0, lambda: self.log(msg))

    def calculate_stuck_time(self):
        """Specific Time wins if filled; else now - 'stuck for' duration."""
        specific = self.specific_time_entry.get().strip()
        if specific:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                        "%H:%M:%S", "%H:%M"):
                try:
                    parsed = datetime.strptime(specific, fmt)
                except ValueError:
                    continue
                if fmt.startswith("%H"):  # time-only -> anchor to today
                    now = datetime.now()
                    parsed = now.replace(hour=parsed.hour, minute=parsed.minute,
                                          second=parsed.second, microsecond=0)
                self.stuck_label.config(
                    text=f"Stuck Time: {parsed.strftime('%Y-%m-%d %H:%M:%S')}")
                return parsed
            self.stuck_label.config(text="Stuck Time: (bad Specific Time)")
            return None

        raw = self.stuck_entry.get().strip()
        if not raw:
            self.stuck_label.config(text="Stuck Time: (enter a time)")
            return None
        try:
            parts = [int(p) for p in raw.split()]
            while len(parts) < 3:
                parts.append(0)
            h, m, s = parts[:3]
            stuck_time = datetime.now() - timedelta(hours=h, minutes=m, seconds=s)
            self.stuck_label.config(
                text=f"Stuck Time: {stuck_time.strftime('%Y-%m-%d %H:%M:%S')}")
            return stuck_time
        except ValueError:
            self.stuck_label.config(text="Stuck Time: (bad duration)")
            return None

    def selected_paths(self):
        return [p for p, var in self.path_vars.items() if var.get()]

    # -------------------------------------------------- step 1: find files
    def start_find_files(self):
        stuck_time = self.calculate_stuck_time()
        if not stuck_time:
            messagebox.showerror(
                "Missing time",
                "Enter either 'Call stuck for' (e.g. '0 15 0') or a Specific Time.")
            return
        paths = self.selected_paths()
        if not paths:
            messagebox.showerror("No paths", "Add and tick at least one T-Server path.")
            return
        try:
            window_min = int(self.window_entry.get().strip() or "5")
        except ValueError:
            messagebox.showerror("Bad window", "File window must be a whole number.")
            return

        self.file_tree.delete(*self.file_tree.get_children())
        self.connection_tree.delete(*self.connection_tree.get_children())

        start_range = stuck_time - timedelta(minutes=window_min)
        end_range = stuck_time + timedelta(minutes=window_min)
        self.log(f"\n--- Finding files covering "
                 f"{start_range:%Y-%m-%d %H:%M:%S} .. {end_range:%H:%M:%S} ---\n")

        total_matched = 0
        for path in paths:
            if not os.path.isdir(path):
                self.log(f"[!] Path not found: {path}\n")
                continue
            friendly = self.path_display_names.get(path, path)
            parent = self.file_tree.insert("", "end", text=friendly, open=True)
            try:
                files = sorted(f for f in os.listdir(path) if FILE_TS_RE.search(f))
            except OSError as exc:
                self.log(f"[!] Cannot list {path}: {exc}\n")
                continue

            matched_here = 0
            for file in files:
                child = self.file_tree.insert(parent, "end", text=file)
                match = FILE_TS_RE.search(file)
                try:
                    file_time = datetime.strptime(match.group(1) + match.group(2),
                                                   "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                if start_range <= file_time <= end_range:
                    self.file_tree.item(child, tags=("matched",))
                    self.file_tree.selection_add(child)
                    matched_here += 1
            total_matched += matched_here
            self.log(f"[{friendly}] {matched_here} matched of {len(files)} files\n")

        if total_matched == 0:
            self.log("[!] No files matched. Widen the '± min' window, or check that "
                     "filenames contain a .YYYYMMDD_HHMMSS stamp.\n")
        self._save_last_search()

    def select_all_files(self):
        for parent in self.file_tree.get_children():
            for child in self.file_tree.get_children(parent):
                self.file_tree.selection_add(child)

    # ----------------------------------------------- step 2: search queue
    def start_search(self):
        if self._search_thread and self._search_thread.is_alive():
            return
        queue = self.queue_entry.get().strip()
        if not queue:
            messagebox.showerror("Missing queue", "Enter the Virtual Queue to search for.")
            return
        selected = [i for i in self.file_tree.selection() if self.file_tree.parent(i)]
        if not selected:
            messagebox.showerror("No files",
                                 "Run 'Find Log Files' first, or select files manually.")
            return

        files = []
        for item in selected:
            folder_friendly = self.file_tree.item(self.file_tree.parent(item))["text"]
            file_name = self.file_tree.item(item)["text"]
            real_path = self.friendly_to_real_path.get(folder_friendly)
            if real_path:
                full = os.path.join(real_path, file_name)
                if os.path.isfile(full):
                    files.append(full)

        terminal_keywords = [k.strip().lower()
                             for k in self.terminal_entry.get().split(",") if k.strip()]

        self.connection_tree.delete(*self.connection_tree.get_children())
        self.search_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._cancel.clear()
        self.progress_bar.configure(maximum=max(1, len(files) * 2), value=0)

        self._search_thread = threading.Thread(
            target=self._search_worker, args=(files, queue, terminal_keywords),
            daemon=True)
        self._search_thread.start()
        self._save_last_search()

    def stop_search(self):
        self._cancel.set()
        self.log("[i] Stopping...\n")

    def _search_worker(self, files, queue, terminal_keywords):
        queue_lower = queue.lower()
        candidates = {}  # conn_id -> (start_time, file_name)
        progress = 0

        # ---- Pass 1: collect ConnIDs mentioned near the queue -------------
        self.log_threadsafe(f"\n--- Searching {len(files)} file(s) for queue "
                            f"'{queue}' ---\n")
        for full_path in files:
            if self._cancel.is_set():
                self._finish_search(None, cancelled=True)
                return
            file_name = os.path.basename(full_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    armed_until = -1
                    start_time_val = None
                    start_time_line = None
                    for line_no, line in enumerate(f, 1):
                        lower_line = line.lower()

                        if "starttime" in lower_line:
                            m = START_TIME_RE.search(line)
                            if m:
                                start_time_val = m.group(1).strip()
                                start_time_line = line_no

                        if queue_lower in lower_line:
                            armed_until = line_no + PROXIMITY_LINES
                        elif "queue" in lower_line:
                            # A DIFFERENT queue is mentioned - close the window
                            # immediately, otherwise connection IDs belonging to
                            # that other queue get attributed to ours just
                            # because they fall within PROXIMITY_LINES.
                            armed_until = -1

                        if line_no <= armed_until:
                            # Only attribute a StartTime that was logged close
                            # enough to this ConnID to plausibly belong to the
                            # same call - see START_TIME_LOOKBACK_LINES.
                            if (start_time_line is not None and
                                    line_no - start_time_line <= START_TIME_LOOKBACK_LINES):
                                st = start_time_val
                            else:
                                st = None
                            for rx in (CONNID_RE, ATTR_CONNID_RE):
                                for conn_id in rx.findall(line):
                                    if conn_id not in candidates:
                                        candidates[conn_id] = (st, file_name)
            except OSError as exc:
                self.log_threadsafe(f"[!] Error reading {file_name}: {exc}\n")
            progress += 1
            self.after(0, lambda v=progress: self.progress_bar.configure(value=v))

        self.log_threadsafe(f"[i] Pass 1: {len(candidates)} connection ID(s) found "
                            f"near the queue.\n")
        if not candidates:
            self.log_threadsafe("[!] Nothing found. Check the queue name spelling, or "
                                "that ConnectionID/AttributeConnID appear within "
                                f"{PROXIMITY_LINES} lines of the queue mention.\n")
            self._finish_search(0)
            return

        # ---- Pass 2: drop any ConnID that also shows a terminal event -----
        ended = set()
        for full_path in files:
            if self._cancel.is_set():
                self._finish_search(None, cancelled=True)
                return
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        lower_line = line.lower()
                        if not any(k in lower_line for k in terminal_keywords):
                            continue
                        for conn_id in candidates:
                            if conn_id in ended:
                                continue
                            if conn_id.lower() in lower_line:
                                ended.add(conn_id)
            except OSError:
                pass
            progress += 1
            self.after(0, lambda v=progress: self.progress_bar.configure(value=v))

        active = {cid: v for cid, v in candidates.items() if cid not in ended}
        self.log_threadsafe(f"[i] Pass 2: {len(ended)} ended, "
                            f"{len(active)} still active.\n")

        for conn_id, (start_time_val, file_name) in active.items():
            self.after(0, lambda c=conn_id, s=start_time_val, fn=file_name:
                       self.connection_tree.insert(
                           "", "end", values=(s if s else "-", c, fn)))
        self._finish_search(len(active))

    def _finish_search(self, count, cancelled=False):
        def done():
            self.search_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            if cancelled:
                self.log("[i] Search stopped.\n")
            else:
                self.log(f"[i] Done - {count} active call(s) on the queue.\n")
        self.after(0, done)

    # ------------------------------------------------------- optional cmd
    def run_cmd_for_selected_item(self, event):
        """Double-click a row to run the INI-configured command for that
        ConnID (optional - only if [cmd] command is set in the INI, with a
        'ConnectionID' placeholder in it)."""
        if not self.cmd_template:
            return
        item_id = self.connection_tree.identify_row(event.y)
        if not item_id:
            return
        # Read via tree.set(), NOT item()["values"] - the latter lets Tkinter
        # type-guess, which strips leading zeros from an all-numeric ConnID
        # (e.g. "0071234567890123" would come back as 71234567890123).
        conn_id = self.connection_tree.set(item_id, "connid").strip()
        cmd = self.cmd_template.replace("ConnectionID", conn_id)
        self.log(f"\n[Double-Click] Running:\n{cmd}\n")
        threading.Thread(target=self._execute_cmd, args=(cmd,), daemon=True).start()

    def _execute_cmd(self, cmd):
        try:
            output = subprocess.check_output(cmd, shell=True,
                                              stderr=subprocess.STDOUT,
                                              text=True, timeout=15)
            self.log_threadsafe(f"[CMD Output]:\n{output}\n")
        except Exception as exc:
            self.log_threadsafe(f"[CMD Error]: {exc}\n")


if __name__ == "__main__":
    TServerStuckCallApp().mainloop()
