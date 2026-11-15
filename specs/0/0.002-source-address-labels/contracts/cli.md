# CLI Contract: Source Address Labels

The existing commands and `--json` option remain. This contract extends [feature 0.001](../../0.001-imap-mail-sync/contracts/cli.md); `PYTHONPATH=src python3 -m mailsync <command>` remains the invocation.

## Configuration and `doctor`

For each source, `username` must be an email address that yields a source label under the final-domain-suffix rule. `doctor` reports `INVALID_LABEL_ADDRESS` for an invalid source address and `LABEL_UNSUPPORTED` if alpha does not advertise `X-GM-EXT-1`. It checks existing connectivity and Inbox access, but does not create a label, alter a message, or initialize a baseline. It never prints the address or derived label.

## `run-once`

The command preserves source baseline, exact 24-hour `INTERNALDATE` copy eligibility, account isolation, lock, and existing exit codes. Before a new APPEND, it checks label capability and ensures the user label exists. On a confirmed new copy, it attempts and verifies labeling. Pending label jobs are retried on each run by saved UID or exact marker, even after 24 hours and even if that source cannot currently be read. Such repair cannot initiate a new APPEND after the source cutoff.

Each `accounts[]` item retains `account_id`, `status`, `copied`, `skipped`, `failed`, optional `error_code`, and optional `issue_uids`. It adds:

| Field | Meaning |
|-------|---------|
| `labeled` | Jobs whose correct label was confirmed during this run, including repairs. |
| `label_pending` | Jobs still awaiting a confirmed label at the end of this run for this account. |

`copied` counts newly confirmed destination copies, including one awaiting a label. `status=success` requires no pending label job or new-copy failure for that account. A label failure or uncertainty sets a non-success status and includes the affected source UID in `issue_uids`. A missing copy remains visible through `status` and is never silently recopied. `overall_status` and exit codes keep their 0/1/2/3 meanings from feature 0.001.

Sanitized error codes include `INVALID_LABEL_ADDRESS`, `LABEL_UNSUPPORTED`, `LABEL_CREATE_FAILED`, `LABEL_UNCONFIRMED`, `LABEL_COPY_MISSING`, and `MULTIPLE_MARKER_MATCHES`. The code never includes an IMAP server response, address, label text, credential, header, or body.

## `status`

The command remains local and read-only. It adds per-source `pending_labels` and `missing_labels` counts to the JSON baseline summary and default text output. Its latest cycle retains existing fields. It does not list label names or message contents. A `pending_labels` count greater than zero is actionable even when the latest copy cycle had no new messages.

## `init-source`

The initial UIDNEXT baseline behavior is unchanged. Upgrading does not rerun this command or reset a source's baseline.
