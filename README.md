# MailSync

MailSync copies messages that arrive in source IMAP Inboxes **after each source is activated and within the preceding 24 hours** into alpha's Inbox. It uses IMAP arrival time, not the message's `Date` header. It leaves source messages, deletions, moves, and later flag changes alone. Each destination copy retains the original message and adds an `X-Mailsync-Key` header to make retries safe. A systemd user timer can run a cycle every five minutes.

The production service uses Python 3.12 standard-library IMAP and SQLite. The commands below run from a source checkout; a Python package installation also includes the numbered SQLite migrations. Linux and systemd are needed for the supplied five-minute timer. The imapsync evaluation is retained under `experiments/imapsync/`; its [decision record](specs/0/0.001-imap-mail-sync/research.md) explains why it is not the production transfer engine.

## Configure accounts

Each account has `accounts/<id>/.secrets/account.json` with the shape in [account.example.json](accounts/example/account.example.json). `alpha` is the destination; every other account directory with a private JSON is a source. From a fresh clone, create the private files before running `doctor`:

```bash
mkdir -p accounts/alpha/.secrets accounts/yahoo/.secrets
chmod 700 accounts/alpha/.secrets accounts/yahoo/.secrets
cp accounts/example/account.example.json accounts/alpha/.secrets/account.json
cp accounts/example/account.example.json accounts/yahoo/.secrets/account.json
chmod 600 accounts/alpha/.secrets/account.json accounts/yahoo/.secrets/account.json
```

Fill in each file's provider, username, app password, and provider-specific IMAP host and port. The JSON must use `"security": "SSL/TLS"` and `"authenticationRequired": true`. The Yahoo and Google configurations may also contain SMTP settings, which MailSync ignores. Do not put real credentials in the example file or any tracked file.

Keep each `.secrets` directory mode `700` and each `account.json` mode `600`. Both `.secrets/` and `.state/` are Git-ignored. MailSync validates those permissions and uses certificate-verifying TLS. It never puts passwords in command arguments or output.

## Run locally

From the repository root:

```bash
PYTHONPATH=src python3 -m mailsync doctor
PYTHONPATH=src python3 -m mailsync init-source yahoo
PYTHONPATH=src python3 -m mailsync run-once --json
PYTHONPATH=src python3 -m mailsync status --json
```

`doctor` checks authentication and Inbox access without copying. Run `init-source` **once per new source**, after reviewing its current Inbox: it records the current UIDNEXT and excludes all existing mail. If a baseline already exists, do not reset it or initialize that source again. Add another source by creating its private JSON and running `init-source <id>` for that account. `run-once` copies only source UIDs at or above their baseline whose exact IMAP arrival time is within 24 hours, using `BODY.PEEK[]` so source mail is not marked read. It checks age again immediately before APPEND. It confirms a destination copy by searching recent alpha Inbox messages for its unique marker before recording success. A repeated run skips confirmed source UIDs, including copies later deleted from alpha. Messages that age out before confirmation are skipped and not backfilled. Source identity or UIDVALIDITY changes block that source until reviewed.

The earlier 24-hour Yahoo-to-alpha transfer established the same age limit now required for unattended production. A message delayed by an outage beyond 24 hours is not transferred when service resumes.

Use `--root /path/to/checkout` before the subcommand when running outside the repository root. CLI output contains account IDs, counts, status, sanitized errors, and source UIDs for message-level issues; it omits credentials and message content. `status` shows the latest cycle and baselines; `journalctl --user -u mailsync.service` retains per-run issue UIDs when the timer runs.

## Schedule every five minutes

Review [mailsync.service](deploy/systemd/mailsync.service) and [mailsync.timer](deploy/systemd/mailsync.timer). The service assumes this checkout is at `%h/IdeaProjects/ImapMailSync`; edit `WorkingDirectory` if yours differs. Activate each intended source first, then install and enable the user units:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/mailsync.service deploy/systemd/mailsync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mailsync.timer
loginctl enable-linger "$USER"
systemctl --user list-timers mailsync.timer
journalctl --user -u mailsync.service -n 20 --no-pager
```

The timer does not overlap a still-running service invocation, and the application also holds a process lock for manual runs. User lingering keeps the timer running after logout. Check recent results with `PYTHONPATH=src python3 -m mailsync status --json` and `journalctl --user -u mailsync.service -n 20 --no-pager`. To stop the timer, run `systemctl --user disable --now mailsync.timer`; use `loginctl disable-linger "$USER"` only if no other user services need lingering.

## State, recovery, and tests

SQLite state lives in `.state/mailsync.sqlite3` with numbered migrations in `src/mailsync/migrations/`. Back it up with SQLite's backup API while keeping the destination file private, together with the account configuration. Copying only the database file while the service is writing can miss WAL transactions. A missing or corrupt state file does not silently backfill: sources without a baseline fail until explicitly initialized. Never reset a baseline to fix an outage; inspect `status`, restore the state backup if needed, and use disposable accounts to rehearse recovery.

For a consistent database backup, set a private backup directory and run:

```bash
mkdir -p ~/.local/share/mailsync-backups
chmod 700 ~/.local/share/mailsync-backups
python3 - <<'PY'
from pathlib import Path
import sqlite3

backup = Path.home() / ".local/share/mailsync-backups/mailsync.sqlite3"
with sqlite3.connect(".state/mailsync.sqlite3") as source:
    with sqlite3.connect(backup) as destination:
        source.backup(destination)
backup.chmod(0o600)
PY
```

To recover, stop the timer, restore a known-good database backup into `.state/` with mode `600`, run `status` and `doctor`, then resume the timer after reviewing source identity and UIDVALIDITY. Keep a private backup of each `account.json` alongside the database backup.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m unittest discover -s experiments/imapsync -p 'test_*.py' -v
```

The offline suites and Python compilation run on each PR through [Python checks](.github/workflows/python-checks.yml); they do not use account credentials. The [quickstart](specs/0/0.001-imap-mail-sync/quickstart.md) describes controlled mailbox and timer validation. Disposable direct IMAP integration scripts are under `experiments/direct_imap/`; they require the pinned local GreenMail test image and OpenSSL. These tests cover the 24-hour cutoff, attachments, lost APPEND acknowledgement, source outages, repeat runs, and a 100-message batch. The first two production timer activations succeeded; live arrival-to-alpha timing still needs a naturally arriving post-baseline message. No live mailbox deletion is part of normal operation or validation.
