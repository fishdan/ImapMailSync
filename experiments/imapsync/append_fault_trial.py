#!/usr/bin/env python3
"""Drop a disposable IMAP APPEND acknowledgement after server acceptance."""

import argparse
from email import policy
from email.parser import BytesParser
import imaplib
import os
from pathlib import Path
import re
import socket
import subprocess
from threading import Event, Thread

from local_trial import HOST, IMAPSYNC, LOCAL, ROOT, USERS, connect, fetch, fixture, uids
from verify_copies import HEADERS, payloads


class AckDropProxy:
    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((HOST, 0))
        self.listener.listen()
        self.listener.settimeout(0.5)
        self.port = self.listener.getsockname()[1]
        self.faulted = Event()
        self.stopped = Event()
        self.thread = Thread(target=self._accept, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)

    def _accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except (OSError, socket.timeout):
                continue
            if self.faulted.is_set():
                client.close()
                continue
            Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        upstream = socket.create_connection((HOST, self.upstream_port), timeout=15)
        client.settimeout(None)
        upstream.settimeout(None)
        tag = [None]
        pending_commands = [b""]
        pending_responses = [b""]

        def client_to_server():
            try:
                while not self.faulted.is_set():
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    pending_commands[0] += chunk
                    while b"\r\n" in pending_commands[0]:
                        line, pending_commands[0] = pending_commands[0].split(b"\r\n", 1)
                        match = re.match(rb"^([A-Za-z0-9.]+) APPEND\s", line, re.IGNORECASE)
                        if match:
                            tag[0] = match.group(1)
                    upstream.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        forwarder = Thread(target=client_to_server, daemon=True)
        forwarder.start()
        try:
            while not self.faulted.is_set():
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                pending_responses[0] += chunk
                while b"\r\n" in pending_responses[0]:
                    line, pending_responses[0] = pending_responses[0].split(b"\r\n", 1)
                    if tag[0] and line.upper().startswith(tag[0].upper() + b" OK"):
                        self.faulted.set()
                        return  # Server accepted APPEND; client never receives tagged OK.
                    client.sendall(line + b"\r\n")
        except OSError:
            pass
        finally:
            client.close()
            upstream.close()
            forwarder.join(timeout=2)


def run_imapsync(source_port: int, destination_port: int, uid: int, state: Path, label: str,
                 marker: str | None = None) -> tuple[int, int]:
    descriptors = []
    try:
        for number, (_, password) in enumerate(USERS, start=1):
            fd = os.memfd_create(f"fault-trial-password-{number}", 0)
            os.write(fd, (password + "\n").encode())
            os.lseek(fd, 0, os.SEEK_SET)
            descriptors.append(fd)
        command = [
            str(IMAPSYNC),
            "--host1", HOST, "--port1", str(source_port), "--user1", USERS[0][0],
            "--passfile1", f"/proc/self/fd/{descriptors[0]}",
            "--host2", HOST, "--port2", str(destination_port), "--user2", USERS[1][0],
            "--passfile2", f"/proc/self/fd/{descriptors[1]}",
            "--nossl1", "--nossl2", "--notls1", "--notls2",
            "--folder", "INBOX", "--search1", f"UID {uid}",
            "--useuid", "--tmpdir", str(state), "--noresyncflags", "--noexpunge1", "--noexpunge2",
            "--nofoldersizes", "--nofoldersizesatend", "--nochecknoabletosearch",
            "--nolog", "--noreleasecheck",
        ]
        if marker:
            command.extend(("--regexmess", rf"s{{\A}}{{X-Mailsync-Key: {marker}\r\n}}"))
        path = LOCAL / f"append-fault-{label}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            try:
                result = subprocess.run(command, pass_fds=descriptors, stdout=output, stderr=subprocess.STDOUT,
                                        timeout=25, check=False)
            except subprocess.TimeoutExpired:
                return 124, -1
        match = re.search(r"^Messages transferred\s*:\s*(\d+)", path.read_text(errors="replace"), re.MULTILINE)
        return result.returncode, int(match.group(1)) if match else -1
    finally:
        for fd in descriptors:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--marker", action="store_true", help="inject a source-UID marker and reconcile without retry")
    args = parser.parse_args()
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL.chmod(0o700)
    state = ROOT / ".state" / f"append-fault-trial-{os.getpid()}"
    state.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    source = connect(args.port, *USERS[0])
    destination = connect(args.port, *USERS[1])
    try:
        source.append("INBOX", None, None, fixture("ack-loss", "<ack-loss@example.test>", "plain"))
        uid = uids(source)[-1]
        marker = f"local-source-{uid}" if args.marker else None
        before = set(uids(destination))
        proxy = AckDropProxy(args.port)
        proxy.start()
        try:
            first_exit, first_copied = run_imapsync(args.port, proxy.port, uid, state, "lost-ack", marker)
        finally:
            proxy.stop()
        source = connect(args.port, *USERS[0])
        destination = connect(args.port, *USERS[1])
        uids(source)
        after_fault = set(uids(destination))
        newly_arrived = after_fault - before
        cache_after_fault = [path for path in state.rglob("*") if path.is_file() and re.fullmatch(r"\d+_\d+", path.name)]
        source_raw = fetch(source, uid, "BODY.PEEK[]")
        destination_raw = fetch(destination, next(iter(newly_arrived)), "BODY.PEEK[]") if len(newly_arrived) == 1 else b""
        copied_payload_matches = (
            len(newly_arrived) == 1
            and payloads(source_raw) == payloads(destination_raw)
        )
        if marker:
            source_mail = BytesParser(policy=policy.default).parsebytes(source_raw)
            destination_mail = BytesParser(policy=policy.default).parsebytes(destination_raw)
            headers_preserved = all(source_mail.get_all(header, []) == destination_mail.get_all(header, [])
                                    for header in HEADERS)
            status, data = destination.uid("SEARCH", None, "HEADER", "X-Mailsync-Key", marker)
            marker_matches = len((data[0] or b"").split()) if status == "OK" else -1
            print(f"append_ack_dropped={proxy.faulted.is_set()}")
            print(f"first_exit={first_exit} first_reported_copied={first_copied}")
            print(f"destination_new_after_fault={len(newly_arrived)} cache_mappings_after_fault={len(cache_after_fault)}")
            print(f"destination_payload_matches={copied_payload_matches} original_headers_preserved={headers_preserved}")
            print(f"unique_marker_matches={marker_matches} reconciled_without_retry={marker_matches == 1}")
            return 0 if (proxy.faulted.is_set() and len(newly_arrived) == 1 and copied_payload_matches
                         and headers_preserved and marker_matches == 1) else 1
        # Test the candidate's plain retry behavior. Both accounts are disposable.
        retry_exit, retry_copied = run_imapsync(args.port, args.port, uid, state, "unreconciled-retry")
        after_retry = set(uids(destination))
        print(f"append_ack_dropped={proxy.faulted.is_set()}")
        print(f"first_exit={first_exit} first_reported_copied={first_copied}")
        print(f"destination_new_after_fault={len(newly_arrived)} cache_files_after_fault={len(cache_after_fault)}")
        print(f"destination_payload_matches={copied_payload_matches}")
        print(f"unreconciled_retry_exit={retry_exit} unreconciled_retry_reported_copied={retry_copied}")
        print(f"destination_new_after_retry={len(after_retry - before)}")
        return 0 if proxy.faulted.is_set() and len(newly_arrived) == 1 and copied_payload_matches else 1
    finally:
        for client in (source, destination):
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
