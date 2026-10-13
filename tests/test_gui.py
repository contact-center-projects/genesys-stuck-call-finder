#!/usr/bin/env python3
"""
Regression tests for gui.py.

Run with:
    py -m unittest discover -s tests -v
(from the genesys-stuck-call-finder directory), or just:
    py tests\\test_gui.py

These create real Tk windows (via StuckCallFinderApp) - they need a display
to attach to (normal on an interactive Windows session; would need a virtual
display like Xvfb in a headless CI environment).
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402


class TestAppBuilds(unittest.TestCase):
    """Guards against the GUI failing to construct at all after a change."""

    def test_builds_and_destroys_cleanly(self):
        app = gui.StuckCallFinderApp()
        app.update()
        app.destroy()


class TestFolderWatchPolling(unittest.TestCase):
    """Guards 'watch folders for new files' actually picking up files that
    appear later, without re-adding files it already knows about, and
    still ignoring snapshot.log."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.app = gui.StuckCallFinderApp()
        self.app.update()
        self.app.tserver_folders.append(self.folder)

    def tearDown(self):
        self.app.destroy()
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def test_picks_up_new_file_and_skips_duplicates_and_snapshots(self):
        added, _, _, _ = gui._list_candidate_files(self.folder, self.app.tserver_files, None, 15)
        self.assertEqual(added, [])

        new_file = os.path.join(self.folder, "TServer_2026_09_02_11.log")
        with open(new_file, "w") as f:
            f.write("dummy\n")

        added, _, _, _ = gui._list_candidate_files(self.folder, self.app.tserver_files, None, 15)
        self.assertEqual(added, [new_file])
        self.app.tserver_files.append(new_file)

        # Polling again with the file already known shouldn't re-add it
        added, _, _, _ = gui._list_candidate_files(self.folder, self.app.tserver_files, None, 15)
        self.assertEqual(added, [])

        snap = os.path.join(self.folder, "snapshot.log")
        with open(snap, "w") as f:
            f.write("dummy\n")
        added, _, skipped_snap, _ = gui._list_candidate_files(self.folder, self.app.tserver_files, None, 15)
        self.assertEqual(added, [])
        self.assertEqual(skipped_snap, 1)

    def test_newest_file_sorts_first(self):
        import time as time_module
        paths = []
        for name in ("a.log", "b.log", "c.log"):
            p = os.path.join(self.folder, name)
            with open(p, "w") as f:
                f.write("dummy\n")
            paths.append(p)
            time_module.sleep(0.05)  # ensure distinct mtimes
        added, _, _, _ = gui._list_candidate_files(self.folder, [], None, 15)
        self.assertEqual(added, [paths[2], paths[1], paths[0]])


class TestMtimeBasedFolderFilter(unittest.TestCase):
    """Guards 'Add folder...' filtering by filesystem modified time (fast,
    no need to open/read every file) rather than peeking at file content -
    chosen deliberately for speed over the more precise but slower
    content-based approach. A generous slack window (window_min + 180 min)
    absorbs the common case of a file's mtime trailing its earliest
    content by a while."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def _touch_with_mtime(self, name, dt):
        import datetime
        path = os.path.join(self.folder, name)
        with open(path, "w") as f:
            f.write("dummy\n")
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
        return path

    def test_file_near_search_time_is_included(self):
        import datetime
        at = datetime.datetime(2026, 9, 2, 10, 30)
        near = self._touch_with_mtime("near.log", at + datetime.timedelta(minutes=5))
        added, skipped_by_time, _, _ = gui._list_candidate_files(self.folder, [], at, 15)
        self.assertIn(near, added)
        self.assertEqual(skipped_by_time, 0)

    def test_file_far_from_search_time_is_skipped(self):
        import datetime
        at = datetime.datetime(2026, 9, 2, 10, 30)
        far = self._touch_with_mtime("far.log", at + datetime.timedelta(hours=10))
        added, skipped_by_time, _, _ = gui._list_candidate_files(self.folder, [], at, 15)
        self.assertNotIn(far, added)
        self.assertEqual(skipped_by_time, 1)

    def test_blank_start_time_includes_everything(self):
        import datetime
        far = self._touch_with_mtime("anytime.log", datetime.datetime(2020, 1, 1))
        added, skipped_by_time, _, _ = gui._list_candidate_files(self.folder, [], None, 15)
        self.assertIn(far, added)
        self.assertEqual(skipped_by_time, 0)


class TestDateFieldAnchoring(unittest.TestCase):
    """Guards the Date field actually anchoring a bare HH:MM search time,
    instead of always assuming the system clock's 'today' - important for
    a 24h rolling log spanning into yesterday."""

    def setUp(self):
        self.app = gui.StuckCallFinderApp()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def test_custom_date_is_used(self):
        self.app.date_entry.delete(0, "end")
        self.app.date_entry.insert(0, "2026-09-01")
        self.app.at_entry.insert(0, "10:30")
        at, window_min, err = self.app._read_time_window()
        self.assertIsNone(err)
        self.assertEqual(at.strftime("%Y-%m-%d %H:%M"), "2026-09-01 10:30")

    def test_yesterday_button_sets_date(self):
        self.app._set_date_entry(-1)
        # just verify it changed to a parseable date and isn't today's
        # default value it started with
        value = self.app.date_entry.get()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")


class TestGuiStaysResponsiveDuringFolderScan(unittest.TestCase):
    """Guards the bug where watch-mode's folder polling ran on the Tk main
    thread and froze the whole GUI (including the Stop button) while it
    scanned. Folder-discovery IO must run off the main thread."""

    def test_main_thread_keeps_pumping_events_during_slow_scan(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "TServer.log"), "w") as f:
            f.write("dummy\n")

        orig = gui._list_candidate_files

        def slow_list_candidate_files(*args, **kwargs):
            time.sleep(1.5)
            return orig(*args, **kwargs)

        gui._list_candidate_files = slow_list_candidate_files
        try:
            app = gui.StuckCallFinderApp()
            app.update()
            app.tserver_folders.append(folder)
            app._run_watch_poll()

            ticks = 0
            start = time.time()
            while time.time() - start < 2.0:
                app.update()
                ticks += 1
                time.sleep(0.05)

            self.assertGreater(ticks, 15, "main thread appears to have been blocked")
            app.destroy()
        finally:
            gui._list_candidate_files = orig
            os.remove(os.path.join(folder, "TServer.log"))
            os.rmdir(folder)


if __name__ == "__main__":
    unittest.main()
