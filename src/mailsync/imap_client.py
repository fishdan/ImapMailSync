"""Minimal, certificate-verifying IMAP operations for one-way copying."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import imaplib
import re
import shlex
import ssl

from .config import Account
from .labels import LabelError, encode_imap_label


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
            status, _ = self.client.select("INBOX", readonly=account.role == "source")
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

    def supports_labels(self) -> bool:
        if self.account.role != "destination":
            return False
        status, data = self.client.capability()
        return status == "OK" and any(b"X-GM-EXT-1" in value.split() for value in data if value)

    def ensure_label(self, label: str) -> None:
        if self.account.role != "destination" or not self.supports_labels():
            raise IMAPError("LABEL_UNSUPPORTED")
        try:
            encoded = encode_imap_label(label)
        except LabelError as exc:
            raise IMAPError("INVALID_LABEL_NAME") from exc
        if self._label_exists(label, encoded):
            return
        status, _ = self.client.create(encoded)
        if status != "OK" and not self._label_exists(label, encoded):
            raise IMAPError("LABEL_CREATE_FAILED")
        if not self._label_exists(label, encoded):
            raise IMAPError("LABEL_CREATE_UNCONFIRMED")

    def _label_exists(self, label: str, encoded: bytes) -> bool:
        status, data = self.client.list(b'""', encoded)
        if status != "OK":
            raise IMAPError("LABEL_LIST_FAILED")
        for line in data or []:
            if not line:
                continue
            try:
                names = shlex.split(line.decode("ascii"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise IMAPError("LABEL_LIST_INVALID") from exc
            if names and names[-1] == label:
                return True
        return False

    def labels_for_uid(self, uid: int) -> set[str]:
        if self.account.role != "destination" or uid < 1:
            raise IMAPError("INVALID_LABEL_FETCH")
        status, data = self.client.uid("FETCH", str(uid), "(UID X-GM-LABELS)")
        if status != "OK":
            raise IMAPError("LABEL_FETCH_FAILED")
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            uid_match = UID.search(item)
            if not uid_match or int(uid_match.group(1)) != uid:
                continue
            content = _parenthesized_after(item, b"X-GM-LABELS ")
            if content is None:
                raise IMAPError("LABEL_FETCH_INVALID")
            try:
                return set(shlex.split(content.decode("ascii")))
            except (UnicodeDecodeError, ValueError) as exc:
                raise IMAPError("LABEL_FETCH_INVALID") from exc
        raise IMAPError("DESTINATION_COPY_MISSING")

    def add_label(self, uid: int, label: str) -> None:
        if self.account.role != "destination" or uid < 1:
            raise IMAPError("INVALID_LABEL_STORE")
        try:
            encoded = encode_imap_label(label)
        except LabelError as exc:
            raise IMAPError("INVALID_LABEL_NAME") from exc
        status, _ = self.client.uid("STORE", str(uid), "+X-GM-LABELS", b"(" + encoded + b")")
        if status != "OK":
            raise IMAPError("LABEL_STORE_FAILED")

    def marker_at_uid(self, uid: int, marker: str) -> bool:
        if self.account.role != "destination" or uid < 1 or not MARKER.fullmatch(marker):
            raise IMAPError("INVALID_MARKER_FETCH")
        status, data = self.client.uid(
            "FETCH", str(uid), "(UID BODY.PEEK[HEADER.FIELDS (X-Mailsync-Key)])")
        if status != "OK":
            raise IMAPError("MARKER_FETCH_FAILED")
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            metadata, headers = item
            uid_match = UID.search(metadata)
            if not uid_match or int(uid_match.group(1)) != uid or not isinstance(headers, bytes):
                raise IMAPError("MARKER_FETCH_INVALID")
            return any(line.strip() == b"X-Mailsync-Key: " + marker.encode("ascii")
                       for line in headers.splitlines())
        return False

    def find_marker_exact(self, marker: str) -> list[int]:
        if self.account.role != "destination" or not MARKER.fullmatch(marker):
            raise IMAPError("INVALID_MARKER_SEARCH")
        self.client.noop()
        status, data = self.client.uid("SEARCH", None, "HEADER", "X-Mailsync-Key", marker)
        if status != "OK" or not data:
            raise IMAPError("DESTINATION_SEARCH_FAILED")
        matches = []
        for raw_uid in (data[0] or b"").split():
            try:
                uid = int(raw_uid)
            except ValueError as exc:
                raise IMAPError("DESTINATION_SEARCH_INVALID") from exc
            if self.marker_at_uid(uid, marker):
                matches.append(uid)
        return matches

    def copy_absent_account_wide(self, marker: str) -> bool:
        """Prove absence with targeted searches of Gmail All Mail, Trash, and Junk."""
        if self.account.role != "destination" or not MARKER.fullmatch(marker):
            raise IMAPError("INVALID_MARKER_SEARCH")
        status, rows = self.client.list()
        if status != "OK":
            raise IMAPError("MAILBOX_LIST_FAILED")
        mailboxes = []
        saw_all = False
        for row in rows or []:
            if not row:
                continue
            match = re.match(rb"^\(([^)]*)\)\s+", row)
            if not match:
                raise IMAPError("MAILBOX_LIST_INVALID")
            flags = set(match.group(1).split())
            if b"\\Noselect" in flags:
                continue
            if not flags.intersection({b"\\All", b"\\Trash", b"\\Junk", b"\\Spam"}):
                continue
            try:
                name = shlex.split(row.decode("ascii"))[-1]
                encoded = encode_imap_label(name)
            except (UnicodeDecodeError, ValueError, IndexError, LabelError) as exc:
                raise IMAPError("MAILBOX_LIST_INVALID") from exc
            mailboxes.append(encoded)
            saw_all |= b"\\All" in flags
        if not saw_all:
            return False  # Inbox absence alone is not account-wide deletion.
        absent = True
        try:
            for mailbox in mailboxes:
                status, _ = self.client.select(mailbox, readonly=True)
                if status != "OK":
                    raise IMAPError("MAILBOX_SELECT_FAILED")
                status, data = self.client.uid("SEARCH", None, "HEADER", "X-Mailsync-Key", marker)
                if status != "OK" or not data:
                    raise IMAPError("MAILBOX_SEARCH_FAILED")
                if (data[0] or b"").split():
                    absent = False
                    break
        finally:
            status, _ = self.client.select("INBOX", readonly=False)
            if status != "OK":
                raise IMAPError("INBOX_RESELECT_FAILED")
        return absent


def _parenthesized_after(value: bytes, prefix: bytes) -> bytes | None:
    start = value.find(prefix)
    if start < 0:
        return None
    start += len(prefix)
    if value[start:start + 1] != b"(":
        return None
    quoted = escaped = False
    for index in range(start + 1, len(value)):
        char = value[index]
        if escaped:
            escaped = False
        elif quoted and char == 92:
            escaped = True
        elif char == 34:
            quoted = not quoted
        elif char == 41 and not quoted:
            return value[start + 1:index]
    return None
