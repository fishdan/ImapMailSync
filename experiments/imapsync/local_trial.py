#!/usr/bin/env python3
"""Disposable local IMAP fixture trial for imapsync behavior."""

import argparse
from collections import Counter
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate
import imaplib
import os
from pathlib import Path
import re
import subprocess

from probe import eligible_uids
from verify_copies import payloads


ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(__file__).resolve().parent / ".local"
IMAPSYNC = LOCAL / "upstream" / "imapsync"
STATE = ROOT / ".state" / f"local-imapsync-trial-{os.getpid()}"
HOST = "127.0.0.1"
USERS = (("source", "sourcepass"), ("destination", "destpass"))


def connect(port: int, user: str, password: str) -> imaplib.IMAP4:
    client = imaplib.IMAP4(HOST, port, timeout=30)
    client.login(user, password)
    return client


def uids(client: imaplib.IMAP4) -> list[int]:
    client.select("INBOX", readonly=True)
    status, data = client.uid("SEARCH", None, "ALL")
    if status != "OK":
        raise RuntimeError("UID search failed")
    return [int(value) for value in (data[0] or b"").split()]


def fetch(client: imaplib.IMAP4, uid: int, item: str) -> bytes:
    status, data = client.uid("FETCH", str(uid), f"(UID {item})")
    if status != "OK" or not data:
        raise RuntimeError("fixture fetch failed")
    for entry in data:
        if isinstance(entry, tuple):
            return entry[1] if item == "BODY.PEEK[]" else entry[0]
        if isinstance(entry, bytes):
            return entry
    raise RuntimeError("fixture fetch body missing")


def fixture(subject: str, message_id: str | None, kind: str) -> bytes:
    mail = EmailMessage()
    mail["From"] = "sender@example.test"
    mail["To"] = "source@example.test"
    mail["Subject"] = subject
    mail["Date"] = formatdate(localtime=False)
    if message_id is not None:
        mail["Message-ID"] = message_id
    mail.set_content(f"{subject} plain payload")
    if kind in ("html", "attachment"):
        mail.add_alternative(f"<html><body>{subject} HTML payload</body></html>", subtype="html")
    if kind == "attachment":
        mail.add_attachment(b"fixture attachment bytes", maintype="application", subtype="octet-stream",
                            filename="fixture.bin")
    return mail.as_bytes(policy=policy.SMTP)


