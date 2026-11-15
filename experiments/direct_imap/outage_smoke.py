#!/usr/bin/env python3
"""End-to-end two-source outage and recovery test on disposable IMAP accounts."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from mailsync.baseline import initialize
from mailsync.config import load_account
from mailsync.cycle import run_once
from mailsync.state import Store

from smoke import TLSBridge, account, connect_plain, fixture, uids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imap-port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mailsync-outage-") as temp:
        root = Path(temp)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.environ["SSL_CERT_FILE"] = str(cert)
        main_bridge = TLSBridge(args.imap_port, cert, key)
        delta_bridge = TLSBridge(args.imap_port, cert, key)
        main_bridge.start()
        delta_bridge.start()
        try:
            account(root, "alpha", "destination", main_bridge.port, "destpass")
            account(root, "yahoo", "source", main_bridge.port, "sourcepass")
            account(root, "delta", "delta", delta_bridge.port, "deltapass")
            with Store(root) as store:
                initialize(store, load_account(root, "yahoo"), load_account(root, "alpha"))
                initialize(store, load_account(root, "delta"), load_account(root, "alpha"))
            source = connect_plain(args.imap_port, "source", "sourcepass")
            delta = connect_plain(args.imap_port, "delta", "deltapass")
            destination = connect_plain(args.imap_port, "destination", "destpass")
            try:
                source.append("INBOX", None, None, fixture("healthy", "<healthy@example.test>"))
                delta.append("INBOX", None, None, fixture("recover", "<recover@example.test>"))
                delta_port = delta_bridge.port
                delta_bridge.close()
                first, first_code = run_once(root)
                first_count = len(uids(destination))
                delta_bridge = TLSBridge(args.imap_port, cert, key, bind_port=delta_port)
                delta_bridge.start()
                second, second_code = run_once(root)
                second_count = len(uids(destination))
                first_results = {item["account_id"]: item for item in first["accounts"]}
                second_results = {item["account_id"]: item for item in second["accounts"]}
                print(f"outage_exit={first_code} yahoo_copied={first_results['yahoo']['copied']} delta_failed={first_results['delta']['failed']} destination_count={first_count}")
                print(f"recovery_exit={second_code} delta_copied={second_results['delta']['copied']} yahoo_copied={second_results['yahoo']['copied']} destination_count={second_count}")
                return 0 if (first_code == 1 and first_results["yahoo"]["copied"] == 1
                             and first_results["delta"]["failed"] == 1 and first_count == 1
                             and second_code == 0 and second_results["delta"]["copied"] == 1
                             and second_results["yahoo"]["copied"] == 0 and second_count == 2) else 1
            finally:
                source.logout()
                delta.logout()
                destination.logout()
        finally:
            main_bridge.close()
            delta_bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
