# NetSuite M2M Setup — click-by-click (Phase 1)

Manual NetSuite UI work for Phase 1, in order. Only you can do this — it needs
Administrator access in **sandbox `1321665-sb2`**.

**Do all of this in sandbox.** Production is Phase 4, and only after Phases 1–3
pass against sandbox (working agreement, `CLAUDE.md`).

Permissions referenced here come from architecture doc §6. Where this doc adds
something §6 didn't list, it says so explicitly and tells you what to do rather
than assuming.

**Already done for you:** the keypair exists.

| | |
|---|---|
| Certificate (upload this) | `C:\Users\kiko.barroso\.po-agent\keys\netsuite_m2m_cert.pem` |
| Private key (never upload, never share) | `C:\Users\kiko.barroso\.po-agent\keys\netsuite_m2m_private.pem` |
| SHA-256 fingerprint | `A0:87:2D:5F:8E:F5:DF:51:A0:C5:00:54:6A:B6:54:AE:D6:06:FC:6E:E7:9D:CA:B6:AD:EA:BB:7D:54:08:E2:4E` |
| Certificate expires | **2028-08-03** — put a reminder in your calendar now; auth stops dead on that date |

Those live outside this OneDrive folder on purpose: a private key that can
authenticate as a NetSuite service account should not be synced to the cloud or
inherited by anyone this folder gets shared with.

At the end you'll hand back three values: **Account ID**, **Consumer Key /
Client ID**, **Certificate ID**.

---

## Repository layout — read this before cloning or troubleshooting

The working tree lives in the OneDrive-synced `PO Agent` folder, but **`.git` is a FILE, not a directory.** It contains one line:

```
gitdir: C:/dev/po-agent.git
```

Created with `git init --separate-git-dir "C:\dev\po-agent.git"` so the git database stays **outside** OneDrive — syncing git internals mid-write is a known corruption risk. Everything behaves normally from inside the folder (`git status`, `git log`, `git push`), but **it looks broken if you don't know this**: anything that tests for a `.git` *directory* will report "not a repository", and deleting that small file orphans the history (recoverable — recreate it with the same line).

Remote: `https://github.com/Straight-Down/po-agent-pipeline.git`, **private, and it must stay private** — the tracked vendor corpus contains real supplier pricing, a named inspector and customer contact details. `.gitattributes` marks pdf/xlsx/png/docx as binary; without it Git's text heuristic rewrites a generated PDF's bytes on checkout and silently invalidates the validation corpus.

