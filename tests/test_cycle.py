from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mailsync.baseline import marker_for
from mailsync.config import load_account
from mailsync.cycle import process_lock, run_once
from mailsync.imap_client import IMAPError, SourceMessage
from mailsync.state import Store


def account(root: Path, name: str) -> None:
    secret = root / "accounts" / name / ".secrets"
    secret.mkdir(parents=True)
    secret.chmod(0o700)
    path = secret / "account.json"
    path.write_text(json.dumps({
        "provider": "Test", "username": f"{name}@example.test", "password": "test-password",
        "imap": {"host": "localhost", "port": 993, "security": "SSL/TLS", "authenticationRequired": True},
    }))
    path.chmod(0o600)


class World:
    def __init__(self):
        self.source_uids = {"yahoo": [1, 2], "delta": [1, 2]}
        self.destination = {}
        self.next_uid = 10
        self.append_calls = 0
        self.lose_ack_once = False
        self.unavailable = set()
        self.uidvalidity = {"yahoo": 7, "delta": 7, "alpha": 7}
        self.internaldate = datetime.now(timezone.utc) - timedelta(hours=1)
        self.labels = {}
        self.existing_labels = set()
        self.capability = True
        self.label_failure = False
        self.label_store_calls = 0
        self.lose_store_ack_once = False
        self.account_wide_absent = False
        self.marker_search_failure = False


