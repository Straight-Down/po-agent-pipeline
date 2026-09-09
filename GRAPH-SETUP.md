# Microsoft Graph Setup — Outlook intake (Phase 2)

Companion to `NETSUITE-M2M-SETUP.md`, same shape and same conventions. App-only
(client credentials) certificate auth against Entra ID, so there is no browser
login anywhere and no user token to refresh.

**Identifiers are written as `<placeholders>` here, exactly as the NetSuite setup
doc does** (`NS_CLIENT_ID=<Consumer Key / Client ID from Step 4>`). The real
values live in `.env`, which is gitignored. That is the repo's existing
convention, not a new one invented for this document — and it is also what keeps
`test_config.test_no_credentials_in_tracked_files` passing, since that scan fails
on any GUID-shaped string in a tracked file.

**Values go in `.env`, never in chat.** Identifiers are not secrets, so pasting
one is not an incident — but a chat transcript is a permanent record that nothing
in this repo controls, and this project has already had sensitive values end up in
a permanent record while being carefully removed from the working tree (RUNBOOK
§8 lesson 14). Nothing is gained by routing them through anyone. Same rule in
`NETSUITE-M2M-SETUP.md` Step 6.

---

## What is secret, and what is merely kept out of the tree

The distinction matters because treating everything as equally sensitive is how
people stop taking any of it seriously. Two different reasons, named separately:

| Artefact | Where | Secret? | Why |
|---|---|---|---|
| `po-agent-graph.key` | `C:\dev\po-agent-secrets\` | **YES — genuinely** | The private key. Anything holding it can authenticate as this application. Outside git, outside OneDrive, ACL-locked (below). |
| `po-agent-graph.cer` | `C:\dev\po-agent-secrets\` | No | The public half. It was *uploaded to Entra*, so Microsoft has it and anyone who can read the app registration can fetch it. Kept beside the key for convenience, not for protection. |
| Certificate thumbprint | `.env`, and below | No | A SHA-1 fingerprint of a public certificate. Published here deliberately, so a mismatch can be diagnosed without opening the portal. |
| Tenant / application (client) IDs | `.env` only | No — **but out of the tree** | Identifiers, not credentials; they appear in redirect URLs and Microsoft treats them as non-confidential. Held back from committed files by *convention*, so the repo never becomes a map of the tenant, and so the credential scan can stay strict. |
| Entra Certificate ID (`keyId`) | `.env` as `GRAPH_CERT_KEY_ID` | No — same as above | Identifies *which* credential entry on the app registration. **Not an auth input** — Graph signs with the thumbprint. Recorded solely so rotation knows which entry to remove. |

**`GRAPH_CERT_KEY_ID` is documentation, not configuration.** The loader does not
read it, `config.py` does not mention it, and `GraphConfig` has no field for it —
all three asserted by `test_config`. A garbage value there changes nothing. That
is on purpose: an unread variable that could fail a load would stop being a note
and become a trap.

### The private key's ACL

Stripped of inheritance and granted to the current user only, done outside the
repo:

```powershell
icacls C:\dev\po-agent-secrets /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
```

Same treatment as the NetSuite private key (RUNBOOK §6 item 1), so both keys in
this project are protected identically rather than one being an exception.

**Do not verify this from a Git Bash / MSYS shell.** `ls -l` reports
`-rw-r--r--` on a correctly locked file, because MSYS synthesises POSIX bits it
cannot actually represent — see RUNBOOK §7. Use `icacls` to check.

---

## The artefacts, as they stand

| | |
|---|---|
| Subject / issuer | `CN=po-agent-graph` (self-signed) |
| Key | RSA 2048, PKCS#8 PEM, unencrypted |
| Signature | SHA-256 with RSA |
| Valid | **2026-09-09 → 2028-09-08** (730 days) |
| **Thumbprint (SHA-1)** | **`E05CF5DB8EBFC7CAF259FD5EA6678B966353F016`** |
| Private key | `C:\dev\po-agent-secrets\po-agent-graph.key` |
| Public certificate | `C:\dev\po-agent-secrets\po-agent-graph.cer` |

`config.GraphConfig.from_env` warns from **60 days out** (2028-07-10). Expiry is
otherwise silent until the day it fails.

---

## Step 1 — Generate the key pair

```bash
openssl req -x509 -newkey rsa:2048 -sha256 -days 730 -nodes \
  -keyout po-agent-graph.key \
  -out    po-agent-graph.cer \
  -subj   "/CN=po-agent-graph"