The secrets this document produces — the private key and the API key — are **not** in the repository and are covered by `.gitignore` patterns as defence in depth. They live at `%USERPROFILE%\.po-agent\`.

---

## Step 1 — Enable the features

**Setup > Company > Enable Features > SuiteCloud** subtab.

- **Manage Authentication** section → check **OAUTH 2.0**
- **SuiteTalk (Web Services)** section → check **REST WEB SERVICES**

Save. Leave everything else alone — notably you do **not** need SuiteScript or
RESTlets enabled, because §6 confirmed the standard REST API handles the sublist
writes directly.

While you're here: **Setup > Company > Company Information** → copy the
**ACCOUNT ID** field verbatim (expected `1321665-sb2`). That's value 1 of 3.

---

## Step 2 — Create the "PO Update" role

**Setup > Users/Roles > Manage Roles > New**

- **Name:** `PO Update Automation (M2M)`
- **Center Type:** Classic Center
- **Web Services Only Role:** **check it.** Settled 2026-08-04 — harmless in
  either state for M2M, and checked is the hardened choice (Step 8). Earlier
  revisions said to leave it unchecked until tested; that test has been run.
  **Do not copy this setting from the interactive OAuth flow, where it must be
  UNCHECKED** — the two grants want opposite values (architecture doc §6).

Then add exactly these **seven** permissions. **The subtab is part of the
answer, not navigation** — two of these are not where their names suggest, and
that is precisely where this went wrong the first time. Select the subtab, pick
the permission, set the level, click **Add** to push the row into the sublist,
and **Save**.

| Subtab | Permission | Level | What breaks without it |
|---|---|---|---|
| Transactions | Purchase Order | **Edit** | read and write the PO item sublist — the whole point |
| Lists | Items | **View** | resolve item / style references |
| Lists | Vendors | **View** | read the PO's vendor |
| Setup | REST Web Services | **Full** | use the REST API at all |
| Setup | Log in using OAuth 2.0 Access Tokens | (checkbox, no level) | the role is not even *selectable* in Step 5 |
| **Setup** | **Custom Lists** | **View** | reading `customlist_psgss_product_size`, the size vocabulary |
| **Reports** | **SuiteAnalytics Workbook** | **Edit** (the only level offered) | **every collection `GET`, every `?q=` filter, and all SuiteQL** |

**The two that are not where you would look for them:**

- **`Custom Lists` is on the `Setup` subtab, not `Lists`** — despite governing
  things called lists. It is also **not** `Custom Record Entries`, which governs
  custom *records*. Custom lists carry no per-list permissions, so this is all or
  nothing.
- **`SuiteAnalytics Workbook` is on the `Reports` subtab**, which is the last
  place anyone looks for something gating a REST endpoint. `Edit` is not a
  compromise: the level dropdown offers **no `View`**, so `Edit` is the minimum
  NetSuite permits (RUNBOOK §6 item 8).

**Do not stop at the first five.** Earlier revisions of this document listed five
and said not to add anything beyond them — correct while Phase 1 was an
experiment asking whether the minimum set could write, and **wrong as a build
instruction now**. A five-permission role authenticates, passes
`test_phase1_writeback.py`, and then fails on every real vendor document, because
a by-id write is not gated by `SuiteAnalytics Workbook` and a PO-number lookup is.
See the `400 USER_ERROR` row in Troubleshooting for what that looks like.

> **CONFIRMED FINDING (2026-08-04):** "Log in using OAuth 2.0 Access Tokens" is
> required just for the role to be *selectable* on the OAuth 2.0 Client
> Credentials (M2M) Setup screen (Step 5) — without it, the role doesn't even
> appear in that screen's Role dropdown. This was originally left off
> deliberately (see the old note this replaces) to test whether it was needed;
> now confirmed that it is. It's still a login permission, not a data
> permission, so it doesn't widen what the role can read or write — it only
> gates whether the role can authenticate via this flow at all. §6 has been
> updated with this as a required permission.

---

## Step 3 — Attach the role to an employee (UPDATED: no new employee)

**Confirmed:** a dedicated new Employee record consumes a paid NetSuite user
license. Given that, skip creating one — a new employee was only ever a
"cleaner audit trail" nice-to-have, not a technical requirement of the M2M
flow. NetSuite's Client Credentials grant just needs a valid
(entity, role, integration) triple to exist for the certificate mapping in
Step 5 — the entity can be any existing, already-licensed employee.

**Setup > Users/Roles > Manage Users** → open your own existing employee
record → **Roles** sublist → add `PO Update Automation (M2M)` → Save.

**Trade-off, worth knowing and accepting explicitly:** the `audit_log.actor`
field (data model, §5) will now show Kiko's employee record as the actor for
every automated write this pipeline makes, same as it would for a manual edit
made directly. It won't be distinguishable from your own manual NetSuite
edits by employee identity alone. If that distinction matters later, this
pipeline's own `audit_log` table (which the application controls directly,
separate from NetSuite's own system notes) can record "automated pipeline"
vs. "Kiko manually" itself — arguably a more reliable place for that
distinction to live than relying on NetSuite's employee identity anyway.

If a dedicated, distinctly-identified service account becomes worth the
license cost later (e.g. once this runs in production and audit clarity
matters more), this step can be revisited then — nothing else in this setup
needs to change to do that later.

---

## Step 4 — Create the Integration record

**Setup > Integration > Manage Integrations > New**

- **Name:** `PO Update Automation (M2M)`
- **State:** Enabled

In the **Authentication** section, the checkbox pattern matters — this is
precisely where the existing "Claude AI" record is configured differently and
why it can't be reused (§6):

- ☐ **TOKEN-BASED AUTHENTICATION** — leave unchecked
- ☐ **AUTHORIZATION CODE GRANT** — leave unchecked ← *what the broken "Claude AI"
  record uses; this is the interactive browser flow we're deliberately avoiding*
- ☑ **CLIENT CREDENTIALS (MACHINE TO MACHINE) GRANT** — **check this**
  - Scope → ☑ **REST WEB SERVICES** only. Leave RESTlets unchecked; §6 confirmed
    no RESTlet is needed.

Save.

**The next screen shows the CONSUMER KEY / CLIENT ID exactly once.** Copy it
now — that's value 2 of 3. If you navigate away, you have to regenerate it (and
then update `.env`). A Consumer Secret is also shown; the client-credentials JWT
flow does **not** use it, so you can ignore it.

---

## Step 5 — Upload the certificate

**Setup > Integration > OAuth 2.0 Client Credentials (M2M) Setup** → **Create New**

- **Entity:** your own employee record (Step 3 — no longer a dedicated "PO Update Automation" employee, see the update to Step 3 above)
- **Role:** `PO Update Automation (M2M)` (Step 2)
- **Application / Integration:** `PO Update Automation (M2M)` (Step 4)
- **Certificate:** browse to
  `C:\Users\kiko.barroso\.po-agent\keys\netsuite_m2m_cert.pem`

Save.

The resulting list row shows a **Certificate ID** — value 3 of 3. That string
becomes the JWT `kid` header, which is how NetSuite knows which certificate to
verify our signature against.

If the upload is rejected, the certificate is a 4096-bit RSA, SHA-256,
self-signed X.509 valid until 2028-08-03. Self-signed is correct (NetSuite pins
the exact file, no CA chain involved). If it complains about the validity
period, regenerate shorter: `python generate_m2m_keypair.py --force --days 365`.

---

## Step 6 — Fill in `.env`

In this folder:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` with the three values:

