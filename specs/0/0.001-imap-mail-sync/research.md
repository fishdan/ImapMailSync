# Research: One-Way IMAP Mail Sync

**Date**: 2026-11-15

## Decision 1: Transfer engine candidate

**Decision**: Do not adopt imapsync as the production transfer engine. Implement direct IMAP in Python with a durable per-message marker and copy ledger.

**Rationale**: The [official overview](https://imapsync.lamiral.info/) describes incremental, repeatable IMAP-to-IMAP transfers. The [public source repository](https://github.com/imapsync/imapsync) and [license](https://github.com/imapsync/imapsync/blob/master/LICENSE) permit a self-hosted evaluation without buying the author's packaged download or support. The [official site](https://imapsync.lamiral.info/) also offers paid packaged distribution and support; those are optional for this project.

**Alternatives considered**: Implement read and APPEND operations directly with a Python IMAP client, or use another mailbox synchronization tool. Direct IMAP remains the fallback if imapsync cannot satisfy first-activation baselines, distinct source-message identity, destination deletion behavior, or uncertain-write recovery.

**Status**: No-go recorded after live provider, disposable fixture, and fault tests. The evaluation artifacts remain under `experiments/imapsync/`.

**Pinned candidate**: Tag `imapsync-2.314`, commit `93654c6025ff7814f983ab74dd300f9bed9282d9`, locally verified with `./imapsync --version` and `perl -c imapsync`. See `experiments/imapsync/probe.md` for dependency and security review and the no-go evidence.

## Decision 2: Message selection and duplicate identity

**Decision**: The service owns the first-activation baseline and a durable, source-scoped copy ledger. It enumerates actual source UIDs and fetches each new message with `BODY.PEEK[]`. A rolling age filter is insufficient for the production baseline.

**Rationale**: The [message-selection FAQ](https://imapsync.lamiral.info/FAQ.d/FAQ.Messages_Selection.txt) says imapsync selects all messages by default and documents `--search1` for source-only selection and `--useuid` for UID matching. The [duplicate FAQ](https://imapsync.lamiral.info/FAQ.d/FAQ.Duplicates.txt) says default matching uses `Message-Id` and `Received`, which can collapse distinct source occurrences. The [cache FAQ](https://imapsync.lamiral.info/FAQ.d/FAQ.Use_cache.txt) says UID mappings are stored under the chosen `--tmpdir`; that directory must persist across runs.

**Alternatives considered**: `--maxage` or date search alone. Those are based on message dates and cannot precisely define first arrival after activation, especially for delayed or backdated mail.

**Fault result**: A disposable server accepted an APPEND while a proxy dropped its tagged OK. imapsync had no UID cache mapping and a plain retry duplicated the message. A unique header marker allowed read-only destination reconciliation, but imapsync required a separate process for each uniquely marked source UID. Direct IMAP can retain connections and mark each message independently. Confirmed copy records prevent re-import after alpha deletion.

**Protocol edge case**: [RFC 9051](https://www.rfc-editor.org/rfc/rfc9051.html) states that a UID range ending in `*` always includes the last message's UID, even if the range's starting UID is higher than any assigned UID. Therefore the wrapper must enumerate actual UIDs, filter them by the baseline and copy ledger, and pass explicit bounded UID sets; `UID <baseline>:*` alone is unsafe at first activation.

## Decision 3: Folder and flag behavior

**Decision**: Select only each source's Inbox and APPEND only to alpha's Inbox. Direct IMAP performs no deletion, expunge, move, or later flag synchronization. It copies the source's initial read/unread flag at creation time.

**Rationale**: The [folder-selection FAQ](https://imapsync.lamiral.info/FAQ.d/FAQ.Folders_Selection.txt) says imapsync otherwise synchronizes all folders and documents `--folder`. The [imapsync options](https://github.com/imapsync/imapsync/blob/master/imapsync) say `--resyncflags` is on by default and `--noresyncflags` disables later flag updates. `--delete1` and `--delete2` are separate destructive options and are outside this feature.

**Alternatives considered**: Default folder and flag behavior. It conflicts with the spec's Inbox-only and no-later-flag-sync requirements.

## Decision 4: Authentication and secret handling

**Decision**: Read provider credentials from private `account.json` files under each account's `.secrets/`. Direct Python IMAP uses those credentials in memory and never puts them in process arguments, environment variables, logs, or SQLite. If an account cannot issue an app password, investigate provider-supported OAuth before changing the transfer design.

**Rationale**: [Yahoo's current help](https://my.help.yahoo.com/kb/new-mail-for-desktop/access-yahoo-mail-third-party-apps-sln15241.html) documents third-party app passwords and notes account eligibility can block their creation. [Google's help](https://support.google.com/mail/answer/185833?hl=en) says app passwords require two-step verification and are unavailable in some account configurations. The [imapsync security guidance](https://github.com/imapsync/imapsync/blob/master/imapsync) recommends `--passfile1` and `--passfile2` over command-line passwords because process listings can reveal arguments. The [Gmail IMAP help](https://support.google.com/mail/answer/7126229/check-gmail-through-other-email-platforms?hl=en) says personal Gmail accounts have IMAP enabled by default; older imapsync instructions to toggle it are stale.

**Alternatives considered**: Put passwords in command arguments, environment variables, or a tracked config file. Those can expose credentials through process listings, diagnostic output, or Git history, so none is the default.

**TLS finding**: In pinned imapsync 2.314, `SSL_VERIFY_POLICY` is `SSL_VERIFY_NONE` by default. The trial forced peer verification and proved that invalid certificates were rejected. The production Python client will use `ssl.create_default_context()` with `imaplib.IMAP4_SSL` on both sides, keeping hostname and certificate verification enabled.

## Decision 5: Runtime and state

**Decision**: Use Python 3.12 standard library for configuration, IMAP reading and APPEND, the CLI, and tests; SQLite for transactional local copy records; and a systemd user timer on Linux for five-minute scheduling.

**Rationale**: Python 3.12 and systemd 255 are present in the development environment. Python's `imaplib`, `ssl`, `sqlite3`, and `unittest` cover the service without a production third-party dependency. Numbered schema migrations satisfy the repository constitution's database rule.

**Alternatives considered**: A permanently running daemon and flat-file copy records. The timer gives simple restart behavior; SQLite provides transactional updates for copy confirmation.

## Proof-of-concept result

**Status**: **No-go**. A bounded live Yahoo-to-Gmail trial copied 28 recent messages without source changes or deletions; all 28 raw copies matched and an immediate repeat copied zero. Disposable local fixtures passed old-mail exclusion, attachments, equal/missing `Message-ID`, destination flag stability, confirmed-UID filtering after a destination deletion, and one-source outage. Invalid TLS certificates were rejected. A lost APPEND acknowledgement caused an unmarked imapsync retry to create a duplicate. Unique-marker reconciliation worked, but requires one imapsync process per message; two live single-message dry runs took 11.2 and 11.05 seconds each. Serial processing of 100 messages would take roughly 18 minutes, beyond the 10-minute delivery target. A parallel process design lacks evidence for safe provider connection and cache behavior. The direct IMAP design retains connections, adds a per-source-occurrence marker, and reconciles uncertain APPENDs by destination marker search before retrying. See `experiments/imapsync/probe.md` for the sanitized trial matrix.
