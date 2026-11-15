#!/usr/bin/env python3
"""Disposable end-to-end direct IMAP trial behind a trusted local TLS bridge."""

import argparse
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate
import imaplib
import json
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import tempfile
from threading import Event, Thread

from mailsync.config import load_account
from mailsync.cycle import run_once
from mailsync.imap_client import IMAPSession
from mailsync.state import Store
from mailsync.baseline import initialize


class TLSBridge:
    def __init__(self, upstream_port: int, cert: Path, key: Path, bind_port: int = 0):
        self.upstream_port = upstream_port
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", bind_port))
        self.listener.listen()
        self.listener.settimeout(0.5)
        self.port = self.listener.getsockname()[1]
        self.stopped = Event()
        self.thread = Thread(target=self._accept, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            Thread(target=self._relay, args=(connection,), daemon=True).start()

    def _relay(self, connection: socket.socket):
        try:
            with self.context.wrap_socket(connection, server_side=True) as client:
                with socket.create_connection(("127.0.0.1", self.upstream_port)) as upstream:
                    while True:
                        ready, _, _ = select.select((client, upstream), (), (), 30)
                        if not ready:
                            break
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is client else client).sendall(data)
        except (OSError, ssl.SSLError):
            pass


def account(root: Path, name: str, username: str, port: int, password: str):
    directory = root / "accounts" / name / ".secrets"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    path = directory / "account.json"
    path.write_text(json.dumps({
        "provider": "Disposable GreenMail", "username": username, "password": password,
        "imap": {"host": "127.0.0.1", "port": port, "security": "SSL/TLS", "authenticationRequired": True},
    }))
    path.chmod(0o600)


def fixture(subject: str, message_id: str | None, attachment: bool = False) -> bytes:
    mail = EmailMessage()
    mail["From"] = "sender@example.test"
    mail["To"] = "source@example.test"
    mail["Subject"] = subject
    mail["Date"] = formatdate(localtime=False)
    if message_id:
        mail["Message-ID"] = message_id
    mail.set_content(f"{subject} plain")
    mail.add_alternative(f"<html><body>{subject} html</body></html>", subtype="html")
    if attachment:
        mail.add_attachment(b"attachment bytes", maintype="application", subtype="octet-stream",
                            filename="fixture.bin")
    return mail.as_bytes(policy=policy.SMTP)


def connect_plain(port: int, name: str, password: str):
    client = imaplib.IMAP4("127.0.0.1", port, timeout=30)
    client.login(name, password)
    client.select("INBOX", readonly=True)
    return client


def uids(client) -> list[int]:
    status, data = client.uid("SEARCH", None, "ALL")
    assert status == "OK"
    return [int(value) for value in (data[0] or b"").split()]