def run_imapsync(port: int, selected: list[int], label: str) -> tuple[int, int]:
    if not selected:
        raise ValueError("selection must not be empty")
    descriptors = []
    try:
        for index, (_, password) in enumerate(USERS, start=1):
            fd = os.memfd_create(f"local-trial-password-{index}", 0)
            os.write(fd, (password + "\n").encode())
            os.lseek(fd, 0, os.SEEK_SET)
            descriptors.append(fd)
        command = [
            str(IMAPSYNC),
            "--host1", HOST, "--port1", str(port), "--user1", USERS[0][0],
            "--passfile1", f"/proc/self/fd/{descriptors[0]}",
            "--host2", HOST, "--port2", str(port), "--user2", USERS[1][0],
            "--passfile2", f"/proc/self/fd/{descriptors[1]}",
            "--nossl1", "--nossl2", "--notls1", "--notls2",
            "--folder", "INBOX", "--search1", "UID " + ",".join(map(str, selected)),
            "--useuid", "--tmpdir", str(STATE), "--noresyncflags", "--noexpunge1", "--noexpunge2",
            "--nofoldersizes", "--nofoldersizesatend", "--nochecknoabletosearch",
            "--nolog", "--noreleasecheck",
        ]
        path = LOCAL / f"local-{label}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            result = subprocess.run(command, pass_fds=descriptors, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=120, check=False)
        log = path.read_text(errors="replace")
        match = re.search(r"^Messages transferred\s*:\s*(\d+)", log, re.MULTILINE)
        return result.returncode, int(match.group(1)) if match else -1
    finally:
        for fd in descriptors:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL.chmod(0o700)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    source = connect(args.port, *USERS[0])
    destination = connect(args.port, *USERS[1])
    try:
        source.append("INBOX", None, None, fixture("old", "<old@example.test>", "plain"))
        baseline = max(uids(source)) + 1
        fixtures = (
            ("plain", "<shared@example.test>", "plain"),
            ("html", "<shared@example.test>", "html"),
            ("attached", "<attachment@example.test>", "attachment"),
            ("missing-id", None, "plain"),
        )
        for subject, message_id, kind in fixtures:
            source.append("INBOX", None, None, fixture(subject, message_id, kind))
        selected = eligible_uids(uids(source), baseline, set())
        before = {uid: fetch(source, uid, "FLAGS") for uid in selected}
        first_exit, first_copied = run_imapsync(args.port, selected, "first")
        destination_uids = uids(destination)
        source_unchanged = before == {uid: fetch(source, uid, "FLAGS") for uid in selected}
        source_messages = [BytesParser(policy=policy.default).parsebytes(fetch(source, uid, "BODY.PEEK[]"))
                           for uid in selected]
        destination_messages = [BytesParser(policy=policy.default).parsebytes(fetch(destination, uid, "BODY.PEEK[]"))
                                for uid in destination_uids]
        source_payloads = Counter(payloads(mail.as_bytes(policy=policy.SMTP)) for mail in source_messages)
        dest_payloads = Counter(payloads(mail.as_bytes(policy=policy.SMTP)) for mail in destination_messages)
        ids = [tuple(mail.get_all("Message-ID", [])) for mail in destination_messages]
        duplicate_ids = sum(n - 1 for n in Counter(ids).values() if n > 1)
        missing_ids = sum(not item for item in ids)
        attachments = sum(any(part.get_content_disposition() == "attachment" for part in mail.walk())
                          for mail in destination_messages)
        flagged_uid = destination_uids[1]
        destination.select("INBOX", readonly=False)
        destination.uid("STORE", str(flagged_uid), "+FLAGS", r"(\Seen)")
        second_exit, second_copied = run_imapsync(args.port, selected, "repeat")
        repeat_count = len(uids(destination))
        destination_flag_retained = b"\\Seen" in fetch(destination, flagged_uid, "FLAGS")

        # This deletion is confined to the disposable local destination mailbox.
        deleted_uid = destination_uids[0]
        destination.select("INBOX", readonly=False)
        destination.uid("STORE", str(deleted_uid), "+FLAGS", r"(\Deleted)")
        destination.expunge()
        confirmed = set(selected)
        source.append("INBOX", None, None, fixture("new-after-delete", "<new@example.test>", "plain"))
        remaining = eligible_uids(uids(source), baseline, confirmed)
        third_exit, third_copied = run_imapsync(args.port, remaining, "after-delete")
        final_destination_uids = uids(destination)
        deleted_reappeared = deleted_uid in final_destination_uids

        results = {
            "old_source_excluded": baseline > min(uids(source)) and len(selected) == 4,
            "first_exit": first_exit, "first_copied": first_copied,
            "destination_after_first": len(destination_uids),
            "source_flags_unchanged": source_unchanged,
            "payloads_equal": source_payloads == dest_payloads,
            "duplicate_message_ids_copied": duplicate_ids,
            "missing_message_ids_copied": missing_ids,
            "attachment_messages_copied": attachments,
            "repeat_exit": second_exit, "repeat_copied": second_copied,
            "destination_after_repeat": repeat_count,
            "destination_flag_retained": destination_flag_retained,
            "post_delete_selected": len(remaining),
            "post_delete_exit": third_exit, "post_delete_copied": third_copied,
            "deleted_destination_uid_reappeared": deleted_reappeared,
            "destination_final": len(final_destination_uids),
        }
        for key, value in results.items():
            print(f"{key}={value}")
        return 0 if (
            results["old_source_excluded"] and first_exit == 0 and first_copied == 4
            and len(destination_uids) == 4 and source_unchanged and source_payloads == dest_payloads
            and duplicate_ids == 1 and missing_ids == 1 and attachments == 1
            and second_exit == 0 and second_copied == 0 and repeat_count == 4 and destination_flag_retained
            and len(remaining) == 1 and third_exit == 0 and third_copied == 1
            and not deleted_reappeared and len(final_destination_uids) == 4
        ) else 1
    finally:
        source.logout()
        destination.logout()


if __name__ == "__main__":
    raise SystemExit(main())
