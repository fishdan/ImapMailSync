#!/usr/bin/env python3
"""Read-only content comparison for the bounded live imapsync trial."""

from collections import Counter
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
from pathlib import Path
import ssl

from probe import account_json_credentials


ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".state" / "yahoo" / "imapsync-trial"
HEADERS = ("From", "To", "Subject", "Date", "Message-ID")


def cache_pairs() -> list[tuple[int, int]]:
    pairs = []
    for path in CACHE.rglob("*"):
        if path.is_file():
            source_uid, destination_uid = map(int, path.name.split("_"))
            pairs.append((source_uid, destination_uid))
    if not pairs or len({source for source, _ in pairs}) != len(pairs):
        raise RuntimeError("missing or duplicate source UID mappings")
    if len({destination for _, destination in pairs}) != len(pairs):
        raise RuntimeError("duplicate destination UID mappings")
    return sorted(pairs)


def connect(account_id: str) -> imaplib.IMAP4_SSL:
    host, port, username, password = account_json_credentials(
        ROOT / "accounts" / account_id / ".secrets" / "account.json"
    )
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(username, password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"{account_id} INBOX unavailable")
        return client
    except Exception:
        client.logout()
        raise


def message(client: imaplib.IMAP4_SSL, uid: int) -> bytes:
    status, data = client.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
    if status != "OK" or not data:
        raise RuntimeError("message fetch failed")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("message body missing")


def payloads(raw: bytes) -> tuple[tuple[str, str | None, str], ...]:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    leaves = []
    for part in parsed.walk():
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True)
        if content is None:
            content = part.get_payload().encode("utf-8")
        leaves.append((part.get_content_type(), part.get_content_disposition(), hashlib.sha256(content).hexdigest()))
    return tuple(leaves)


def main() -> int:
    pairs = cache_pairs()
    source = connect("yahoo")
    try:
        destination = connect("alpha")
        try:
            source_ids = []
            counts = Counter()
            for source_uid, destination_uid in pairs:
                raw_source = message(source, source_uid)
                raw_destination = message(destination, destination_uid)
                source_mail = BytesParser(policy=policy.default).parsebytes(raw_source)
                destination_mail = BytesParser(policy=policy.default).parsebytes(raw_destination)
                counts["pairs"] += 1
                counts["raw_equal"] += raw_source == raw_destination
                counts["payload_equal"] += payloads(raw_source) == payloads(raw_destination)
                counts["headers_equal"] += all(
                    source_mail.get_all(header, []) == destination_mail.get_all(header, [])
                    for header in HEADERS
                )
                counts["plain"] += any(p.get_content_type() == "text/plain" for p in source_mail.walk())
                counts["html"] += any(p.get_content_type() == "text/html" for p in source_mail.walk())
                counts["multipart"] += source_mail.is_multipart()
                counts["attachments"] += any(p.get_content_disposition() == "attachment" for p in source_mail.walk())
                source_ids.append(tuple(source_mail.get_all("Message-ID", [])))
            counts["missing_message_id"] = sum(not ids for ids in source_ids)
            counts["repeated_message_id"] = sum(n - 1 for n in Counter(source_ids).values() if n > 1)
            for name in ("pairs", "raw_equal", "payload_equal", "headers_equal", "plain", "html",
                         "multipart", "attachments", "missing_message_id", "repeated_message_id"):
                print(f"{name}={counts[name]}")
            return 0 if counts["payload_equal"] == counts["pairs"] and counts["headers_equal"] == counts["pairs"] else 1
        finally:
            destination.logout()
    finally:
        source.logout()


if __name__ == "__main__":
    raise SystemExit(main())
