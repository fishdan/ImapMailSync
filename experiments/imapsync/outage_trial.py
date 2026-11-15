#!/usr/bin/env python3
"""Disposable two-source outage and recovery imapsync trial."""

import argparse
import imaplib
import os
from pathlib import Path
import re
import subprocess

from local_trial import HOST, IMAPSYNC, LOCAL, ROOT, connect, fetch, fixture, uids


USERS = {"source": "sourcepass", "delta": "deltapass", "destination": "destpass"}


def run(port: int, name: str, uid: int, available: bool, state: Path) -> tuple[int, int]:
    descriptors = []
    try:
        for number, password in enumerate((USERS[name], USERS["destination"]), start=1):
            fd = os.memfd_create(f"outage-trial-password-{number}", 0)
            os.write(fd, (password + "\n").encode())
            os.lseek(fd, 0, os.SEEK_SET)
            descriptors.append(fd)
        source_host = HOST if available else "127.0.0.2"
        command = [
            str(IMAPSYNC),
            "--host1", source_host, "--port1", str(port), "--user1", name,
            "--passfile1", f"/proc/self/fd/{descriptors[0]}",
            "--host2", HOST, "--port2", str(port), "--user2", "destination",
            "--passfile2", f"/proc/self/fd/{descriptors[1]}",
            "--nossl1", "--nossl2", "--notls1", "--notls2",
            "--timeout1", "3", "--timeout2", "3",
            "--folder", "INBOX", "--search1", f"UID {uid}",
            "--useuid", "--tmpdir", str(state / name), "--noresyncflags", "--noexpunge1", "--noexpunge2",
            "--nofoldersizes", "--nofoldersizesatend", "--nochecknoabletosearch",
            "--nolog", "--noreleasecheck",
        ]
        (state / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        label = "outage" if not available else "recovered" if name == "source" else "healthy"
        path = LOCAL / f"outage-{name}-{label}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            result = subprocess.run(command, pass_fds=descriptors, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=45, check=False)
        match = re.search(r"^Messages transferred\s*:\s*(\d+)", path.read_text(errors="replace"), re.MULTILINE)
        return result.returncode, int(match.group(1)) if match else 0
    finally:
        for fd in descriptors:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL.chmod(0o700)
    state = ROOT / ".state" / f"outage-trial-{os.getpid()}"
    state.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    source = connect(args.port, "source", USERS["source"])
    delta = connect(args.port, "delta", USERS["delta"])
    destination = connect(args.port, "destination", USERS["destination"])
    try:
        source.append("INBOX", None, None, fixture("source-recovery", "<source-recovery@example.test>", "plain"))
        delta.append("INBOX", None, None, fixture("delta-healthy", "<delta-healthy@example.test>", "plain"))
        source_uid, delta_uid = uids(source)[-1], uids(delta)[-1]
        before = set(uids(destination))
        failed_exit, failed_copied = run(args.port, "source", source_uid, False, state)
        healthy_exit, healthy_copied = run(args.port, "delta", delta_uid, True, state)
        after_healthy = set(uids(destination))
        recovered_exit, recovered_copied = run(args.port, "source", source_uid, True, state)
        after_recovery = set(uids(destination))
        destination_payloads = {fetch(destination, uid, "BODY.PEEK[]") for uid in after_recovery - before}
        source_payloads = {fetch(source, source_uid, "BODY.PEEK[]"), fetch(delta, delta_uid, "BODY.PEEK[]")}
        print(f"unavailable_source_exit={failed_exit} copied={failed_copied}")
        print(f"healthy_source_exit={healthy_exit} copied={healthy_copied} destination_new={len(after_healthy - before)}")
        print(f"recovered_source_exit={recovered_exit} copied={recovered_copied} destination_new_total={len(after_recovery - before)}")
        print(f"payloads_preserved={destination_payloads == source_payloads}")
        return 0 if (failed_exit != 0 and failed_copied == 0 and healthy_exit == 0 and healthy_copied == 1
                     and len(after_healthy - before) == 1 and recovered_exit == 0 and recovered_copied == 1
                     and len(after_recovery - before) == 2 and destination_payloads == source_payloads) else 1
    finally:
        for client in (source, delta, destination):
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
