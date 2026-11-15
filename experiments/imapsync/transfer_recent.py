#!/usr/bin/env python3
"""One-off, bounded Yahoo INBOX to alpha INBOX imapsync trial."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import imaplib
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys

from probe import account_json_credentials


ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(__file__).resolve().parent / ".local"
IMAPSYNC = LOCAL / "upstream" / "imapsync"
INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
SIZE = re.compile(rb'RFC822.SIZE (\d+)')


def within_window(internaldate: bytes, cutoff: datetime) -> bool:
    arrival = parsedate_to_datetime(internaldate.decode("ascii")).astimezone(timezone.utc)
    return arrival >= cutoff


def recent_uids(account: tuple[str, int, str, str], cutoff: datetime) -> list[int]:
    host, port, username, password = account
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(username, password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("INBOX unavailable")
        status, data = client.uid("SEARCH", None, "SINCE", cutoff.strftime("%d-%b-%Y"))
        if status != "OK":
            raise RuntimeError("UID date search failed")
        selected: list[int] = []
        for raw_uid in (data[0] or b"").split():
            status, response = client.uid("FETCH", raw_uid.decode("ascii"), "(UID INTERNALDATE RFC822.SIZE)")
            if status != "OK" or not response or not response[0]:
                raise RuntimeError("metadata fetch failed")
            metadata = response[0][0] if isinstance(response[0], tuple) else response[0]
            date_match, size_match = INTERNALDATE.search(metadata), SIZE.search(metadata)
            if not date_match or not size_match:
                raise RuntimeError("metadata incomplete")
            if within_window(date_match.group(1), cutoff):
                selected.append(int(raw_uid))
        return selected
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def run_trial(source: tuple[str, int, str, str], destination: tuple[str, int, str, str],
              uids: list[int], destination_uids: list[int], apply: bool) -> int:
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL.chmod(0o700)
    cache = ROOT / ".state" / "yahoo" / "imapsync-trial"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (ROOT / ".state", ROOT / ".state" / "yahoo", cache):
        directory.chmod(0o700)
    descriptors: list[int] = []
    try:
        for index, account in enumerate((source, destination), start=1):
            descriptor = os.memfd_create(f"imapsync-password-{index}", 0)
            os.write(descriptor, (account[3] + "\n").encode("utf-8"))
            os.lseek(descriptor, 0, os.SEEK_SET)
            descriptors.append(descriptor)
        command = [
            str(IMAPSYNC),
            "--host1", source[0], "--port1", str(source[1]), "--user1", source[2],
            "--passfile1", f"/proc/self/fd/{descriptors[0]}",
            "--host2", destination[0], "--port2", str(destination[1]), "--user2", destination[2],
            "--passfile2", f"/proc/self/fd/{descriptors[1]}",
            "--ssl1", "--ssl2", "--sslargs1", "SSL_verify_mode=1", "--sslargs2", "SSL_verify_mode=1",
            "--folder", "Inbox", "--search1", "UID " + ",".join(str(uid) for uid in uids),
            "--search2", "UID " + ",".join(str(uid) for uid in destination_uids) if destination_uids else "NOT ALL",
            "--useuid", "--tmpdir", str(cache), "--noresyncflags", "--noexpunge1", "--noexpunge2",
            "--nofoldersizes", "--nofoldersizesatend", "--nochecknoabletosearch", "--nolog", "--noreleasecheck",
        ]
        if not apply:
            command.append("--dry")
        log = LOCAL / ("recent-apply.log" if apply else "recent-dry.log")
        log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(log_fd, "w", encoding="utf-8") as output:
            result = subprocess.run(command, pass_fds=descriptors, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=1800, check=False)
        print(f"mode={'apply' if apply else 'dry'} source_selected={len(uids)} alpha_selected={len(destination_uids)} exit={result.returncode} private_log={log.relative_to(ROOT)}")
        return result.returncode
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="copy selected messages after inspecting dry-run log")
    args = parser.parse_args()
    source = account_json_credentials(ROOT / "accounts/yahoo/.secrets/account.json")
    destination = account_json_credentials(ROOT / "accounts/alpha/.secrets/account.json")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    uids = recent_uids(source, cutoff)
    destination_uids = recent_uids(destination, cutoff)
    print(f"cutoff_utc={cutoff.isoformat()} yahoo_eligible_count={len(uids)} alpha_recent_count={len(destination_uids)} yahoo_eligible_uids={uids}")
    if not uids:
        return 0
    return run_trial(source, destination, uids, destination_uids, args.apply)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, imaplib.IMAP4.error, subprocess.TimeoutExpired) as exc:
        print(f"trial failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