def raw(client, uid: int) -> bytes:
    status, data = client.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
    assert status == "OK"
    return next(item[1] for item in data if isinstance(item, tuple))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imap-port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mailsync-direct-") as temp:
        root = Path(temp)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.environ["SSL_CERT_FILE"] = str(cert)
        bridge = TLSBridge(args.imap_port, cert, key)
        bridge.start()
        try:
            account(root, "yahoo", "source", bridge.port, "sourcepass")
            account(root, "alpha", "destination", bridge.port, "destpass")
            source = connect_plain(args.imap_port, "source", "sourcepass")
            destination = connect_plain(args.imap_port, "destination", "destpass")
            try:
                source.append("INBOX", None, None, fixture("old", "<old@example.test>"))
                with Store(root) as store:
                    baseline = initialize(store, load_account(root, "yahoo"), load_account(root, "alpha"))
                source.append("INBOX", None, datetime.now(timezone.utc) - timedelta(hours=25),
                              fixture("expired", "<expired@example.test>"))
                for subject, message_id, attached in (
                    ("plain-html", "<same@example.test>", False),
                    ("same-id", "<same@example.test>", False),
                    ("attached", "<attached@example.test>", True),
                    ("missing-id", None, False),
                ):
                    source.append("INBOX", None, None, fixture(subject, message_id, attached))
                first, first_code = run_once(root)
                first_destination = uids(destination)
                second, second_code = run_once(root)
                second_destination = uids(destination)
                markers = []
                original_headers_ok = True
                attachments = 0
                exact_source_payloads = True
                for uid in first_destination:
                    value = BytesParser(policy=policy.default).parsebytes(raw(destination, uid))
                    markers.extend(value.get_all("X-Mailsync-Key", []))
                    original_headers_ok &= bool(value.get("From") and value.get("To") and value.get("Date"))
                    attachments += any(part.get_content_disposition() == "attachment" for part in value.walk())
                with Store(root) as store:
                    for source_uid in uids(source)[1:]:
                        copy = store.copy_record("yahoo", baseline["uidvalidity"], source_uid)
                        if copy is None:
                            continue
                        destination_raw = raw(destination, copy["destination_uid"])
                        exact_source_payloads &= destination_raw.split(b"\r\n", 1)[1] == raw(source, source_uid)
                destination.select("INBOX", readonly=False)
                destination.uid("STORE", str(first_destination[0]), "+FLAGS", r"(\Seen)")
                third, third_code = run_once(root)
                destination.select("INBOX", readonly=True)
                flag_status, flag_data = destination.uid("FETCH", str(first_destination[0]), "(UID FLAGS)")
                destination_flag_retained = flag_status == "OK" and b"\\Seen" in flag_data[0]
                destination.select("INBOX", readonly=False)
                deleted_uid = first_destination[1]
                destination.uid("STORE", str(deleted_uid), "+FLAGS", r"(\Deleted)")
                destination.expunge()
                fourth, fourth_code = run_once(root)
                after_delete = uids(destination)
                source.append("INBOX", None, None, fixture("new-after-delete", "<new@example.test>"))
                fifth, fifth_code = run_once(root)
                after_new = uids(destination)
                print(f"baseline_first_uid={baseline['first_eligible_uid']}")
                print(f"first_exit={first_code} copied={first['accounts'][0]['copied']} destination_count={len(first_destination)}")
                print(f"expired_skipped={first['accounts'][0]['skipped']} age_reported={first['accounts'][0].get('error_code') == 'AGE_WINDOW_EXPIRED'}")
                print(f"second_exit={second_code} copied={second['accounts'][0]['copied']} destination_count={len(second_destination)}")
                print(f"unique_markers={len(set(markers))} original_headers_ok={original_headers_ok} exact_payloads={exact_source_payloads} attachments={attachments}")
                print(f"destination_flag_retained={destination_flag_retained} third_exit={third_code}")
                print(f"after_disposable_delete_exit={fourth_code} count={len(after_delete)} deleted_uid_reappeared={deleted_uid in after_delete}")
                print(f"after_new_exit={fifth_code} copied={fifth['accounts'][0]['copied']} count={len(after_new)} deleted_uid_reappeared={deleted_uid in after_new}")
                return 0 if (first_code == 0 and first["accounts"][0]["copied"] == 4
                             and first["accounts"][0]["skipped"] >= 1
                             and first["accounts"][0].get("error_code") == "AGE_WINDOW_EXPIRED"
                             and len(first_destination) == 4 and second_code == 0
                             and second["accounts"][0]["copied"] == 0 and len(second_destination) == 4
                             and len(set(markers)) == 4 and original_headers_ok and exact_source_payloads
                             and attachments == 1 and third_code == 0 and destination_flag_retained
                             and fourth_code == 0 and len(after_delete) == 3 and deleted_uid not in after_delete
                             and fifth_code == 0 and fifth["accounts"][0]["copied"] == 1
                             and len(after_new) == 4 and deleted_uid not in after_new) else 1
            finally:
                source.logout()
                destination.logout()
        finally:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