class FakeSession:
    def __init__(self, account_value, world):
        self.account = account_value
        self.world = world
        if account_value.account_id in world.unavailable:
            raise IMAPError("UNAVAILABLE")
        self.uidvalidity = world.uidvalidity[account_value.account_id]
        self.uidnext = 3

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def uids(self):
        return self.world.source_uids[self.account.account_id][:]

    def uids_since(self, _cutoff):
        return self.world.source_uids[self.account.account_id][:]

    def fetch(self, uid):
        return SourceMessage(uid, f"Subject: test-{self.account.account_id}-{uid}\r\n\r\nbody".encode(),
                             False, self.world.internaldate)

    def find_marker(self, marker, _cutoff):
        return self.world.destination.get(marker, [])[:]

    def append_copy(self, _raw, _seen, _date, marker):
        self.world.append_calls += 1
        self.world.destination[marker] = [self.world.next_uid]
        self.world.next_uid += 1
        if self.world.lose_ack_once:
            self.world.lose_ack_once = False
            raise IMAPError("ACK_LOST")

    def supports_labels(self):
        return self.world.capability

    def ensure_label(self, label):
        if not self.world.capability:
            raise IMAPError("LABEL_CAPABILITY_UNAVAILABLE")
        self.world.existing_labels.add(label)

    def labels_for_uid(self, uid):
        return self.world.labels.get(uid, set()).copy()

    def add_label(self, uid, label):
        self.world.label_store_calls += 1
        if self.world.label_failure:
            raise IMAPError("LABEL_STORE_FAILED")
        self.world.labels.setdefault(uid, set()).add(label)
        if self.world.lose_store_ack_once:
            self.world.lose_store_ack_once = False
            raise IMAPError("ACK_LOST")

    def marker_at_uid(self, uid, marker):
        return uid in self.world.destination.get(marker, [])

    def find_marker_exact(self, marker):
        if self.world.marker_search_failure:
            raise IMAPError("SEARCH_UNAVAILABLE")
        return self.world.destination.get(marker, [])[:]

    def copy_absent_account_wide(self, marker):
        return self.world.account_wide_absent


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        account(self.root, "alpha")
        account(self.root, "yahoo")
        self.world = World()
        self.patch = patch("mailsync.cycle.IMAPSession", side_effect=lambda value: FakeSession(value, self.world))
        self.patch.start()
        with Store(self.root) as store:
            store.init_baseline("yahoo", load_account(self.root, "yahoo").identity,
                                load_account(self.root, "alpha").identity, 7, 2)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_copies_new_uid_once_and_keeps_history_after_alpha_deletion(self):
        first, first_code = run_once(self.root)
        second, second_code = run_once(self.root)
        self.assertEqual((first_code, first["accounts"][0]["copied"]), (0, 1))
        self.assertEqual((second_code, second["accounts"][0]["copied"]), (0, 0))
        self.assertEqual(self.world.append_calls, 1)
        self.world.destination.clear()
        third, third_code = run_once(self.root)
        self.assertEqual((third_code, third["accounts"][0]["copied"]), (0, 0))
        self.assertEqual(self.world.append_calls, 1)

    def test_new_copy_gets_derived_label_and_prior_copy_is_untouched(self):
        with Store(self.root) as store:
            store.begin_attempt("yahoo", 7, 3, "ms1-legacy")
            store.confirm("yahoo", 7, 3, "ms1-legacy", 7, 99)
        self.world.source_uids["yahoo"] = [2, 3]
        report, code = run_once(self.root)
        self.assertEqual(code, 0)
        uid = next(iter(self.world.labels))
        self.assertEqual(self.world.labels[uid], {"yahoo@example"})
        self.assertEqual(report["accounts"][0]["labeled"], 1)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "confirmed")
            self.assertIsNone(store.label_job("yahoo", 7, 3))

    def test_existing_label_reused_and_unrelated_label_preserved(self):
        self.world.existing_labels.add("yahoo@example")
        self.world.labels[10] = {"personal"}
        first, code = run_once(self.root)
        second, second_code = run_once(self.root)
        self.assertEqual((code, second_code), (0, 0))
        self.assertEqual(first["accounts"][0]["labeled"], 1)
        self.assertEqual(self.world.labels[10], {"personal", "yahoo@example"})
        self.assertEqual(self.world.label_store_calls, 1)
        self.assertEqual(self.world.append_calls, 1)

    def test_invalid_source_address_isolated_from_healthy_source(self):
        account(self.root, "delta")
        path = self.root / "accounts/delta/.secrets/account.json"
        data = json.loads(path.read_text())
        data["username"] = "invalid-address"
        path.write_text(json.dumps(data))
        with Store(self.root) as store:
            store.init_baseline("delta", load_account(self.root, "delta").identity,
                                load_account(self.root, "alpha").identity, 7, 2)
        report, code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in report["accounts"]}
        self.assertEqual(code, 1)
        self.assertEqual(outcomes["delta"]["error_code"], "INVALID_SOURCE_ADDRESS")
        self.assertEqual(outcomes["yahoo"]["labeled"], 1)
        self.assertEqual(self.world.append_calls, 1)

    def test_unsupported_capability_and_invalid_address_prevent_append(self):
        self.world.capability = False
        report, code = run_once(self.root)
        self.assertEqual(code, 1)
        self.assertEqual(report["accounts"][0]["error_code"], "LABEL_CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.world.append_calls, 0)
        self.world.capability = True
        path = self.root / "accounts/yahoo/.secrets/account.json"
        data = json.loads(path.read_text())
        data["username"] = "invalid"
        path.write_text(json.dumps(data))
        report, code = run_once(self.root)
        self.assertEqual(code, 3)  # Identity protection blocks the changed account.
        self.assertEqual(self.world.append_calls, 0)

    def test_label_failure_reports_pending_without_duplicate_append(self):
        self.world.label_failure = True
        report, code = run_once(self.root)
        self.assertEqual(code, 1)
        self.assertEqual(report["accounts"][0]["label_pending"], 1)
        self.assertEqual(report["accounts"][0]["issue_uids"], [2])
        self.assertEqual(self.world.append_calls, 1)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "pending")

    def test_lost_store_ack_is_recovered_without_another_store_or_append(self):
        self.world.lose_store_ack_once = True
        first, first_code = run_once(self.root)
        self.assertEqual(first_code, 1)
        self.assertEqual(first["accounts"][0]["label_pending"], 1)
        second, second_code = run_once(self.root)
        self.assertEqual(second_code, 0)
        self.assertEqual(second["accounts"][0]["labeled"], 1)
        self.assertEqual(self.world.append_calls, 1)
        self.assertEqual(self.world.label_store_calls, 1)

    def _pending_copy(self, *, known_uid=True):
        yahoo = load_account(self.root, "yahoo")
        marker = marker_for("yahoo", yahoo.identity, 7, 2)
        with Store(self.root) as store:
            store.begin_label_job("yahoo", 7, 2, marker, "yahoo@example")
            if known_uid:
                store.confirm("yahoo", 7, 2, marker, 7, 10)
        if known_uid:
            self.world.destination[marker] = [10]
        return marker

    def test_accepted_copy_repaired_after_24_hours_during_source_outage(self):
        self._pending_copy()
        self.world.internaldate = datetime.now(timezone.utc) - timedelta(days=2)
        self.world.unavailable.add("yahoo")
        report, _ = run_once(self.root)
        self.assertEqual(report["accounts"][0]["labeled"], 1)
        self.assertEqual(self.world.append_calls, 0)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "confirmed")

    def test_lost_append_ack_is_reconciled_after_24_hours_without_source(self):
        marker = self._pending_copy(known_uid=False)
        self.world.destination[marker] = [10]
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with Store(self.root) as store, store.db:
            store.db.execute("UPDATE transfer_attempts SET started_at=? WHERE account_id='yahoo'", (old,))
        self.world.unavailable.add("yahoo")
        report, _ = run_once(self.root)
        self.assertEqual(report["accounts"][0]["labeled"], 1)
        self.assertEqual(self.world.append_calls, 0)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "confirmed")
            self.assertEqual(store.copy_record("yahoo", 7, 2)["destination_uid"], 10)

    def test_stale_saved_uid_uses_exact_marker_reconciliation(self):
        marker = self._pending_copy()
        self.world.destination[marker] = [11]
        self.world.source_uids["yahoo"] = []
        report, code = run_once(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(report["accounts"][0]["labeled"], 1)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["destination_uid"], 11)
            self.assertEqual(store.copy_record("yahoo", 7, 2)["destination_uid"], 11)

    def test_unaccepted_old_job_expires_without_append(self):
        self._pending_copy(known_uid=False)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with Store(self.root) as store, store.db:
            store.db.execute("UPDATE transfer_attempts SET started_at=? WHERE account_id='yahoo'", (old,))
        self.world.source_uids["yahoo"] = []
        report, _ = run_once(self.root)
        self.assertEqual(self.world.append_calls, 0)
        self.assertEqual(report["accounts"][0]["issue_uids"], [2])
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "expired")

    def test_inbox_miss_stays_pending_until_account_wide_absence_proven(self):
        marker = self._pending_copy()
        self.world.destination.pop(marker)
        self.world.source_uids["yahoo"] = []
        first, _ = run_once(self.root)
        self.assertEqual(first["accounts"][0]["label_pending"], 1)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "pending")
        self.world.account_wide_absent = True
        second, _ = run_once(self.root)
        self.assertEqual(self.world.append_calls, 0)
        self.assertEqual(second["accounts"][0]["status"], "failed")
        self.assertEqual(second["accounts"][0]["label_pending"], 0)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "missing")

    def test_marker_search_failure_leaves_job_pending(self):
        self._pending_copy(known_uid=False)
        self.world.marker_search_failure = True
        self.world.source_uids["yahoo"] = []
        run_once(self.root)
        with Store(self.root) as store:
            self.assertEqual(store.label_job("yahoo", 7, 2)["status"], "pending")

    def test_multiple_pending_marker_matches_block_without_append(self):
        marker = self._pending_copy(known_uid=False)
        self.world.destination[marker] = [10, 11]
        self.world.source_uids["yahoo"] = []
        report, code = run_once(self.root)
        self.assertEqual(code, 3)
        self.assertEqual(report["accounts"][0]["error_code"], "MULTIPLE_MARKER_MATCHES")
        self.assertEqual(self.world.append_calls, 0)

    def test_lost_ack_is_reconciled_before_retry(self):
        self.world.lose_ack_once = True
        first, first_code = run_once(self.root)
        self.assertEqual(first_code, 1)
        self.assertEqual(first["accounts"][0]["failed"], 1)
        self.assertEqual(first["accounts"][0]["issue_uids"], [2])
        with Store(self.root) as store:
            self.assertEqual(store.attempt("yahoo", 7, 2)["status"], "uncertain")
        second, second_code = run_once(self.root)
        self.assertEqual(second_code, 0)
        self.assertEqual(second["accounts"][0]["copied"], 1)
        self.assertEqual(self.world.append_calls, 1)

    def test_one_source_failure_does_not_stop_other(self):
        account(self.root, "delta")
        with Store(self.root) as store:
            store.init_baseline("delta", load_account(self.root, "delta").identity,
                                load_account(self.root, "alpha").identity, 7, 2)
        self.world.unavailable.add("delta")
        report, code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in report["accounts"]}
        self.assertEqual(code, 1)
        self.assertEqual(outcomes["delta"]["status"], "failed")
        self.assertEqual(outcomes["yahoo"]["copied"], 1)

    def test_invalid_source_configuration_does_not_stop_healthy_source(self):
        account(self.root, "delta")
        with Store(self.root) as store:
            store.init_baseline("delta", load_account(self.root, "delta").identity,
                                load_account(self.root, "alpha").identity, 7, 2)
        yahoo_path = self.root / "accounts" / "yahoo" / ".secrets" / "account.json"
        yahoo = json.loads(yahoo_path.read_text())
        yahoo["password"] = "YOUR_APP_PASSWORD"
        yahoo_path.write_text(json.dumps(yahoo))
        report, code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in report["accounts"]}
        self.assertEqual(code, 1)
        self.assertEqual(outcomes["yahoo"]["error_code"], "PLACEHOLDER_CREDENTIALS")
        self.assertEqual(outcomes["delta"]["copied"], 1)
        self.assertNotIn("YOUR_APP_PASSWORD", str(report))

    def test_adding_delta_keeps_yahoo_history(self):
        first, first_code = run_once(self.root)
        self.assertEqual((first_code, first["accounts"][0]["copied"]), (0, 1))
        account(self.root, "delta")
        with Store(self.root) as store:
            store.init_baseline("delta", load_account(self.root, "delta").identity,
                                load_account(self.root, "alpha").identity, 7, 2)
        second, second_code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in second["accounts"]}
        self.assertEqual(second_code, 0)
        self.assertEqual(outcomes["yahoo"]["copied"], 0)
        self.assertEqual(outcomes["delta"]["copied"], 1)
        with Store(self.root) as store:
            self.assertEqual(store.confirmed_uids("yahoo", 7), {2})

    def test_multiple_marker_matches_block_source(self):
        yahoo = load_account(self.root, "yahoo")
        self.world.destination[marker_for("yahoo", yahoo.identity, 7, 2)] = [10, 11]
        report, code = run_once(self.root)
        self.assertEqual(code, 3)
        self.assertEqual(report["accounts"][0]["status"], "blocked")
        with Store(self.root) as store:
            self.assertEqual(store.baseline("yahoo")["status"], "blocked")

    def test_uidvalidity_change_blocks_source(self):
        self.world.uidvalidity["yahoo"] = 8
        report, code = run_once(self.root)
        self.assertEqual(code, 3)
        self.assertEqual(report["accounts"][0]["error_code"], "SOURCE_UIDVALIDITY_CHANGED")
        self.assertEqual(self.world.append_calls, 0)

    def test_connection_identity_change_blocks_source(self):
        yahoo_path = self.root / "accounts" / "yahoo" / ".secrets" / "account.json"
        yahoo = json.loads(yahoo_path.read_text())
        yahoo["imap"]["host"] = "another.example.test"
        yahoo_path.write_text(json.dumps(yahoo))
        report, code = run_once(self.root)
        self.assertEqual(code, 3)
        self.assertEqual(report["accounts"][0]["error_code"], "ACCOUNT_IDENTITY_CHANGED")
        self.assertEqual(self.world.append_calls, 0)

    def test_lock_prevents_overlapping_cycle(self):
        with process_lock(self.root):
            report, code = run_once(self.root)
        self.assertEqual(code, 2)
        self.assertEqual(report["error_code"], "RUN_ALREADY_ACTIVE")

    def test_expired_source_message_is_never_appended(self):
        self.world.internaldate = datetime.now(timezone.utc) - timedelta(hours=25)
        report, code = run_once(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(report["accounts"][0]["copied"], 0)
        self.assertGreaterEqual(report["accounts"][0]["skipped"], 1)
        self.assertEqual(report["accounts"][0]["issue_uids"], [2])
        self.assertEqual(self.world.append_calls, 0)

    def test_message_aging_out_during_cycle_is_not_appended(self):
        with patch("mailsync.cycle.within_24_hours", side_effect=[True, False]):
            report, code = run_once(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(report["accounts"][0]["copied"], 0)
        self.assertEqual(self.world.append_calls, 0)
        with Store(self.root) as store:
            self.assertEqual(store.attempt("yahoo", 7, 2)["error_code"], "AGE_WINDOW_EXPIRED")

    def test_uncertain_attempt_ages_out_without_old_mail_search(self):
        yahoo = load_account(self.root, "yahoo")
        marker = marker_for("yahoo", yahoo.identity, 7, 2)
        with Store(self.root) as store:
            store.begin_attempt("yahoo", 7, 2, marker)
            store.mark_uncertain("yahoo", 7, 2, "TRANSFER_UNCERTAIN")
            old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            with store.db:
                store.db.execute("UPDATE transfer_attempts SET started_at=? WHERE account_id='yahoo'", (old_time,))
        self.world.source_uids["yahoo"] = []
        report, code = run_once(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(report["accounts"][0]["skipped"], 1)
        self.assertEqual(report["accounts"][0]["error_code"], "AGE_WINDOW_EXPIRED")
        self.assertEqual(self.world.append_calls, 0)
        with Store(self.root) as store:
            attempt = store.attempt("yahoo", 7, 2)
            self.assertEqual((attempt["status"], attempt["error_code"]),
                             ("failed", "AGE_WINDOW_EXPIRED"))


if __name__ == "__main__":
    unittest.main()
