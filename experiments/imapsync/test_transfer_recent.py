"""Offline boundary checks for the exact trial time window."""

from datetime import datetime, timezone
import unittest

from transfer_recent import within_window


class WindowTests(unittest.TestCase):
    def test_before_cutoff_is_excluded(self):
        cutoff = datetime(2026, 11, 14, 20, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(within_window(b"14-Nov-2026 14:59:59 -0500", cutoff))

    def test_at_and_after_cutoff_are_included(self):
        cutoff = datetime(2026, 11, 14, 20, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(within_window(b"14-Nov-2026 15:00:00 -0500", cutoff))
        self.assertTrue(within_window(b"14-Nov-2026 15:00:01 -0500", cutoff))
