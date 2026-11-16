"""One nonoverlapping sync cycle with durable per-source recovery."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import imaplib
import os
from pathlib import Path
import sqlite3
import ssl
import time

from .baseline import eligible_uids, marker_for, within_24_hours
from .config import Account, discover_accounts
from .imap_client import IMAPError, IMAPSession
from .labels import LabelError, derive_source_label
from .state import StateError, Store, utc_now


class CycleError(Exception):
    """A sanitized global cycle failure."""


@contextmanager
def process_lock(root: Path):
    directory = root / ".state"
    if directory.is_symlink():
        raise CycleError("UNSAFE_STATE_PATH")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    lock_path = directory / "run.lock"
    if lock_path.is_symlink():
        raise CycleError("UNSAFE_LOCK_PATH")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CycleError("RUN_ALREADY_ACTIVE") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _result(account_id: str, status: str, copied: int = 0, skipped: int = 0,
            failed: int = 0, error_code: str | None = None,
            issue_uids: set[int] | None = None, labeled: int = 0,
            label_pending: int = 0) -> dict:
    result = {"account_id": account_id, "status": status, "copied": copied,
              "skipped": skipped, "failed": failed, "labeled": labeled,
              "label_pending": label_pending}
    if error_code:
        result["error_code"] = error_code
    if issue_uids:
        result["issue_uids"] = sorted(issue_uids)
    return result


def _matches(destination: IMAPSession, marker: str, *, after_append: bool = False) -> list[int]:
    for attempt in range(3 if after_append else 1):
        matches = destination.find_marker(marker, datetime.now(timezone.utc) - timedelta(hours=24))
        if matches or not after_append or attempt == 2:
            return matches
        time.sleep(1)
    return []


def _confirm_label(store: Store, destination: IMAPSession, account_id: str,
                   source_uidvalidity: int, source_uid: int, destination_uid: int,
                   label: str) -> None:
    if label not in destination.labels_for_uid(destination_uid):
        destination.add_label(destination_uid, label)
    if label not in destination.labels_for_uid(destination_uid):
        raise IMAPError("LABEL_UNCONFIRMED")
    store.set_label_status(account_id, source_uidvalidity, source_uid, "confirmed")


def _repair_pending(store: Store, destination: IMAPSession, account_id: str,
                    label: str) -> tuple[int, int, int, int, int, set[int], str | None, bool]:
    """Reconcile only recorded jobs, without reading source mail or broad old mail."""
    copied = labeled = pending = skipped = missing = 0
    issue_uids: set[int] = set()
    error_code = None
    for job in store.pending_label_jobs(account_id):
        uid = job["source_uid"]
        key = (account_id, job["uidvalidity"], uid)
        try:
            target = None
            if (job["destination_uid"] is not None
                    and job["destination_uidvalidity"] == destination.uidvalidity
                    and destination.marker_at_uid(job["destination_uid"], job["marker"])):
                target = job["destination_uid"]
            if target is None:
                matches = destination.find_marker_exact(job["marker"])
                if len(matches) > 1:
                    store.block_source(account_id)
                    store.set_label_status(*key, "pending", "MULTIPLE_MARKER_MATCHES")
                    issue_uids.add(uid)
                    return (copied, labeled, pending + 1, skipped, missing,
                            issue_uids, "MULTIPLE_MARKER_MATCHES", True)
                if matches:
                    target = matches[0]
                elif job["destination_uid"] is not None:
                    if destination.copy_absent_account_wide(job["marker"]):
                        store.set_label_status(*key, "missing", "LABEL_COPY_MISSING")
                        missing += 1
                        issue_uids.add(uid)
                        error_code = "LABEL_COPY_MISSING"
                    else:
                        store.set_label_status(*key, "pending", "DESTINATION_COPY_INACCESSIBLE")
                        pending += 1
                        issue_uids.add(uid)
                        error_code = "DESTINATION_COPY_INACCESSIBLE"
                    continue
                else:
                    started = datetime.fromisoformat(store.attempt(*key)["started_at"])
                    if started < datetime.now(timezone.utc) - timedelta(hours=24):
                        store.set_label_status(*key, "expired", "AGE_WINDOW_EXPIRED")
                        store.mark_failed(*key, "AGE_WINDOW_EXPIRED")
                        issue_uids.add(uid)
                        skipped += 1
                        error_code = "AGE_WINDOW_EXPIRED"
                    continue
            if job["destination_uid"] is None:
                store.confirm(*key, job["marker"], destination.uidvalidity, target)
                copied += 1
            elif (job["destination_uidvalidity"], job["destination_uid"]) != (
                    destination.uidvalidity, target):
                store.reconcile_destination_uid(*key, job["marker"],
                                                destination.uidvalidity, target)
            _confirm_label(store, destination, *key, target, label)
            labeled += 1
        except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
            store.set_label_status(*key, "pending", "LABEL_RECOVERY_UNCONFIRMED")
            pending += 1
            issue_uids.add(uid)
            error_code = "LABEL_RECOVERY_UNCONFIRMED"
    return copied, labeled, pending, skipped, missing, issue_uids, error_code, False


def _sync_source(store: Store, source_account: Account, destination_account: Account,
                 destination: IMAPSession) -> dict:
    account_id = source_account.account_id
    baseline = store.baseline(account_id)
    if baseline is None:
        return _result(account_id, "failed", failed=1, error_code="SOURCE_UNINITIALIZED")
    if baseline["status"] != "active":
        return _result(account_id, "blocked", failed=1, error_code="SOURCE_BLOCKED")
    if (baseline["account_identity"] != source_account.identity
            or baseline["destination_identity"] != destination_account.identity):
        store.block_source(account_id)
        return _result(account_id, "blocked", failed=1, error_code="ACCOUNT_IDENTITY_CHANGED")
    try:
        label = derive_source_label(source_account.username)
    except LabelError:
        return _result(account_id, "failed", failed=1, error_code="INVALID_LABEL_ADDRESS")
    try:
        destination.ensure_label(label)
    except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError) as exc:
        code = str(exc) if isinstance(exc, IMAPError) else "LABEL_PREPARE_FAILED"
        return _result(account_id, "failed", failed=1, error_code=code)
    try:
        copied, labeled, label_pending, skipped, missing, issue_uids, error_code, blocked = (
            _repair_pending(store, destination, account_id, label))
    except StateError:
        store.block_source(account_id)
        return _result(account_id, "blocked", failed=1, error_code="STATE_INCONSISTENT")
    if blocked:
        return _result(account_id, "blocked", copied, skipped, label_pending,
                       error_code, issue_uids, labeled, label_pending)
    failed = label_pending + missing
    expired_now = store.expire_old_attempts(account_id, baseline["uidvalidity"],
                                            datetime.now(timezone.utc) - timedelta(hours=24))
    issue_uids.update(expired_now)
    expired = store.expired_uids(account_id, baseline["uidvalidity"])
    if expired_now and not error_code:
        error_code = "AGE_WINDOW_EXPIRED"
    try:
        with IMAPSession(source_account) as source:
            if source.uidvalidity != baseline["uidvalidity"]:
                store.block_source(account_id)
                return _result(account_id, "blocked", failed=1, error_code="SOURCE_UIDVALIDITY_CHANGED")
            candidate_uids = source.uids_since(datetime.now(timezone.utc) - timedelta(hours=24))
            confirmed = store.confirmed_uids(account_id, source.uidvalidity)
            selected = eligible_uids(candidate_uids, baseline["first_eligible_uid"], confirmed | expired)
            skipped = sum(uid >= baseline["first_eligible_uid"] for uid in candidate_uids) - len(selected)
            skipped += len(expired_now - set(candidate_uids))
            for uid in selected:
                marker = marker_for(account_id, source_account.identity, source.uidvalidity, uid)
                try:
                    previous_attempt = store.attempt(account_id, source.uidvalidity, uid)
                    store.begin_attempt(account_id, source.uidvalidity, uid, marker)
                    item = source.fetch(uid)
                    if not within_24_hours(item.internaldate, datetime.now(timezone.utc)):
                        code = ("FUTURE_INTERNALDATE" if item.internaldate > datetime.now(timezone.utc)
                                else "AGE_WINDOW_EXPIRED")
                        store.mark_failed(account_id, source.uidvalidity, uid, code)
                        issue_uids.add(uid)
                        skipped += 1
                        error_code = code
                        continue
                    matches = _matches(destination, marker)
                    if len(matches) > 1:
                        store.mark_uncertain(account_id, source.uidvalidity, uid, "MULTIPLE_MARKER_MATCHES")
                        store.block_source(account_id)
                        issue_uids.add(uid)
                        return _result(account_id, "blocked", copied, skipped, failed + 1,
                                       "MULTIPLE_MARKER_MATCHES", issue_uids)
                    if previous_attempt and not store.label_job(account_id, source.uidvalidity, uid) and matches:
                        store.confirm(account_id, source.uidvalidity, uid, marker,
                                      destination.uidvalidity, matches[0])
                        copied += 1  # Pre-feature accepted copy: no backfill label job.
                        continue
                    if not matches:
                        if not within_24_hours(item.internaldate, datetime.now(timezone.utc)):
                            store.mark_failed(account_id, source.uidvalidity, uid, "AGE_WINDOW_EXPIRED")
                            issue_uids.add(uid)
                            skipped += 1
                            error_code = "AGE_WINDOW_EXPIRED"
                            continue
                        store.begin_label_job(account_id, source.uidvalidity, uid, marker, label)
                        destination.append_copy(item.raw, item.seen, item.internaldate, marker)
                        matches = _matches(destination, marker, after_append=True)
                    else:
                        store.begin_label_job(account_id, source.uidvalidity, uid, marker, label)
                    if len(matches) > 1:
                        store.mark_uncertain(account_id, source.uidvalidity, uid, "MULTIPLE_MARKER_MATCHES")
                        store.block_source(account_id)
                        issue_uids.add(uid)
                        return _result(account_id, "blocked", copied, skipped, failed + 1,
                                       "MULTIPLE_MARKER_MATCHES", issue_uids)
                    if len(matches) != 1:
                        store.mark_uncertain(account_id, source.uidvalidity, uid, "DESTINATION_UNCONFIRMED")
                        issue_uids.add(uid)
                        failed += 1
                        error_code = "DESTINATION_UNCONFIRMED"
                        break
                    store.confirm(account_id, source.uidvalidity, uid, marker,
                                  destination.uidvalidity, matches[0])
                    copied += 1
                    try:
                        _confirm_label(store, destination, account_id, source.uidvalidity,
                                       uid, matches[0], label)
                        labeled += 1
                    except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
                        store.set_label_status(account_id, source.uidvalidity, uid,
                                               "pending", "LABEL_UNCONFIRMED")
                        issue_uids.add(uid)
                        label_pending += 1
                        failed += 1
                        error_code = "LABEL_UNCONFIRMED"
                        break
                except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
                    store.mark_uncertain(account_id, source.uidvalidity, uid, "TRANSFER_UNCERTAIN")
                    issue_uids.add(uid)
                    failed += 1
                    error_code = "TRANSFER_UNCERTAIN"
                    break
                except StateError:
                    store.block_source(account_id)
                    issue_uids.add(uid)
                    return _result(account_id, "blocked", copied, skipped, failed + 1,
                                   "STATE_INCONSISTENT", issue_uids)
    except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
        failed += 1
        error_code = "SOURCE_UNAVAILABLE"
    remaining = store.pending_label_jobs(account_id)
    if len(remaining) > label_pending:
        failed += len({job["source_uid"] for job in remaining} - issue_uids)
        label_pending = len(remaining)
        issue_uids.update(job["source_uid"] for job in remaining)
        if not error_code:
            error_code = "LABEL_UNCONFIRMED"
    return _result(account_id, "failed" if failed else "success", copied, skipped, failed,
                   error_code, issue_uids, labeled, label_pending)


def run_once(root: Path) -> tuple[dict, int]:
    """Run all sources; return a sanitized cycle report and CLI exit code."""
    started = utc_now()
    accounts, errors = discover_accounts(root)
    if "alpha" not in accounts:
        return ({"cycle_started_at": started, "cycle_ended_at": utc_now(),
                 "overall_status": "failed", "accounts": [_result("alpha", "failed", failed=1,
                 error_code=errors.get("alpha", "MISSING_CONFIG"))]}, 2)
    if len(accounts) == 1 and not any(account_id != "alpha" for account_id in errors):
        return ({"cycle_started_at": started, "cycle_ended_at": utc_now(),
                 "overall_status": "failed", "accounts": [], "error_code": "NO_SOURCE_ACCOUNTS"}, 2)
    try:
        with process_lock(root), Store(root) as store:
            cycle_id = store.start_cycle()
            results = []
            try:
                with IMAPSession(accounts["alpha"]) as destination:
                    for account_id, code in sorted(errors.items()):
                        if account_id != "alpha":
                            results.append(_result(account_id, "failed", failed=1, error_code=code))
                    for account_id, account in sorted(accounts.items()):
                        if account_id == "alpha":
                            continue
                        results.append(_sync_source(store, account, accounts["alpha"], destination))
            except (IMAPError, imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError):
                for account_id in sorted(set(accounts) - {"alpha"}):
                    if not any(item["account_id"] == account_id for item in results):
                        results.append(_result(account_id, "failed", failed=1,
                                               error_code="DESTINATION_UNAVAILABLE"))
            for result in results:
                store.record_account(cycle_id, result["account_id"], result["status"],
                                     result["copied"], result["skipped"], result["failed"],
                                     result.get("error_code"))
            status = "success" if all(item["status"] == "success" for item in results) else "partial"
            if results and all(item["status"] != "success" for item in results):
                status = "failed"
            store.finish_cycle(cycle_id, status)
            ended = utc_now()
            return ({"cycle_started_at": started, "cycle_ended_at": ended,
                     "overall_status": status, "accounts": results},
                    3 if any(item["status"] == "blocked" for item in results) else 1 if status != "success" else 0)
    except (CycleError, StateError, sqlite3.DatabaseError) as exc:
        return ({"cycle_started_at": started, "cycle_ended_at": utc_now(),
                 "overall_status": "failed", "accounts": [], "error_code": str(exc)}, 2)
