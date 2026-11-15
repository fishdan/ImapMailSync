# Tasks: One-Way IMAP Mail Sync

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [CLI contract](contracts/cli.md), and [quickstart.md](quickstart.md)

**Organization**: The imapsync proof of concept ended with a no-go decision. Production implementation uses direct Python IMAP. Story phases follow the priority order in the spec.

## Phase 1: Setup and Imapsync Decision Gate

**Goal**: Establish safe test conditions and decide whether imapsync can meet the specification. T007 recorded no-go; T008 revised the design before Phase 2.

- [x] T001 Add `accounts/**/.secrets/`, `.state/`, and local probe output patterns to `.gitignore`; verify `git check-ignore` on a representative passfile before creating credentials.
- [x] T002 Pin a public imapsync release or revision, document its installation and Perl dependency audit in `experiments/imapsync/probe.md`, and record the validated version in `research.md`.
- [x] T003 Validate owner-only Yahoo and Gmail account JSON and authentication without printing secrets; use disposable local IMAP accounts for destructive and fault tests because live accounts must not lose messages.
- [x] T004 Implement a read-only baseline and fixture-inspection helper in `experiments/imapsync/probe.py` that records UIDVALIDITY, UIDNEXT, message counts, and flags, enumerates actual UIDs, filters values below the baseline, and proposes per-message confirmation evidence; document fixtures in `experiments/imapsync/probe.md`.
- [x] T005 Run a bounded, nondeleting Yahoo-to-Gmail trial; validate content, certificate rejection, exact Inbox selection, and repeat behavior. Use disposable local IMAP mailboxes for baseline, attachment, equal/missing-`Message-ID`, destination deletion, and flag tests; record sanitized evidence in `experiments/imapsync/probe.md`.
- [x] T006 Exercise an interrupted destination APPEND and a one-source outage in disposable local mailboxes; record recovery, destination confirmation evidence, duplicate counts, and independent-source behavior in `experiments/imapsync/probe.md`.
- [x] T007 Evaluate the adoption criteria and record a no-go decision in `research.md` and `.config/ai/progress.ai`: plain retry duplicates after lost APPEND acknowledgement, while per-message marker processes miss the serial throughput target.
- [x] T008 Revise `plan.md`, `research.md`, `data-model.md`, `contracts/cli.md`, `quickstart.md`, and `tasks.md` for direct IMAP before production transfer implementation.

## Phase 2: Foundational Infrastructure

**Depends on**: The no-go decision at T007 and direct IMAP revision at T008.

- [x] T009 Create the Python 3.12 package skeleton in `pyproject.toml`, `src/mailsync/__init__.py`, and `src/mailsync/__main__.py`; document local test and run commands in `README.md`.
- [x] T010 Create the numbered SQLite schema in `src/mailsync/migrations/001_initial.sql` for source baselines, per-message transfer attempts with markers, confirmed copy records, and cycle results; implement versioned migration application in `src/mailsync/state.py`.
- [x] T011 Write `tests/test_config.py` for account ID/role validation, secret-path containment, account JSON permissions, and exactly one alpha; implement the configuration loader in `src/mailsync/config.py` using private `account.json`.
- [x] T012 Write `tests/test_state.py` for transactional baseline creation, unique copy key (`account_id`, `uidvalidity`, `source_uid`), migration versioning, and corrupt-state fail-closed behavior; complete `src/mailsync/state.py`.

## Phase 3: User Story 1 — Receive New Mail in Alpha (P1, MVP)

**Goal**: One source can be initialized and its later Inbox arrivals copied to alpha without changing source mail.

**Independent Test**: Initialize yahoo with a pre-existing message, deliver a new multipart message with an attachment, run one cycle, and verify only the new message appears once in alpha with readable content and unchanged source flags.

- [x] T013 [US1] Write failing tests in `tests/test_baseline.py` for read-only UIDVALIDITY/UIDNEXT capture, old-mail exclusion, arrival during a cycle, and the RFC 9051 `UID n:*` last-message edge case.
- [x] T014 [US1] Implement `init-source` baseline capture and identity checks in `src/mailsync/baseline.py` and expose the command in `src/mailsync/cli.py`.
- [x] T015 [US1] Write failing protocol-boundary tests in `tests/test_imap_client.py` for verified TLS, read-only source selection, explicit UID enumeration, `BODY.PEEK[]`, INBOX-only APPEND, original-content preservation, stable unique markers, and absence of deletion or later flag writes.
- [x] T016 [US1] Implement persistent source/destination IMAP connections and per-message APPEND with marker search in `src/mailsync/imap_client.py`.
- [x] T017 [US1] Implement one-source `run-once` orchestration in `src/mailsync/cycle.py` and `src/mailsync/cli.py`, recording only confirmed copies in `src/mailsync/state.py`.
- [x] T018 [US1] Execute the User Story 1 independent test from `quickstart.md` against disposable accounts and record sanitized results in `.config/ai/progress.ai`.

