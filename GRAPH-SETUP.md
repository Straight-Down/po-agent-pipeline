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
§8 lesson 13). Nothing is gained by routing them through anyone. Same rule in
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

**Scoping: CONFIRMED BY IT, 2026-09-09 — scoped to the single mailbox.**

**How it was confirmed matters, and the distinction is deliberate: this is
ASSERTED BY IT, not OBSERVED.** IT stated the permission is restricted to the one
mailbox. Nothing in this repo has yet watched a request to a different mailbox be
refused, and the two are not the same kind of evidence — an assurance can be
mistaken, out of date, or about a different app registration.

`Mail.Read` app-only grants read access to **every mailbox in the tenant** unless
an application access policy restricts it:

```powershell
New-ApplicationAccessPolicy -AppId <application (client) id> `
  -PolicyScopeGroupId <mail-enabled security group> `
  -AccessRight RestrictAccess
```

**The pipeline behaves identically whether or not that policy exists.** It reads
one mailbox either way, every test passes either way, and no error, log line or
API response distinguishes the two. That is why this item cannot be closed by
watching the pipeline work — it is the one part of the setup that fails silently
*toward more access*, so success proves nothing about scope.

**To turn the assurance into an observation, run `scripts/probe_graph_auth.py`.**
Its step (d) requests a mailbox that should be denied and asserts a `403`. A `200`
there would mean the grant is tenant-wide regardless of what was assured, which
is exactly the finding an assurance cannot produce. Re-run it after any change to
the app registration, and at rotation.

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
7. **Verify** — Step 6. The pairing and thumbprint checks catch a half-finished
   swap here, on your machine, rather than at the next poll.
8. **Run a real intake** end to end on the new credential.
9. **Only now**, ask IT to remove the OLD certificate, identified by its
   `keyId` — which is the entire reason `GRAPH_CERT_KEY_ID` is recorded. Keep the
   old key file until this is done and confirmed; delete it afterwards.

---

## Certificates on the registration — CONFIRMED CLEAN, 2026-09-09

IT confirmed the registration carries **exactly one** certificate,
`E05CF5DB8EBFC7CAF259FD5EA6678B966353F016` — ours, and the only one. An earlier
certificate had been suspected; it does not exist.

Worth re-checking at rotation, since that is the one moment two certificates are
deliberately present at once (see step 4 of Rotation) and the whole point is that
only one survives. Any thumbprint on the registration other than the one in
`.env` is an **unclaimed public key**: nobody here holds its private half, so it
cannot serve this pipeline, and its presence means the application would accept
an assertion signed by whoever does hold it.

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
