"""Minimal, certificate-verifying IMAP operations for one-way copying."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import imaplib
import re
import ssl

from .config import Account


UID = re.compile(rb"\bUID (\d+)\b")
FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
INTERNALDATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
MARKER = re.compile(r"^ms1-[a-f0-9]{64}$")


class IMAPError(Exception):
    """A sanitized IMAP operation failure."""


@dataclass(frozen=True)
class SourceMessage:
    uid: int
    raw: bytes
    seen: bool
    internaldate: datetime


class IMAPSession:
    def __init__(self, account: Account):
        self.account = account
        context = ssl.create_default_context()
        self.client = imaplib.IMAP4_SSL(account.host, account.port, ssl_context=context, timeout=30)
        try:
            status, _ = self.client.login(account.username, account.password)
            if status != "OK":
                raise IMAPError("AUTH_FAILED")
            status, _ = self.client.select("INBOX", readonly=True)
            if status != "OK":
                raise IMAPError("INBOX_UNAVAILABLE")
            self.uidvalidity = self._response_integer("UIDVALIDITY")
            self.uidnext = self._response_integer("UIDNEXT")
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "IMAPSession":
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        try:
            self.client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def _response_integer(self, name: str) -> int:
        _, values = self.client.response(name)
        if not values or not values[0]:
            raise IMAPError(f"MISSING_{name}")
        try:
            number = int(values[0])
        except ValueError as exc:
            raise IMAPError(f"INVALID_{name}") from exc
        if number < 1:
            raise IMAPError(f"INVALID_{name}")
        return number

    def uids(self) -> list[int]:
        status, data = self.client.uid("SEARCH", None, "ALL")
        if status != "OK" or not data:
            raise IMAPError("UID_SEARCH_FAILED")
        return [int(value) for value in (data[0] or b"").split()]

    def uids_since(self, cutoff: datetime) -> list[int]:
        if self.account.role != "source" or cutoff.tzinfo is None:
            raise IMAPError("INVALID_SOURCE_WINDOW")
        # IMAP SINCE compares calendar dates without timezone; include the prior
        # day so negative UTC offsets cannot omit a message still under 24h old.
        search_date = (cutoff.astimezone(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = self.client.uid("SEARCH", None, "SINCE", search_date)
        if status != "OK" or not data:
            raise IMAPError("UID_SEARCH_FAILED")
        return [int(value) for value in (data[0] or b"").split()]

    def fetch(self, uid: int) -> SourceMessage:
        if self.account.role != "source" or uid < 1:
            raise IMAPError("INVALID_SOURCE_FETCH")
        status, data = self.client.uid("FETCH", str(uid), "(UID FLAGS INTERNALDATE BODY.PEEK[])")
        if status != "OK" or not data:
            raise IMAPError("SOURCE_FETCH_FAILED")
        for item in data:
            if not isinstance(item, tuple) or not isinstance(item[1], bytes):
                continue
            metadata, raw = item
            uid_match, flags_match, date_match = (UID.search(metadata), FLAGS.search(metadata),
                                                  INTERNALDATE.search(metadata))
            if not uid_match or not flags_match or not date_match:
                raise IMAPError("SOURCE_METADATA_INCOMPLETE")
            if int(uid_match.group(1)) != uid:
                raise IMAPError("SOURCE_UID_MISMATCH")
            try:
                arrival = parsedate_to_datetime(date_match.group(1).decode("ascii"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise IMAPError("SOURCE_DATE_INVALID") from exc
            flags = flags_match.group(1).split()
            return SourceMessage(uid, raw, b"\\Seen" in flags, arrival)
        raise IMAPError("SOURCE_MESSAGE_MISSING")

    def find_marker(self, marker: str, cutoff: datetime) -> list[int]:
        if self.account.role != "destination" or not MARKER.fullmatch(marker) or cutoff.tzinfo is None:
            raise IMAPError("INVALID_MARKER_SEARCH")
        self.client.noop()  # Refresh selected Inbox after an APPEND or another client write.
        search_date = (cutoff.astimezone(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = self.client.uid("SEARCH", None, "SINCE", search_date,
                                       "HEADER", "X-Mailsync-Key", marker)
        if status != "OK" or not data:
            raise IMAPError("DESTINATION_SEARCH_FAILED")
        matches = []
        for raw_uid in (data[0] or b"").split():
            uid = int(raw_uid)
            status, metadata_data = self.client.uid("FETCH", str(uid), "(UID INTERNALDATE)")
            if status != "OK" or not metadata_data or not metadata_data[0]:
                raise IMAPError("DESTINATION_DATE_FETCH_FAILED")
            first = metadata_data[0]
            metadata = first[0] if isinstance(first, tuple) else first
            uid_match, date_match = UID.search(metadata), INTERNALDATE.search(metadata)
            if not uid_match or int(uid_match.group(1)) != uid or not date_match:
                raise IMAPError("DESTINATION_DATE_INVALID")
            try:
                arrival = parsedate_to_datetime(date_match.group(1).decode("ascii")).astimezone(timezone.utc)
            except (ValueError, UnicodeDecodeError) as exc:
                raise IMAPError("DESTINATION_DATE_INVALID") from exc
            if cutoff <= arrival <= datetime.now(timezone.utc):
                matches.append(uid)
        return matches

    def append_copy(self, raw: bytes, seen: bool, internaldate: datetime, marker: str) -> None:
        if self.account.role != "destination" or not MARKER.fullmatch(marker):
            raise IMAPError("INVALID_DESTINATION_APPEND")
        marked = b"X-Mailsync-Key: " + marker.encode("ascii") + b"\r\n" + raw
        flags = r"(\Seen)" if seen else None
        status, _ = self.client.append("INBOX", flags, internaldate, marked)
        if status != "OK":
            raise IMAPError("DESTINATION_APPEND_FAILED")
