# PO Update Automation — Deployment Plan (hosting)

**Status:** PLAN ONLY, 2026-11-18. Nothing here is built or provisioned. It covers moving the PO Agent from hand-started commands on Kiko's laptop to an Azure Function App. Read `RUNBOOK.md` §2 (what is built), §11 ("Not started: production cutover") and §12 (the cutover checklist) first: this plan serves them, and does not replace them.

**Target shape (given):** one Azure Function App on the **Flex Consumption** plan, hosting:
- the **poller**, plus the extraction that follows it, on a **timer trigger**;
- the **review app**, served over HTTP behind **Microsoft Entra sign-in**, restricted to named users.

**Why do this at all.** RUNBOOK §12 item 8: today `verdict_by` is whatever `PO_AGENT_REVIEWER` was set to when the app started. So nothing anywhere shows who approved a change, and item 7's recommendation rests entirely on fixing that. **A sign-in that puts the reviewer's own identity into `verdict_by` is the main point of hosting** (§4 below). Running unattended is the second.

**How to read the flags.** Every point that cannot be settled from the repo is marked **CHECK IN PORTAL**, or **DECISION (Kiko)** where it is a choice rather than a fact. Section 10 gathers them all. Platform facts were checked against Microsoft Learn on 2026-11-18 (sources at the end). Platform behaviour changes, so re-check anything marked from the docs before relying on it.

---

## 1. The target, in one picture

```
            shipments@ (Exchange Online)                    NetSuite (Sandbox 2, then production)
                     │ Graph, app-only Mail.Read                 ▲ M2M certificate, PO Update role
                     ▼                                           │
 ┌──────────────── Function App (Flex Consumption, Linux, Python) ┼───────────────────────────┐
 │  timer: poll_and_extract  ── poller.poll_once ─► extract_pending ─► ingest ─► matcher ────┤
 │  http:  review app (FastAPI via AsgiFunctionApp) ── Approve ─► write_path.write_po_batch ──┘
 │         ▲ Easy Auth (Microsoft Entra), only assigned users reach it                          │
 └─────────┼──────────────────────────────┬─────────────────────────┬─────────────────────────┘
           │ browser (Paula)             │ managed identity        │ managed identity
           │                             ▼                         ▼
           │                  Azure SQL sqldb-po-agent     Storage account: attachments container,
           │                  (system of record)           unrecorded-writes append blob, host storage
           │                                               Key Vault: private keys, API key, CSRF key
           └──────── Application Insights (logs, the CRITICAL unrecorded-write line, alerts)
```

---

## 2. What has to change in the code

The library code (`poller.poll_once`, `extract_pending.extract_pending`, `ingest`, `matcher`, `write_path`, `review_app.app.create_app`) is already shaped as functions with explicit inputs. Most of the work is a thin hosting layer, plus removing the assumptions listed below. **The local command-line paths stay working throughout:** development, tests and the run databases keep using them (§8).

### 2.1 Entry points

- **New `function_app.py` at the repo root.** It is the composition root, and neither `review_app/` nor the pipeline modules import it.
  - It builds the review app's FastAPI instance from hosting settings and serves it with `func.AsgiFunctionApp(app=..., http_auth_level=func.AuthLevel.ANONYMOUS)`. Anonymous at the function-key level, because the browser cannot send a function key. Access control is Easy Auth's job (§4).
  - It registers the timer function on that same app object. `AsgiFunctionApp` is a `FunctionApp`, so it should take a `@app.timer_trigger` too. **Verify in the first spike (§9).**
- **Timer function `poll_and_extract`.** It calls `poller.poll_once(engine, graph_client, mailbox, store, ...)` and then `extract_pending.extract_pending(engine, limit=..., client=NetSuiteClient(config=...))`, with what is now `main()`'s argument handling turned into settings.
  - It never calls `poller.main()` or `extract_pending.main()`: those parse a command line, `print`, and return exit codes.
- **`host.json`:**
  - `functionTimeout` (§6);
  - `extensions.http.routePrefix` set to `""`, because the review app's routes are `/`, `/po/...`, `/source/...` and FastAPI under Functions expects an empty prefix;
  - logging levels, so the `write_path` CRITICAL line reaches Application Insights.
- **`requirements.txt`:**
  - add `azure-functions`, `azure-storage-blob` and `azure-identity`;
  - **move `pyodbc` from `requirements-dev.txt` into it.** It is a runtime dependency the moment the app talks to Azure SQL. Today it is installed only for the test harness.
  - `uvicorn` stays, for the local review app; it is unused in hosting.
- **The import rule is unchanged.** `test_review_app_never_imports_netsuite_client` pins what the `review_app` package may import. `function_app.py` imports both sides by design, and lives outside that package so the rule still holds.

### 2.2 How the review app is served

- **One Function App, HTTP routed into FastAPI.** Templates are rendered server-side, and the CSS is inline in `base.html`, so there are no static files to serve.
- **The CSRF token is per process today, and breaks under scale-out.**
  - `create_app` mints `secrets.token_urlsafe(32)` at start-up and checks every POST against it (`review_app/app.py`).
  - On Flex, a page served by one instance can be posted to another, and a restart invalidates every open page.
  - **Change:** derive the token as an HMAC of the signed-in user's object id under a key held in Key Vault (`PO_AGENT_CSRF_KEY`, new). It is then stable across instances and restarts, and bound to the person.
  - The alternative, capping the HTTP group at one instance, is allowed (the lowest maximum is 1). But Microsoft warns that HTTP apps capped below 40 throttle under load. It is not needed if the token is fixed.
