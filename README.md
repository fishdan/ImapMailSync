# MailSync

MailSync copies messages that arrive in source IMAP Inboxes **after each source is activated and within the preceding 24 hours** into alpha's Inbox. It uses IMAP arrival time, not the message's `Date` header. It leaves source messages, deletions, moves, and later flag changes alone. Each destination copy retains the original message and adds an `X-Mailsync-Key` header to make retries safe. New copies receive a Gmail label derived from the configured source address. A systemd user timer can run a cycle every five minutes.

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

Fill in each file's provider, username, app password, and provider-specific IMAP host and port. Each source `username` must be an email address; its final domain suffix is removed to form the destination label. For example, `person@yahoo.com` becomes `person@yahoo`. The rule removes only the final segment, so `person@example.co.uk` becomes `person@example.co`. The JSON must use `"security": "SSL/TLS"` and `"authenticationRequired": true`. The Yahoo and Google configurations may also contain SMTP settings, which MailSync ignores. Do not put real credentials in the example file or any tracked file.

Keep each `.secrets` directory mode `700` and each `account.json` mode `600`. Both `.secrets/` and `.state/` are Git-ignored. MailSync validates those permissions and uses certificate-verifying TLS. It never puts passwords in command arguments or output.

## Run locally

From the repository root:

```bash
PYTHONPATH=src python3 -m mailsync doctor
PYTHONPATH=src python3 -m mailsync init-source yahoo
PYTHONPATH=src python3 -m mailsync run-once --json
PYTHONPATH=src python3 -m mailsync status --json
```

`doctor` checks authentication, Inbox access, source-address shape, and alpha's Gmail `X-GM-EXT-1` label capability without creating a label or copying. Run `init-source` **once per new source**, after reviewing its current Inbox: it records the current UIDNEXT and excludes all existing mail. If a baseline already exists, do not reset it or initialize that source again. Add another source by creating its private JSON and running `init-source <id>` for that account. `run-once` copies only source UIDs at or above their baseline whose exact IMAP arrival time is within 24 hours, using `BODY.PEEK[]` so source mail is not marked read. It checks age again immediately before APPEND. Before copying, it creates or reuses the source's Gmail label. It confirms the label by reading that exact destination UID. A repeated run skips confirmed source UIDs, including copies later deleted from alpha. An unaccepted message that ages out is skipped and not backfilled. Source identity or UIDVALIDITY changes block that source until reviewed.

The earlier 24-hour Yahoo-to-alpha transfer established the same age limit now required for unattended production. A message delayed by an outage beyond 24 hours is not transferred when service resumes. A copy already accepted in alpha can have its pending label repaired after 24 hours by its saved destination UID or exact marker, even if the source is unavailable. That repair never APPENDs the old source message again. Copies made before this label feature have no label job and are not backfilled.

Use `--root /path/to/checkout` before the subcommand when running outside the repository root. CLI output contains account IDs, counts, status, sanitized errors, and source UIDs for message-level issues; it omits addresses, label names, credentials, and message content. Each `run-once` account result includes `labeled` and `label_pending`; `status` shows `pending_labels` and `missing_labels` beside each source baseline. A pending label is not reported as a fully successful account run. `journalctl --user -u mailsync.service` retains per-run issue UIDs when the timer runs.

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

SQLite state lives in `.state/mailsync.sqlite3` with numbered migrations in `src/mailsync/migrations/`. Migration 002 adds an empty label-job table; it preserves existing baselines and copy records and does not label old mail. Back up state with SQLite's backup API before upgrading, keeping the destination file private together with the account configuration. Copying only the database file while the service is writing can miss WAL transactions. A missing or corrupt state file does not silently backfill: sources without a baseline fail until explicitly initialized. Never reset a baseline to fix an outage; inspect `status`, restore the state backup if needed, and use disposable accounts to rehearse recovery.

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

The offline suites and Python compilation run on each PR through [Python checks](.github/workflows/python-checks.yml); they do not use account credentials. The [source-label quickstart](specs/0/0.002-source-address-labels/quickstart.md) describes the migration and live acceptance steps. The localhost Gmail-extension integration tests cover label creation/reuse, the 24-hour cutoff, source outages, APPEND/STORE acknowledgement loss, three repeat cycles, and 100 messages. Earlier GreenMail scripts under `experiments/direct_imap/` cover the standard IMAP copy engine and require the pinned local test image and OpenSSL; GreenMail is not used to validate Gmail labels. Live label and arrival-time acceptance still require a naturally arriving post-feature message. No live mailbox deletion is part of normal operation or validation.
