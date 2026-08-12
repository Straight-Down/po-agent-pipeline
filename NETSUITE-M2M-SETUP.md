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
- **Web Services Only Role:** **leave UNCHECKED for now.** See Step 7 — we test
  it deliberately rather than assuming, and starting unchecked means a first-run
  auth failure can't be caused by this box.

Then add exactly these five permissions (each on its own subtab, click **Add**
after each):

| Subtab | Permission | Level |
|---|---|---|
| Transactions | Purchase Order | **Edit** |
| Lists | Items | **View** |
| Lists | Vendors | **View** |
| Setup | REST Web Services | **Full** |
| Setup | Log in using OAuth 2.0 Access Tokens | (checkbox, no level) |

Save. Do **not** add anything beyond these five — the whole point of Phase 1
is finding out whether this least-privilege set is sufficient for the write
itself.

> **CONFIRMED FINDING (2026-08-04):** "Log in using OAuth 2.0 Access Tokens" is
> required just for the role to be *selectable* on the OAuth 2.0 Client
> Credentials (M2M) Setup screen (Step 5) — without it, the role doesn't even
> appear in that screen's Role dropdown. This was originally left off
> deliberately (see the old note this replaces) to test whether it was needed;
> now confirmed that it is. It's still a login permission, not a data
> permission, so it doesn't widen what the role can read or write — it only
> gates whether the role can authenticate via this flow at all. §6 has been
> updated with this as the fifth required permission.

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

You can also just paste those three values back to me in chat and I'll write the
file — they're identifiers, not secrets (the private key is the secret, and it
never moves).

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

## Step 8 — The "Web Services Only Role" experiment

§6 reasons that checking this box is *likely* correct for M2M (a service-account
role has no business supporting interactive UI login) but flags it as
**empirically unverified against this account**. So verify it, in this order:

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
