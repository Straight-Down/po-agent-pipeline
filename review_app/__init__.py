"""
The Phase 3 review UI, v1: a local FastAPI app for Paula to approve or reject
proposed PO changes, one PO at a time.

**Approve writes to NetSuite** (since 2026-11-02): it records the verdicts, then
calls `write_path.write_po_batch` -- one PATCH for the PO's approvable lines --
while the reviewer waits, and shows the result page. That one function is the
ONLY thing this package takes from the write side:
`test_review_app.test_review_app_never_imports_netsuite_client` pins its import,
its call and its signature, and fails on any other route to `netsuite_client`,
`matcher` or `ingest`.

Requirements are `PO-Update-Automation-Phase3-Requirements.md`; each section of
the per-PO page cites the entry it implements.

    set PO_AGENT_REVIEWER=<the person whose decision this records>
    .venv\\Scripts\\python -m review_app --db sqlite:///<database> ^
        --blob-root %USERPROFILE%\\.po-agent\\attachments
"""
