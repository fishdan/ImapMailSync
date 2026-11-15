import unittest
from datetime import datetime, timedelta, timezone

from mailsync.baseline import eligible_uids, marker_for, within_24_hours


class BaselineTests(unittest.TestCase):
    def test_excludes_old_mail_and_confirmed_uids(self):
        self.assertEqual(eligible_uids([3, 8, 10, 11], 10, {10}), [11])

    def test_empty_next_uid_boundary_never_selects_last_old_message(self):
        self.assertEqual(eligible_uids([3, 8, 10], 11, set()), [])

    def test_arrival_during_cycle_is_found_on_later_enumeration(self):
        first = eligible_uids([5, 6], 7, set())
        later = eligible_uids([5, 6, 7], 7, set())
        self.assertEqual(first, [])
        self.assertEqual(later, [7])

    def test_marker_is_stable_and_scoped_to_source_occurrence(self):
        first = marker_for("yahoo", "identity", 7, 101)
        self.assertEqual(first, marker_for("yahoo", "identity", 7, 101))
        self.assertNotEqual(first, marker_for("yahoo", "identity", 7, 102))
        self.assertNotEqual(first, marker_for("delta", "identity", 7, 101))
        self.assertTrue(first.startswith("ms1-"))

    def test_exact_arrival_cutoff_including_timezone_offset(self):
        now = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=24)
        self.assertTrue(within_24_hours(cutoff, now))
        self.assertFalse(within_24_hours(cutoff - timedelta(seconds=1), now))
        self.assertTrue(within_24_hours(datetime(2026, 11, 14, 15, 0,
                                                  tzinfo=timezone(timedelta(hours=-5))), now))
        self.assertFalse(within_24_hours(now + timedelta(seconds=1), now))


if __name__ == "__main__":
    unittest.main()
