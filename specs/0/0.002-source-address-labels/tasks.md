# Tasks: Source Address Labels in Alpha

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [CLI contract](contracts/cli.md), and [quickstart.md](quickstart.md).

**Testing order**: Write focused tests before the corresponding implementation. The operator explicitly requested testing of the chosen sync mechanism in feature 0.001; this feature also requires interruption and after-24-hour evidence. Keep real provider tests read only until disposable protocol and fault gates pass.

## Phase 1: Protocol and state foundation

- [x] **T001** Create a disposable Gmail-extension IMAP fixture under `tests/` or `experiments/direct_imap/` supporting `X-GM-EXT-1`, LIST/CREATE, `UID FETCH X-GM-LABELS`, additive `UID STORE`, marker search, and fault injection. Keep its addresses synthetic and document how CI runs it.
- [x] **T002** Write failing label derivation and safe IMAP encoding tests in `tests/test_labels.py`: final suffix only, case, two addresses mapping to one label, invalid shape, controls, slash, quoting, and non-ASCII behavior.
- [x] **T003** Write failing migration and job transition tests in `tests/test_state.py`: version-1 upgrade without baseline or ledger loss, empty jobs for legacy copies, job-before-APPEND durability, UID evidence, terminal states, and pending-job queries.
- [x] **T004** Implement source-label derivation in `src/mailsync/labels.py` and migration `src/mailsync/migrations/002_label_jobs.sql` plus transaction-safe job methods in `src/mailsync/state.py`; pass T002–T003 tests.

## Phase 2: User Story 1 — Label each future copy (P1)

**Goal**: A fresh copied message remains in alpha Inbox with the correct source label.

**Independent test**: One synthetic `person@yahoo.com` source message yields one alpha Inbox copy carrying `person@yahoo`; existing destination labels remain.

- [x] **T005 [US1]** Write failing protocol tests in `tests/test_imap_client.py` using T001: capability gate, read-write destination/read-only source selection, label LIST/CREATE/reuse, additive `UID STORE`, exact UID label FETCH, and safe parsing/quoting.
- [x] **T006 [US1]** Implement Gmail label operations and exact marker/UID verification in `src/mailsync/imap_client.py`, using `src/mailsync/labels.py` for encoding; pass T005. Do not issue delete, expunge, move, or flag STORE.
- [x] **T007 [US1]** Write failing cycle tests in `tests/test_cycle.py` for job creation before APPEND, one new copy and confirmed label, existing label reuse, prior feature-0.001 copy untouched, invalid source address isolation, and unsupported alpha capability preventing new APPEND.
- [x] **T008 [US1]** Integrate derivation, capability, job creation, APPEND reconciliation, label application, and confirmation in `src/mailsync/cycle.py`; pass T007 and preserve exact 24-hour source cutoff.

## Phase 3: User Story 2 — Repair interrupted labels (P2)

**Goal**: A previously accepted copy can get its label on a later cycle without another APPEND, including after 24 hours.

**Independent test**: Fault an accepted APPEND or STORE acknowledgement, advance the clock, restart, and find exactly one labeled alpha copy.

- [x] **T009 [US2]** Write failing interruption tests in `tests/test_cycle.py` and the T001 fixture: crash after accepted APPEND, lost APPEND acknowledgement, crash after copy-ledger commit, lost STORE acknowledgement, and temporary connection failure. Assert that state retains the same marker and one copy.
- [x] **T010 [US2]** Write failing after-24-hour tests in `tests/test_cycle.py`: targeted saved-UID/marker repair succeeds after source age-out and during source outage; unaccepted old mail never APPENDs; verified account-wide missing copy is reported without recreation; an Inbox-only miss remains pending/inaccessible; multiple marker matches block safely.
- [x] **T011 [US2]** Implement pending-job processing and exact target verification in `src/mailsync/cycle.py`, `src/mailsync/state.py`, and `src/mailsync/imap_client.py`. Use targeted marker checks before declaring account-wide absence; an Inbox-only miss stays pending. Reconcile jobs before attempt expiration, and keep transient failures pending. Pass T009–T010.

## Phase 4: User Story 3 — Multi-source correctness and operations (P3)

**Goal**: Multiple sources get their intended labels, and failures remain visible without affecting healthy sources.

**Independent test**: Two synthetic source addresses produce correctly labeled Inbox copies; one source label failure leaves its UID visible while the other succeeds.

- [x] **T012 [US3]** Write failing multi-source and CLI tests in `tests/test_cycle.py` and `tests/test_cli.py`: source-specific derivation, healthy-source isolation, pending/missing counts, `issue_uids`, sanitized errors, and `doctor` capability/address checks with no writes.
- [x] **T013 [US3]** Implement `doctor`, `run-once`, `status`, and text/JSON report changes in `src/mailsync/cli.py` and `src/mailsync/cycle.py`; pass T012. Keep addresses, label text, raw IMAP responses, credentials, and message content out of output.

## Phase 5: Validation and rollout

- [ ] **T014** Run the full offline unit suite and compilation, then disposable standard IMAP smoke, two-source outage, accepted-copy and label-ack fault cases, three-repeat idempotence, and 100-message throughput. Record commands, counts, and remaining limits in `.config/ai/progress.ai`.
- [ ] **T015** Update `README.md` with the label rule, Gmail capability requirement, private-state migration, pending-label recovery, run/status fields, and upgrade behavior. Align feature docs and progress log; verify no public artifact contains a real address, label, credential, or private path.
- [ ] **T016** Check the packaged wheel includes migration 002; run CI-equivalent tests, `git diff --check`, Git-ignore checks, and staged secret-value scan. Record validation and reviewable handoff in `.config/ai/progress.ai` and `.config/ai/handoff.ai`.
- [ ] **T017** After disposable gates pass, back up ignored production state, inspect timer status, verify migration preserves source baselines, run read-only alpha capability preflight, and observe a naturally arriving future message through the active five-minute timer. Record whether the live label and ten-minute delivery criteria were actually observed; do not claim them from disposable tests alone.

## Dependencies and execution order

T001–T004 form the foundation. T005/T007 tests precede T006/T008 implementation. T009–T010 precede T011. T012 precedes T013. T014–T017 follow implementation in order. The existing five-minute timer and production state are not modified by planning; rollout happens only after the disposable gates and migration review. No task permits a historical alpha backfill, baseline reset, deletion, or older-source APPEND.