```

`-nodes` leaves the key unencrypted, which is what the loader expects (it parses
with no passphrase); the file's protection is the ACL, not a password. `-days 730`
produced the two-year validity above.

**Recorded so rotation is a re-run rather than a rediscovery.** This command was
reconstructed from the certificate itself rather than transcribed as it was typed
— the artefact is self-signed `CN=po-agent-graph`, RSA 2048, SHA-256, exactly 730
days, and carries the `subjectKeyIdentifier` / `authorityKeyIdentifier` /
`basicConstraints` extension set that OpenSSL 3.x `req -x509` adds by default.
It reproduces an equivalent certificate; it is not a byte-for-byte account of
history.

## Step 2 — Move the pair out of OneDrive and lock it

```powershell
mkdir C:\dev\po-agent-secrets
Move-Item po-agent-graph.key,po-agent-graph.cer C:\dev\po-agent-secrets\
icacls C:\dev\po-agent-secrets /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
```

Never inside this project folder: it is OneDrive-synced, so a private key placed
here would upload to the cloud and be inherited by anyone the folder is shared
with. `*.key`, `*.cer`, `*.crt`, `*.der`, `*.pem`, `*.pfx` are all gitignored as
a second line of defence, but the first line is the key not being here at all.

## Step 3 — IT uploads the certificate

Send **`po-agent-graph.cer`** — the `.cer` only, never the `.key`. IT uploads it
under *App registrations → (the app) → Certificates & secrets → Certificates*.

Ask them for three things back:

- the **Directory (tenant) ID** → `GRAPH_TENANT_ID`
- the **Application (client) ID** → `GRAPH_CLIENT_ID`
- the **Certificate ID** shown on the uploaded row → `GRAPH_CERT_KEY_ID`

Then confirm the thumbprint on that row reads
`E05CF5DB8EBFC7CAF259FD5EA6678B966353F016`. If it does not, the certificate they
uploaded is not this one.

## Step 4 — Permissions

`Mail.Read` **application** permission, admin-consented.

**Scoping: OBSERVED 2026-09-09 — genuinely restricted to the single mailbox.**

**How it was established, because that is the part that matters.** IT first
*asserted* it. That assurance was then **converted into an observation** by
`scripts/probe_graph_auth.py` step (d), which requested a real, populated second
mailbox in the same tenant and got:

```
GET /users/kiko.barroso@straightdown.com/messages?$top=1
status: 403
error code: ErrorAccessDenied
```

An assurance and an observation are not the same kind of evidence — an assurance
can be mistaken, out of date, or about a different app registration, and one on
this very setup turned out to be premature the same day (see *Certificates on the
registration* below). This one held.

`Mail.Read` app-only grants read access to **every mailbox in the tenant** unless
an application access policy restricts it:

```powershell
New-ApplicationAccessPolicy -AppId <application (client) id> `
  -PolicyScopeGroupId <mail-enabled security group> `
  -AccessRight RestrictAccess