```
NS_ACCOUNT_ID=1321665-sb2
NS_CLIENT_ID=<Consumer Key / Client ID from Step 4>
NS_CERTIFICATE_ID=<Certificate ID from Step 5>
```

`NS_PRIVATE_KEY_PATH` is already correct. `.env` is gitignored.

**Edit `.env` directly — these values do not go in chat.** They are identifiers
rather than secrets (the private key is the secret, and it never moves), so
pasting one is not an incident. The reason is durability, not sensitivity: **a
value typed into a chat persists in a transcript long after it stops being useful
there**, in a place this repo does not control and cannot clean up. There is
nothing to gain — appending a line to a file is not work that needs delegating —
and a permanent copy to lose. Same rule in `GRAPH-SETUP.md`.

---

## Step 7 — Run the test

Dry run first — reads and prints the exact payload, writes nothing:

```powershell
.\.venv\Scripts\python.exe test_phase1_writeback.py --dry-run
```

Then the real thing:

```powershell
.\.venv\Scripts\python.exe test_phase1_writeback.py
```

It authenticates via signed JWT (no browser), reads PO `8489541` line 18,
confirms it's still the `M120246`/`TID`/`3X` line, writes all four fields in one
PATCH, reads back and checks each field individually, then reverts and verifies
the revert.

Exit codes: `0` pass · `1` fail · `2` config/refused · `3` **permission finding**.

Paste me the output either way.

---

## Step 8 — "Web Services Only Role" — SETTLED 2026-08-04, no experiment needed

**Check the box. It is harmless in *either* state — confirmed empirically on this
account, for both authentication and the by-id write path — so this is a
preference for the hardened configuration, not a requirement to verify.**

Set it at Step 2 and skip the rest of this section. Its opposite reputation comes
from the *interactive* Authorization Code grant, where checking it blocks the
browser login outright; M2M has no browser login to block. Do not carry the
interactive flow's setting across (architecture doc §6).

<details>
<summary>The experiment as originally written, kept for method rather than result</summary>

§6 reasoned that checking this box was *likely* correct for M2M (a service-account
role has no business supporting interactive UI login) but flagged it as
**empirically unverified against this account**. The procedure was:

1. Get a **PASS** in Step 7 with the box unchecked. Now you have a known-good
   baseline.
2. **Setup > Users/Roles > Manage Roles** → `PO Update Automation (M2M)` → check
   **Web Services Only Role** → Save.
3. Re-run `test_phase1_writeback.py`.
   - **Still passes** → keep it checked. That's the hardened configuration, and
     it's now a confirmed finding for §6 rather than an assumption.
   - **Now fails** → uncheck it, re-run to confirm you're back to passing, and
     tell me. Also a finding worth recording.

Doing it in this order means a failure is unambiguously attributable to that one
box. Don't skip step 1 and set both at once.

**It was run, and it passed with the box checked.** The isolation discipline
above is the part worth reusing — change one thing, keep a known-good baseline,
attribute the failure unambiguously. It is the same discipline RUNBOOK §8 lesson
2 arrived at from the opposite direction.

</details>

---

## Troubleshooting

