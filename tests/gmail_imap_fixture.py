"""Disposable, localhost-only IMAP server with the Gmail label extension.

This is a protocol fixture, not a general IMAP server. Tests connect with
``imaplib.IMAP4(fixture.host, fixture.port)`` and keep all mail synthetic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import shlex
import socketserver
import threading


@dataclass
class Message:
    uid: int
    raw: bytes
    internaldate: datetime
    flags: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)


class GmailIMAPFixture:
    """Owns one synthetic Inbox and one-shot protocol faults."""

    host = "127.0.0.1"

    def __init__(self, *, gmail_extension: bool = True):
        self.gmail_extension = gmail_extension
        self.uidvalidity = 7
        self.messages: dict[int, Message] = {}
        self.labels: set[str] = set()
        self.commands: list[str] = []
        self.faults: dict[str, str] = {}
        self._next_uid = 1
        self._lock = threading.RLock()
        self._server = None
        self._thread = None

    def __enter__(self):
        fixture = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"* OK disposable IMAP ready\r\n")
                self.wfile.flush()
                selected = False
                readonly = False
                while line := self.rfile.readline():
                    line = line.rstrip(b"\r\n")
                    literal = b""
                    size = re.search(rb"\{(\d+)\}$", line)
                    if size:
                        self.wfile.write(b"+ send literal\r\n")
                        self.wfile.flush()
                        literal = self.rfile.read(int(size.group(1)))
                        if self.rfile.read(2) != b"\r\n":
                            return
                        line = line[:size.start()].rstrip()
                    try:
                        tag, command, *rest = line.decode("utf-8").split(" ", 2)
                    except (UnicodeDecodeError, ValueError):
                        return
                    args = rest[0] if rest else ""
                    with fixture._lock:
                        fixture.commands.append(command.upper() + (" " + args if args else ""))
                        operation = ("UID " + args.split(" ", 1)[0].upper()
                                     if command.upper() == "UID" else command.upper())
                        fault = fixture.faults.pop(operation, None)
                        if fault == "disconnect":
                            return
                        if fault == "reject":
                            self._reply(tag, "NO", "disposable fault")
                            continue
                        self._lose_ack = fault == "lose_ack"
                        result = self._dispatch(tag, command.upper(), args, literal,
                                                selected, readonly, fixture)
                        if result is None:
                            return
                        selected, readonly = result
                        if fault == "lose_ack":
                            return

            def _reply(self, tag, status="OK", detail="completed"):
                if getattr(self, "_lose_ack", False):
                    return
                self.wfile.write(f"{tag} {status} {detail}\r\n".encode("ascii"))
                self.wfile.flush()

            def _dispatch(self, tag, command, args, literal, selected, readonly, fixture):
                if command == "CAPABILITY":
                    extension = " X-GM-EXT-1" if fixture.gmail_extension else ""
                    self.wfile.write(f"* CAPABILITY IMAP4rev1 UIDPLUS{extension}\r\n".encode())
                elif command == "LOGIN":
                    pass
                elif command in {"SELECT", "EXAMINE"}:
                    selected = True
                    readonly = command == "EXAMINE"
                    self.wfile.write(f"* {len(fixture.messages)} EXISTS\r\n".encode())
                    self.wfile.write(f"* OK [UIDVALIDITY {fixture.uidvalidity}] valid\r\n".encode())
                    self.wfile.write(f"* OK [UIDNEXT {fixture._next_uid}] next\r\n".encode())
                    self.wfile.write(("* FLAGS (\\Seen)\r\n* OK [READ-ONLY] selected\r\n"
                                      if readonly else
                                      "* FLAGS (\\Seen)\r\n* OK [READ-WRITE] selected\r\n").encode())
                elif command == "LIST":
                    names = ["INBOX", *sorted(fixture.labels)]
                    try:
                        pattern = shlex.split(args)[-1]
                    except (ValueError, IndexError):
                        pattern = "*"
                    for name in names:
                        if pattern in {"*", "%", name}:
                            self.wfile.write(b'* LIST (\\HasNoChildren) "/" ' +
                                             _quoted(name) + b"\r\n")
                elif command == "CREATE":
                    try:
                        name, = shlex.split(args)
                    except ValueError:
                        self._reply(tag, "BAD", "invalid mailbox")
                        return selected, readonly
                    if name in fixture.labels:
                        self._reply(tag, "NO", "already exists")
                        return selected, readonly
                    fixture.labels.add(name)
                elif command == "APPEND":
                    if readonly or not literal:
                        self._reply(tag, "NO", "append unavailable")
                        return selected, readonly
                    date_match = re.search(r'"(\d{1,2}-\w{3}-\d{4} \d\d:\d\d:\d\d [+-]\d{4})"', args)
                    arrival = (datetime.strptime(date_match.group(1), "%d-%b-%Y %H:%M:%S %z")
                               if date_match else None)
                    flags = {r"\Seen"} if r"\Seen" in args else set()
                    uid = fixture.seed(literal, flags=flags, internaldate=arrival)
                    self._reply(tag, "OK", f"[APPENDUID {fixture.uidvalidity} {uid}] appended")
                    return selected, readonly
                elif command == "UID":
                    if not selected:
                        self._reply(tag, "BAD", "select mailbox first")
                        return selected, readonly
                    self._uid(tag, args, readonly, fixture)
                    return selected, readonly
                elif command == "NOOP":
                    pass
                elif command == "LOGOUT":
                    self.wfile.write(b"* BYE disposable IMAP closing\r\n")
                    self._reply(tag)
                    return None
                else:
                    self._reply(tag, "BAD", "unsupported command")
                    return selected, readonly
                self._reply(tag)
                return selected, readonly

            def _uid(self, tag, args, readonly, fixture):
                parts = args.split(" ", 2)
                if len(parts) < 2:
                    self._reply(tag, "BAD", "missing UID arguments")
                    return
                command = parts[0].upper()
                uid_arg = parts[1]
                details = parts[2] if len(parts) > 2 else ""
                if command == "SEARCH":
                    criteria = args[len("SEARCH "):]
                    marker = re.search(r'HEADER X-Mailsync-Key (?:"([^"]+)"|(\S+))',
                                       criteria, re.IGNORECASE)
                    found = []
                    for message in fixture.messages.values():
                        if marker:
                            expected = (marker.group(1) or marker.group(2)).encode()
                            headers = message.raw.split(b"\r\n\r\n", 1)[0]
                            if not re.search(rb"(?im)^X-Mailsync-Key:[ \t]*" +
                                             re.escape(expected) + rb"[ \t]*\r?$", headers):
                                continue
                        found.append(str(message.uid))
                    self.wfile.write(("* SEARCH " + " ".join(found) + "\r\n").encode())
                elif command == "FETCH":
                    try:
                        uid = int(uid_arg)
                    except ValueError:
                        self._reply(tag, "BAD", "invalid UID")
                        return
                    message = fixture.messages.get(uid)
                    if message:
                        fields = [f"UID {uid}"]
                        if "FLAGS" in details.upper():
                            fields.append("FLAGS (" + " ".join(sorted(message.flags)) + ")")
                        if "INTERNALDATE" in details.upper():
                            date = message.internaldate.strftime("%d-%b-%Y %H:%M:%S %z")
                            fields.append(f'INTERNALDATE "{date}"')
                        if "X-GM-LABELS" in details.upper():
                            labels = b" ".join(_quoted(label) for label in sorted(message.labels))
                            fields.append("X-GM-LABELS (" + labels.decode() + ")")
                        prefix = f"* 1 FETCH ({' '.join(fields)}".encode()
                        if "BODY.PEEK[]" in details.upper():
                            prefix += f" BODY[] {{{len(message.raw)}}}\r\n".encode()
                            self.wfile.write(prefix + message.raw + b")\r\n")
                        elif "BODY.PEEK[HEADER.FIELDS" in details.upper():
                            header = message.raw.split(b"\r\n\r\n", 1)[0]
                            selected_headers = b"\r\n".join(line for line in header.split(b"\r\n")
                                                            if line.lower().startswith(b"x-mailsync-key:"))
                            selected_headers += b"\r\n\r\n"
                            prefix += (b" BODY[HEADER.FIELDS (X-Mailsync-Key)] {" +
                                       str(len(selected_headers)).encode() + b"}\r\n")
                            self.wfile.write(prefix + selected_headers + b")\r\n")
                        else:
                            self.wfile.write(prefix + b")\r\n")
                elif command == "STORE":
                    if readonly or "+X-GM-LABELS" not in details.upper():
                        self._reply(tag, "NO", "additive label STORE required")
                        return
                    message = fixture.messages.get(int(uid_arg))
                    match = re.search(r"\+X-GM-LABELS(?:\.SILENT)?\s+\((.*)\)", details,
                                      re.IGNORECASE)
                    if not message or not match:
                        self._reply(tag, "NO", "message or labels missing")
                        return
                    try:
                        names = shlex.split(match.group(1))
                    except ValueError:
                        self._reply(tag, "BAD", "invalid label")
                        return
                    if any(name not in fixture.labels for name in names):
                        self._reply(tag, "NO", "label does not exist")
                        return
                    message.labels.update(names)
                    if ".SILENT" not in details.upper():
                        labels = b" ".join(_quoted(name) for name in sorted(message.labels))
                        self.wfile.write(b"* 1 FETCH (UID " + str(message.uid).encode() +
                                         b" X-GM-LABELS (" + labels + b"))\r\n")
                else:
                    self._reply(tag, "BAD", "unsupported UID command")
                    return
                self._reply(tag)

        self._server = Server((self.host, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def seed(self, raw: bytes, *, labels=(), flags=(), internaldate=None) -> int:
        with self._lock:
            uid = self._next_uid
            self._next_uid += 1
            self.messages[uid] = Message(uid, raw, internaldate or datetime.now(timezone.utc),
                                         set(flags), set(labels))
            self.labels.update(labels)
            return uid

    def fault_next(self, operation: str, mode: str) -> None:
        """One-shot ``reject``, ``disconnect``, or ``lose_ack`` fault."""
        if mode not in {"reject", "disconnect", "lose_ack"}:
            raise ValueError("unknown fault mode")
        with self._lock:
            self.faults[operation.upper()] = mode


def _quoted(value: str) -> bytes:
    return b'"' + value.replace("\\", "\\\\").replace('"', '\\"').encode() + b'"'
