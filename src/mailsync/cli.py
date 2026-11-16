"""Operator CLI for IMAP connection checks, activation, cycles, and status."""

import argparse
import imaplib
import json
from pathlib import Path
import sqlite3
import ssl
import sys

from .baseline import initialize
from .config import discover_accounts
from .cycle import run_once
from .imap_client import IMAPError, IMAPSession
from .labels import LabelError, derive_source_label
from .state import StateError, Store


def _show(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    print(f"status={report.get('overall_status', report.get('status', 'unknown'))}")
    if report.get("error_code"):
        print(f"error={report['error_code']}")
    for baseline in report.get("baselines", []):
        print(f"account={baseline['account_id']} baseline_status={baseline['status']} "
              f"first_eligible_uid={baseline['first_eligible_uid']} "
              f"pending_labels={baseline.get('pending_labels', 0)} "
              f"missing_labels={baseline.get('missing_labels', 0)}")
    latest = report.get("latest_cycle")
    if latest:
        print(f"latest_cycle={latest['cycle_id']} status={latest['status']} "
              f"started={latest['started_at']} ended={latest['ended_at']}")
        for item in latest["accounts"]:
            print(f"account={item['account_id']} status={item['status']} copied={item['copied']} "
                  f"skipped={item['skipped']} failed={item['failed']} "
                  f"error={item['error_code'] or 'none'}")
    for item in report.get("accounts", []):
        parts = [f"account={item['account_id']}", f"status={item['status']}"]
        for key in ("copied", "skipped", "failed", "labeled", "label_pending",
                    "error_code", "issue_uids"):
            if key in item:
                parts.append(f"{key}={item[key]}")
        print(" ".join(parts))


def doctor(root: Path) -> tuple[dict, int]:
    accounts, errors = discover_accounts(root)
    results = []
    if len(accounts) == 1 and "alpha" in accounts and not any(account_id != "alpha" for account_id in errors):
        results.append({"account_id": "sources", "status": "failed", "error_code": "NO_SOURCE_ACCOUNTS"})
    for account_id, code in sorted(errors.items()):
        results.append({"account_id": account_id, "status": "failed", "error_code": code})
    for account_id, account in sorted(accounts.items()):
        if account.role == "source":
            try:
                derive_source_label(account.username)
            except LabelError:
                results.append({"account_id": account_id, "status": "failed",
                                "error_code": "INVALID_LABEL_ADDRESS"})
                continue
        try:
            with IMAPSession(account) as session:
                if account.role == "destination" and not session.supports_labels():
                    results.append({"account_id": account_id, "status": "failed",
                                    "error_code": "LABEL_UNSUPPORTED"})
                else:
                    results.append({"account_id": account_id, "status": "success"})
        except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
            results.append({"account_id": account_id, "status": "failed", "error_code": "IMAP_CONNECTION_FAILED"})
    status = "success" if all(item["status"] == "success" for item in results) else "failed"
    return {"overall_status": status, "accounts": results}, 0 if status == "success" else 2 if "alpha" in errors else 1


def init_source(root: Path, account_id: str) -> tuple[dict, int]:
    accounts, errors = discover_accounts(root)
    if "alpha" not in accounts:
        code = errors.get("alpha", "MISSING_CONFIG")
    elif account_id == "alpha" or account_id not in accounts:
        code = errors.get(account_id, "INVALID_SOURCE")
    else:
        try:
            with Store(root) as store:
                baseline = initialize(store, accounts[account_id], accounts["alpha"])
            return {"status": "success", "account_id": account_id,
                    "uidvalidity": baseline["uidvalidity"],
                    "first_eligible_uid": baseline["first_eligible_uid"]}, 0
        except StateError as exc:
            code = str(exc)
        except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
            code = "IMAP_CONNECTION_FAILED"
    return {"status": "failed", "account_id": account_id, "error_code": code}, 2


def status(root: Path) -> tuple[dict, int]:
    try:
        with Store(root) as store:
            latest = store.latest_cycle()
            baselines = store.baselines()
            for baseline in baselines:
                baseline.update(store.label_counts(baseline["account_id"]))
        return {"status": "success", "baselines": baselines, "latest_cycle": latest}, 0
    except (StateError, sqlite3.DatabaseError) as exc:
        return {"status": "failed", "error_code": str(exc) if isinstance(exc, StateError)
                else "CORRUPT_STATE"}, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy new source Inbox mail into alpha")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "init-source", "run-once", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "init-source":
            sub.add_argument("account_id")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "doctor":
        report, code = doctor(root)
    elif args.command == "init-source":
        report, code = init_source(root, args.account_id)
    elif args.command == "run-once":
        report, code = run_once(root)
    else:
        report, code = status(root)
    _show(report, args.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
