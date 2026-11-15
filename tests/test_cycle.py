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
        "provider": "Test", "username": name, "password": "test-password",
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
