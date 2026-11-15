# Imapsync Adoption Probe

**Status**: Adoption gate completed. imapsync is not selected as the production transfer engine.

## Candidate and installation

- Upstream: `https://github.com/imapsync/imapsync.git`
- Pinned tag: `imapsync-2.314`
- Commit: `93654c6025ff7814f983ab74dd300f9bed9282d9`
- Script SHA-256: `89faed96f7c389723ddd7e78a00421b90d5b85bf79407fcf1b11209f3d4d7416`
- Local executable: `experiments/imapsync/.local/upstream/imapsync` (ignored, not redistributed in this repository)
- Runtime: Perl 5.38.2. `./imapsync --version` returned `2.314`; `perl -c imapsync` returned `syntax OK`.
- Installation command: `git clone --depth 1 --branch imapsync-2.314 https://github.com/imapsync/imapsync.git experiments/imapsync/.local/upstream`, followed by Ubuntu package installation described below.

## Dependency and security review

The installed Ubuntu 24.04 packages are `libencode-imaputf7-perl` 1.05-2.1, `libfile-copy-recursive-perl` 0.45-4, `libfile-tail-perl` 1.3-8, `libio-tee-perl` 0.66-2, `libmail-imapclient-perl` 3.43-1, `libparse-recdescent-perl` 1.967015+dfsg-4, `libregexp-common-perl` 2017060201-3, `libsys-meminfo-perl` 0.99-2build4, and `libunicode-string-perl` 2.10-2build4. `libtest-mockobject-perl` and its two small dependencies were added for the upstream test attempt. Runtime modules already present on the host include `IO::Socket::SSL`, `Digest::HMAC_SHA1`, `Readonly`, and `Term::ReadKey`.

