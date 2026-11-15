#!/usr/bin/env python3
"""Check that imapsync rejects an untrusted TLS peer on each host side."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
from threading import Thread

from probe import account_json_credentials


ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(__file__).resolve().parent / ".local"
IMAPSYNC = LOCAL / "upstream" / "imapsync"


@contextmanager
def untrusted_server(cert: Path, key: Path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(20)
    port = listener.getsockname()[1]
    errors = []

    def serve():
        try:
            connection, _ = listener.accept()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            try:
                with context.wrap_socket(connection, server_side=True):
                    pass
            except ssl.SSLError:
                pass  # Expected when the client rejects the certificate.
        except OSError as exc:
            errors.append(type(exc).__name__)
        finally:
            listener.close()

    thread = Thread(target=serve)
    thread.start()
    try:
        yield port
    finally:
        thread.join(timeout=21)
        if thread.is_alive() or errors:
            raise RuntimeError("local TLS probe server did not finish")


def check_side(side: int, port: int) -> bool:
    yahoo = account_json_credentials(ROOT / "accounts/yahoo/.secrets/account.json")
    alpha = account_json_credentials(ROOT / "accounts/alpha/.secrets/account.json")
    accounts = [yahoo, alpha]
    accounts[side - 1] = ("127.0.0.1", port, "probe", "probe")
    descriptors = []
    try:
        for number, account in enumerate(accounts, start=1):
            descriptor = os.memfd_create(f"imapsync-tls-probe-{number}", 0)
            os.write(descriptor, (account[3] + "\n").encode())
            os.lseek(descriptor, 0, os.SEEK_SET)
            descriptors.append(descriptor)
        command = [
            str(IMAPSYNC),
            "--host1", accounts[0][0], "--port1", str(accounts[0][1]), "--user1", accounts[0][2],
            "--passfile1", f"/proc/self/fd/{descriptors[0]}",
            "--host2", accounts[1][0], "--port2", str(accounts[1][1]), "--user2", accounts[1][2],
            "--passfile2", f"/proc/self/fd/{descriptors[1]}",
            "--ssl1", "--ssl2", "--sslargs1", "SSL_verify_mode=1", "--sslargs2", "SSL_verify_mode=1",
            "--justlogin", "--nolog", "--noreleasecheck",
        ]
        log = LOCAL / f"tls-reject-host{side}.log"
        log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(log_fd, "w") as output:
            result = subprocess.run(command, pass_fds=descriptors, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=60, check=False)
        text = log.read_text(errors="replace").lower()
        return result.returncode != 0 and ("certificate verify failed" in text or "certificate verification failed" in text)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def main() -> int:
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOCAL.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="tls-probe-", dir=LOCAL) as temporary:
        cert, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        for side in (1, 2):
            with untrusted_server(cert, key) as port:
                rejected = check_side(side, port)
            print(f"host{side}_invalid_certificate_rejected={rejected}")
            if not rejected:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
