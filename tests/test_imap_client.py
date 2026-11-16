from datetime import datetime, timedelta, timezone
import ssl
import unittest
from unittest.mock import patch

from mailsync.baseline import marker_for
from mailsync.config import Account
from mailsync.imap_client import IMAPSession
from mailsync.imap_client import IMAPError
from gmail_imap_fixture import GmailIMAPFixture
import imaplib


class FakeIMAP:
    def __init__(self):
        self.context = None
        self.commands = []
        self.appended = None

    def login(self, username, password):
        self.commands.append(("LOGIN", username, password))
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"7" if name == "UIDVALIDITY" else b"43"]

    def uid(self, command, *args):
        self.commands.append(("UID", command, args))
        if command == "SEARCH" and args[-1] == "ALL":
            return "OK", [b"1 42"]
        if command == "SEARCH" and "SINCE" in args and "HEADER" not in args:
            return "OK", [b"42"]
        if command == "SEARCH":
            return "OK", [b"17 18"]
        if command == "FETCH" and args[0] == "17":
            return "OK", [b'1 (UID 17 INTERNALDATE "14-Nov-2026 14:59:59 -0500")']
        if command == "FETCH" and args[0] == "18":
            return "OK", [b'2 (UID 18 INTERNALDATE "14-Nov-2026 15:00:00 -0500")']
        if command == "FETCH":
            return "OK", [(b'1 (UID 42 FLAGS (\\Seen) INTERNALDATE "15-Nov-2026 12:00:00 +0000" BODY[] {23}',
                           b"From: source@test\r\n\r\nbody")]
        raise AssertionError(command)

    def noop(self):
        self.commands.append(("NOOP",))
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, message):
        self.commands.append(("APPEND", mailbox, flags))
        self.appended = (mailbox, flags, date_time, message)
        return "OK", [b"APPENDUID 7 17"]

    def logout(self):
        self.commands.append(("LOGOUT",))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.account = Account("yahoo", "Test", "source@test", "password", "imap.example.test", 993)
        self.fake = FakeIMAP()

    def factory(self, *_args, **kwargs):
        self.fake.context = kwargs["ssl_context"]
        return self.fake

    def test_tls_and_read_only_fetch(self):
        with patch("mailsync.imap_client.imaplib.IMAP4_SSL", side_effect=self.factory):
            with IMAPSession(self.account) as session:
                self.assertEqual(session.uidvalidity, 7)
                self.assertEqual(session.uidnext, 43)
                self.assertEqual(session.uids(), [1, 42])
                self.assertEqual(session.uids_since(datetime(2026, 11, 14, 20,
                                                               tzinfo=timezone.utc)), [42])
                item = session.fetch(42)
        self.assertTrue(self.fake.context.check_hostname)
        self.assertEqual(self.fake.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn(("SELECT", "INBOX", True), self.fake.commands)
        self.assertTrue(item.seen)
        self.assertEqual(item.raw, b"From: source@test\r\n\r\nbody")
        self.assertIn(("UID", "FETCH", ("42", "(UID FLAGS INTERNALDATE BODY.PEEK[])")), self.fake.commands)
        self.assertIn(("UID", "SEARCH", (None, "SINCE", "13-Nov-2026")), self.fake.commands)

    def test_marker_append_and_reconciliation_search(self):
        destination = Account("alpha", "Test", "dest@test", "password", "imap.example.test", 993)
        marker = marker_for("yahoo", self.account.identity, 7, 42)
        cutoff = datetime(2026, 11, 14, 20, tzinfo=timezone.utc)
        with patch("mailsync.imap_client.imaplib.IMAP4_SSL", side_effect=self.factory):
            with IMAPSession(destination) as session:
                self.assertEqual(session.find_marker(marker, cutoff), [18])
                session.append_copy(b"From: source@test\r\n\r\nbody", True,
                                    datetime(2026, 11, 15, 12, tzinfo=timezone.utc), marker)
        mailbox, flags, _, raw = self.fake.appended
        self.assertEqual((mailbox, flags), ("INBOX", r"(\Seen)"))
        self.assertTrue(raw.startswith(b"X-Mailsync-Key: " + marker.encode() + b"\r\n"))
        self.assertTrue(raw.endswith(b"From: source@test\r\n\r\nbody"))
        self.assertIn(("UID", "SEARCH", (None, "SINCE", "13-Nov-2026", "HEADER",
                                           "X-Mailsync-Key", marker)), self.fake.commands)
        self.assertFalse(any(command[0] in {"STORE", "EXPUNGE"} for command in self.fake.commands))


class GmailProtocolTests(unittest.TestCase):
    def account(self, account_id):
        return Account(account_id, "Disposable", f"{account_id}@example.test", "synthetic",
                       "127.0.0.1", 993)

    def connect(self, fixture, account):
        def factory(*_args, **_kwargs):
            return imaplib.IMAP4(fixture.host, fixture.port)
        return patch("mailsync.imap_client.imaplib.IMAP4_SSL", side_effect=factory)

    def test_source_readonly_destination_readwrite_and_capability_gate(self):
        with GmailIMAPFixture(gmail_extension=False) as fixture:
            with self.connect(fixture, self.account("source")):
                with IMAPSession(self.account("source")):
                    pass
            self.assertIn("EXAMINE INBOX", fixture.commands)
            with self.connect(fixture, self.account("alpha")):
                with IMAPSession(self.account("alpha")) as destination:
                    with self.assertRaises(IMAPError) as caught:
                        destination.ensure_label("source/example")
            self.assertEqual(str(caught.exception), "LABEL_UNSUPPORTED")
            self.assertIn("SELECT INBOX", fixture.commands)
            self.assertFalse(any(command.startswith("CREATE") for command in fixture.commands))

    def test_create_reuse_additive_store_and_exact_fetch(self):
        with GmailIMAPFixture() as fixture:
            uid = fixture.seed(b"X-Mailsync-Key: ms1-test\r\n\r\nbody", labels={"other"})
            with self.connect(fixture, self.account("alpha")):
                with IMAPSession(self.account("alpha")) as destination:
                    destination.ensure_label('source/"quoted"')
                    destination.ensure_label('source/"quoted"')
                    self.assertEqual(destination.labels_for_uid(uid), {"other"})
                    destination.add_label(uid, 'source/"quoted"')
                    self.assertEqual(destination.labels_for_uid(uid),
                                     {"other", 'source/"quoted"'})
            self.assertEqual(sum(command.startswith("CREATE") for command in fixture.commands), 1)
            self.assertEqual(fixture.messages[uid].labels, {"other", 'source/"quoted"'})
            self.assertTrue(any(command.startswith("UID STORE") and "+X-GM-LABELS" in command
                                for command in fixture.commands))

    def test_exact_marker_lookup_and_uid_verification(self):
        with GmailIMAPFixture() as fixture:
            marker = "ms1-" + "a" * 64
            uid = fixture.seed(b"X-Mailsync-Key: " + marker.encode() + b"\r\n\r\nbody")
            fixture.seed(b"X-Mailsync-Key: " + marker.encode() + b"-other\r\n\r\nbody")
            with self.connect(fixture, self.account("alpha")):
                with IMAPSession(self.account("alpha")) as destination:
                    self.assertEqual(destination.find_marker_exact(marker), [uid])
                    self.assertTrue(destination.marker_at_uid(uid, marker))
                    self.assertFalse(destination.marker_at_uid(uid, "ms1-" + "b" * 64))

    def test_account_wide_absence_requires_all_mail_and_trash_search(self):
        with GmailIMAPFixture() as fixture:
            marker = "ms1-" + "c" * 64
            uid = fixture.seed(b"X-Mailsync-Key: " + marker.encode() + b"\r\n\r\nbody")
            fixture.mailbox_uids["INBOX"].remove(uid)
            with self.connect(fixture, self.account("alpha")):
                with IMAPSession(self.account("alpha")) as destination:
                    self.assertFalse(destination.copy_absent_account_wide(marker))
                    fixture.mailbox_uids["[Gmail]/All Mail"].remove(uid)
                    self.assertTrue(destination.copy_absent_account_wide(marker))
                    self.assertFalse(destination.marker_at_uid(uid, marker))


if __name__ == "__main__":
    unittest.main()
