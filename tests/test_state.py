from pathlib import Path
import sqlite3
import tempfile
import unittest

from mailsync.state import StateError, Store


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_baseline_and_copy_record_survive_reopen(self):
        with Store(self.root) as store:
            self.assertEqual(store.version(), 1)
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            store.begin_attempt("yahoo", 7, 101, "ms1-marker")
            store.confirm("yahoo", 7, 101, "ms1-marker", 9, 501)
        with Store(self.root) as store:
            self.assertEqual(store.baseline("yahoo")["first_eligible_uid"], 101)
            self.assertEqual(store.confirmed_uids("yahoo", 7), {101})
            self.assertEqual(store.copy_record("yahoo", 7, 101)["destination_uid"], 501)

    def test_baseline_cannot_reset_and_copy_key_is_unique(self):
        with Store(self.root) as store:
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            with self.assertRaises(StateError):
                store.init_baseline("yahoo", "source-id", "dest-id", 7, 1)
            store.begin_attempt("yahoo", 7, 101, "ms1-marker")
            store.confirm("yahoo", 7, 101, "ms1-marker", 9, 501)
            with self.assertRaises(StateError):
                store.confirm("yahoo", 7, 101, "ms1-marker", 9, 502)

    def test_uncertain_attempt_retains_marker(self):
        with Store(self.root) as store:
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            store.begin_attempt("yahoo", 7, 101, "ms1-marker")
            store.mark_uncertain("yahoo", 7, 101, "APPEND_UNKNOWN")
            self.assertEqual(store.attempt("yahoo", 7, 101)["marker"], "ms1-marker")
            store.begin_attempt("yahoo", 7, 101, "ms1-marker")
            with self.assertRaises(StateError):
                store.begin_attempt("yahoo", 7, 101, "different-marker")

    def test_corrupt_or_unknown_schema_fails_closed(self):
        with Store(self.root):
            pass
        database = self.root / ".state" / "mailsync.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("UPDATE schema_version SET version = 999")
        connection.commit()
        connection.close()
        with self.assertRaises(StateError):
            Store(self.root)


if __name__ == "__main__":
    unittest.main()
