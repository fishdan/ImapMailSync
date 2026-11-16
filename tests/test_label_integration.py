"""Disposable end-to-end source-label cycles over localhost IMAP sockets."""

from datetime import datetime, timedelta, timezone
import imaplib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gmail_imap_fixture import GmailIMAPFixture
from mailsync.config import load_account
from mailsync.cycle import run_once
from mailsync.state import Store


class LabelIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.alpha = GmailIMAPFixture().__enter__()
        self.sources = {}
        self._account("alpha", self.alpha.port)
        self._add_source("yahoo")
        self.patch = patch("mailsync.imap_client.imaplib.IMAP4_SSL", side_effect=self._connect)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        for source in self.sources.values():
            source.__exit__(None, None, None)
        self.alpha.__exit__(None, None, None)
        self.temp.cleanup()

    def _connect(self, host, port, **_kwargs):
        return imaplib.IMAP4(host, port)

    def _account(self, name, port):
        directory = self.root / "accounts" / name / ".secrets"
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        path = directory / "account.json"
        path.write_text(json.dumps({
            "provider": "Disposable", "username": f"{name}@example.test", "password": "synthetic",
            "imap": {"host": "127.0.0.1", "port": port, "security": "SSL/TLS",
                     "authenticationRequired": True},
        }))
        path.chmod(0o600)

    def _add_source(self, name):
        fixture = GmailIMAPFixture().__enter__()
        self.sources[name] = fixture
        self._account(name, fixture.port)
        fixture.seed(b"Subject: old\r\n\r\nold")
        with Store(self.root) as store:
            store.init_baseline(name, load_account(self.root, name).identity,
                                load_account(self.root, "alpha").identity, 7, 2)
        return fixture

    def _new(self, source, number, *, old=False):
        date = datetime.now(timezone.utc) - timedelta(hours=25 if old else 1)
        return source.seed(f"Subject: test-{number}\r\n\r\nbody-{number}".encode(),
                           internaldate=date)

    def test_labels_repeat_cycles_age_cutoff_and_source_isolation(self):
        yahoo = self.sources["yahoo"]
        delta = self._add_source("delta")
        self._new(yahoo, "expired", old=True)
        for number in range(2):
            self._new(yahoo, number)
        self._new(delta, "delta")
        first, code = run_once(self.root)
        self.assertEqual(code, 0)
        outcomes = {item["account_id"]: item for item in first["accounts"]}
        self.assertEqual((outcomes["yahoo"]["copied"], outcomes["yahoo"]["labeled"]), (2, 2))
        self.assertEqual((outcomes["delta"]["copied"], outcomes["delta"]["labeled"]), (1, 1))
        self.assertEqual(len(self.alpha.messages), 3)
        self.assertEqual(sorted(tuple(message.labels) for message in self.alpha.messages.values()),
                         [("delta@example",), ("yahoo@example",), ("yahoo@example",)])
        for _ in range(3):
            repeat, repeat_code = run_once(self.root)
            self.assertEqual(repeat_code, 0)
            self.assertEqual(sum(item["copied"] for item in repeat["accounts"]), 0)
        self.assertEqual(len(self.alpha.messages), 3)
        self.assertEqual(yahoo.messages[3].flags, set())

    def test_lost_append_and_store_acknowledgements(self):
        source_uid = self._new(self.sources["yahoo"], "append")
        self.alpha.fault_next("APPEND", "lose_ack")
        first, first_code = run_once(self.root)
        self.assertEqual(first_code, 1)
        self.assertEqual(first["accounts"][0]["issue_uids"], [2])
        self.sources["yahoo"].messages[source_uid].internaldate -= timedelta(days=2)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with Store(self.root) as store, store.db:
            store.db.execute("UPDATE transfer_attempts SET started_at=? WHERE source_uid=?",
                             (old, source_uid))
        second, second_code = run_once(self.root)
        self.assertEqual(second_code, 0)
        self.assertEqual(second["accounts"][0]["labeled"], 1)
        self.assertEqual(len(self.alpha.messages), 1)

        source_uid = self._new(self.sources["yahoo"], "store")
        self.alpha.fault_next("UID STORE", "lose_ack")
        third, third_code = run_once(self.root)
        self.assertEqual(third_code, 1)
        self.assertEqual(third["accounts"][0]["label_pending"], 1)
        self.sources["yahoo"].messages[source_uid].internaldate -= timedelta(days=2)
        fourth, fourth_code = run_once(self.root)
        self.assertEqual(fourth_code, 0)
        self.assertEqual(fourth["accounts"][0]["labeled"], 1)
        self.assertEqual(len(self.alpha.messages), 2)

    def test_one_source_outage_does_not_stop_the_other(self):
        delta = self._add_source("delta")
        self._new(delta, "delta")
        self._new(self.sources["yahoo"], "yahoo")
        delta.fault_next("CAPABILITY", "disconnect")
        first, first_code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in first["accounts"]}
        self.assertEqual(first_code, 1)
        self.assertEqual(outcomes["delta"]["status"], "failed")
        self.assertEqual(outcomes["yahoo"]["labeled"], 1)
        second, second_code = run_once(self.root)
        outcomes = {item["account_id"]: item for item in second["accounts"]}
        self.assertEqual(second_code, 0)
        self.assertEqual(outcomes["delta"]["labeled"], 1)
        self.assertEqual(outcomes["yahoo"]["copied"], 0)
        self.assertEqual(len(self.alpha.messages), 2)

    def test_hundred_message_batch(self):
        source = self.sources["yahoo"]
        for number in range(100):
            self._new(source, number)
        started = time.perf_counter()
        first, code = run_once(self.root)
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0)
        self.assertEqual((first["accounts"][0]["copied"], first["accounts"][0]["labeled"]),
                         (100, 100))
        self.assertLess(elapsed, 300)
        self.assertEqual(len(self.alpha.messages), 100)
        self.assertTrue(all(message.labels == {"yahoo@example"}
                            for message in self.alpha.messages.values()))
        repeat, repeat_code = run_once(self.root)
        self.assertEqual(repeat_code, 0)
        self.assertEqual(repeat["accounts"][0]["copied"], 0)


if __name__ == "__main__":
    unittest.main()
