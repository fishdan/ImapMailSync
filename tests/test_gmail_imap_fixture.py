"""Wire-level checks for the disposable Gmail extension fixture."""

import imaplib
from datetime import datetime, timezone
import unittest

from gmail_imap_fixture import GmailIMAPFixture


class GmailFixtureTests(unittest.TestCase):
    def connect(self, fixture):
        client = imaplib.IMAP4(fixture.host, fixture.port)
        self.assertEqual(client.login("person@example.test", "disposable")[0], "OK")
        return client

    def test_create_store_fetch_and_marker_search_over_imap(self):
        with GmailIMAPFixture() as fixture:
            client = self.connect(fixture)
            self.assertIn(b"X-GM-EXT-1", client.capability()[1][0])
            self.assertEqual(client.select("INBOX")[0], "OK")
            self.assertEqual(client.list('""', '"person/example"')[1], [None])
            self.assertEqual(client.create('"person/example"')[0], "OK")
            self.assertIn(b'"person/example"', client.list('""', '"person/example"')[1][0])
            raw = b"X-Mailsync-Key: ms1-test-marker\r\nFrom: sender@example.test\r\n\r\nbody"
            arrival = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
            self.assertEqual(client.append("INBOX", r"(\Seen)", arrival, raw)[0], "OK")
            uid = max(fixture.messages)
            self.assertEqual(client.uid("SEARCH", None, "HEADER", "X-Mailsync-Key",
                                        "ms1-test-marker")[1], [str(uid).encode()])
            self.assertIn(b"X-GM-LABELS ()", client.uid("FETCH", str(uid),
                                                        "(UID X-GM-LABELS)")[1][0])
            self.assertEqual(client.uid("STORE", str(uid), "+X-GM-LABELS",
                                        '("person/example")')[0], "OK")
            labels = client.uid("FETCH", str(uid), "(UID X-GM-LABELS)")[1][0]
            self.assertIn(b'X-GM-LABELS ("person/example")', labels)
            self.assertEqual(fixture.messages[uid].raw, raw)
            self.assertEqual(fixture.messages[uid].internaldate, arrival)
            self.assertEqual(fixture.messages[uid].flags, {r"\Seen"})
            self.assertEqual(fixture.messages[uid].labels, {"person/example"})
            client.logout()

    def test_readonly_and_lost_ack_faults(self):
        with GmailIMAPFixture() as fixture:
            client = self.connect(fixture)
            client.select("INBOX", readonly=True)
            self.assertEqual(client.uid("STORE", "1", "+X-GM-LABELS", '("source")')[0], "NO")
            client.logout()

            client = self.connect(fixture)
            client.select("INBOX")
            fixture.fault_next("APPEND", "lose_ack")
            with self.assertRaises((imaplib.IMAP4.abort, OSError)):
                client.append("INBOX", None, None, b"Subject: synthetic\r\n\r\nbody")
            client.shutdown()
            self.assertEqual(len(fixture.messages), 1)

            client = self.connect(fixture)
            client.select("INBOX")
            client.create('"source"')
            fixture.fault_next("UID STORE", "lose_ack")
            with self.assertRaises((imaplib.IMAP4.abort, OSError)):
                client.uid("STORE", "1", "+X-GM-LABELS", '("source")')
            client.shutdown()
            self.assertEqual(fixture.messages[1].labels, {"source"})

    def test_rejection_and_connection_loss(self):
        with GmailIMAPFixture(gmail_extension=False) as fixture:
            client = self.connect(fixture)
            self.assertNotIn(b"X-GM-EXT-1", client.capability()[1][0])
            fixture.fault_next("CREATE", "reject")
            self.assertEqual(client.create('"source"')[0], "NO")
            self.assertNotIn("source", fixture.labels)
            fixture.fault_next("LIST", "disconnect")
            with self.assertRaises((imaplib.IMAP4.abort, OSError)):
                client.list()
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
