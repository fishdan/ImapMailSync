# Quickstart and Validation: One-Way IMAP Mail Sync

This is the validation guide for the direct IMAP implementation. The imapsync evaluation and no-go decision are recorded in `experiments/imapsync/probe.md`.

## Prerequisites

- Linux with Python 3.12 and systemd user services.
- Disposable IMAP mailboxes for fault and deletion tests. Live Yahoo and Gmail accounts are used only for bounded, nondeleting checks.
- Provider-supported credentials stored only in `accounts/<account-id>/.secrets/account.json` with owner-only file permissions. See [CLI contract](contracts/cli.md).
- A private `.state/` directory for SQLite history. Back it up with the account configuration; loss of state requires an explicit recovery decision.

## Completed imapsync evaluation

The pinned imapsync build copied 28 recent live Yahoo messages into Gmail without deletions. Disposable GreenMail tests covered the baseline, content fixtures, flags, destination deletion, lost APPEND acknowledgement, and a source outage. A plain retry after lost acknowledgement duplicated a message. Unique-marker recovery required one imapsync process per UID, and single-UID live dry runs each took about 11 seconds. The decision is no-go for the production transfer engine. See `experiments/imapsync/probe.md` and `research.md` for exact evidence.

## Application setup and smoke test

Run the following against disposable account configuration, or a newly added source whose baseline has not been established:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m mailsync doctor
PYTHONPATH=src python3 -m mailsync init-source yahoo
PYTHONPATH=src python3 -m mailsync run-once --json
PYTHONPATH=src python3 -m mailsync status
```

Expect `doctor` to report valid access without copying. `init-source` is a one-time baseline operation and excludes existing Inbox messages; do not rerun it for an active source. After a new test message arrives in a disposable source, `run-once` reports one copy and alpha contains one readable message with the original headers and MIME parts plus an internal `X-Mailsync-Key` header. Messages more than 24 hours old by IMAP `INTERNALDATE` are skipped even if above the baseline, and no message can cross the cutoff between selection and APPEND. A second `run-once` reports no new copy. Repeat with delta and the failure/recovery cases below. Check that no password or full message body appears in command output or logs.

In disposable accounts, test a lost APPEND acknowledgement and restart. While the source remains within 24 hours, the next run must search recent alpha mail for the marker and confirm the existing copy before retrying. If an uncertain message ages past 24 hours, the next run must skip it without APPEND. Delete a confirmed disposable destination copy and verify it stays absent. Change a destination read flag and verify later runs leave it unchanged. Disconnect one source and verify another still copies. Do not run destructive tests against live Yahoo or alpha accounts.

The reproducible local test harness uses the official GreenMail container image pinned in `experiments/imapsync/probe.md`. Run a fresh container with users `source`, `delta`, and `destination`, mapping its IMAP port 3143 only to `127.0.0.1`. Pass that mapped host port to each script below; restart the disposable container between scenarios so each starts with empty mailboxes:

```bash
docker run -d --rm --name mailsync-disposable -p 127.0.0.1::3143 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=source:sourcepass,delta:deltapass,destination:destpass' \
  greenmail/standalone@sha256:309ba544ea482e5d4265d09921cdab07c8dad2cc4481d715c9b8ff5d3f0a149f
docker port mailsync-disposable 3143/tcp
PYTHONPATH=src python3 experiments/direct_imap/smoke.py --imap-port <mapped-port>
docker stop mailsync-disposable
```

Repeat the `docker run` and `docker stop` steps with a fresh container for `fault_smoke.py`, `outage_smoke.py`, and `throughput.py`, passing each the newly mapped port.

The scripts create a temporary trusted local TLS bridge, private throwaway account JSON, and temporary SQLite state. They print counts and booleans, not message content or credentials. `smoke.py` deletes only one disposable destination fixture to prove it stays absent. `fault_smoke.py` drops an APPEND acknowledgement after the disposable server accepts the message. `outage_smoke.py` stops one local source bridge while another source remains healthy.

## Scheduled run

Install the reviewed `deploy/systemd/mailsync.service` and `mailsync.timer` as user units, setting their working directory to this checkout and Python interpreter. Start the timer and inspect its status:

```bash
systemctl --user enable --now mailsync.timer
systemctl --user list-timers mailsync.timer
systemctl --user status mailsync.service
```

Observe at least two timer activations five minutes apart. Deliver a new disposable source message between them; confirm it appears once in alpha within ten minutes and that concurrent runs do not overlap. Disable the timer when the test is complete with `systemctl --user disable --now mailsync.timer`.
