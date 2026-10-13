#!/usr/bin/env python3
"""
GUI for find_stuck_calls.py - Genesys stuck-call finder.

Pick your T-Server / StatServer log files, enter the VQ name and a
threshold, click "Scan", and the longest-active (most likely stuck) call
on that VQ shows at the top of the results table with its ANI/DNIS.

Run:
    python gui.py

Requires only the Python standard library (tkinter ships with CPython on
Windows/macOS; on Linux install it via your package manager, e.g.
`sudo apt install python3-tk`, if it's not already present).
"""

import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from find_stuck_calls import (find_stuck_calls, is_snapshot_file, configure_logging,
                               DEFAULT_LOG_PATH, parse_at_time, ScanCancelled)

LOG_EXTENSIONS = (".log", ".txt")


def _list_candidate_files(folder, already_have, at, window_min, debug_sink=None):
    """Pure file-IO, no Tk widget access - safe to run on a background
    thread so folder scanning (which can be slow over a network share)
    never blocks the GUI event loop / Stop button.

    Filters by filesystem modified-time (fast: no need to open/read every
    file). This is quicker than peeking at each file's actual content, at
    the cost of being fooled by a file that's opened long before or kept
    open long after the period it actually contains (e.g. a log still
    being actively written all day has an mtime of "now", not when its
    earliest content was written) - a generous slack window absorbs most
    of that. The real per-call time matching happens during Scan itself,
    which reads each included file's actual content precisely.

    Returns (new_paths: list[str], skipped_by_time: int, skipped_snapshot: int,
    included_unparseable: int) - the last is always 0 now (kept for
    call-site compatibility) since mtime is always available.
    """
    search_lo = search_hi = None
    if at:
        # Generous slack: rotated log files are named/stamped by when they
        # were CLOSED (or last appended to), which can be well after the
        # events inside them started - widen rather than miss the file.
        slack = timedelta(minutes=window_min + 180)
        search_lo = (at - slack).timestamp()
        search_hi = (at + slack).timestamp()

    new_paths, skipped_by_time, skipped_snapshot, included_unparseable = [], 0, 0, 0
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return [], 0, 0, 0  # folder temporarily unreachable - just skip this poll

    for name in names:
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        if not name.lower().endswith(LOG_EXTENSIONS):
            continue
        if is_snapshot_file(full):
            skipped_snapshot += 1
            continue
        if search_lo is not None:
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if not (search_lo <= mtime <= search_hi):
                skipped_by_time += 1
                continue
        if full not in already_have:
            new_paths.append(full)

    # Newest-added first, so the list (and therefore the order files show
    # up in the GUI) reflects "most recent first" rather than alphabetical.
    try:
        new_paths.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        pass  # a file vanished mid-sort - fall back to listing order

    return new_paths, skipped_by_time, skipped_snapshot, included_unparseable


class StuckCallFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Genesys Stuck Call Finder")
        self.geometry("900x600")
        self.minsize(700, 450)

        self.tserver_files: list[str] = []
        self.statserver_files: list[str] = []
        # Folders added via "Add folder..." are remembered here so watch mode
        # can re-poll them for newly-created rotated log files.
        self.tserver_folders: list[str] = []
        self.statserver_folders: list[str] = []
        self._debug_queue: "queue.Queue[str]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._watch_job = None

        configure_logging(DEFAULT_LOG_PATH)

        self._build_widgets()
        self.after(150, self._drain_debug_queue)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Virtual Queue:").grid(row=0, column=0, sticky="w")
        self.vq_entry = ttk.Entry(top, width=30)
        self.vq_entry.grid(row=0, column=1, sticky="w", padx=(4, 20))

        ttk.Label(top, text="Threshold (sec):").grid(row=0, column=2, sticky="w")
        self.threshold_entry = ttk.Entry(top, width=8)
        self.threshold_entry.insert(0, "0")
        self.threshold_entry.grid(row=0, column=3, sticky="w", padx=(4, 20))

        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Debug unmatched lines",
                         variable=self.debug_var).grid(row=0, column=4, sticky="w")

        ttk.Label(top, text="Call start time (HH:MM):").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.at_entry = ttk.Entry(top, width=10)
        self.at_entry.grid(row=1, column=1, sticky="w", padx=(4, 8), pady=(6, 0))

        ttk.Label(top, text="Date:").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.date_entry = ttk.Entry(top, width=12)
        self.date_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.date_entry.grid(row=1, column=3, sticky="w", padx=(4, 8), pady=(6, 0))
        ttk.Button(top, text="Today", width=7,
                   command=lambda: self._set_date_entry(0)).grid(row=1, column=4, pady=(6, 0))
        ttk.Button(top, text="Yesterday", width=9,
                   command=lambda: self._set_date_entry(-1)).grid(row=1, column=5, pady=(6, 0), padx=(4, 0))
        ttk.Label(top, text="blank start time = what's stuck right now",
                  foreground="#777").grid(row=1, column=6, sticky="w", padx=(12, 0), pady=(6, 0))

        ttk.Label(top, text="Search radius (± min):").grid(row=2, column=0, sticky="w")
        self.window_entry = ttk.Entry(top, width=8)
        self.window_entry.insert(0, "15")
        self.window_entry.grid(row=2, column=1, sticky="w", padx=(4, 20))

        self.watch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Watch folders for new files (live prod)",
                         variable=self.watch_var,
                         command=self._on_watch_toggle).grid(row=2, column=2, columnspan=2, sticky="w")
        ttk.Label(top, text="Poll every (sec):").grid(row=2, column=4, sticky="w", padx=(12, 0))
        self.watch_interval_entry = ttk.Entry(top, width=6)
        self.watch_interval_entry.insert(0, "30")
        self.watch_interval_entry.grid(row=2, column=5, sticky="w", padx=(4, 0))

        # File pickers
        files_frame = ttk.LabelFrame(self, text="Log files")
        files_frame.pack(fill="x", **pad)

        self.tserver_list = self._file_picker_row(
            files_frame, "T-Server logs:", 0, self.tserver_files, self.tserver_folders)
        self.statserver_list = self._file_picker_row(
            files_frame, "StatServer logs (optional):", 1, self.statserver_files, self.statserver_folders)

        # Actions
        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(actions, text="Scan", command=self._on_scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status_label = ttk.Label(actions, text="")
        self.status_label.pack(side="left", padx=12)

        # Results table
        results_frame = ttk.LabelFrame(self, text="Calls on VQ (longest duration first)")
        results_frame.pack(fill="both", expand=True, **pad)

        columns = ("elapsed", "status", "conn_id", "ani", "dnis", "queue")
        self.tree = ttk.Treeview(results_frame, columns=columns, show="headings")
        headings = {"elapsed": "Elapsed", "status": "Status", "conn_id": "ConnID",
                    "ani": "ANI", "dnis": "DNIS", "queue": "Queue"}
        widths = {"elapsed": 90, "status": 70, "conn_id": 180, "ani": 130, "dnis": 130, "queue": 180}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.tag_configure("top_hit", background="#ffe0b2")
        self.tree.pack(fill="both", expand=True, side="left")

        scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(fill="y", side="right")

        # Debug output
        self.debug_text = tk.Text(self, height=6, state="disabled", bg="#f5f5f5")
        self.debug_text.pack(fill="x", **pad)

        ttk.Label(self, text=f"Debug/perf log: {DEFAULT_LOG_PATH}",
                  foreground="#777").pack(fill="x", padx=8, pady=(0, 4))

    def _file_picker_row(self, parent, label, row, target_list, target_folders):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=8, pady=4)
        listbox = tk.Listbox(parent, height=3, width=70)
        listbox.grid(row=row, column=1, sticky="we", padx=4, pady=4)
        btns = ttk.Frame(parent)
        btns.grid(row=row, column=2, sticky="n")
        ttk.Button(btns, text="Add files...",
                   command=lambda: self._add_files(listbox, target_list)).pack(fill="x")
        ttk.Button(btns, text="Add folder...",
                   command=lambda: self._add_folder(listbox, target_list, target_folders)).pack(fill="x", pady=(2, 0))
        ttk.Button(btns, text="Clear",
                   command=lambda: self._clear_files(listbox, target_list, target_folders)).pack(fill="x", pady=(2, 0))
        parent.columnconfigure(1, weight=1)
        return listbox

    def _add_files(self, listbox, target_list):
        paths = filedialog.askopenfilenames(title="Select log file(s)",
                                             filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")])
        skipped = 0
        for p in paths:
            if is_snapshot_file(p):
                skipped += 1
                continue
            if p not in target_list:
                target_list.append(p)
                listbox.insert("end", p)
        if skipped:
            self.status_label.configure(
                text=f"Ignored {skipped} snapshot.log file(s) - not an event log.")

    def _add_folder(self, listbox, target_list, target_folders):
        """Scan a folder for log files. If a start time + search radius is
        set, only add files whose modified time falls near that window
        (with generous slack, since a rotated file's mtime reflects when it
        was last written, not necessarily when its earliest content is
        from) - handy for rotated Genesys logs so you don't have to hunt
        for the right file by hand. Exact per-call time matching happens
        during Scan itself, which reads each included file's real content.
        The folder is remembered so "Watch folders for new files" can pick
        up files created after this point (e.g. the next rotation on a
        live prod system).

        The actual scanning runs on a background thread - folder scans can
        be slow over a slow network share, and running that on the main
        thread would freeze the whole GUI, including the Stop button,
        until it finished."""
        folder = filedialog.askdirectory(title="Select folder containing log files")
        if not folder:
            return
        if folder not in target_folders:
            target_folders.append(folder)

        at, window_min, err = self._read_time_window()
        if err:
            messagebox.showerror("Invalid time", err)
            return

        self.status_label.configure(text=f"Scanning {folder} for matching files...")
        already_have = list(target_list)
        threading.Thread(
            target=self._add_folder_worker,
            args=(folder, listbox, target_list, already_have, at, window_min),
            daemon=True).start()

    def _add_folder_worker(self, folder, listbox, target_list, already_have, at, window_min):
        new_paths, skipped_by_time, skipped_snapshot, included_unparseable = _list_candidate_files(
            folder, already_have, at, window_min, debug_sink=self._debug_queue.put)
        self.after(0, self._apply_add_folder_result, folder, listbox, target_list,
                   new_paths, skipped_by_time, skipped_snapshot, included_unparseable)

    def _apply_add_folder_result(self, folder, listbox, target_list, new_paths,
                                  skipped_by_time, skipped_snapshot, included_unparseable):
        added = 0
        for full in new_paths:
            if full not in target_list:
                target_list.append(full)
                listbox.insert("end", full)
                added += 1

        msg = f"Added {added} file(s) from {folder}."
        if skipped_by_time:
            msg += f" Skipped {skipped_by_time} file(s) whose modified time is well outside that window."
        if skipped_snapshot:
            msg += f" Ignored {skipped_snapshot} snapshot.log file(s)."
        self.status_label.configure(text=msg)

    def _clear_files(self, listbox, target_list, target_folders=None):
        target_list.clear()
        listbox.delete(0, "end")
        if target_folders is not None:
            target_folders.clear()

    def _set_date_entry(self, day_offset: int):
        d = datetime.now() + timedelta(days=day_offset)
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, d.strftime("%Y-%m-%d"))

    def _read_time_window(self):
        """Returns (at_datetime_or_None, window_minutes, error_str_or_None).
        The Date field anchors bare 'HH:MM' input to the right calendar day -
        important since a 24h rolling log can span into "yesterday", so the
        real system clock's today isn't always the day you want to search."""
        date_str = self.date_entry.get().strip()
        try:
            anchor_day = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
        except ValueError:
            return None, None, "Date must look like '2026-09-02'."
        try:
            at = parse_at_time(self.at_entry.get(), today=anchor_day)
        except ValueError as exc:
            return None, None, str(exc)
        try:
            window_min = int(self.window_entry.get().strip() or "60")
        except ValueError:
            return None, None, "Window (± min) must be an integer."
        return at, window_min, None

    # ----------------------------------------------------------- live watch
    def _on_watch_toggle(self):
        if self.watch_var.get():
            if not self.tserver_folders and not self.statserver_folders:
                messagebox.showerror(
                    "No folders to watch",
                    "Watch mode re-polls folders added via 'Add folder...'. "
                    "Add at least one folder first (individual files won't be watched).")
                self.watch_var.set(False)
                return
            self._schedule_watch_poll()
        elif self._watch_job is not None:
            self.after_cancel(self._watch_job)
            self._watch_job = None

    def _schedule_watch_poll(self):
        try:
            interval_ms = max(5, int(self.watch_interval_entry.get().strip() or "30")) * 1000
        except ValueError:
            interval_ms = 30000
        self._watch_job = self.after(interval_ms, self._run_watch_poll)

    def _run_watch_poll(self):
        # Read Tk widget state (Entry.get()) here on the main thread only -
        # Tkinter widgets aren't safe to touch from a background thread.
        # The actual folder-listing/file-peeking I/O runs in the worker.
        at, window_min, err = self._read_time_window()
        if err:
            self.status_label.configure(text=f"Watch mode paused: {err}")
            if self.watch_var.get():
                self._schedule_watch_poll()
            return

        folder_specs = ([(f, self.tserver_list, self.tserver_files) for f in self.tserver_folders] +
                         [(f, self.statserver_list, self.statserver_files) for f in self.statserver_folders])
        snapshot = [(folder, listbox, target_list, list(target_list))
                    for folder, listbox, target_list in folder_specs]

        threading.Thread(target=self._watch_poll_worker, args=(snapshot, at, window_min),
                          daemon=True).start()

    def _watch_poll_worker(self, snapshot, at, window_min):
        results = []
        for folder, listbox, target_list, already_have in snapshot:
            new_paths, _, _, _ = _list_candidate_files(
                folder, already_have, at, window_min, debug_sink=self._debug_queue.put)
            results.append((listbox, target_list, new_paths))
        self.after(0, self._apply_watch_poll_result, results)

    def _apply_watch_poll_result(self, results):
        total_added = 0
        for listbox, target_list, new_paths in results:
            for full in new_paths:
                if full not in target_list:
                    target_list.append(full)
                    listbox.insert("end", full)
                    total_added += 1

        if total_added:
            self.status_label.configure(
                text=f"Watch mode: picked up {total_added} new file(s), re-scanning...")
            self._on_scan()

        if self.watch_var.get():  # still enabled - keep polling
            self._schedule_watch_poll()

    # --------------------------------------------------------------- logic
    def _on_scan(self):
        if self._worker is not None and self._worker.is_alive():
            return  # a scan is already running (e.g. watch-mode triggered while busy)
        vq = self.vq_entry.get().strip()
        if not vq:
            messagebox.showerror("Missing input", "Enter a Virtual Queue name.")
            return
        if not self.tserver_files and not self.statserver_files:
            messagebox.showerror("Missing input", "Add at least one T-Server or StatServer log file.")
            return
        try:
            threshold = int(self.threshold_entry.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Invalid input", "Threshold must be an integer number of seconds.")
            return

        at, window_min, err = self._read_time_window()
        if err:
            messagebox.showerror("Invalid input", err)
            return
        window = (at - timedelta(minutes=window_min), at + timedelta(minutes=window_min)) if at else None

        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Scanning...")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.debug_text.configure(state="normal")
        self.debug_text.delete("1.0", "end")
        self.debug_text.configure(state="disabled")

        debug_on = self.debug_var.get()
        log_paths = list(self.tserver_files) + list(self.statserver_files)

        self._cancel_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_scan, args=(vq, log_paths, threshold, debug_on, window, self._cancel_event),
            daemon=True)
        self._worker.start()

    def _on_stop(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
            self.status_label.configure(text="Stopping...")
            self.stop_btn.configure(state="disabled")

    def _run_scan(self, vq, log_paths, threshold, debug_on, window, cancel_event):
        try:
            stuck = find_stuck_calls(vq, log_paths, threshold, debug_on,
                                      debug_sink=self._debug_queue.put, window=window,
                                      cancel_event=cancel_event)
            self.after(0, self._show_results, stuck, None)
        except ScanCancelled:
            self.after(0, self._show_results, [], "cancelled")
        except Exception as exc:  # surfaced to the user, not swallowed
            self.after(0, self._show_results, [], exc)

    def _show_results(self, stuck, error):
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._cancel_event = None
        if error == "cancelled":
            self.status_label.configure(text="Scan stopped.")
            return
        if error:
            self.status_label.configure(text="Error")
            messagebox.showerror("Scan failed", str(error))
            return

        if not stuck:
            self.status_label.configure(text="No matching calls found.")
            return

        self.status_label.configure(text=f"{len(stuck)} candidate call(s) found.")
        for i, (elapsed, rec) in enumerate(stuck):
            mins, secs = divmod(int(elapsed), 60)
            values = (f"{mins}m{secs:02d}s", rec.status, rec.conn_id, rec.ani or "-",
                      rec.dnis or "-", rec.queue or "-")
            tags = ("top_hit",) if i == 0 else ()
            self.tree.insert("", "end", values=values, tags=tags)

    def _drain_debug_queue(self):
        while True:
            try:
                msg = self._debug_queue.get_nowait()
            except queue.Empty:
                break
            self.debug_text.configure(state="normal")
            self.debug_text.insert("end", msg + "\n")
            self.debug_text.see("end")
            self.debug_text.configure(state="disabled")
        self.after(150, self._drain_debug_queue)


if __name__ == "__main__":
    StuckCallFinderApp().mainloop()