- **Remove the 127.0.0.1-only guard in hosting, and keep it locally.** `review_app/__main__.py` binds to loopback because "no auth exists". In hosting that reason is replaced by §4. **The hosted app must refuse to serve any request unless identity mode is on** (§4.4).
- **Cold starts.** With zero always-ready instances, the first page after an idle spell starts an instance, and one Q&A thread reports intermittent 503s on the Entra sign-in callback on Flex. **DECISION (Kiko):** one always-ready instance for the HTTP group (a small standing cost), or accept a slow first page. Recommendation: start at zero, and add one if the sign-in 503s appear.
- **Two concurrent writers for the first time.** The timer ingests while Paula approves. The existing guards already cover this: the page fingerprint is re-checked at decision time, and `write_path` re-reads every line before writing.
- **`resolve_source`'s own docstring flags its check-then-serve race** ("revisit if the route is ever reachable from more than one process"). §2.3 closes it: the bytes are downloaded, then hashed, then served.

### 2.3 Everything that assumes a local filesystem

| Where | Assumption today | Change |
|---|---|---|
| `poller.BlobStore`, `DEFAULT_STORE` | Attachments written under `%USERPROFILE%\.po-agent\attachments`, as `<sha[:2]>/<sha><suffix>` | A store interface with two implementations: the existing local directory, and an **Azure Blob container** using the same name layout. Hosting uses the container. |
| `attachments.stored_uri` | Holds an **absolute Windows path** (for example `C:\Users\...\de\de23....pdf`) | Store a **store-relative key** (`<sha[:2]>/<sha><suffix>`) from now on, resolved through the store. Local reads still accept the absolute paths in existing run databases. The production database starts empty, so there is nothing to migrate there. |
| `extract_pending` → `ingest_shipment(paths=...)` | Parsers open files by path | Download each attachment to a per-run temporary directory and pass those paths. `display_names` already carries the vendor's filename, so nothing depends on the path's name. Delete the temporary files when the run ends. |
| `review_app` `/source/{change_id}` | Serves a file under `--blob-root` | Read the blob through the store, **verify its SHA-256 against `source_sha256`, then stream it**. |
| `config.unrecorded_writes_path()` | `~/.po-agent/unrecorded-writes.jsonl` | An append blob (§5). |
| `NetSuiteConfig.from_env`, and its consumer | `NS_PRIVATE_KEY_PATH` must be an existing file. `NetSuiteConfig.private_key_path` is a `Path`, and the client reads it with `read_bytes()` when it signs | Also accept the key's PEM text from a setting (`NS_PRIVATE_KEY_PEM`, new) filled by a Key Vault reference. Exactly one of the two may be set, and setting both is refused. **The dataclass field and the signing code change too**, not only `from_env`, and the existing tests that build configs from paths must still pass. |
| `GraphConfig.from_env`, and its consumer | `GRAPH_CERT_PATH` and `GRAPH_CERT_PUBLIC_PATH` must be files, and the pair is validated. `GraphConfig.cert_path` and `cert_public_path` are `Path`s, and `graph_client` reads both with `read_text()` when it acquires a token | Accept `GRAPH_CERT_PEM` (private, from Key Vault) and `GRAPH_CERT_PUBLIC_PEM` (public), with the **same pair-and-thumbprint validation**. That validation is what found a missing certificate before (`graph_client.py` docstring), and it must not be lost. **The fields and the token code change too**, not only `from_env`. |
| Home-directory defaults: `poller.DEFAULT_STORE`, `config.unrecorded_writes_path`, the review app's `--blob-root`, `claude_extractor.SECRETS_ENV_PATH` and its `ANTHROPIC_CONFIG_DIR` fallback | `%USERPROFILE%` (`DEFAULT_STORE`, through `expandvars`) and `Path.home()`. On Linux, `%USERPROFILE%` does not expand, and `DEFAULT_STORE` becomes a literal relative path | Hosting sets every one of these explicitly, and **the default store must never be constructed on the Function App**. Make the hosting settings loader refuse to start rather than fall back to a home-directory default. These are environment reads too (`USERPROFILE`, `HOME`), even though no `os.environ` call names them. |
| `claude_extractor._load_secrets_env` | Reads `~/.po-agent/.env` on import | No change. It is a silent no-op when the file is absent. In hosting `ANTHROPIC_API_KEY` comes from app settings. |
| `config.load_env_file(".env")` | Reads `.env` from the working directory | No change: absent in hosting, so a no-op. **No `.env` file may be deployed.** Add it to the deployment's exclusions (§9). |

**The writable local disk on Flex is temporary, and nothing durable may live on it.** Everything in the "Change" column above is durable elsewhere. Only the per-run download directory uses local temporary storage. **CHECK IN PORTAL:** the temporary storage size on the instance memory size chosen. For scale, all seven test emails together were about 10 MB fetched (RUNBOOK §6 item 29).

### 2.4 Everything that assumes a terminal

- **The cold-start refusal in `poller.main`.** It prints a menu (`--since` / `--from-beginning` / `--dry-run`) and returns 2. In hosting:
  - the first run reads `PO_AGENT_POLL_SINCE` (new, an ISO timestamp);
  - with no watermark and no such setting, it **logs an ERROR and does nothing**, which is fail-closed, and the alert in §9 picks it up;
  - it never defaults to the whole mailbox (§8: the mailbox holds seven test messages).