## Phase 4: User Story 2 — Run Repeatedly Without Duplicates (P2)

**Goal**: Repeated and interrupted cycles preserve one confirmed copy per source occurrence, tolerate account failures, and run on a five-minute timer.

**Independent Test**: Repeat a successful cycle three times, interrupt a transfer, restart, disconnect one source while another remains healthy, and observe two timer activations without duplicate mail or overlap.

- [x] T019 [US2] Write failing tests in `tests/test_cycle.py` for repeat-run ledger filtering, uncertain-attempt reconciliation, no re-import after alpha deletion, and independent account failures.
- [x] T020 [US2] Implement durable transfer-attempt and confirmed-copy state transitions in `src/mailsync/state.py`; filter already confirmed source UIDs in `src/mailsync/cycle.py`.
- [x] T021 [US2] Implement uncertain-write marker reconciliation, mailbox-identity fail-closed behavior, and per-source retry isolation in `src/mailsync/cycle.py` and `src/mailsync/imap_client.py`.
- [x] T022 [US2] Add a nonoverlapping process lock in `src/mailsync/cycle.py` and a five-minute user timer in `deploy/systemd/mailsync.service` and `deploy/systemd/mailsync.timer`.
- [x] T023 [US2] Run the User Story 2 interruption, restart, outage, deletion, and timer scenarios from `quickstart.md`; record sanitized outcomes in `.config/ai/progress.ai`.

## Phase 5: User Story 3 — Add and Operate Accounts Safely (P3)

**Goal**: The operator can add sources independently, validate configuration, and see per-account results without exposing secrets or content.

**Independent Test**: Add delta, run `doctor` and a cycle with yahoo unavailable, inspect `status` and JSON output, and scan the repository and logs for secret or message-body leakage.

- [x] T024 [US3] Write failing tests in `tests/test_config.py` and `tests/test_cycle.py` for adding a source without resetting another source, invalid configuration isolation, safe counts, and secret-free output.
- [x] T025 [US3] Implement `doctor` and per-account configuration validation in `src/mailsync/cli.py` and `src/mailsync/config.py`.
- [x] T026 [US3] Implement per-account cycle reports, `status`, JSON output, and documented exit codes in `src/mailsync/cycle.py`, `src/mailsync/state.py`, and `src/mailsync/cli.py`.
- [x] T027 [US3] Run the User Story 3 multi-source and secret-leak checks from `quickstart.md`; record sanitized outcomes in `.config/ai/progress.ai`.

## Phase 6: Final Validation and Documentation

- [x] T028 Finalize `accounts/example/account.example.json`, `README.md`, and `quickstart.md` with setup, recovery, backup, and systemd instructions that match the validated behavior.
- [x] T029 Run `python3 -m unittest discover -s tests -v`, all controlled live scenarios, `git diff --check`, `git check-ignore` for secret paths, and a dependency vulnerability review; record commands, findings, and fixes in `.config/ai/progress.ai`.
- [x] T030 Review `spec.md` acceptance scenarios and SC-001 through SC-006 against recorded results, update `checklists/requirements.md` if needed, and record completion or remaining gaps in `.config/ai/progress.ai` and `.config/ai/handoff.ai`.

## Phase 7: Permanent 24-Hour Policy and Authorized Activation

- [x] T031 Update the direct IMAP source selection and alpha marker search to use a rolling, exact 24-hour `INTERNALDATE` cutoff on every cycle; recheck age immediately before APPEND, skip and report expired messages, and add boundary, retry, and disposable integration tests. Update the plan, model, CLI contract, quickstart, README, and progress log to match the operator's permanent policy.
- [x] T032 After T031 passes, establish Yahoo's production UIDNEXT baseline, install and enable the five-minute systemd user timer as authorized, and verify its first two activations, source preservation, alpha copy counts, and absence of deletion. Record the live result and any provider limitations without exposing account details.

## Phase 8: PR Readiness

- [x] T033 Review the complete proposed PR file set for secrets, reproducibility, and spec coverage; package the numbered migration with the advertised CLI; include source UIDs in per-run message-error reports; update `README.md` so clone setup, tests, activation, and recovery match the implemented service; add a PR check for offline tests; run the documented checks and record any remaining acceptance gaps in the progress log and handoff.

## Dependencies and Delivery

- T001 precedes credential creation in T003. T002–T006 produced the evidence for T007's no-go; T008 replaced the imapsync implementation tasks.
- Phase 2 blocks all user stories. Complete each story's failing tests before its implementation tasks, then run its independent test.
- The MVP is Phase 1 gate, Phase 2, and User Story 1. User Stories 2 and 3 complete unattended, multi-account operation.
- Tasks share the same small state and CLI modules, so no task is marked `[P]`; parallel execution is not assumed.