- Commands: `sudo apt-get update`; `sudo apt-get install --no-install-recommends -y` with the runtime packages above and then `libtest-mockobject-perl`; `apt list --upgradable` filtered to those packages; `./imapsync --version`; `perl -c imapsync`.
- Package source: Ubuntu `noble` and `noble/universe`; none of the newly installed imapsync packages had a pending upgrade in the refreshed package index.
- [NVD CVE-2023-34204](https://nvd.nist.gov/vuln/detail/CVE-2023-34204) affects imapsync versions through 2.229 due to predictable temporary paths. The pinned 2.314 release is outside that affected range. The planned stable cache will also reside in a private `.state/` directory.
- Pinned-source inspection found `SSL_VERIFY_POLICY => SSL_VERIFY_NONE`; TLS alone does not authenticate the server. The trial and production command must force `--ssl1 --ssl2 --sslargs1 SSL_verify_mode=1 --sslargs2 SSL_verify_mode=1`, and the trial must show a bad certificate is rejected.
- The upstream `--tests` command prints environment variables, which can include secrets, and later contacts external test servers. A first attempt exposed this behavior; a second attempt used a scrubbed environment and was stopped after the offline checks when it entered network login tests. Do not run `--tests` in a normal environment or capture its full output in project history. `--version` and syntax checks pass.
- `apt-get update` reported an unrelated HashiCorp repository signature warning. Ubuntu package lists refreshed and the imapsync packages came from Ubuntu repositories; no HashiCorp package was used.
- Review status: no known high or critical vulnerability found in the pinned imapsync release or the newly installed direct runtime packages during this targeted review. This is not a complete audit of every transitive Perl module; revisit before deployment.

## Provider setup

Yahoo's local JSON contains working credentials. A read-only Python IMAP4_SSL login with certificate verification succeeded; selecting `INBOX` read-only and searching its UIDs confirmed mailbox access. Alpha's local Gmail JSON also contains working credentials; its read-only, certificate-verified IMAP login and Inbox inspection succeeded. No baseline was created, no message body was fetched, and no message was changed in either account. The transfer trial remained pending at this point. Credential values must remain under `accounts/<id>/.secrets/`, with owner-only permissions, and must not be pasted into this document or a command line. The SMTP sections are retained but not used for IMAP copying. The imapsync trial must pass JSON passwords through private `--passfile1` and `--passfile2` descriptors, force TLS with `--ssl1 --ssl2`, and explicitly enable certificate verification with `--sslargs1 SSL_verify_mode=1 --sslargs2 SSL_verify_mode=1`.

## Read-only probe helper

`python3 experiments/imapsync/probe.py yahoo --config accounts/yahoo/.secrets/account.json` makes a read-only inspection. Add `--activate` only when the operator is ready to establish yahoo's first-activation baseline; that records UIDVALIDITY and UIDNEXT in ignored `.local/yahoo-baseline.json` and excludes the current mail from later transfers. The helper uses a verified TLS connection and read-only `EXAMINE`, reports a message count and actual eligible UIDs, and emits exact `search1_queries` for only eligible, unconfirmed messages. It fetches flags only when `--flags` is requested after activation, avoiding one request per existing message. It never uses the unsafe `UID n:*` range. Use account ID `alpha` with its private Gmail JSON to inspect the destination, without `--activate`.

The proposed confirmation evidence is imapsync's cache file named `<source-uid>_<destination-uid>` together with existence of that destination UID. Source inspection of the pinned release shows the cache file is created after a successful APPEND in `append_message_on_host2`. This is a candidate proof, not yet validated against provider behavior or an interrupted acknowledgement. The wrapper must treat an absent cache file as uncertain and reconcile it before retrying. If that cannot be done reliably, imapsync fails the adoption gate.

For the destination-deletion experiment, place a JSON array of manually confirmed source UIDs in ignored `.local/yahoo-confirmed.json` only after verifying their copies. The helper excludes those UIDs from future `search1_queries` even if their destination copies are later deleted. This models the planned durable copy ledger; the live trial must verify it with imapsync.

The helper has offline tests, including the RFC UID-range edge case, confirmed-UID filtering, and rejection of placeholder credentials. Run them with `python3 -m unittest discover -s experiments/imapsync -p 'test_*.py' -v`.

## 24-hour live transfer trial (2026-11-15)

The operator authorized copying only Yahoo Inbox messages received in the preceding 24 hours into alpha's Inbox, with no deletion. `experiments/imapsync/transfer_recent.py` is a one-off trial runner for that scope; it does not establish the planned first-activation baseline or implement the production copy ledger. It selects candidate UIDs using IMAP `SEARCH SINCE` and then checks each candidate's `INTERNALDATE` against an exact UTC cutoff. It applies the same exact cutoff to alpha's Inbox and passes explicit UID lists through `--search1` and `--search2`. This avoids scanning or matching older alpha mail. A message whose arrival time falls before the cutoff is excluded even if it arrived on the same calendar day. The live run's cutoff was `2026-11-14T20:20:30.103013+00:00`; the earliest selected Yahoo arrival was `2026-11-15T00:43:05+00:00`.

The first dry run selected zero folders because Yahoo lists its source folder as `Inbox`, while Gmail lists `INBOX`. The runner now uses `--folder Inbox`; imapsync maps that to alpha's `INBOX`. The corrected bounded dry run considered 28 Yahoo UIDs and 33 alpha UIDs and projected 28 copies. No write occurred in either dry run.

The live run selected 28 Yahoo messages and 33 recent alpha messages, synced one folder, and reported 28 messages transferred, 0 skipped, 0 deleted on Yahoo, 0 deleted on alpha, and 0 errors. The selected 28 source messages' flags, `INTERNALDATE`, and size matched the pre-run snapshot, and Yahoo's Inbox count was unchanged. Alpha's Inbox grew by 28 messages. imapsync created 28 distinct source/destination UID cache pairs, and all 28 destination UIDs were found in alpha's Inbox by read-only UID FETCH. A second run with the same 24-hour selection transferred 0 and skipped 28, again reporting no deletions. A later independent alpha arrival was absent from the transfer cache. Private trial logs and cache files are Git-ignored and owner-accessible; credentials were supplied by inherited memory-backed passfile descriptors. The command forced TLS, peer certificate verification, `--noresyncflags`, `--noexpunge1`, and `--noexpunge2`, with no delete option.

The bounded live run validated an ordinary transfer and immediate repeat. The subsequent disposable and fault tests are recorded below. Do not reuse this one-off runner as an unattended scheduler: it has no durable copy ledger and selects a rolling 24-hour window.

## Additional adoption evidence (2026-11-15)

- `verify_copies.py` fetched the 28 live source/destination UID pairs with read-only `BODY.PEEK[]`. All 28 raw messages, checked original headers, and decoded MIME payloads matched byte-for-byte. The sample included 14 plain-text, 28 HTML, and 15 multipart messages; it had no attachment or equal/missing-`Message-ID` fixture.
- `tls_rejection_probe.py` connected each imapsync host side in turn to a temporary localhost server with a self-signed certificate. Both attempts failed certificate verification with explicit `SSL_verify_mode=1`; neither test wrote mail.
- An isolated [GreenMail test server](https://greenmail-mail-test.github.io/greenmail/) container, image `greenmail/standalone:2.1.15` at digest `sha256:309ba544ea482e5d4265d09921cdab07c8dad2cc4481d715c9b8ff5d3f0a149f`, supplied disposable IMAP mailboxes. `local_trial.py` established a UIDNEXT baseline after an old fixture, then copied four newer fixtures. All four copied once with equal decoded payloads and unchanged source flags, including an attachment, two distinct messages sharing a `Message-ID`, and one without it. A repeat copied zero. A changed destination `\Seen` flag remained after the repeat with `--noresyncflags`. After deleting **only a disposable local destination copy**, the confirmed-UID filter selected only one newly added source UID; it copied that new message without restoring the deleted copy. No live Yahoo or alpha message was deleted.
- `append_fault_trial.py` used a local TCP proxy to drop the tagged APPEND success response after the disposable server accepted a message. The destination contained one matching copy but imapsync had no source/destination UID cache mapping. A plain retry reported one more transfer and created a duplicate. A separate trial injected a unique `X-Mailsync-Key` header through `--regexmess` for one source UID. After the same lost acknowledgement, IMAP `SEARCH HEADER X-Mailsync-Key` found exactly one accepted copy, with original headers and MIME payload preserved, allowing reconciliation without retry. This method requires one imapsync process per uniquely marked message.
- `outage_trial.py` used two disposable source users. One unavailable source failed with no copy; the healthy source copied its message; after reconnecting, the first source copied its message. Both destination payloads matched. This validates process-level isolation, not the not-yet-built multi-source scheduler.
- Two single-UID `--dry` checks against the live providers, with the same exact 24-hour source and alpha selection, took 11.2 and 11.05 seconds **per message**. A serial 100-message cycle would therefore take roughly 18 minutes in process overhead alone, outside the feature's 10-minute delivery target. Concurrent per-message imapsync calls, cache isolation, and provider connection limits were not validated. The direct IMAP design can keep both connections open and add a per-occurrence marker without a new process per message.

## Trial matrix

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Yahoo and Gmail login without paid forwarding | Pass for authentication and basic transfer | Both logins and the 28-message live copy passed |
| Valid certificates accepted and invalid certificate rejected | Pass | Live providers accepted; untrusted local certificate rejected on both sides |
| Existing mail excluded at first activation | Pass in disposable test | UIDNEXT baseline excluded old local fixture |
| Plain, HTML, multipart, and attachments preserved | Pass across live and disposable tests | 28 live raw copies matched; local attachment payload matched |
| Distinct messages with equal or absent `Message-ID` copied | Pass in disposable test | Two equal-ID messages and one missing-ID message copied |
| Repeated run and restart create no duplicate | Partial | Immediate repeats copied zero; plain retry after lost APPEND acknowledgement duplicated |
| Source flags unchanged; later alpha flags unchanged | Pass across live and disposable tests | 28 live source flags unchanged; local destination `\Seen` retained |
| Alpha deletion does not cause re-import | Pass in disposable test with proposed filter | Deleted local copy stayed absent when confirmed UID was excluded |
| Interrupted APPEND reconciles safely | Conditional | Unique-marker search reconciled; plain imapsync retry duplicated; marker requires per-message process |
| One source outage leaves other sources working | Pass at process boundary | Healthy local source copied after another source failed; recovery copied the first |

## Decision

**No-go for imapsync as the production transfer engine.** Its plain retry duplicates after a lost APPEND acknowledgement. Unique-marker reconciliation works only with one process per source UID; two live single-UID dry runs took about 11 seconds each. At that rate a serial 100-message batch misses the required delivery target, and the parallel alternative lacks provider and cache-safety evidence. Keep imapsync and these probes as evaluation artifacts. Continue with the direct IMAP design recorded in `research.md` and the revised plan.
