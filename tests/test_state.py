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
            self.assertEqual(store.version(), 2)
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
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version VALUES (999, 'now')")
        connection.commit()
        connection.close()
        with self.assertRaises(StateError):
            Store(self.root)

    def test_version_one_upgrade_preserves_legacy_data_without_label_jobs(self):
        database = self.root / ".state" / "mailsync.sqlite3"
        database.parent.mkdir()
        connection = sqlite3.connect(database)
        migration = (Path(__file__).resolve().parents[1] / "src/mailsync/migrations/001_initial.sql")
        connection.executescript(migration.read_text())
        connection.execute("INSERT INTO schema_version VALUES (1, '2026-11-15T00:00:00+00:00')")
        connection.execute("INSERT INTO source_baselines VALUES (?,?,?,?,?,?,?)",
                           ("yahoo", "source-id", "dest-id", 7, 101, "now", "active"))
        connection.execute("INSERT INTO transfer_attempts VALUES (?,?,?,?,?,?,?,?)",
                           ("yahoo", 7, 101, "ms1-legacy", "confirmed", "now", "now", None))
        connection.execute("INSERT INTO copy_records VALUES (?,?,?,?,?,?,?)",
                           ("yahoo", 7, 101, "ms1-legacy", 9, 501, "now"))
        connection.commit()
        connection.close()
        with Store(self.root) as store:
            self.assertEqual(store.version(), 2)
            self.assertEqual(store.baseline("yahoo")["first_eligible_uid"], 101)
            self.assertEqual(store.copy_record("yahoo", 7, 101)["destination_uid"], 501)
            self.assertEqual(store.pending_label_jobs(), [])
            self.assertIsNone(store.label_job("yahoo", 7, 101))

    def test_job_precedes_append_and_uid_evidence_survives_reopen(self):
        with Store(self.root) as store:
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            store.begin_label_job("yahoo", 7, 101, "ms1-new", "person/yahoo")
            self.assertEqual(store.attempt("yahoo", 7, 101)["status"], "running")
            self.assertEqual(store.label_job("yahoo", 7, 101)["status"], "pending")
            self.assertIsNone(store.copy_record("yahoo", 7, 101))
        with Store(self.root) as store:
            self.assertEqual(store.pending_label_jobs("yahoo")[0]["marker"], "ms1-new")
            store.confirm("yahoo", 7, 101, "ms1-new", 9, 502)
            job = store.label_job("yahoo", 7, 101)
            self.assertEqual((job["destination_uidvalidity"], job["destination_uid"]), (9, 502))
            self.assertEqual(job["status"], "pending")
            self.assertEqual(store.copy_record("yahoo", 7, 101)["destination_uid"], 502)

    def test_job_identity_and_terminal_transitions(self):
        with Store(self.root) as store:
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            store.begin_label_job("yahoo", 7, 101, "ms1-new", "person/yahoo")
            store.begin_label_job("yahoo", 7, 101, "ms1-new", "person/yahoo")
            with self.assertRaises(StateError):
                store.begin_label_job("yahoo", 7, 101, "ms1-new", "other/label")
            store.confirm("yahoo", 7, 101, "ms1-new", 9, 502)
            store.set_label_status("yahoo", 7, 101, "pending", "LABEL_STORE_FAILED")
            store.set_label_status("yahoo", 7, 101, "confirmed")
            self.assertEqual(store.pending_label_jobs(), [])
            with self.assertRaises(StateError):
                store.set_label_status("yahoo", 7, 101, "pending")

    def test_expiration_skips_pending_label_jobs(self):
        from datetime import datetime, timedelta, timezone

        with Store(self.root) as store:
            store.init_baseline("yahoo", "source-id", "dest-id", 7, 101)
            store.begin_label_job("yahoo", 7, 101, "ms1-new", "person/yahoo")
            cutoff = datetime.now(timezone.utc) + timedelta(days=2)
            self.assertEqual(store.expire_old_attempts("yahoo", 7, cutoff), set())
            self.assertEqual(store.label_job("yahoo", 7, 101)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
