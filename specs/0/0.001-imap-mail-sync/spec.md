# Feature Specification: One-Way IMAP Mail Sync

**Feature Branch**: `main` (specification created without a feature branch)

**Created**: 2026-11-15

**Status**: Implemented; live delivery timing observed for natural arrivals on 2026-11-16

**Input**: Copy new mail from source IMAP accounts such as yahoo, delta, and gamma into destination IMAP account alpha, preferably every five minutes. Give each account its own directory under `./accounts` with a Git-ignored `.secrets` directory.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Receive New Mail in Alpha (Priority: P1)

As the operator, I want new messages arriving in any configured source Inbox to appear in alpha's Inbox so I can read all incoming mail in one account.

**Why this priority**: This is the core value of the service.

**Independent Test**: Configure alpha and one source, start with an empty sync history, deliver a new message with an attachment to the source, run a sync, and inspect both accounts.

**Acceptance Scenarios**:

1. **Given** alpha and yahoo are configured and yahoo receives a new message after sync is enabled, **when** the next successful sync finishes, **then** one copy appears in alpha's Inbox and the source message remains in yahoo.
2. **Given** a new source message has headers, body parts, and attachments, **when** it is copied, **then** those parts remain readable and the original sender, recipients, subject, and sent date are retained.
3. **Given** yahoo and delta each receive a new message, **when** a sync finishes, **then** both messages appear in alpha without one source replacing or suppressing the other.

---

### User Story 2 - Run Repeatedly Without Duplicates (Priority: P2)

As the operator, I want the service to check for mail about every five minutes and resume safely after restarts or transient failures.

**Why this priority**: An unattended sync is useful only if repeated runs are reliable.

**Independent Test**: Copy messages, rerun sync, interrupt a copy, restart, and inspect alpha and the reported results.

**Acceptance Scenarios**:

1. **Given** a source message was copied successfully, **when** later sync cycles run, **then** no additional copy is created.
2. **Given** a run stops before a copy is confirmed, **when** the service resumes, **then** it checks whether the message already reached alpha and completes the transfer without creating a duplicate.
3. **Given** yahoo is temporarily unavailable while delta is healthy, **when** a sync runs, **then** delta's new messages are copied, yahoo's failure is reported, and yahoo is retried on a later run.

---

### User Story 3 - Add and Operate Accounts Safely (Priority: P3)

As the operator, I want a clear place for each account's settings and credentials, plus a way to see whether each sync succeeded.

**Why this priority**: The service must remain manageable as source accounts are added.

**Independent Test**: Add a second source account directory, verify that its credentials are excluded from Git, run one sync cycle, and review the account-level result without exposing message content or credentials.

**Acceptance Scenarios**:

1. **Given** a valid new source account directory is added, **when** sync runs, **then** the account is included without changing existing accounts' sync history.
2. **Given** an account has missing or invalid settings, **when** sync starts, **then** that account is reported as misconfigured and no message is altered because of that error.
3. **Given** a sync finishes, **when** the operator views its result, **then** they can see per-account success or failure and counts of copied and skipped messages without credentials or message bodies being disclosed.

### Edge Cases