- **`print` summaries** (`PollReport.summary()`, `ExtractionReport.summary()`) become `logger.info`.
- **`logging.basicConfig`** is called only inside the `main()` functions, so the Functions host's logging is not disturbed. Keep it that way.
- **The review app's start-up line, "Reviewing as …"** does not apply in hosting. The page header's "Reviewing as" comes from `settings.reviewer` today, and comes from the request instead (§4.3).
- **`PO_AGENT_REVIEWER` refusing to start** is replaced in hosting by §4.4.
- **Two banners that only the operator can clear, and hosting removes the way to clear them.** Both have the same shape: a banner the approver sees on every page, an action only the operator can take, and no hosted way to take it. **One decision covers both (decision 10).**
  - **The unrecorded-writes banner** (RUNBOOK §9, §11 decision 2).
    - It shows while the unrecorded-writes record lists a write that reached, or may have reached, NetSuite with no `write_attempts` row.
    - Clearing it means the operator checks each listed line in NetSuite, then archives the file. In hosting the file becomes an append blob (§5), and "archive" becomes a copy-then-reset done by hand in the storage account.
    - **It also carries an instruction for the approver:** "Don't approve those POs again until Kiko has checked them." So, unlike the other banner, it cannot be wholly addressed away from her.
  - **The unprocessable-message banner** (migration 0018; RUNBOOK §6 item 33 and §9).
    - It shows an email whose attachment cannot be read as a file until someone runs `poller.py --resolve-unprocessable <id> --by <name> --note <text>` against the database.
    - It says "Kiko clears this message once that's done", so it is implicitly addressed to the operator. Its only instruction for the approver is to ask the sender for the files again.
  - **What breaks once hosted, for both.**
    - **The operator's action needs hand-run access to production.** The unprocessable banner needs a workstation writing to `sqldb-po-agent`. The unrecorded-writes banner needs someone resetting a blob in the storage account. The only hand-run access this plan provides for is migrations (§8.2).
    - **The person who sees the banners is the approver, Paula, not the operator.** They sit on every page she uses, and most of what they ask is not hers to do.
    - **Neither clear records an authenticated identity.** `--by` is a typed label, like `PO_AGENT_REVIEWER`, and archiving a file records nobody. That is fine for an operator at a terminal, but it is the item 8 problem again if it becomes the hosted path.
  - **DECISION (Kiko), before hosting. Choose one, for both banners:**
    - **(A) Move each clear action into the review app, behind the sign-in.** It records the signed-in identity in the same way, and with the same fail-closed rules, as a verdict (§4.3-§4.4), and requires a reason.
      - **Unprocessable message:** a button that writes the existing `MESSAGE_UNPROCESSABLE_RESOLVED` audit event and sets `unprocessable_resolved_at`.
      - **Unrecorded writes:** a "reconciled" action per entry. It must not edit or delete the record. Instead it appends a reconciliation line to the same append blob, naming the entry, the signed-in person and their reason. The banner then shows entries with no such line. Archiving stops being a manual blob operation.
      - **Who may clear is part of this.** Neither clear changes NetSuite, so neither need be limited to `PO_AGENT_APPROVERS`. But the production approver list is Paula alone (§4.2), so an operator not on it could not clear either, unless a separate list exists. This is the same shape as the view-only question in §4.2: answer them together.
    - **(B) Address each banner explicitly, so the approver knows what is hers and what is not.**
      - **Unprocessable message:** addressed to the operator. It says plainly that this is for Kiko, that nothing on the page is affected, and that she need not act beyond asking for the files again.
      - **Unrecorded writes:** split. The approver part ("don't approve these POs again") stays addressed to her. The rest is marked as the operator's.
      - Clearing stays an operator action for both. Each then needs a sanctioned, recorded way to reach production (the database for one, the storage account for the other), written into the RUNBOOK.
      - The operator also needs to learn the banners are showing without being in the app. That is the CRITICAL unrecorded-write log line and the poller's ERROR line, picked up by the §9 alerts.
  - **Recommendation: (A).** The banners are shown in the app, so the actions that clear them belong there. A signed-in identity is a better record than a typed name or an archived file. And it keeps production free of hand-run writes outside migrations.
  - Either way, **neither banner may ever be cleared by anything that is not a person's recorded decision.** No timeout and no auto-expiry: for the unprocessable banner that is the silent skip migration 0018 exists to prevent, and for the unrecorded-writes banner it is a write NetSuite holds that nothing records.

### 2.5 Database connections in a long-lived worker

