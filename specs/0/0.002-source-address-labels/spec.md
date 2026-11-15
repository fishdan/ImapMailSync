# Feature Specification: Source Address Labels in Alpha

**Feature Branch**: `feat/0.002-source-address-labels`

**Created**: 2026-11-15

**Status**: Draft

**Input**: Label every message copied into alpha after this feature is enabled with the source email address minus its final `.ext` suffix. For example, mail copied from `person@yahoo.com` receives the Gmail label `person@yahoo`.

## User Scenarios & Testing

### User Story 1 - Recognize the Source in Alpha (Priority: P1)

As the operator, I want each future source copy in alpha's Inbox to carry a label identifying the source account, so I can filter and recognize its origin in Gmail.

**Independent Test**: Configure alpha and a source whose account address is `person@yahoo.com`, deliver one eligible source Inbox message, run a cycle, and verify that the single copy remains in alpha's Inbox with the `person@yahoo` label.

**Acceptance Scenarios**:

1. **Given** a new eligible message in the `person@yahoo.com` source Inbox, **when** its copy is completed in alpha, **then** that copy has the `person@yahoo` user label and remains in alpha's Inbox.
2. **Given** the label already exists in alpha, **when** another message from that source is copied, **then** the existing label is applied; another label with a different spelling is not created.
3. **Given** a message arrives directly in alpha or was copied before this feature was enabled, **when** a sync cycle runs, **then** this feature does not add a source label to that message.

---

### User Story 2 - Recover an Interrupted Label Operation (Priority: P2)

As the operator, I want a destination copy to be labeled even if the service stops between copying and labeling, without receiving a second copy.

**Independent Test**: Interrupt a disposable transfer after alpha accepts the copy but before its label is confirmed. Restart the service and verify one alpha copy with the expected label and a visible recovery result.

**Acceptance Scenarios**:

1. **Given** alpha accepted a copy but label application failed or its acknowledgement was lost, **when** the service resumes, **then** it finds that exact copy and adds or confirms the source label without APPENDing a duplicate.
2. **Given** label creation or application is temporarily unavailable, **when** the cycle ends, **then** the account reports an incomplete label operation with the affected source UID; it does not report the message as fully synchronized.
3. **Given** a labeled copy is encountered on a repeat cycle, **when** its state is checked, **then** no extra copy or duplicate label is created and any other labels remain intact.

---

### User Story 3 - Keep Labels Correct Across Sources (Priority: P3)

As the operator, I want each configured source address to produce its own label while preserving the existing one-way copy rules.

**Independent Test**: Deliver one eligible message to each of two disposable source accounts with different addresses, run a cycle, and verify each destination copy has only its corresponding source label while both stay in alpha's Inbox.

**Acceptance Scenarios**:

1. **Given** two sources with different configured addresses, **when** each has an eligible message copied, **then** each copy receives the label derived from its own source address.
2. **Given** one source has an invalid label address or alpha rejects its label operation, **when** a cycle runs, **then** the problem is reported for that source and healthy sources still complete.
3. **Given** a source username changes, **when** the existing identity protection blocks that source, **then** the service does not silently relabel its earlier copies or invent a new baseline.

### Edge Cases

- Label derivation uses the configured source account address, never the message's `From`, `To`, or `Message-ID` headers.
- Remove the final dot suffix in the domain: `person@yahoo.com` becomes `person@yahoo`; `person@sub.example.org` becomes `person@sub.example`. For a multi-part public suffix, this literal rule removes only the final segment (`person@example.co.uk` becomes `person@example.co`).
- Two different configured addresses can intentionally produce the same label under this rule (`person@yahoo.com` and `person@yahoo.net` both become `person@yahoo`). Their messages remain distinct in copy history even though the displayed source label is shared.
- A configured source username without one `@`, a nonempty local part, a domain, and a final dot suffix cannot produce a label. The source reports a configuration error before copying an unlabeled new message.
- A destination label may already exist, or the destination may reject creating or applying it. These conditions must not remove the copy, remove another label, or change read state.
- A copy may be accepted just before an outage. The service must distinguish a pending label from an unsent message and must not create a second copy during recovery.
- A pending label may remain unresolved when the source copy's 24-hour eligibility window closes. Recovery must not backfill an untransferred source message.

