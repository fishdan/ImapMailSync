# CLI Contract: One-Way IMAP Mail Sync

All commands run from the repository root as `PYTHONPATH=src python3 -m mailsync <command>`. The transfer engine uses Python's standard-library IMAP client.

## Configuration contract

Each account has a private `accounts/<account-id>/.secrets/account.json` with `provider`, `username`, `password`, and an `imap` object containing `host`, `port`, `security = "SSL/TLS"`, and `authenticationRequired = true`. An optional `smtp` object may be present but is ignored by this IMAP-only service. The account ID `alpha` is the sole destination; every other configured account is a source. The JSON file has owner-only read permissions. Placeholder credentials, duplicate IDs, missing files, invalid connection settings, and paths escaping `.secrets/` are errors. Passwords remain in process memory and are never placed in process arguments or logs.

## Commands

### `doctor`

Validates account files and permissions, TLS connectivity, authentication, and Inbox access. It performs no message copy or baseline initialization. Reports one result per account without secret values.

### `init-source <account-id>`

Captures the source Inbox's UIDVALIDITY and UIDNEXT as the first-activation baseline. Existing messages remain excluded. It fails if the source already has a baseline, if alpha or the source is invalid, or if mailbox identity is unavailable. It never resets history implicitly.

### `run-once`

Runs one complete cycle over initialized source accounts. It checks identity and state, selects only source Inbox messages at or above the activation baseline whose IMAP arrival time is no more than 24 hours old, fetches them without marking read, and rechecks exact age before APPEND. It searches only recent alpha Inbox messages for a stable reconciliation marker before retrying and confirms a copy only after finding exactly one destination match. Expired unconfirmed messages are counted as skipped; an existing attempt records `AGE_WINDOW_EXPIRED`. An uninitialized source is reported as a configuration failure; healthy initialized sources still run. A concurrent invocation exits without starting a second transfer. The command never deletes mail.

### `status`

Reports the latest cycle and each source's baseline/blocked state. It does not contact providers or print message content. Per-message UID issues from a prior run remain in that run's JSON output and service journal.

## Output and exit codes

Human-readable output is the default; a `--json` option emits one machine-readable result with `cycle_started_at`, `cycle_ended_at`, `overall_status`, and `accounts[]`. Each account result has `account_id`, `status`, `copied`, `skipped`, `failed`, and optional sanitized `error_code` and `issue_uids` (source UIDs for messages skipped or failed in that run). Secret values, full raw messages, bodies, and attachments are excluded.

| Exit code | Meaning |
|-----------|---------|
| `0` | Requested operation succeeded; all configured accounts succeeded or had no new mail. |
| `1` | A cycle completed with one or more account or message failures; healthy accounts were still processed. |
| `2` | Global configuration or executable validation failed before any transfer. |
| `3` | A source is blocked because its mailbox identity or stored state is unsafe; other healthy sources may still have been processed. |

The exact error text may evolve, but exit codes and JSON field names are stable within this feature.
