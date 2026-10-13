# Genesys Stuck Call Finder

Tools for finding calls that are stuck (still active, never terminated) in a Genesys Virtual Queue.

## Approaches

| Script | Source | Notes |
|---|---|---|
| `icon_stuck_calls.py` | ICON (Interaction Concentrator) SQL database | Recommended. Tkinter GUI; queries via PowerShell + `System.Data.SqlClient`, so nothing to install. |
| `icon_stuck_calls_sqlcmd.py` | ICON database via `sqlcmd.exe` | Experimental variant to reduce process startup overhead. No true parameter binding; see the file's docstring. |
| `find_stuck_calls.py` + `gui.py` | T-Server / StatServer log files | Reconstructs call lifecycles by ConnID and lists calls with no terminal event. Parser regexes are isolated so they can be adapted to your log format. |
| `tserver_stuck_calls.py` | T-Server logs | Selects log files by filename timestamp, proximity-searches for ConnIDs near the queue name, and keeps only calls never terminated. |
| `watch_live.py`, `watch_any_open.py` | ICON database | Small polling helpers for watching a test call appear in the queue. |

`sql/stuck_calls_v4.sql` holds the ICON query; `icon_stuck_calls.ini` stores the saved connection settings (server, database, default queue). Passwords are never stored.

## Usage

Requires Python 3.8+ (standard library only; tkinter for the GUIs). The ICON tools target Windows.

```bash
# ICON database GUI
python icon_stuck_calls.py

# Log-file scan (CLI)
python find_stuck_calls.py --vq VQ_Sales --tserver tserver.log [--statserver statserver.log] [--threshold-sec 300] [--debug-unmatched]

# Log-file scan (GUI)
python gui.py
```

## Tests

```bash
python -m unittest discover tests
```
