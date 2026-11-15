# Data Model: One-Way IMAP Mail Sync

**State location**: `.state/mailsync.sqlite3`, with versioned schema in `src/mailsync/migrations/`. Runtime state is not committed.

## Account configuration

Loaded from private `accounts/<account-id>/.secrets/account.json`. A tracked `accounts/example/account.example.json` shows the required shape with placeholders. The JSON supplied for Yahoo includes SMTP details; this feature reads only its IMAP section.

| Field | Rule |
|-------|------|
| `account_id` | Unique directory name; lowercase letters, digits, `_`, and `-`; alpha is the sole destination. |
| `role` | Derived from account ID: `alpha` is the sole destination; other configured account IDs are sources. |
| `provider` | Descriptive name; not used to select transfer behavior. |
| `username`, `password` | Required non-placeholder strings; password is read from the private JSON and never logged or stored in SQLite. |
| `imap.host`, `imap.port` | Required IMAP connection settings; validated before use. |
| `imap.security`, `imap.authenticationRequired` | Must be `SSL/TLS` and `true` for this feature. |
| `smtp` | Optional retained provider settings; ignored by the sync service. |
| `mailbox` | Always `INBOX` for this feature. |

The account identity is a stable fingerprint of IMAP host, port, username, role, and mailbox. A configuration change that would point a source or alpha at a different mailbox must not silently reuse old state.

## Source baseline

One row per source account.

| Field | Rule |
|-------|------|
| `account_id` | Primary key referencing a configured source. |
| `account_identity` | Must match current nonsecret connection identity. |
| `destination_identity` | Must match current alpha identity. |
| `uidvalidity` | Positive mailbox generation value returned by the source. |
| `first_eligible_uid` | Source UIDNEXT at activation; earlier UIDs are out of scope. |
| `created_at` | UTC activation time. |
| `status` | `active` or `blocked`; a mismatch or corrupt state blocks transfers. |

Activation is read-only and records the baseline atomically. It never resets a prior baseline without an explicit recovery operation documented separately.

## Transfer attempt

A durable record of a selected source occurrence before APPEND starts.

| Field | Rule |
|-------|------|
| `attempt_id` | Unique identifier, scoped to one source occurrence. |
| `account_id` | Source being processed. |
| `started_at`, `ended_at` | UTC timestamps; end may be absent after a crash. |
| `status` | `running`, `confirmed`, `failed`, or `uncertain`; an aged-out unconfirmed attempt is `failed` with `AGE_WINDOW_EXPIRED`. |
| `uidvalidity`, `source_uid` | Exact source occurrence, at or above the baseline and absent from confirmed copy records. |
| `marker` | Stable digest of account identity, source UIDVALIDITY, and source UID; inserted as `X-Mailsync-Key` in the destination copy. |
| `error_code` | Sanitized code, if failed or uncertain. |

After an interrupted attempt, the source occurrence remains unconfirmed until the next run searches alpha for the exact marker, provided its source `INTERNALDATE` remains within 24 hours. An attempt row alone never proves a successful copy. A marker with multiple destination matches blocks the source for review. When an unconfirmed source occurrence ages out, its attempt is marked failed and it is not retried.

## Copy record

One row per confirmed source message occurrence.

| Field | Rule |
|-------|------|
| `account_id` | Source account. |
| `uidvalidity` | Source mailbox generation. |
| `source_uid` | Source message UID. |
| `confirmed_at` | UTC time at which destination presence was confirmed. |
| `marker` | Stable internal header value used for destination reconciliation. |
| `destination_uid` | UID of the destination copy found by marker search; scoped to alpha's UIDVALIDITY. |
| `destination_uidvalidity` | Alpha Inbox mailbox generation at confirmation. |

The primary key is (`account_id`, `uidvalidity`, `source_uid`); `Message-ID` is never the identity key. Confirmed rows are not removed merely because a destination message is later deleted, so a user's deletion in alpha does not cause re-import.

## Sync cycle result

One result per scheduled or manual cycle, with one child result per configured source. The result stores start and end times, copied/skipped/failed counts, status, and sanitized error codes. It may be retained in SQLite or emitted as structured local output; the implementation task fixes the exact representation before coding. It contains no password, raw message, body, or attachment.

## State transitions and invariants

1. `unconfigured` → `active`: account settings and credentials validate; source UIDVALIDITY and UIDNEXT are captured without importing existing mail.
2. `active` → `attempted`: one eligible, unconfirmed UID and its deterministic marker are recorded before APPEND.
3. `attempted` → `confirmed`: exactly one destination match for that marker is verified, then a copy record is committed.
4. `attempted` → `uncertain`: transfer stops without reliable confirmation; no copy record is committed. The next run reconciles before attempting another copy.
5. `active` → `blocked`: source UIDVALIDITY, account identity, destination identity, or state integrity changes unexpectedly. No source mail is copied while blocked.
6. An eligible source occurrence whose exact IMAP arrival time is older than 24 hours when considered is skipped. An existing unconfirmed attempt transitions to `failed` with `AGE_WINDOW_EXPIRED`; no APPEND occurs.

All schema changes are made by numbered migrations. State writes use transactions, and the state directory is private to the operator.