- `schema.connect(url)` is a bare `create_engine(url)`. Its docstring claims it switches on SQLite's foreign keys, but the body does not. Whoever adds a hosting engine factory should not rely on that docstring. A Function App worker holds that engine for hours between timer runs and page views, and Azure SQL closes idle connections. The first query after an idle spell would then fail on a dead pooled connection.
- **Change:** the hosting engine gets `pool_recycle` (well under the server's idle cutoff) and `pool_pre_ping=True`.
- **This collides with a CLAUDE.md rule:** `pool_pre_ping` may be enabled only if its silent replacements are counted and printed beside the connection-retry count. That rule was written for the **test harness**, where an uncounted replacement can turn a bad link into a green run. In hosting the same principle applies to logs:
  - attach a pool event listener that counts and logs each replacement;
  - leave the harness's engines untouched.
- **DECISION (Kiko):** confirm that this reading of the rule is acceptable before it is built.
- **Authentication:**
  - The test database is reached as a contained SQL user with a password in the URL.
  - For production, use the Function App's **managed identity**: a contained user created from the external provider, with ODBC Driver 18's `Authentication=ActiveDirectoryMsi`. `PO_AGENT_DB_URL` then carries no secret.
  - Give the runtime identity data rights only (read and write), not DDL. Migrations are run by hand with a separate identity (§8).

### 2.6 Python version and the ODBC driver

- **The local venv is Python 3.14.6.**
  - Microsoft Learn currently lists Python 3.10-3.14 as GA on Flex Consumption, with 3.15 in preview.
  - A mid-2026 Q&A thread reported remote builds failing for 3.14 on Flex.
  - **CHECK IN PORTAL:** `az functionapp list-flexconsumption-runtimes --location <region> --runtime python`, for the region `sql-po-agent-prod` is in.
  - Pick the highest GA version that builds there. Run the **full suite on that exact version** before deploying, on SQLite and once on `--mssql` (RUNBOOK §7).
- **The ODBC driver:**
  - Microsoft pairs Python 3.11+ with **ODBC Driver 18**.
  - Whether the driver is **preinstalled on the Linux Flex image is contradicted between sources**, and Flex does not run custom containers.
  - **CHECK IN PORTAL (spike, §9):** deploy a one-function app that logs `pyodbc.drivers()` and opens a connection to the test database.
  - If Driver 18 is absent, the fallback is a different SQL Server driver package, and that needs its own dual-dialect run (RUNBOOK §7, category E). Decide it then, not now.

---

## 3. Configuration: every value, and where it goes

**Rules:**
- No values in this document, the repo or any log.
- Secrets go in **Key Vault**, reached from app settings by Key Vault references through the Function App's managed identity.
- Non-secret identifiers are plain app settings.
- **No `.env` file is deployed.** The code's `.env` loading is a harmless no-op only while the file is absent.
- GUID-shaped values (tenant, client and object ids) go only in app settings, **never in tracked files**. `test_no_credentials_in_tracked_files` refuses them, rightly (CLAUDE.md gotchas).

### 3.1 Read today, from `.env`, `~/.po-agent/.env` or `.env.production`

| Variable | Read by | Hosting | Notes |
|---|---|---|---|
| `GRAPH_CLIENT` | `config.GraphConfig` | App setting `real` | No default in code, by design. Setting it explicitly keeps that rule. |
| `GRAPH_TENANT_ID` | `config.GraphConfig` | App setting | |
| `GRAPH_CLIENT_ID` | `config.GraphConfig` | App setting | Same app registration as today. Its RBAC-for-Applications scope to `shipments@` does not depend on where the call comes from. **CHECK IN PORTAL:** whether any Conditional Access policy for workload identities restricts this service principal by location. |
| `GRAPH_MAILBOX` | `config.GraphConfig` | App setting | |
| `GRAPH_CERT_PATH` | `config.GraphConfig` | **Replaced** by `GRAPH_CERT_PEM`, a Key Vault reference | The private key. Code change in §2.3. |
| `GRAPH_CERT_PUBLIC_PATH` | `config.GraphConfig` | **Replaced** by `GRAPH_CERT_PUBLIC_PEM`, an app setting | Public certificate, not secret. |
| `GRAPH_CERT_THUMBPRINT` | `config.GraphConfig` | App setting | Validated against the pair. |
| `GRAPH_CERT_KEY_ID` | **Nothing in the code reads it** (asserted by `test_config`) | Not deployed | The Entra credential entry's id: "documentation, not configuration", kept so that a key rotation knows which credential entry to remove (`GRAPH-SETUP.md`). Record it beside the Key Vault secret's notes instead. |
| `NS_ACCOUNT_ID` | `NetSuiteConfig` | App setting | Sandbox 2's id for the hosted rehearsal, production's at cutover (§9). `.env.production` holds the production set. |
| `NS_CLIENT_ID` | `NetSuiteConfig` | App setting | Per account, like the above. |
| `NS_CERTIFICATE_ID` | `NetSuiteConfig` | App setting | Per account. |
| `NS_PRIVATE_KEY_PATH` | `NetSuiteConfig` | **Replaced** by `NS_PRIVATE_KEY_PEM`, a Key Vault reference | One key per account. Code change in §2.3. |
| `NS_PRIVATE_KEY_PASSPHRASE` | `NetSuiteConfig` | Key Vault reference, only if the key is encrypted | Not set in `.env` today. |
| `NS_JWT_ALGORITHM` | `NetSuiteConfig` | App setting | Defaults to PS256. |
| `NS_HTTP_TIMEOUT` | `NetSuiteConfig` | App setting `60`, or left unset | Defaults to 60 s. §6 is computed on 60. Do not lower it to fit the HTTP limit; that is what the write deadline is for. |
| `ANTHROPIC_API_KEY` | `claude_extractor` (via `~/.po-agent/.env`) | Key Vault reference | |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_CONFIG_DIR` | `claude_extractor.credentials_available` | Not set | Alternatives to the API key. Unused here. |
| `PO_AGENT_DB_URL` | `review_app/__main__`, `migrations/env.py` | App setting | `mssql+pyodbc://` to `sqldb-po-agent`, with managed-identity authentication (no password, §2.5). It is deliberately unset in `.env` today (RUNBOOK §12 item 3), and this is where §12 item 3 is discharged. |
| `PO_AGENT_ALLOW_PRODUCTION_WRITES` | `write_path` | **Unset until §12 item 5**, then the production account id | Leaving it unset keeps every production write refused in code. |
| `PO_AGENT_UNRECORDED_WRITES` | `config`, used by `write_path` and the review app | App setting: the append blob's URL (§5) | One setting read by both, in one app. That is RUNBOOK §11's "the writer and the review app must see the same file", satisfied structurally. |
| `PO_AGENT_REVIEWER` | `review_app.settings` | **Must NOT be set** | Replaced by §4. The hosted app refuses to start if it is set (§4.4), so a leftover cannot silently name the reviewer. |
| `PO_AGENT_TEST_DB_URL` | `dialect_target` | **Never set in hosting** | The harness drops every table on whatever it names. |
| `PO_AGENT_MIGRATE_CONFIRM` | `migrations/env.py` | **Never set in hosting** | Migrations are run by hand (§8). |

### 3.2 New settings this plan introduces

| Variable | Hosting | Purpose |
|---|---|---|
| `PO_AGENT_AUTH_MODE` | App setting `easyauth` | Selects identity from the sign-in (§4). Absent means local mode, which serves only on 127.0.0.1 with `PO_AGENT_REVIEWER`. |
| `PO_AGENT_APPROVERS` | App setting | Entra **object ids** allowed to record a verdict (§4.3). Values never go in tracked files. |
| `PO_AGENT_CSRF_KEY` | Key Vault reference | Key for the per-user CSRF token (§2.2). |
| `PO_AGENT_ATTACHMENT_STORE` | App setting | The attachments container's URL. Access is through the managed identity. |
| `PO_AGENT_POLL_SINCE` | App setting, used for the first run only | The cold-start boundary (§2.4, §8).<br><br>**The timer wrapper must pass it as `since` ONLY when no watermark exists.** `poll_once(since=...)` overrides the watermark entirely, on every run (`poller.py`). Passed unconditionally, it re-reads from the cutover moment every hour, silently, because dedup makes the re-read look clean.<br><br>Test the effect, not the acceptance (CLAUDE.md, the `--from-beginning` gotcha): with a watermark present, the setting must not change the window. |
| `PO_AGENT_EXTRACT_LIMIT` | App setting | Messages extracted per timer run (§6). |
| `NS_PRIVATE_KEY_PEM`, `GRAPH_CERT_PEM`, `GRAPH_CERT_PUBLIC_PEM` | As in §3.1 | Replace the three path settings. |

### 3.3 Platform settings (not read by this code)

These are set by the platform or the deployment:
- `AzureWebJobsStorage`, identity-based. The timer's single-instance lock lives here, and the app's identity needs **Storage Blob Data Owner** on it.
- the Application Insights connection string;
- the Flex runtime and version, which are set on the plan, not as `FUNCTIONS_WORKER_RUNTIME`.

**CHECK IN PORTAL:** which classic app settings Flex rejects, when the app is created.

### 3.4 What the Function App's managed identity needs

- **Key Vault Secrets User** on the vault.
- **Storage Blob Data Owner** on host storage.
- **Storage Blob Data Contributor** on the attachments and unrecorded-writes containers.
- **A contained database user in `sqldb-po-agent`** with data rights only.
- **Nothing for NetSuite or Graph.** Both keep their certificate credentials, now loaded from Key Vault.

---

## 4. How the reviewer's identity reaches `verdict_by` (cutover item 8)

### 4.1 The mechanism

**App Service Authentication ("Easy Auth"), Microsoft Entra provider, configured on the Function App:**
- Require authentication.
- Unauthenticated browser requests are redirected to sign-in.
- The platform then passes the signed-in user to the app in request headers. `X-MS-CLIENT-PRINCIPAL` carries the claims as Base64-encoded JSON, with companion `-ID` and `-NAME` headers.
- **External requests cannot set these headers.** Only the platform does, and that is what makes them usable.

**CHECK IN PORTAL:** Easy Auth on Flex Consumption.
- A 2024 Microsoft answer said it was not enabled on Flex. The current migration guidance and recent user reports describe it working, but no Flex-specific page states it.
- Confirm it in the first spike.

### 4.2 Restricting it to named users: two layers

1. **Entra:** on the enterprise application behind the app's sign-in, set **Assignment required = Yes** and assign the named users. Anyone else is refused at sign-in. Easy Auth's `allowedPrincipals` (checked against the `oid` claim) is an equivalent platform-side list.
2. **In the app:** `PO_AGENT_APPROVERS`, a list of object ids. A signed-in user not on it gets a page saying they are not an approver, and nothing is recorded.

**Why both:** layer 1 is configuration in a portal nobody re-reads; layer 2 is tested code.

**Paula's ruling** (sole approver, no delegation; RUNBOOK §6 item 30) means **the production list is Paula alone**.

**DECISION (Kiko):** does anyone else need to *see* the queue (Kiko, for operations)? The app has no read-only mode, so anyone it admits can approve. Options:
- admit only Paula;
- build a view-only role (a small change: an allowlist for viewing, a separate one for verdicts).

For the hosted rehearsal against Sandbox 2 (§9), the approver list is Kiko, set in that environment's settings only.

### 4.3 From the request to the database

**Code change, the core of item 8.** A new `review_app/identity.py` reads the principal on every request that records a decision: approve, reject and leave as it is, which are the three `data` functions that take a reviewer today. "Send again" records no reviewer, but it writes to NetSuite, so gate it on an allow-listed sign-in as well. It:
- decodes `X-MS-CLIENT-PRINCIPAL`;
- takes the object id (`oid`, or its mapped claim name), the display name and the sign-in name (UPN);
- **refuses the request** if the header is missing, cannot be decoded, has no object id, or the object id is not in `PO_AGENT_APPROVERS`;
- returns a `Reviewer(oid, name, upn)`.

**What gets recorded:**
- `data.approve`, `data.reject` and `data.give_up` take that reviewer **from the request**, not from `settings.reviewer`.
- **`verdict_by`, `quantity_approved_by` and the `audit_log.actor` of `REVIEW_APPROVED`, `REVIEW_REJECTED` and `WRITE_GIVEN_UP`** record a readable form: `Name <upn>`. All three columns are `Unicode(320)`. SQL Server raises on an over-long value rather than truncating it, which would roll back the whole decision. So `identity.py` truncates the display name, never the UPN, to fit.
- The audit event's `detail_json` also records the **object id**, because names and UPNs change and the object id does not.
- No migration is needed.

**The page shows the signed-in reviewer.** "Reviewing as" comes from the request, not from start-up configuration.

### 4.4 Fail closed

- **Hosted app, at start-up:** if `PO_AGENT_AUTH_MODE=easyauth` and `PO_AGENT_REVIEWER` is set, refuse to start. If the mode is anything else while not bound to loopback, refuse to start.
- **Hosted app, per request:** no principal means no page, not a default.
- **DECISION (Kiko), defence in depth:** should the app also **validate the Entra ID token itself**? With the token store on, Easy Auth forwards it. The app would check its signature, issuer, tenant and audience. This holds even if Easy Auth is ever mis-set to allow anonymous traffic. Whether the principal headers are still stripped from outside traffic when Easy Auth is *off* is not documented in what was checked. Recommendation: **yes**. It is a small amount of code, and it removes the one way the identity could be forged.
- **Local mode is unchanged:** `PO_AGENT_REVIEWER`, loopback only. That is acceptable for development, never for production. Add a refusal so that **local mode will not start against a database whose name is `sqldb-po-agent`**, the same shape as the harness's name guard.

### 4.5 "Done" for item 8 (from RUNBOOK §12)

- Every reviewer field comes from the authenticated sign-in.
- `PO_AGENT_REVIEWER` decides nothing in hosting.
- **A test fails if a verdict can be recorded without an authenticated, allow-listed reviewer.** Cover: no header, a malformed header, an object id not on the list, and `PO_AGENT_REVIEWER` set in hosted mode.
- The tests forge the header. That is legitimate in tests, and exactly what the platform guarantees cannot happen from outside.

---

## 5. Durable storage for the unrecorded-writes record

**What it is for (RUNBOOK §11 decision 2; CLAUDE.md gotchas).** When a write reaches NetSuite and its `write_attempts` row cannot be written, the database is the thing that just failed. So the record goes somewhere else. The review app shows a banner while an entry says the write reached, or may have reached, NetSuite. A Function App's local disk is not durable and is not shared between instances, so the file cannot stay a file.

**Plan: an Azure Storage append blob**, one line of JSON per entry, the same format as today:
- `write_path` appends a block per entry. Append blobs take concurrent appends from several instances.
- The review app reads the blob for the banner, through the same `read_unrecorded_writes` parsing, which keeps unparseable lines rather than dropping them.
- `PO_AGENT_UNRECORDED_WRITES` holds the blob URL. Its handling becomes "a path locally, a blob URL in hosting", behind one small interface. The local file path keeps working.
- **It is deliberately not Azure SQL.** It must survive exactly the failure it records.
- **The existing last resort stays.** If the append also fails, `write_path` logs the entry in full at CRITICAL. In hosting that line lands in Application Insights, so **an alert on it is part of this plan** (§9). It is the only record in that case.
- **Protect the blob:**
  - turn on soft delete and versioning on its container;
  - grant the app's identity data rights on that container only;
  - "clear it by archiving" (RUNBOOK §9) becomes copy-then-reset, done by hand, unless decision 10 moves clearing into the review app (§2.4).

**CHECK IN PORTAL:** whether the storage account should be separate from the Function App's host storage. Recommendation: one account is acceptable, with separate containers and separate permissions.

---

## 6. Timeouts

### 6.1 The figures the timeouts must clear (from the code and RUNBOOK §11)

**One NetSuite request** (`netsuite_client._request`):
- up to 3 attempts, each with a 60-second timeout, plus 0.5 s and 1.0 s of backoff;
- so **about 181 s worst case** per request, plus a token request if one is due.

**One Approve** (`write_po_batch`):
- an opening read, up to `MAX_BATCH_REQUESTS = 3` PATCHes, and up to `CLOSING_READ_ATTEMPTS = 2` closing reads;
- so up to 6 requests, and **about 18 minutes in the theoretical worst case**.

**What has been measured:**
- about 11 s for a PO of any size;
- a 60-second stall on about 1 request in 15 (2026-11-02), production reads included;
- so one stall makes an Approve about 71 s, and two about 131 s.

**Extraction:**
- `claude_extractor` allows up to **900 s per model call**;
- one email can take several calls (classification, extraction, cross-checks).

### 6.2 The two limits that matter

**`functionTimeout` (host.json): set it to `01:00:00`.**
- Flex's default is 30 minutes, and there is no enforced maximum.
- One hour is comfortably above the 60-second stall and above the theoretical 18-minute Approve, so a write never dies mid-batch on the app's own timeout.
- It also lines up with the 60-minute grace the platform gives a run during scale-in.
- **A run cut short is safe:**
  - the poller stores each message in its own transaction;
  - `ingest_shipment` commits in separate steps: attachment and message bookkeeping, then the per-sheet verdicts, then the shipment and every row under it in one transaction. So a kill leaves no shipment or a complete one, **never a partial shipment**;
  - `extract_pending` marks the message extracted only after that. An unfinished message is taken again by the next run, and the duplicate check skips a shipment that did complete;
  - stored classification verdicts are replayed, not re-asked. RUNBOOK §6 item 31 reads the ingest steps this way; that is a reading of the code, not a test.
- **`PO_AGENT_EXTRACT_LIMIT` keeps each run short.** Start at 3 messages per run. The next hourly run continues.
- **CHECK, in the hosted rehearsal:** one unresolved 2026 Q&A thread reports a Flex timer stopped after 3-4 minutes despite a longer `functionTimeout`. Watch for it. The design tolerates it, but the run would make little progress.

**The HTTP limit: 230 seconds, whatever `functionTimeout` says.**
- An HTTP-triggered function that has not responded within 230 s loses the connection at the load balancer.
- Approve is synchronous by decision (RUNBOOK §11, Phase 4 decision 2), so an Approve that meets four or more stalls exceeds it.
- **What happens if it does:**
  - the browser gets an error page;
  - the invocation keeps running and finishes the write, because `functionTimeout` is an hour;
  - `write_attempts` records each line;
  - the result page is rebuilt from those records on the next load.
- **Nothing is lost, but the screen lies by omission.** A reviewer seeing an error may approve again; the fingerprint check refuses that, with a confusing message.
- **DECISION (Kiko), two options:**
  - **(A) A write deadline inside `write_po_batch`** (recommended). After about 180 s it sends no new request. Every line not yet confirmed is recorded with the outcomes that already exist: approved but not sent, or "check the line in NetSuite before trying again". The response returns before 230 s, and "send again" is already the way forward. It keeps decision 2 and adds no new state.
  - **(B) Asynchronous writes:** Approve records the verdicts and queues the write, and the result page polls. This reverses decision 2 and is more to build.
- Either way, keep the existing progress messages at 20 s and 75 s, and add one before the deadline. They are client-side script in `review_app/templates/base.html`, so the new message is a template change, not a `write_path` one.

---

## 7. The poller's timer schedule

**Recommendation: hourly, at a quarter past, in UTC.** The NCRONTAB is `0 15 * * * *`, with `run_on_startup=False` and `use_monitor=True`.

- **Why that cadence is enough.**
  - A slip is emailed when a container **ships**, weeks before it arrives (sea freight; RUNBOOK §4), and nothing is received against the PO in that time.
  - Volume is 10-20 emails a week.
  - So an hour's delay costs nothing. The architecture doc's "every 15-30 minutes" pre-dates knowing that slips come at dispatch.
- **Why it is not slower.** Hourly keeps Paula's queue current within her working day. It also keeps each run small (§6). A poll of an unchanged mailbox is one Graph request from the watermark.
- **Why not on the hour:** to stay off the platform's busiest minute.
- **Time zone.** On Linux Flex Consumption, `WEBSITE_TIME_ZONE` and `TZ` are **not supported**, and Microsoft warns that setting them can cause SSL and metrics problems. Schedules are UTC, which does not matter for an hourly schedule.
  - It will matter for the daily digest (Phase 4 decision 1, not built). That needs a UTC time chosen ahead of Paula's working morning, which shifts by an hour across daylight saving.
  - **CHECK:** Paula's time zone, when the digest is built.
- **Concurrency.** A timer trigger runs on one instance at a time, through a blob lease in host storage, and does not start again while a run is still going. So two runs never interleave on the watermark.

---

## 8. What stays local, what moves

### 8.1 Moves to Azure

| | Where |
|---|---|
| Polling and extraction | The timer function |
| The review app | The HTTP side of the Function App, behind sign-in |
| Attachment bytes | The blob container (§2.3) |
| The unrecorded-writes record | The append blob (§5) |
| Secrets (NetSuite and Graph private keys, Anthropic API key, CSRF key) | Key Vault |
| **The system of record** | **Azure SQL `sqldb-po-agent`**, migrated to head (0018 today) by hand before the app first runs against it |

### 8.2 Stays local

- **Development and the tests.** SQLite for the fast loop; the full `--mssql` suite against `sqldb-po-agent-test` before a merge (RUNBOOK §7). The `-test` and not-`dbo` harness guards are unchanged.
- **Migrations, run by hand.** From a workstation, with `PO_AGENT_DB_URL` and `PO_AGENT_MIGRATE_CONFIRM=sqldb-po-agent` set for that one command, as a database user with DDL rights. That is a separate identity from the app's data-only user. **Never from the Function App:** an app that can change its own schema at start-up has no human check on it.
- **The read-only scripts:** `scripts/probe_graph_auth.py`, `scripts/read_po_line_states.py` (the production re-read, §12 item 4) and `scripts/estimate_pre_existing_receipts.py`.
- **Key generation** (`generate_m2m_keypair.py`). Its outputs are imported into Key Vault, and the local copies stay outside OneDrive as now.
- **The run databases** in `~/po-agent-runs/`, as records of their runs (RUNBOOK §5).

### 8.3 `po_agent.db` is retired at this point

- **What it holds:** the 2026-09-14 hand-run poll of the seven test messages in `shipments@`, their extraction against Sandbox 2, and that mailbox's watermark.
- **Nothing in it is production state.** Its proposals are Sandbox 2's (RUNBOOK §8 lesson 31), and its messages are test forwards.
- **Do not copy anything from it into `sqldb-po-agent`.**
- Keep the file as a record: move it into `~/po-agent-runs/`.
- **DECISION (Kiko):** also remove the `sqlite:///po_agent.db` defaults, so nothing reaches it by accident. They are in `alembic.ini` and in the `--db` defaults of `poller.py` and `extract_pending.py`. Make `--db` required, as the review app already does. Recommended.

### 8.4 The first hosted poll against production

- The production database has no watermark, so the first run is a cold start.
- `shipments@` holds the seven test messages, including the seeded "Shipment updates" that mixes three vendors.
- **Set `PO_AGENT_POLL_SINCE` to the cutover moment,** so they are never ingested as real mail.
- **DECISION (Kiko):** alternatively, empty the mailbox of test mail before cutover. Doing both is the safest.
- How vendor mail reaches `shipments@` (vendors redirected, or Paula forwarding) is still open (RUNBOOK §6 item 11). It needs settling before cutover, or the hosted poller has nothing to read.

---

## 9. Order of work (when it is built)

1. **Spike, in a throwaway Function App in the target region.** Confirm, before writing the real thing:
   - the Python version builds remotely;
   - ODBC Driver 18 is present and connects to `sqldb-po-agent-test` with the app's managed identity;
   - Easy Auth works on Flex in this tenant, and the principal header arrives;
   - `AsgiFunctionApp` takes a timer trigger beside the ASGI app;
   - temporary storage is writable, and how large it is.

   Delete the spike afterwards.
2. **Code changes (§2-§5), behind settings,** with the local mode unchanged. Each one with tests:
   - the store interface (local and blob);
   - the PEM settings;
   - the append-blob record;
   - identity from the request, with the item 8 tests;
   - the CSRF change;
   - the cold-start setting;
   - the pool settings and their counter;
   - the write deadline, if option A is chosen.

   SQLite for logic, `--mssql` for categories A-E (RUNBOOK §7), and the full suite on the hosting Python version before merging.
3. **Provision:**
   - resource group and region;
   - storage account (host storage, deployment container, attachments container, unrecorded-writes container);
   - Key Vault;
   - Application Insights;
   - the Function App with its managed identity;
   - the role assignments in §3.4;
   - the contained database user;
   - Easy Auth and the enterprise application's assignment.
4. **Alerts, which are part of the first deploy, not later** (build plan Phase 4 item 8):
   - the CRITICAL unrecorded-write log line;
   - a timer run that logs an ERROR, including the cold-start refusal;
   - no successful timer run for, say, three hours.
5. **Hosted rehearsal on Sandbox 2:**
   - NetSuite settings point at `1321665-sb2`;
   - **a separate rehearsal database**, never `sqldb-po-agent`, which stays ring-fenced (RUNBOOK §5). **DECISION (Kiko):** a third Basic database for staging, dropped after cutover (recommended), or rehearse in `sqldb-po-agent` and rebuild it empty before cutover;
   - the real mailbox, read-only;
   - Kiko as the only approver;
   - approve one PO end to end, and read the result page and the Sandbox 2 system note;
   - force one write past the deadline, and confirm the alerts fire.
6. **Cutover, by RUNBOOK §12.** Switch the NetSuite settings to production and `PO_AGENT_DB_URL` to `sqldb-po-agent`. Then items 4-8 in order:
   - item 4 (re-read production) before anything is approved;
   - items 5-6 at the moment of cutover;
   - item 7's production system-note check after the first write;
   - item 8 holds from the first request.

   The approver list becomes Paula alone.

---

## 10. To check in the portal, and decisions for Kiko

**CHECK IN PORTAL (facts the repo cannot give):**
1. **The region** of `sql-po-agent-prod`, Flex availability there, and the Python versions with remote build (`az functionapp list-flexconsumption-runtimes`).
2. **The production database's tier.** RUNBOOK §5 records Basic "as stated by Kiko", not measured. Confirm it, and its idle-connection behaviour, for `pool_recycle`.
3. **ODBC Driver 18 on the Flex Python image.** Sources conflict. Settled by the spike.
4. **Easy Auth on Flex in this tenant.** Sources conflict on Flex specifically. Also:
   - who holds the Entra role needed to create the enterprise application and set assignment;
   - **the user object ids** for `PO_AGENT_APPROVERS` (app settings only, never in the repo).
5. **Azure SQL network access.** The current firewall rules; whether "allow Azure services" is on; and whether a private endpoint with VNet integration is wanted (Flex supports VNet integration). Flex outbound addresses are not fixed.
6. **Whether a Key Vault and an Application Insights or Log Analytics workspace already exist,** and who receives alerts.
7. **Conditional Access for workload identities** on the Graph app registration's service principal: would calls from Azure be blocked?
8. **NetSuite:** any IP-address restriction on the integration record, the role or the account that would refuse calls from Azure's outbound addresses.
9. **Which classic app settings Flex rejects,** and the instance memory size and temporary storage per size.
10. **Whether Mail.Send has been granted** (Phase 4 decision 1). Not needed for this plan, but it decides when the digest can follow.

**DECISION (Kiko):**
1. **Approve beyond 230 s:** option A, a write deadline (recommended), or option B, asynchronous writes, which reverses Phase 4 decision 2 (§6).
2. **Validate the Entra ID token in the app as well as trusting Easy Auth's headers** (recommended, §4.4).
3. **Whether anyone besides Paula needs to see the queue in production,** and therefore whether a view-only role is built (§4.2).
4. **One always-ready instance for the HTTP group,** or accept cold starts (§2.2).
5. **The `pool_pre_ping` reading:** a counter in logs in hosting satisfies the CLAUDE.md condition, and the harness is untouched (§2.5).
6. **Where the hosted rehearsal's database lives** (§9 step 5).
7. **Retire the `po_agent.db` defaults,** making `--db` required (§8.3).
8. **Test mail in `shipments@` at cutover:** `PO_AGENT_POLL_SINCE`, emptying the mailbox, or both (§8.4).
9. **How vendor mail reaches `shipments@` in production** (RUNBOOK §6 item 11), before cutover.
10. **Clearing the two operator-only banners, before hosting:** the unrecorded-writes banner and the unprocessable-message banner, decided together (§2.4).
    - (A) Signed-in clear actions in the review app (recommended).
    - (B) Banners addressed explicitly, so the approver knows which part is hers, with clearing staying an operator action through a sanctioned route to production.

    Settle "who may clear" together with the view-only question (decision 3).

---

## Sources (Microsoft Learn, checked 2026-11-18)

- Flex Consumption plan (languages, instances, always ready): https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan
- Create and manage Flex Consumption apps: https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-how-to
- Functions hosting and scale (timeouts, the 230-second HTTP limit): https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale
- Runtime and language versions: https://learn.microsoft.com/en-us/azure/azure-functions/functions-versions and https://learn.microsoft.com/en-us/azure/azure-functions/supported-languages
- Timer trigger (single-instance lease, UTC, time-zone settings unsupported on Linux Flex): https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-timer
- HTTP trigger and `AsgiFunctionApp`: https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger and https://learn.microsoft.com/en-us/python/api/azure-functions/azure.functions?view=azure-python
- Easy Auth, user identities and headers: https://learn.microsoft.com/en-us/azure/app-service/overview-authentication-authorization, https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-user-identities, https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad
- Consumption to Flex migration (Easy Auth note, maximum instances below 40 for HTTP): https://learn.microsoft.com/en-us/azure/azure-functions/migration/migrate-plan-consumption-to-flex
- Key Vault references: https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references
- Storage considerations and file access on Flex: https://learn.microsoft.com/en-us/azure/azure-functions/storage-considerations, https://learn.microsoft.com/en-us/azure/azure-functions/concept-file-access-options
- ODBC driver on Linux: https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server?view=sql-server-ver17

**Community reports cited above (not official):**
- Easy Auth 503s on Flex: https://learn.microsoft.com/en-us/answers/questions/6023897/azure-functions-flex-consumption-intermittent-http
- A Flex timer cut short: https://learn.microsoft.com/en-sg/answers/questions/5900705/timer-trigger-on-flex-consumption-gets-terminated
- Python 3.14 remote build on Flex: https://learn.microsoft.com/en-us/answers/questions/5946219/python-3-12-version-is-unsupported-by-orx-build
- ODBC on Linux Functions: https://learn.microsoft.com/en-us/answers/questions/1531593/using-pyodbc-from-azure-function-do-i-need-to-inst
