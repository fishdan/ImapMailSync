# Quickstart: Implement and Validate Source Labels

This is the implementation runbook for feature 0.002. All account JSON and SQLite state remain private and Git-ignored. Do not put real addresses, passwords, or derived labels in test fixtures or command output.

## Development order

1. Add the failing tests in [tasks.md](tasks.md) first: address derivation, Gmail command encoding/response parsing, migration upgrade, pending-job recovery, cutover, and CLI reporting.
2. Implement `002_label_jobs.sql` and the smallest changes to `state.py`, `imap_client.py`, `cycle.py`, and `cli.py` needed to pass those tests.
3. Run the offline suite from repository root:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
   python3 -m compileall -q src tests
   git diff --check
   ```

4. Run disposable IMAP scenarios with a Gmail-extension fixture. Seed fresh source messages and a destination message with an unrelated label. Verify `CREATE` or reuse, additive `UID STORE`, exact `UID FETCH`, Inbox placement, no source flag change, no delete, and no duplicate after three repeat cycles. Fault the APPEND and STORE acknowledgements separately. Advance the test clock beyond 24 hours and verify repair of an accepted copy, while an uncopied old source message is never APPENDed.
5. Run the existing disposable copy, outage, and 100-message scenarios to check that baseline and throughput behavior still hold. Record actual commands and results in `.config/ai/progress.ai`.

## Upgrade and production acceptance

1. Back up the ignored SQLite state using the repository's existing recovery procedure. Confirm the active timer is not running a cycle during upgrade. Apply migration 002 through the application startup path; check schema version 2 and unchanged baseline/copy-record counts. Existing copies should have zero label jobs.
2. Run `PYTHONPATH=src python3 -m mailsync doctor --json` and verify alpha reports label capability and configured sources pass address validation. This step reads providers but creates no labels or messages.
3. Let the already configured five-minute timer run with the new code. Verify a naturally arriving, post-feature source message is copied once into alpha Inbox and the expected source label appears on that exact copy. Check that a repeat reports zero new copies and that no earlier alpha copy acquired a label. Do not use a real-mail deletion or historical backfill test.
4. Check `PYTHONPATH=src python3 -m mailsync status --json` and the user service journal for `label_pending`/`pending_labels`. If label work remains pending, leave the state intact and allow the next cycle to retry the same copy. Never reset the UIDNEXT baseline to fix a label issue.

## Acceptance matrix

| Scenario | Expected result |
|----------|-----------------|
| Fresh eligible source message | One alpha Inbox copy; correct source label observed; source unchanged. |
| Existing matching label | Reused; no second label variant or duplicate copy. |
| Two source addresses | Each new copy has its own derived label; unrelated alpha mail untouched. |
| Lost APPEND acknowledgement | Marker finds the accepted copy; no second APPEND; label eventually confirmed. |
| Lost STORE acknowledgement | Exact UID FETCH sees label or additive STORE is retried; other labels remain. |
| Pending accepted copy after 24 hours | Label repair targets that copy and succeeds or reports pending/missing. |
| No accepted copy after 24 hours | No new APPEND; attempt expires visibly. |
| Alpha capability absent | No new APPEND; sanitized capability failure. |
| Invalid source address | That source fails before copy; other sources continue. |
| Prior feature-0.001 copy | No label job or label added. |

The live provider check is an acceptance observation, not a reason to change or delete existing messages. All fault injection occurs in disposable accounts.