## Requirements

### Functional Requirements

- **FR-001**: For each source account, the service MUST derive one destination label from that source's configured email address by removing the final `.` and following extension from the domain. It MUST use the same derived label for every future copy from that account.
- **FR-002**: Every message copied from a source Inbox into alpha after this feature is enabled MUST receive its source's label on that specific alpha message. The copy MUST remain in alpha's Inbox.
- **FR-003**: The service MUST reuse an existing matching user label or create it if absent. It MUST NOT delete, rename, or remove any existing label.
- **FR-004**: A transfer MUST NOT be reported as fully successful until the correct source label is confirmed on the destination copy. A failed label operation MUST identify the source account and source UID in sanitized output.
- **FR-005**: If copying succeeds but labeling is uncertain or fails, the service MUST retain evidence of the existing destination copy and retry labeling it without creating another copy. Repeating a successful cycle MUST not duplicate a destination message or alter its other labels.
- **FR-006**: The source UIDNEXT baseline, rolling 24-hour source eligibility, Inbox-only scope, five-minute schedule, and one-way nondeleting behavior from feature 0.001 MUST remain in force. This feature MUST NOT label historical alpha mail or copy older source mail.
- **FR-007**: The service MUST validate that a source address can yield an unambiguous label before copying new mail from that source. A source-specific failure MUST NOT stop other sources.
- **FR-008**: If alpha is known not to support user labels before a copy begins, the service MUST skip new copies and report the capability failure. A label failure discovered after a copy was accepted MUST leave that copy pending and MUST NOT be treated as complete.
- **FR-009**: Source credentials and message content MUST NOT be disclosed in label-operation output or logs. The derived label is destination metadata, not a new message header or a modification to the source message.
- **FR-010**: Recovery of an already accepted destination copy MAY inspect that exact recorded copy after 24 hours if needed to finish its label, but MUST NOT scan or import unrelated old mail in either account. This is a proposed exception to feature 0.001's alpha inspection window, pending operator confirmation.

### Key Entities

- **Source label**: The destination user label derived from one configured source address, for example `person@yahoo`.
- **Labeled copy**: A source occurrence whose one destination copy has been confirmed in alpha's Inbox with its source label.
- **Pending label**: A destination copy whose existence is known or uncertain but whose source label is not yet confirmed.

## Success Criteria

### Measurable Outcomes

- **SC-001**: In a controlled run of 100 eligible messages from one source, 100 destination copies remain in alpha's Inbox and each has exactly the expected source label; no source message is deleted or marked read by the sync.
- **SC-002**: In a controlled run with at least two source addresses, every copied message is associated with the label derived from its own source and no unrelated destination message receives one.
- **SC-003**: In interruption and lost-acknowledgement tests at both the copy and label steps, each source occurrence produces at most one destination copy and reaches a confirmed labeled state or a visible pending/error state.
- **SC-004**: On three repeat cycles after successful labeling, destination copy counts and existing non-source labels remain unchanged.
- **SC-005**: For any failed label operation, the cycle reports the affected account and source UID without a credential value, message body, or attachment content.

## Assumptions and Boundaries

- “Mail moved into alpha” means the existing nondeleting copy into alpha's Inbox. The source message stays in place.
- “Future mail” means messages copied after this feature is enabled. Existing alpha messages, including copies made by feature 0.001 before that point, are outside this feature's backfill scope.
- The configured source `username` is the source email address used to derive the label. A separate address field for providers with non-email login names is outside this draft's scope.
- The `.ext` rule is literal: remove only the final dot segment, retain the rest of the address, and preserve its configured letter case. Multi-part suffix handling can be revised before implementation if the operator prefers a different display name.
- Alpha is the configured Gmail destination. Google's [Gmail IMAP extensions](https://developers.google.com/workspace/gmail/imap/imap-extensions) document adding labels to an existing message and creating labels through IMAP; protocol selection and fault tests belong in the later implementation plan.
- The exact policy for retrying a pending label after the destination copy becomes older than 24 hours is awaiting operator confirmation. FR-010 states the proposed recovery behavior so the remaining decision is visible.