- Two different source messages share the same `Message-ID`: both must be eligible for copying.
- A source message has no `Message-ID`: it must still be eligible for copying and duplicate prevention.
- A message arrives while a sync cycle is running: it is copied in that cycle or a later one, without being permanently skipped.
- A destination write succeeds but confirmation is lost: the next run reconciles the outcome before retrying.
- A source or destination disconnects mid-transfer: no partial message is treated as a completed copy.
- A source folder is empty, a message is deleted before it can be read, or a mailbox changes identity: the service reports the condition and continues safely.
- Two scheduled runs overlap: they must not process the same source concurrently.
- An account contains a very large message or the provider rejects it: that message's failure is reported and later messages remain eligible for sync.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST support one destination account, alpha, and one or more independently configured source accounts.
- **FR-002**: The service MUST read new messages from each source account's Inbox and copy them into alpha's Inbox. It MUST leave source messages in place and MUST NOT mark them read, delete them, or change their flags as a side effect of reading.
- **FR-003**: The service MUST consider a message eligible only when it first arrives in a source Inbox after that account's sync baseline is established and its IMAP `INTERNALDATE` is no more than 24 hours old at the moment of transfer. It MUST NOT import messages already present at first activation or copy a message after the 24-hour window closes. The message's `Date` header MUST NOT determine eligibility.
- **FR-004**: The service MUST preserve each copied message's original headers, body parts, and attachments, and retain its sent date and read/unread state where the destination supports those properties. It MAY add one internal `X-Mailsync-Key` header to the destination copy for reliable retry reconciliation.
- **FR-005**: The service MUST maintain durable per-source copy history so successful copies are not repeated across cycles or process restarts. Distinct source messages MUST remain distinct even when their `Message-ID` values are missing or equal.
- **FR-006**: After an uncertain destination write, the service MUST determine whether the message is already present in alpha before retrying; it MUST NOT record success until the destination copy is confirmed.
- **FR-007**: The service MUST provide an automatic sync cycle every five minutes while running and a way to invoke a single cycle on demand. A new cycle MUST NOT overlap an unfinished cycle.
- **FR-008**: The service MUST process source accounts independently. Failure of one source MUST NOT prevent healthy sources from syncing. Failed messages MUST remain eligible for later retry while they are within the 24-hour arrival window; once that window closes, the service MUST skip them and report the expiration without copying them.
- **FR-009**: The service MUST keep each account's configuration under `./accounts/<account-id>/`. Any configuration file containing credentials MUST be inside `./accounts/<account-id>/.secrets/`, and all such `.secrets` directories MUST be excluded from Git.
- **FR-010**: The service MUST validate required account settings and credentials before syncing that account and report actionable configuration errors without printing secret values.
- **FR-011**: Each cycle MUST report its start and end, per-account outcome, counts of copied, skipped, and failed messages, and enough error context to identify the affected account and message without logging credentials or full message content.
- **FR-012**: The service MUST not copy messages from alpha back to any source or synchronize deletions, moves, or later flag changes in either direction.
- **FR-013**: If a source mailbox changes identity or the stored copy history cannot be trusted, the service MUST stop copying from that source and report a recovery action rather than silently replaying or skipping mail.
- **FR-014**: Each cycle MUST limit source selection and alpha marker reconciliation to messages received in the preceding 24 hours, using IMAP arrival times for exact cutoff checks. It MUST recheck source age immediately before APPEND. It MUST NOT delete messages in either account. When alpha's append reply includes `APPENDUID` and that UIDVALIDITY matches the selected Inbox, the service MUST store that destination UID as confirmation of the write and MUST NOT search the mailbox to validate it. A reply with no usable id keeps the 24-hour marker search.

### Key Entities

- **Account**: A named source or destination mailbox with nonsecret connection settings and separately stored credentials.
- **Source message**: A particular message occurrence in one source Inbox; its identity is scoped to that account and mailbox, independently of its `Message-ID` header.
- **Copy record**: Durable evidence that a source message was confirmed in alpha, used to prevent repeat copies.
- **Sync cycle**: One attempt to inspect all configured sources and report the outcome for each.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a controlled test with healthy accounts and up to 100 new messages per source, at least 99% of new messages appear in alpha within 10 minutes of arrival; any remaining messages have an explicit failure report.
- **SC-002**: Across three repeated cycles and a restart after a successful copy, alpha contains exactly one copy for each test source message.
- **SC-003**: Across a test that interrupts transfer before confirmation, every message either appears once in alpha after recovery or has a reported failure; none is silently lost from the eligible queue.
- **SC-004**: In tests covering plain text, HTML, multipart mail, and attachments, 100% of successfully copied messages retain readable content, attachments, and original sender, recipient, subject, and sent-date fields.
- **SC-005**: With one source unavailable, all new messages from healthy test sources still reach alpha in the same or following cycle, while the unavailable source is visibly reported and retried.
- **SC-006**: A repository check finds zero committed account credentials, and sync output contains zero credential values or full message bodies.

## Assumptions

- This first release is for one operator, one destination, and any number of source accounts that grant the operator access.
- Source and destination providers allow the required IMAP read and write operations with credentials supplied by the operator.
- Only the Inbox of each source is monitored, and all copied mail lands in alpha's Inbox. The `./accounts` directories are local account configuration directories, not destination mail folders.
- Existing source mail is excluded at first activation; historical backfill can be specified separately.
- The 24-hour window used in the initial live transfer is also a permanent production rule. Production eligibility is the intersection of the per-source first-activation baseline and the rolling 24-hour IMAP arrival window. A long outage can cause untransferred mail to age out; the service reports that condition and does not backfill it.
- Copied messages are independent copies. Later source edits, deletions, or read-state changes are not mirrored.
- The service runs on a host that stays online; provider outages and network failures may delay delivery beyond the normal five-minute interval.