```

**The pipeline behaves identically whether or not that policy exists.** It reads
one mailbox either way, every test passes either way, and no error, log line or
API response distinguishes the two. That is why this could never be closed by
watching the pipeline work — it is the one part of the setup that fails silently
*toward more access*, so success proves nothing about scope. Only a request that
is expected to be **refused** carries information.

**Re-run the probe after any change to the app registration, and at rotation.** A
`200` at step (d) would mean the grant had become tenant-wide, and nothing else
in the system would notice.

Also settled by the same run: **`Mail.Read` is both granted and admin-consented.**
Those fail separately and look identical from outside, and step (c) reading the
target mailbox (`200`, 1 message) proves both — an unconsented application
permission returns `403` on the call itself. Note that the probe's step (b),
which inspects the token's `roles` claim, **cannot** establish this: a Graph
access token is opaque to the client and carries no readable `roles` claim. See
RUNBOOK §8 lesson 12.

## Step 5 — Fill in `.env`

Append to the existing `.env` (it already holds the NetSuite credentials — do not
overwrite it):

```
GRAPH_CLIENT=mock
GRAPH_TENANT_ID=<Directory (tenant) ID from Step 3>
GRAPH_CLIENT_ID=<Application (client) ID from Step 3>
GRAPH_MAILBOX=<the mailbox to poll>
GRAPH_CERT_PATH=C:/dev/po-agent-secrets/po-agent-graph.key
GRAPH_CERT_PUBLIC_PATH=C:/dev/po-agent-secrets/po-agent-graph.cer
GRAPH_CERT_THUMBPRINT=E05CF5DB8EBFC7CAF259FD5EA6678B966353F016
GRAPH_CERT_KEY_ID=<Certificate ID from Step 3>
```

**Forward slashes in the paths.** Most `.env` readers treat a backslash as an
escape character and will silently mangle a Windows path.

`GRAPH_CLIENT` selects the client and takes **exactly** `mock` or `real`, lower
case. There is no default, and no input produces `real` by accident — `Real` is
rejected rather than folded, because this switch decides whether live requests
reach a real mailbox. Leave it `mock` until you actually want live calls;
`GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` and `GRAPH_MAILBOX` are not required while
it is, so development is never blocked waiting on IT.

`.env` is gitignored; `.env.example` documents every variable and holds no values.

## Step 6 — Verify

```bash
python -c "import config; print(config.GraphConfig.from_env())"
python test_config.py
```

Loading validates the whole chain **before any network call**: the key parses,
its thumbprint matches the certificate, the key and certificate are a genuine
pair, and in `real` mode all three identifiers are present. The pairing check is
the one that matters — a thumbprint match alone only proves the fingerprint was
typed correctly, not that the local key belongs to the certificate Entra holds.
Without it, a mismatch surfaces much later as an opaque `AADSTS700027`.

The printed config shows presence markers rather than values; `repr` deliberately
carries no identifier, so it is safe in a log line.

### And then verify against Entra, because the above cannot

**Every check in Step 6 passed on 2026-09-09 at a moment when Entra held no
certificate for this application at all.** They are local checks: they prove the
pair on this machine is internally consistent, and they are blind to the other
half of the arrangement. Nothing on this side can tell "correctly configured" from
"correctly configured and never uploaded".

```bash
python scripts/probe_graph_auth.py
```

Four steps, no pipeline code, nothing written anywhere: mint a token, report the
token's claims, read **one** message header from `GRAPH_MAILBOX`, then request a
second mailbox that must be refused. It prints status codes and a message
**count** — never a subject, sender, address or body — and never the token.
`CLEAN` requires a token, `200` on the target mailbox and `403` on the other.

Run it after any change to the app registration, after rotation, and any time
the credential chain is in doubt.

---

## Rotation — before 2028-09-08

**Overlap first, remove second.** Doing it the other way round is an outage: the
moment the old certificate is deleted, every assertion signed with the old key is
rejected, and if the new one is not yet uploaded and configured there is no
working credential.

1. **Generate a new pair** — Step 1, into a new filename (`po-agent-graph-2028.key` / `.cer`).
2. **Lock it** — Step 2's `icacls`, or place it in the already-locked directory.
3. **Send the new `.cer` to IT.** The `.cer` only.
4. **IT uploads it ALONGSIDE the existing certificate.** Both live on the app
   registration at once; Entra accepts an assertion signed by either. Do not let
   them remove anything yet.
5. **Get the new Certificate ID** from the new row, and its thumbprint.
6. **Update `.env`**: `GRAPH_CERT_THUMBPRINT`, `GRAPH_CERT_PATH`,
   `GRAPH_CERT_PUBLIC_PATH`, `GRAPH_CERT_KEY_ID` — all four, since the paths
   change with the filename.
7. **Verify locally** — Step 6. The pairing and thumbprint checks catch a
   half-finished swap here, on your machine, rather than at the next poll.
8. **Run `scripts/probe_graph_auth.py`.** This is the step that proves the new
   certificate actually reached Entra, which step 7 cannot — and it re-checks
   mailbox scoping at the same time. Then run a real intake end to end.
9. **Only now**, ask IT to remove the OLD certificate, identified by its
   `keyId` — which is the entire reason `GRAPH_CERT_KEY_ID` is recorded. Keep the
   old key file until this is done and confirmed; delete it afterwards.

---

## Certificates on the registration — RESOLVED 2026-09-09, and the timeline matters

**Our certificate is registered and authenticating.** `scripts/probe_graph_auth.py`
mints a token with it, so this is observed rather than reported.

The route there is worth keeping, because it is the clearest example in this
project of a confirmation being honest and the state being wrong anyway:

| When | What |
|---|---|
| morning, 2026-09-09 | IT confirmed the certificate for this app is `E05CF5DB…F016` — **correct**, and it was the only one |
| 21:13 UTC | first probe run: **`AADSTS700027` — "the key was not found"**. Entra did not hold that thumbprint for `<application (client) id>` |
| immediately after | the local pair re-verified as provably correct — thumbprint matches the certificate, key and certificate are a genuine pair — which isolated the fault to the **Entra side** rather than leaving it ambiguous |
| later, same day | IT completed the upload; the probe authenticates |

**The confirmation answered "is this the right thumbprint?", and it did so
accurately. It could not answer "has it been uploaded", because that was never
the question.** Recorded as RUNBOOK §8 lesson 11.

Note what made this diagnosable rather than a mystery: `AADSTS700027` names the
thumbprint the client offered *and* the app id it was offered to, so the two
sides could be compared. That is why the probe prints Microsoft's error text
verbatim while redacting its own preamble.

### Is an earlier certificate still present?

**Unknown from here, and it needs IT to look.** The probe authenticates *as* the
application; listing a registration's certificates requires
`Application.Read.All`, which this app does not have and should not be given for
this purpose. So the probe proves **ours is present** and can say nothing about
what sits alongside it.

IT stated in the morning that ours was the only one — but that statement is from
before the upload that actually put ours there, so it describes a registration
state that no longer exists. **Ask IT to list the thumbprints now.** Anything
other than `E05CF5DB8EBFC7CAF259FD5EA6678B966353F016` is an **unclaimed public
key**: nobody here holds its private half, so it cannot serve this pipeline, and
its presence means the application would accept an assertion signed by whoever
does hold it.

Re-check at rotation too, since step 4 there deliberately puts two certificates
on the registration at once and the whole point is that only one survives.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `GRAPH_CLIENT must be exactly 'mock' or 'real'` | Unset, misspelled, or differently cased. Deliberately strict — no default, and `Real` is not accepted. |
| `GRAPH_CERT_THUMBPRINT does not match the certificate` | The thumbprint in `.env` and the file on disk are different certificates. The error prints both. |
| `is not the private key for the certificate` | Thumbprint matched but the pair does not. The certificate uploaded to Entra was generated separately from this key. |
| `did not parse as an unencrypted PEM private key` | `GRAPH_CERT_PATH` is pointing at the `.cer`, or the key has a passphrase. |
| `GRAPH_CERT_THUMBPRINT must be 40 hex characters` | Truncated, or the Entra **Certificate ID** GUID was pasted instead of the thumbprint. |
| `AADSTS700027` at runtime | Entra does not hold a certificate matching the assertion — usually the local pair was rotated without uploading, or the old certificate was removed too early. |
| `ErrorAccessDenied` on a mailbox | `Mail.Read` not admin-consented, or an application access policy excludes this mailbox (see Step 4). |
| `ls -l` shows the key as world-readable | An MSYS artefact, not the real ACL. Check with `icacls`. RUNBOOK §7. |
