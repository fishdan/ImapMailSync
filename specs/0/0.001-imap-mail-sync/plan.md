# Implementation Plan: One-Way IMAP Mail Sync

**Branch**: `main` | **Date**: 2026-11-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/0/0.001-imap-mail-sync/spec.md`; the imapsync trial resulted in a no-go decision. The operator subsequently required the exact 24-hour arrival cutoff on every production run and authorized activation after implementation.

## Summary

Build a small, local, one-way mail service that initializes a per-source Inbox baseline, runs a five-minute cycle, and copies later arrivals into alpha's Inbox. Python's standard-library IMAP client will keep verified TLS connections open, read each selected source message without changing it, and APPEND a copy carrying a stable, unique source-occurrence marker. A SQLite ledger records confirmed copies. The imapsync trial is complete and its no-go evidence is in `research.md` and `experiments/imapsync/probe.md`.

## Technical Context

**Language/Version**: Python 3.12.

**Primary Dependencies**: Python standard library only in production. Account JSON is read with `json`; SMTP fields in the Yahoo template are retained but unused. Pinned imapsync and the disposable GreenMail container remain test artifacts only.

**Storage**: Local SQLite state under `.state/mailsync.sqlite3` for source baselines, in-flight attempts, confirmed copy records, and cycle results. Versioned schema migrations live in `src/mailsync/migrations/` and ship with the package; runtime state is Git-ignored and backed up as operational data.

**Testing**: Python `unittest` for configuration, state, protocol, and cycles; disposable local IMAP mailboxes for destructive or fault tests; bounded live provider checks; the scenarios in `quickstart.md`.

**Target Platform**: Linux host with Python 3.12 and systemd user services. Perl was used only for the completed imapsync evaluation. Other platforms can run a single cycle but scheduled deployment is scoped to Linux.

**Project Type**: Command-line service invoked by a systemd timer.

**Performance Goals**: A normal cycle checks each source every five minutes; 99% of up to 100 new messages per healthy source appear in alpha within ten minutes, per SC-001.

**Constraints**: New source Inbox mail within the preceding 24 hours by IMAP `INTERNALDATE`; one destination Inbox; no reverse sync, source deletion, destination deletion, or later flag resync; no overlapping cycles; no credentials in arguments, logs, or Git; TLS certificate checks remain enabled. A copied message may contain one internal `X-Mailsync-Key` header while retaining every original header and MIME part.

**Scale/Scope**: One operator, one destination, multiple source accounts. No web UI, hosted service, or historical backfill in this feature.

## Constitution Check

*Gate: checked before research and again after design.*

| Principle | Plan response | Status |
|-----------|---------------|--------|
| Spec and task traceability | All implementation work maps to `spec.md`, `tasks.md`, and progress entries. | Pass |
| Tests and local/CI validation | Unit tests, controlled fault tests, and live provider acceptance scenarios are explicit tasks. Live tests use disposable mailboxes; unit tests run in CI. | Pass |
| Reproducibility | Standard-library production runtime, documented configuration and validation, numbered migrations. | Pass |
| Security and dependency review | No production third-party package; verified TLS and private account JSON. | Pass |
| Narrow scope and reviewability | A small CLI owns setup, transfer, scheduling, and reporting. | Pass |
| Version-controlled database changes | The initial SQLite schema and future changes use numbered, committed migrations. | Pass |

**Design re-check**: The imapsync no-go decision was recorded before production implementation. The direct IMAP design introduces no constitution exception.

## Phase 0: Research and Decision Gate — completed, no-go for imapsync

See [research.md](research.md) and `experiments/imapsync/probe.md` for evidence. The trial used live Yahoo and Gmail only for bounded, nondeleting checks; all deletion and fault tests used disposable local IMAP mailboxes. The decisive observations were:

1. The pinned 2.314 imapsync copied 28 recent Yahoo messages to Gmail with no deletion and matched raw content. Disposable fixtures passed baseline, attachment, duplicate-header, flag, and outage checks.
2. A lost APPEND acknowledgement left the destination copy present but no UID cache mapping; plain imapsync retry duplicated it.
3. A unique marker made reconciliation possible, but imapsync required one process per UID. Two live single-UID dry runs took about 11 seconds each, putting a serial 100-message cycle beyond the 10-minute delivery target.
4. Direct IMAP was selected to keep connections open while adding unique markers per message.

The no-go decision closes the imapsync adoption gate. Production code now follows the direct IMAP design below.

## Phase 1: Design

### Baseline and identity

The service reads each source Inbox without changing message flags, records its server mailbox identity (UIDVALIDITY), connection identity, and first eligible UID (UIDNEXT), then uses IMAP `UID SEARCH SINCE <calendar date>` to limit candidates. It enumerates actual returned UIDs, filters values below the baseline and confirmed UIDs, and checks each candidate's exact `INTERNALDATE` against `now - 24 hours`. The `Date` header is ignored for eligibility. A bare `UID <baseline>:*` search is prohibited because IMAP can include the current last message even when its UID is below the baseline. The service checks mailbox identity before each cycle and fails closed on a change. A newly added source gets its own baseline and ledger records, independent of existing sources.

### Transfer and uncertain-write reconciliation

The service opens certificate-verifying `IMAP4_SSL` connections to alpha and each source. It selects source and destination Inboxes read-only where possible, uses `BODY.PEEK[]` to fetch a selected source UID without marking it read, and rechecks exact source age immediately before APPEND. Only messages no more than 24 hours old are APPENDed to alpha's Inbox, with the source's initial read/unread flag and IMAP internal date. The copy retains all original headers and MIME bytes, plus a stable `X-Mailsync-Key` header derived from account identity, source UIDVALIDITY, and source UID. The key contains only a digest, not credentials or message content. Before APPEND, a transaction records an in-flight attempt and key. Destination marker search uses the same date-limited IMAP search and exact alpha `INTERNALDATE` check. After a successful APPEND, exactly one current-window destination match confirms the copy with UID evidence. If APPEND acknowledgement is lost, the next cycle searches for that marker before retrying while the source remains within the 24-hour window. When an unconfirmed source message ages out, it is skipped and its attempt is marked failed with `AGE_WINDOW_EXPIRED`; it is never APPENDed later. A confirmed source UID is never resent even if the alpha copy is later deleted. No source or destination delete, expunge, move, or later flag-update operation is issued.

### Scheduling and isolation

One `run-once` command handles all configured sources in a cycle and records a separate result for each. A process lock prevents overlapping cycles. A systemd user timer triggers the command every five minutes; an unfinished cycle is not started again. One source's failure does not stop others. Reuse the alpha connection across messages and reconnect on transient failures.

### State and observability

The SQLite state stores each source's baseline, mailbox identity, in-flight marker, and confirmed copy ledger. Updates are transactional. Losing or corrupting the state blocks that source and gives a recovery instruction rather than silently importing old mail. Cycle results contain timestamps, account identifiers, counts, and sanitized errors. Per-run output also lists source UIDs for message-level issues; it omits credentials and full message bodies.

## Project Structure

### Documentation (this feature)

```text
specs/0/0.001-imap-mail-sync/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/cli.md
├── tasks.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
accounts/
├── example/account.example.json
├── alpha/.secrets/account.json
└── yahoo/.secrets/account.json
src/mailsync/
├── __main__.py
├── cli.py
├── config.py
├── baseline.py
├── state.py
├── imap_client.py
├── cycle.py
└── migrations/001_initial.sql
deploy/systemd/
├── mailsync.service
└── mailsync.timer
tests/
├── test_config.py
├── test_baseline.py
├── test_state.py
├── test_imap_client.py
├── test_cycle.py
└── test_cli.py
experiments/imapsync/
└── probe.md
```

**Structure Decision**: A single Python package owns account setup and operations using only standard-library IMAP and SQLite APIs. Each account's private JSON under `accounts/<id>/.secrets/` contains its IMAP settings and credential; `.state/` contains local runtime data. SQLite provides transactional copy records with a versioned schema. The imapsync experiment remains permanent, sanitized project evidence.

## Risks and Decision Points

- A rolling date filter is not an exact first-activation baseline; the service intersects the source mailbox identity and UID boundary with the rolling window.
- IMAP `SINCE` has calendar-day precision and ignores timezone offsets. Searches start on the calendar day before the UTC cutoff so a message stored with a negative offset cannot be omitted; exact `INTERNALDATE` filtering still enforces the 24-hour window for source and alpha candidates. A source message is rechecked immediately before APPEND because it can age out during a cycle.
- An IMAP `UID n:*` range can include the mailbox's last message when `n` is greater than every assigned UID. Explicit eligible UID sets prevent old-mail import at an empty baseline boundary.
- An uncertain APPEND result can cause duplicate copies; the stable marker is searched before any retry. More than one marker match blocks that source.
- If a destination message is deleted after a confirmed copy, the durable ledger prevents re-import.
- Gmail and Yahoo authentication options vary by account. `doctor` reports provider authentication failures without exposing credentials.
