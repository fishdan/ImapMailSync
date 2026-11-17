# Implementation Plan: Source Address Labels in Alpha

**Branch**: `feat/0.002-source-address-labels` | **Date**: 2026-11-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature 0.002 specification and the operator's decision to repair the label on an accepted copy even after 24 hours.

## Summary

Add a source-derived Gmail label to each new copy in alpha's Inbox. Keep the existing one-way, five-minute, 24-hour source-copy policy. Use Gmail's IMAP label extension to add and verify the label on the exact destination UID. A versioned SQLite label job, recorded before APPEND, makes accepted copies recoverable after a crash or lost acknowledgement. Existing copies have no label job and are outside this feature.

## Technical Context

**Language/Version**: Python 3.12.

**Primary Dependencies**: Python standard library; `imaplib` and `sqlite3`. No new production package.

**Storage**: Existing private `.state/mailsync.sqlite3`, with packaged migration `002_label_jobs.sql`. Existing source baselines and copy records remain intact.

**Testing**: Python `unittest` in local and CI runs, a disposable Gmail-extension IMAP test endpoint, existing disposable IMAP smoke/fault scenarios, and a bounded read-only capability check against alpha before activation.

**Target Platform**: Linux, existing systemd user timer every five minutes.

**Project Type**: Local command-line service.

**Performance Goals**: Preserve the existing ten-minute delivery target for up to 100 fresh messages per healthy source; label repair runs each cycle without delaying eligible copies indefinitely.

**Constraints**: Source Inbox read only; destination Inbox remains the only copy target; exact rolling 24-hour `INTERNALDATE` cutoff before any new APPEND; no delete, expunge, move, or flag update. Only a pending label job may target an older alpha copy, by saved UID or exact marker. Private addresses, labels, credentials, and message content are absent from logs and CLI output.

**Scale/Scope**: One Gmail destination, multiple configured IMAP sources, no historical backfill.

## Constitution Check

| Principle | Design response | Status |
|-----------|-----------------|--------|
| Spec and task traceability | `spec.md`, this plan, `tasks.md`, and progress log cover each change. | Pass |
| Testing first and CI | Fault, interruption, migration, and protocol tests are explicit tasks before implementation; unit tests run in CI. | Pass |
| Version-controlled schema | Sequential `002_label_jobs.sql` migration; no manual production schema edits. | Pass |
| Reproducibility and dependencies | Standard-library runtime and disposable protocol fixture documented in `quickstart.md`. | Pass |
| Security | Existing verified TLS and ignored private state/config; sanitized output; no real-mail destructive tests. | Pass |
| Narrow scope | Source labels only; existing scheduling, baseline, and one-way semantics preserved. | Pass |

**Design re-check**: The label job is an additive schema change. It needs no constitution exception.

## Research and Protocol Decision

See [research.md](research.md). Gmail's [IMAP extensions](https://developers.google.com/workspace/gmail/imap/imap-extensions) document `X-GM-EXT-1`, label creation through `CREATE`, and per-message label reads/writes via `X-GM-LABELS`. Alpha previously advertised `X-GM-EXT-1` in a read-only check. The implementation must still prove quoting, label reuse, `UID STORE +X-GM-LABELS`, and `UID FETCH X-GM-LABELS` against a disposable protocol endpoint before any live label write. GreenMail remains useful for generic copy tests but is not assumed to implement Gmail's extension.

## Design

### Derivation and capability gate

Derive the label from the configured source `username`: require one `@`, nonempty local/domain/final suffix, and remove only the last domain dot segment. Preserve case, including when two addresses produce the same label. Validate each source independently before starting its new copies. Validate and safely quote the label as IMAP protocol data; malformed or unsafe names fail closed. Before a new APPEND, alpha must advertise `X-GM-EXT-1` and the desired user label must be known or created. A capability or creation failure prevents new copies from that source. `doctor` checks capability and address shape without writing a label.

### Copy and label protocol

Open source Inbox read only and alpha Inbox read-write. The latter is required for `UID STORE +X-GM-LABELS`; the service issues no flag, delete, or expunge command. Keep existing source selection and exact age checks. Before APPEND, commit a label job keyed by the source occurrence and its stable marker. Search alpha for that marker using the current 24-hour window before a new APPEND. If no copy exists and the source remains eligible, APPEND once. An `APPENDUID` whose UIDVALIDITY matches the selected Inbox is stored immediately and is not validated by a search or fetch. A reply with no usable id is reconciled by the 24-hour marker search. Commit its UID/UIDVALIDITY to the existing copy ledger and label job. Fetch `X-GM-LABELS` for that exact UID; if missing, add only the requested label, fetch again, and mark the job confirmed only when observed. The copy remains in Inbox and other labels remain untouched.

### Recovery and the 24-hour boundary

Process pending jobs on each run, including when a source is unavailable. If a job has destination UID evidence, verify current UIDVALIDITY, UID, and marker before writing a label. If UID evidence is stale or absent after a lost APPEND acknowledgement, search Inbox by that job's exact marker without a date bound; reject multiple matches. Any inspection of older alpha mail must be confined to that job's UID or exact marker. A temporary search or fetch failure leaves the job pending. Absence from Inbox alone does not prove account-wide deletion: keep the job pending/inaccessible unless targeted marker checks across provider mailboxes prove the copy absent from alpha. Only then mark it missing and report the source UID; never recreate it. If an unconfirmed APPEND has no destination match and the source is now older than 24 hours, record `AGE_WINDOW_EXPIRED` and never APPEND it. The existing attempt expiration must skip pending label jobs until this reconciliation runs. Recovery may make progress without refetching old source mail.

### Upgrade boundary and consistency

Migration 002 creates an empty job table, so pre-feature confirmed copies are legacy and never queued for labeling. New jobs are committed before every post-feature APPEND. An attempt from the old version that already has an alpha marker match is treated as a pre-feature copy; a still-eligible old attempt with no match gets a label job before any new APPEND. No baseline reset occurs. A confirmed copy is still excluded from future APPENDs even if its label is pending or its destination copy was deleted. A lost label STORE acknowledgement is handled by fetching current labels before retrying the additive STORE.

### Reporting

Per-account run output retains `copied`, `skipped`, `failed`, and `issue_uids`, and adds `labeled` and `label_pending` counts. `copied` remains a count of newly confirmed destination copies, not a claim that their labels succeeded. An account cannot report success while its label job is pending. `status` shows pending/missing label counts by source from private state, without addresses, label text, or message content. Specific sanitized codes distinguish unsupported capability, invalid address, unconfirmed label, and missing copy. See [contracts/cli.md](contracts/cli.md).

## Project Structure

```text
specs/0/0.002-source-address-labels/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/cli.md
├── tasks.md
└── checklists/requirements.md
src/mailsync/
├── config.py
├── imap_client.py
├── cycle.py
├── state.py
├── cli.py
└── migrations/002_label_jobs.sql
tests/
├── test_labels.py
├── test_imap_client.py
├── test_state.py
├── test_cycle.py
└── test_cli.py
experiments/direct_imap/
└── disposable label/fault fixtures
```

**Structure decision**: Extend the existing single Python package and tests. No new service or scheduler component is needed.
