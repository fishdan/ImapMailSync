"""Versioned SQLite state for baselines, attempts, copies, and cycle results."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3


MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateError(Exception):
    """State is absent, inconsistent, or unsafe for copying."""


class Store:
    def __init__(self, root: Path):
        directory = root / ".state"
        if directory.is_symlink():
            raise StateError("UNSAFE_STATE_PATH")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "mailsync.sqlite3"
        if self.path.is_symlink():
            raise StateError("UNSAFE_STATE_PATH")
        self.db = None
        try:
            self.db = sqlite3.connect(self.path, timeout=30)
            self.path.chmod(0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StateError("CORRUPT_STATE")
            self.db.execute("PRAGMA journal_mode = WAL")
        except StateError:
            if self.db is not None:
                self.db.close()
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            if self.db is not None:
                self.db.close()
            raise StateError("CORRUPT_STATE") from exc

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_):
        self.db.close()

    def _migrate(self) -> None:
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "schema_version" not in tables:
            raise StateError("UNRECOGNIZED_STATE")
        current = self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0] if tables else 0
        scripts = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
        versions = [int(path.name.split("_", 1)[0]) for path in scripts]
        if not scripts or versions != list(range(1, len(scripts) + 1)) or current > versions[-1]:
            raise StateError("UNSUPPORTED_SCHEMA_VERSION")
        for version, path in zip(versions, scripts):
            if version <= current:
                continue
            script = path.read_text(encoding="utf-8")
            try:
                self.db.executescript(f"BEGIN IMMEDIATE;\n{script}\n"
                                      f"INSERT INTO schema_version(version, applied_at) VALUES ({version}, '{utc_now()}');\n"
                                      "COMMIT;")
            except sqlite3.DatabaseError as exc:
                self.db.rollback()
                raise StateError("MIGRATION_FAILED") from exc

    def version(self) -> int:
        return int(self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])

    def init_baseline(self, account_id: str, account_identity: str, destination_identity: str,
                      uidvalidity: int, first_eligible_uid: int) -> None:
        if uidvalidity < 1 or first_eligible_uid < 1:
            raise StateError("INVALID_BASELINE")
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO source_baselines VALUES (?, ?, ?, ?, ?, ?, 'active')",
                    (account_id, account_identity, destination_identity, uidvalidity, first_eligible_uid, utc_now()),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError("BASELINE_EXISTS") from exc

    def baseline(self, account_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM source_baselines WHERE account_id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def baselines(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM source_baselines ORDER BY account_id")]

    def block_source(self, account_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE source_baselines SET status='blocked' WHERE account_id=?", (account_id,))

    def confirmed_uids(self, account_id: str, uidvalidity: int) -> set[int]:
        rows = self.db.execute(
            "SELECT source_uid FROM copy_records WHERE account_id=? AND uidvalidity=?",
            (account_id, uidvalidity),
        )
        return {int(row[0]) for row in rows}

    def copy_record(self, account_id: str, uidvalidity: int, source_uid: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM copy_records WHERE account_id=? AND uidvalidity=? AND source_uid=?",
            (account_id, uidvalidity, source_uid),
        ).fetchone()
        return dict(row) if row else None

    def attempt(self, account_id: str, uidvalidity: int, source_uid: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM transfer_attempts WHERE account_id=? AND uidvalidity=? AND source_uid=?",
            (account_id, uidvalidity, source_uid),
        ).fetchone()
        return dict(row) if row else None

    def begin_attempt(self, account_id: str, uidvalidity: int, source_uid: int, marker: str) -> None:
        previous = self.attempt(account_id, uidvalidity, source_uid)
        if previous and previous["marker"] != marker:
            raise StateError("ATTEMPT_MARKER_CHANGED")
        if self.copy_record(account_id, uidvalidity, source_uid):
            raise StateError("COPY_ALREADY_CONFIRMED")
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO transfer_attempts(account_id,uidvalidity,source_uid,marker,status,started_at) "
                    "VALUES (?,?,?,?, 'running', ?) "
                    "ON CONFLICT(account_id,uidvalidity,source_uid) DO UPDATE SET "
                    "status='running',ended_at=NULL,error_code=NULL",
                    (account_id, uidvalidity, source_uid, marker, utc_now()),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError("MARKER_COLLISION") from exc

    def mark_uncertain(self, account_id: str, uidvalidity: int, source_uid: int, error_code: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE transfer_attempts SET status='uncertain',ended_at=?,error_code=? "
                "WHERE account_id=? AND uidvalidity=? AND source_uid=?",
                (utc_now(), error_code, account_id, uidvalidity, source_uid),
            )

    def mark_failed(self, account_id: str, uidvalidity: int, source_uid: int, error_code: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE transfer_attempts SET status='failed',ended_at=?,error_code=? "
                "WHERE account_id=? AND uidvalidity=? AND source_uid=?",
                (utc_now(), error_code, account_id, uidvalidity, source_uid),
            )

    def expire_old_attempts(self, account_id: str, uidvalidity: int, cutoff: datetime) -> set[int]:
        """Expire uncertain writes without reading old source or alpha messages."""
        cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
        with self.db:
            rows = self.db.execute(
                "SELECT source_uid FROM transfer_attempts WHERE account_id=? AND uidvalidity=? "
                "AND status IN ('running','uncertain') AND started_at<?",
                (account_id, uidvalidity, cutoff_text),
            ).fetchall()
            self.db.execute(
                "UPDATE transfer_attempts SET status='failed',ended_at=?,error_code='AGE_WINDOW_EXPIRED' "
                "WHERE account_id=? AND uidvalidity=? AND status IN ('running','uncertain') AND started_at<?",
                (utc_now(), account_id, uidvalidity, cutoff_text),
            )
        return {int(row[0]) for row in rows}

    def expired_uids(self, account_id: str, uidvalidity: int) -> set[int]:
        rows = self.db.execute(
            "SELECT source_uid FROM transfer_attempts WHERE account_id=? AND uidvalidity=? "
            "AND status='failed' AND error_code='AGE_WINDOW_EXPIRED'",
            (account_id, uidvalidity),
        )
        return {int(row[0]) for row in rows}

    def confirm(self, account_id: str, uidvalidity: int, source_uid: int, marker: str,
                destination_uidvalidity: int, destination_uid: int) -> None:
        if destination_uidvalidity < 1 or destination_uid < 1:
            raise StateError("INVALID_DESTINATION_UID")
        existing = self.copy_record(account_id, uidvalidity, source_uid)
        if existing:
            if (existing["marker"], existing["destination_uidvalidity"], existing["destination_uid"]) != (
                marker, destination_uidvalidity, destination_uid
            ):
                raise StateError("COPY_EVIDENCE_CHANGED")
            return
        attempt = self.attempt(account_id, uidvalidity, source_uid)
        if not attempt or attempt["marker"] != marker:
            raise StateError("MISSING_ATTEMPT")
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO copy_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, uidvalidity, source_uid, marker, destination_uidvalidity,
                     destination_uid, utc_now()),
                )
                self.db.execute(
                    "UPDATE transfer_attempts SET status='confirmed',ended_at=?,error_code=NULL "
                    "WHERE account_id=? AND uidvalidity=? AND source_uid=?",
                    (utc_now(), account_id, uidvalidity, source_uid),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError("COPY_COLLISION") from exc

    def start_cycle(self) -> int:
        with self.db:
            cursor = self.db.execute("INSERT INTO cycles(started_at,status) VALUES (?, 'running')", (utc_now(),))
            return int(cursor.lastrowid)

    def record_account(self, cycle_id: int, account_id: str, status: str, copied: int, skipped: int,
                       failed: int, error_code: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO cycle_accounts VALUES (?,?,?,?,?,?,?)",
                (cycle_id, account_id, status, copied, skipped, failed, error_code),
            )

    def finish_cycle(self, cycle_id: int, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE cycles SET ended_at=?,status=? WHERE cycle_id=?", (utc_now(), status, cycle_id))

    def latest_cycle(self) -> dict | None:
        row = self.db.execute("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        result = dict(row)
        result["accounts"] = [dict(item) for item in self.db.execute(
            "SELECT * FROM cycle_accounts WHERE cycle_id=? ORDER BY account_id", (row["cycle_id"],)
        )]
        return result
