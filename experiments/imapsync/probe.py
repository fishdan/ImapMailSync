#!/usr/bin/env python3
"""Read-only, disposable-mailbox inspector for the imapsync adoption trial."""

from __future__ import annotations

import argparse
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import sys


LOCAL_DIR = Path(__file__).resolve().parent / ".local"
ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


def explicit_uid_batches(uids: list[int], max_items: int = 50, max_chars: int = 1800) -> list[str]:
    """Return SEARCH arguments with exact UIDs; never use UID n:* ranges."""
    if max_items < 1 or max_chars < 5:
        raise ValueError("invalid batch limit")
    batches: list[str] = []
    current: list[str] = []
    for uid in sorted(set(uids)):
        if uid < 1:
            raise ValueError("UIDs must be positive")
        value = str(uid)
        candidate = ",".join([*current, value])
        if current and (len(current) >= max_items or len("UID " + candidate) > max_chars):
            batches.append("UID " + ",".join(current))
            current = [value]
        else:
            current.append(value)
        if len("UID " + ",".join(current)) > max_chars:
            raise ValueError("UID does not fit batch limit")
    if current:
        batches.append("UID " + ",".join(current))
    return batches


def eligible_uids(uids: list[int], first_eligible_uid: int, confirmed: set[int]) -> list[int]:
    if first_eligible_uid < 1:
        raise ValueError("baseline UID must be positive")
    return sorted({uid for uid in uids if uid >= first_eligible_uid and uid not in confirmed})


def private_password(path: Path) -> str:
    require_private_file(path)
    password = path.read_text(encoding="utf-8").splitlines()
    if not password or not password[0]:
        raise ValueError("password file is empty")
    return password[0]


def require_private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("credential path must be a regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError("credential file must have owner-only permissions")


def account_json_credentials(path: Path) -> tuple[str, int, str, str]:
    require_private_file(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("imap"), dict):
        raise ValueError("invalid account JSON")
    username = data.get("username")
    password = data.get("password")
    host = data["imap"].get("host")
    port = data["imap"].get("port")
    if not all(isinstance(value, str) and value for value in (username, password, host)):
        raise ValueError("account JSON is missing IMAP credentials")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("account JSON has invalid IMAP port")
    if username.startswith("your-address@") or password.startswith("YOUR_"):
        raise ValueError("account JSON still has placeholder credentials")
    if data["imap"].get("security") != "SSL/TLS":
        raise ValueError("account JSON must require SSL/TLS")
    if data["imap"].get("authenticationRequired") is not True:
        raise ValueError("account JSON must require authentication")
    return host, port, username, password


def response_integer(client: imaplib.IMAP4_SSL, name: str) -> int:
    _, values = client.response(name)
    if not values or not values[0]:
        raise ValueError(f"mailbox did not return {name}")
    value = int(values[0])
    if value < 1:
        raise ValueError(f"mailbox returned invalid {name}")
    return value


def inspect_mailbox(
    host: str,
    port: int,
    username: str,
    password: str,
    first_eligible_uid: int | None = None,
    confirmed: set[int] | None = None,
    include_flags: bool = False,
) -> dict:
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=30)
    try:
        status, _ = client.login(username, password)
        if status != "OK":
            raise RuntimeError("authentication failed")
        status, count_data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("INBOX is not readable")
        uidvalidity = response_integer(client, "UIDVALIDITY")
        uidnext = response_integer(client, "UIDNEXT")
        status, search_data = client.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError("UID search failed")
        uids = [int(value) for value in (search_data[0] or b"").split()]
        eligible = eligible_uids(uids, first_eligible_uid, confirmed or set()) if first_eligible_uid else []
        flags: dict[str, list[str]] = {}
        if include_flags:
            for uid in eligible:
                status, data = client.uid("FETCH", str(uid), "(FLAGS)")
                if status != "OK":
                    raise RuntimeError("UID flag fetch failed")
                first = data[0] if data else None
                header = first[0] if isinstance(first, tuple) else first
                match = FLAGS.search(header or b"")
                flags[str(uid)] = match.group(1).decode("ascii", errors="replace").split() if match else []
        return {
            "uidvalidity": uidvalidity,
            "uidnext": uidnext,
            "message_count": int(count_data[0]),
            "uid_count": len(uids),
            "eligible_uids": eligible,
            "search1_queries": explicit_uid_batches(eligible),
            "flags": flags if include_flags else "not requested",
        }
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def write_baseline(path: Path, account_id: str, snapshot: dict) -> None:
    LOCAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL_DIR.chmod(0o700)
    payload = {
        "account_id": account_id,
        "uidvalidity": snapshot["uidvalidity"],
        "first_eligible_uid": snapshot["uidnext"],
    }
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")


def read_confirmed(account_id: str) -> set[int]:
    path = LOCAL_DIR / f"{account_id}-confirmed.json"
    if not path.exists():
        return set()
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or any(not isinstance(value, int) or value < 1 for value in values):
        raise ValueError("confirmed UID file must contain positive integer UIDs")
    return set(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account_id")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--user")
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--passfile", type=Path)
    credentials.add_argument("--config", type=Path, help="private account JSON containing IMAP credentials")
    parser.add_argument("--activate", action="store_true", help="save UIDNEXT as a new baseline")
    parser.add_argument("--flags", action="store_true", help="fetch flags for eligible messages")
    args = parser.parse_args()
    if not ACCOUNT_ID.fullmatch(args.account_id):
        parser.error("account_id must start with a lowercase letter and contain only lowercase letters, digits, _ or -")
    try:
        if args.config:
            host, port, username, password = account_json_credentials(args.config)
        else:
            if not args.host or not args.user or not 1 <= args.port <= 65535:
                parser.error("--host and --user are required with --passfile")
            host, port, username, password = args.host, args.port, args.user, private_password(args.passfile)
        baseline_path = LOCAL_DIR / f"{args.account_id}-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
        if args.flags and baseline is None:
            raise ValueError("activate a baseline before requesting eligible-message flags")
        confirmed = read_confirmed(args.account_id) if baseline else set()
        snapshot = inspect_mailbox(
            host,
            port,
            username,
            password,
            first_eligible_uid=baseline["first_eligible_uid"] if baseline else None,
            confirmed=confirmed,
            include_flags=args.flags,
        )
        if args.activate:
            write_baseline(baseline_path, args.account_id, snapshot)
        if baseline:
            if baseline["account_id"] != args.account_id:
                raise ValueError("baseline belongs to another account")
            if baseline["uidvalidity"] != snapshot["uidvalidity"]:
                raise ValueError("UIDVALIDITY changed; baseline is unsafe")
            snapshot["first_eligible_uid"] = baseline["first_eligible_uid"]
            snapshot["confirmed_uids"] = sorted(confirmed)
        elif args.activate:
            snapshot["first_eligible_uid"] = snapshot["uidnext"]
        snapshot["account_id"] = args.account_id
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, imaplib.IMAP4.error) as exc:
        print(f"probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