| Symptom | Most likely cause |
|---|---|
| `invalid_client` | `NS_CERTIFICATE_ID` doesn't match the Step 5 row; or `NS_CLIENT_ID` is the Consumer *Secret* instead of the Key; or the uploaded certificate isn't the pair of this private key |
| `invalid_grant` | The Step 5 mapping's entity/role is wrong, or the role isn't actually assigned to the employee in Step 3 |
| `unsupported_grant_type` | Step 4's **CLIENT CREDENTIALS (MACHINE TO MACHINE) GRANT** box isn't checked |
| `invalid_request` | Assertion malformed — try `NS_JWT_ALGORITHM=RS256` in `.env`, and check this machine's clock isn't skewed |
| Token works, read 403s | Role is missing Purchase Order (Edit) or Items (View) |
| Token works, write 403s | **The finding this phase is looking for.** Report it, don't widen the role |
| Write returns 204 but a value didn't change | Field-level access restriction — also a finding. Check *Customization > Lists,Records,&Fields > Transaction Line Fields > [field] > Access* |
| Everything breaks after a sandbox refresh | Sandbox refresh wipes Integration records and certificate mappings. Redo Steps 1–5; the keypair itself stays valid |
| **`400` with `USER_ERROR: Your current role does not have permission to perform this action`** on a *collection* call | **Missing `Reports > SuiteAnalytics Workbook`.** See below — this is the one that looks like a bad request rather than a permission problem |

### The `400 USER_ERROR` signature, in full

The single most misleading failure in this setup, because **NetSuite does not use
`403` for it.** A genuine permission refusal arrives as:

```
HTTP 400
{"type":"...","title":"Bad Request","status":400,
 "o:errorDetails":[{"detail":"Your current role does not have permission to perform
                    this action. Please contact your account administrator.",
                    "o:errorCode":"USER_ERROR"}]}
```

`400` and `Bad Request` both say *you sent something wrong*. Nothing was wrong
with the request. Any error handling that branches on `403` to mean "permissions"
will classify this as a malformed call and send you to debug the payload.

**What makes it hard to attribute: by-id reads and writes keep working.**

| Call | Without `SuiteAnalytics Workbook` |
|---|---|
| `GET /purchaseOrder/8489541` | **200** — works |
| `PATCH /purchaseOrder/8489541` (the sublist write) | **204** — works |
| `GET /purchaseOrder?limit=1` | **400 `USER_ERROR`** |
| `GET /purchaseOrder?q=tranId IS "PO0001662"` | **400 `USER_ERROR`** |
| `POST /query/v1/suiteql` | **400 `USER_ERROR`** |

So `test_phase1_writeback.py` passes on a role missing it — that test targets an
internal id directly. The failure surfaces later, on the first real vendor
document, where a printed `1662` has to be resolved to internal id `8489541`.

**`Edit` is the only level offered.** The role editor's level dropdown for this
permission has no `View` entry; Oracle's own documentation says *"set the access
to Edit"* rather than describing a range. It is the minimum, not a widening.

---

## Re-verification: any permission change re-runs the write-back test

**Rule, and it is not optional: change the role's permission set — add, remove or
re-level anything — and re-run `test_phase1_writeback.py`. Then date the result in
RUNBOOK §6.**

This exists because the connection was missing once and nobody noticed for six
weeks. The role was proven on 2026-08-04 with five permissions. It gained
`SuiteAnalytics Workbook` on 2026-08-12 and `Custom Lists` after that, and
**nothing re-ran the test that had proven the write**. The claim "the write path
works under this role" silently became a claim about a role that no longer
existed. It did in fact still pass when finally re-run on 2026-09-14 — but that
was luck confirmed after the fact, not a controlled state.

Two reasons the test is the right instrument rather than re-reading the role
screen:

- **A permission can read as set and never have been saved** (RUNBOOK §8 lesson
  1 — `SuiteAnalytics Workbook` sat at `None` through five probe cycles that all
  believed it was on). The role editor shows intent; the test shows behaviour.
- **NetSuite can accept a `PATCH` with `204` and silently discard a field** it
  will not let the role write. Only a field-by-field read-back catches that, which
  is exactly what Step 3 of the test does.

Cheap to run — under a minute, reverts what it writes, and verifies the revert.

---

## What Phase 1 has left after this

`test_phase1_writeback.py` exiting `0` is the exit criterion. That closes the
last open validation step in §6 — proving the least-privilege role, not just the
CFO role, can make these writes.

Not in scope here, deliberately: the parsing layer (Prompt 2), email intake and
matching (Prompt 3). The §6.1 business-logic questions for Paula
(date-to-field mapping and transit buffer, split-shipment semantics, lines
absent from a packing slip) don't block Phase 1, but they do block Phase 3 —
worth raising with her while you're waiting on NetSuite admin access.
