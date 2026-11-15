#!/usr/bin/env python3
"""Measure one 100-message direct IMAP cycle on disposable local mailboxes."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time

from mailsync.baseline import initialize
from mailsync.config import load_account
from mailsync.cycle import run_once
from mailsync.state import Store

from smoke import TLSBridge, account, connect_plain, fixture, uids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imap-port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mailsync-throughput-") as temp:
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
            account(root, "alpha", "destination", bridge.port, "destpass")
            account(root, "yahoo", "source", bridge.port, "sourcepass")
            with Store(root) as store:
                initialize(store, load_account(root, "yahoo"), load_account(root, "alpha"))
            source = connect_plain(args.imap_port, "source", "sourcepass")
            for number in range(100):
                source.append("INBOX", None, None,
                              fixture(f"batch-{number}", f"<batch-{number}@example.test>"))
            source.logout()
            start = time.perf_counter()
            first, first_code = run_once(root)
            seconds = time.perf_counter() - start
            destination = connect_plain(args.imap_port, "destination", "destpass")
            destination_count = len(uids(destination))
            destination.logout()
            second, second_code = run_once(root)
            destination = connect_plain(args.imap_port, "destination", "destpass")
            final_count = len(uids(destination))
            destination.logout()
            print(f"first_exit={first_code} copied={first['accounts'][0]['copied']} seconds={seconds:.2f} destination_count={destination_count}")
            print(f"repeat_exit={second_code} copied={second['accounts'][0]['copied']} destination_count={final_count}")
            return 0 if (first_code == 0 and first["accounts"][0]["copied"] == 100
                         and seconds < 300 and destination_count == 100 and second_code == 0
                         and second["accounts"][0]["copied"] == 0 and final_count == 100) else 1
        finally:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
