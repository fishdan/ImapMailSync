#!/usr/bin/env python3
"""End-to-end lost APPEND acknowledgement test for the direct IMAP service."""

import argparse
import os
from pathlib import Path
import re
import select
import socket
import ssl
import subprocess
import tempfile
from threading import Event

from mailsync.baseline import initialize
from mailsync.config import load_account
from mailsync.cycle import run_once
from mailsync.state import Store

from smoke import TLSBridge, account, connect_plain, fixture, uids


class AckDropBridge(TLSBridge):
    def __init__(self, upstream_port: int, cert: Path, key: Path):
        super().__init__(upstream_port, cert, key)
        self.faulted = Event()

    def _relay(self, connection: socket.socket):
        if self.faulted.is_set():
            return super()._relay(connection)
        try:
            with self.context.wrap_socket(connection, server_side=True) as client:
                with socket.create_connection(("127.0.0.1", self.upstream_port)) as upstream:
                    tag = None
                    commands = responses = b""
                    while True:
                        ready, _, _ = select.select((client, upstream), (), (), 30)
                        if not ready:
                            return
                        for source in ready:
                            chunk = source.recv(65536)
                            if not chunk:
                                return
                            if source is client:
                                commands += chunk
                                while b"\r\n" in commands:
                                    line, commands = commands.split(b"\r\n", 1)
                                    match = re.match(rb"^([A-Za-z0-9.]+) APPEND\s", line, re.IGNORECASE)
                                    if match:
                                        tag = match.group(1)
                                upstream.sendall(chunk)
                            else:
                                responses += chunk
                                while b"\r\n" in responses:
                                    line, responses = responses.split(b"\r\n", 1)
                                    if tag and line.upper().startswith(tag.upper() + b" OK"):
                                        self.faulted.set()
                                        return
                                    client.sendall(line + b"\r\n")
        except (OSError, ssl.SSLError):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imap-port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mailsync-fault-") as temp:
        root = Path(temp)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.environ["SSL_CERT_FILE"] = str(cert)
        bridge = AckDropBridge(args.imap_port, cert, key)
        bridge.start()
        try:
            account(root, "yahoo", "source", bridge.port, "sourcepass")
            account(root, "alpha", "destination", bridge.port, "destpass")
            with Store(root) as store:
                baseline = initialize(store, load_account(root, "yahoo"), load_account(root, "alpha"))
            source = connect_plain(args.imap_port, "source", "sourcepass")
            destination = connect_plain(args.imap_port, "destination", "destpass")
            try:
                source.append("INBOX", None, None, fixture("ack-loss", "<ack-loss@example.test>"))
                first, first_code = run_once(root)
                first_count = len(uids(destination))
                source_uid = uids(source)[-1]
                with Store(root) as store:
                    uncertain = store.attempt("yahoo", baseline["uidvalidity"], source_uid)["status"] == "uncertain"
                second, second_code = run_once(root)
                second_count = len(uids(destination))
                with Store(root) as store:
                    confirmed = store.copy_record("yahoo", baseline["uidvalidity"], source_uid) is not None
                print(f"ack_dropped={bridge.faulted.is_set()} first_exit={first_code} first_destination_count={first_count} uncertain={uncertain}")
                print(f"second_exit={second_code} reconciled={confirmed} second_destination_count={second_count}")
                return 0 if (bridge.faulted.is_set() and first_code == 1 and first_count == 1 and uncertain
                             and second_code == 0 and second["accounts"][0]["copied"] == 1
                             and confirmed and second_count == 1) else 1
            finally:
                source.logout()
                destination.logout()
        finally:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
