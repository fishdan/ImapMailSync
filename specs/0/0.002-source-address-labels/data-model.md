# Data Model: Source Address Label Jobs

Migration: `src/mailsync/migrations/002_label_jobs.sql`, applied by the existing numbered migration runner. Schema version 1 remains valid input. Migration 2 creates an empty table; it neither changes `source_baselines` nor enqueues existing `copy_records`.

## `label_jobs`

| Field | Meaning |
|-------|---------|
| `account_id`, `uidvalidity`, `source_uid` | Composite primary key for one source Inbox occurrence; foreign key to `transfer_attempts`. |
| `marker` | Existing unique `X-Mailsync-Key` value, also unique in this table. |
| `label_name` | Derived source label, held only in Git-ignored owner-only state. Immutable for a job. |
| `destination_uidvalidity`, `destination_uid` | Nullable together until exactly one alpha Inbox copy is reconciled. Match `copy_records` once known. |
| `status` | `pending`, `confirmed`, `missing`, or `expired`. |
| `created_at`, `updated_at` | UTC timestamps for recovery and operator inspection. |
| `error_code` | Optional sanitized failure code; never a raw server response. |

Indexes support scanning `pending` jobs by account and counting pending/missing jobs. The composite key prevents a second job for the same occurrence. A transaction creates or preserves an attempt and its job before APPEND. Confirmation of destination UID updates `copy_records` and the job atomically; if the process dies between IMAP acceptance and SQLite confirmation, marker reconciliation restores evidence. A successful label FETCH changes only the job status to `confirmed` in SQLite.

## State transitions

```text
no job (legacy or not attempted)
    └─ before a post-feature APPEND → pending, UID unknown
pending, UID unknown
    ├─ APPENDUID for the current UIDVALIDITY → pending, UID known, no follow-up search
    ├─ one marker match → pending, UID known
    ├─ no marker match while source eligible → APPEND/reconcile
    └─ no marker match after source ages out → expired; no APPEND
pending, UID known
    ├─ label observed → confirmed
    ├─ transient failure → pending
    └─ copy verified absent → missing; no APPEND
confirmed, missing, expired → terminal; no automatic APPEND
```

`missing` means a previously confirmed copy has been verified absent from alpha, not merely from Inbox. A missing Inbox UID or marker can also mean that the operator archived or moved the message; without account-wide targeted evidence, the job stays `pending` with an inaccessible-copy error. `expired` means no alpha copy was confirmed and the source is outside the 24-hour copy window. A temporary network failure proves neither absence nor expiration; the job stays `pending`. Multiple marker matches block the source as inconsistent state and leave the job pending. All terminal failures remain visible in `status` until an explicit future repair procedure is specified.

## Upgrade boundary

Existing version-1 `copy_records` have no label job and remain historical. An old uncertain attempt whose marker is already in alpha is confirmed as a legacy copy, with no label job. If no alpha marker exists and a new APPEND is still allowed by the current source 24-hour rule, a job is created before that APPEND. The first migration does not reset UIDNEXT baselines, account identities, or prior cycle records.

## Invariants

1. A job never causes APPEND after source eligibility expires.
2. A confirmed `copy_record` excludes its occurrence from any future APPEND, regardless of label outcome.
3. A job's label and marker do not change after creation; account identity changes block the source as before.
4. A `confirmed` label job means the label was observed on the same alpha Inbox copy, not merely that STORE returned OK.
5. A destination UID is trusted only while alpha UIDVALIDITY and the message marker match saved evidence.
