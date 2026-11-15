"""Source Inbox baseline and stable occurrence identity."""

import hashlib
from datetime import datetime, timedelta, timezone

from .config import Account
from .imap_client import IMAPSession
from .state import Store


def eligible_uids(uids: list[int], first_eligible_uid: int, confirmed: set[int]) -> list[int]:
    if first_eligible_uid < 1:
        raise ValueError("invalid baseline")
    return sorted({uid for uid in uids if uid >= first_eligible_uid and uid not in confirmed})


def marker_for(account_id: str, identity: str, uidvalidity: int, source_uid: int) -> str:
    if uidvalidity < 1 or source_uid < 1:
        raise ValueError("invalid source occurrence")
    parts = ("ms1", account_id, identity, str(uidvalidity), str(source_uid))
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"ms1-{digest}"


def within_24_hours(internaldate: datetime, now: datetime) -> bool:
    """Use exact IMAP arrival time; future server dates are not in the preceding window."""
    if internaldate.tzinfo is None or now.tzinfo is None:
        raise ValueError("IMAP arrival time must include a timezone")
    arrival = internaldate.astimezone(timezone.utc)
    current = now.astimezone(timezone.utc)
    return current - timedelta(hours=24) <= arrival <= current


def initialize(store: Store, source: Account, destination: Account) -> dict:
    if source.role != "source" or destination.role != "destination":
        raise ValueError("invalid account roles")
    with IMAPSession(destination), IMAPSession(source) as session:
        store.init_baseline(source.account_id, source.identity, destination.identity,
                            session.uidvalidity, session.uidnext)
        return store.baseline(source.account_id)
