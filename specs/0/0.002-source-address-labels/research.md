# Research: Gmail Labels Through IMAP

## Decision

Use Gmail's IMAP extension on the existing alpha connection. Do not introduce the Gmail REST API or a new credential flow. Google's [IMAP extension reference](https://developers.google.com/workspace/gmail/imap/imap-extensions) states that `X-GM-EXT-1` advertises the extension, user labels are listed/created as IMAP mailboxes, `FETCH X-GM-LABELS` reads a message's labels, and `STORE +X-GM-LABELS` adds one. The extension works through standard IMAP authentication. A read-only production check on alpha during specification found `X-GM-EXT-1`; that is capability evidence only, not a completed write test.

## Protocol choices

| Question | Decision and reason |
|----------|---------------------|
| How is a label added? | `UID STORE <destination UID> +X-GM-LABELS (<quoted label>)`, followed by `UID FETCH <UID> (UID X-GM-LABELS)` to verify. Additive STORE preserves existing labels; the client never issues replacement `X-GM-LABELS`, `COPY`, `MOVE`, or flag STORE. |
| How is a label created or reused? | Check the exact user-label name with `LIST`; issue `CREATE` only if absent. If another client creates it concurrently, re-list and accept the exact existing name. Never rename or delete a label. |
| Which mailbox mode? | Source remains selected read only. Alpha's Inbox must be selected read-write for STORE. Keep the copy itself in Inbox. |
| How is a lost acknowledgement handled? | Read the exact destination UID's labels before any retry. If the label is already present, confirm the job; otherwise repeat the additive STORE and verify. |
| How is a lost APPEND acknowledgement handled? | Reconcile by the existing `X-Mailsync-Key` marker. For a pending job, an exact marker-only search can run after 24 hours; a new APPEND still requires a currently eligible source message. |
| What if the saved UID is unsafe? | Check destination UIDVALIDITY and the marker on the fetched message. On mismatch, use the exact marker search; never apply a label to the stale UID. |

## Encoding and safety gate

The [Gmail reference](https://developers.google.com/workspace/gmail/imap/imap-extensions) describes label values as IMAP `ASTRING`s and notes modified UTF-7 encoding where appropriate. Implement a single protocol encoder/parser for label names; tests must cover the requested slash, spaces, quotes, backslashes, case preservation, and non-ASCII input or an explicit safe failure. Reject control characters and any value that cannot be encoded safely. Do not log source addresses, derived labels, IMAP command arguments, or raw server responses, because they can contain private data.

## Test gate

Existing GreenMail scenarios validate standard copying and outage behavior, but GreenMail support for `X-GM-LABELS` is not assumed. Add a disposable Gmail-compatible IMAP protocol fixture in the test suite, with controllable failures at CREATE, APPEND acknowledgement, STORE acknowledgement, FETCH, and connection loss. Verify the actual command shape and responses through `imaplib`, not only mocked high-level method calls. Exercise the 100-message case and repeated cycles against disposable mailboxes. Keep real alpha checks read only until this gate passes. No live message deletion or synthetic historical backfill is needed.

## Alternatives considered

- **Copy into a label folder**: rejected because it does not prove the exact existing Inbox copy was labeled and risks unintended message placement or duplication.
- **Gmail REST API**: unnecessary for this scope and would require a separate authorization path.
- **Label only during the copy cycle**: fails the operator-approved recovery rule when a copy is accepted just before an outage or after the 24-hour source window closes.
