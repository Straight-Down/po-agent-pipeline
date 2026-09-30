"""
The Phase 3 review UI, v1: a local FastAPI app for Paula to approve or reject
proposed PO changes, one PO at a time.

**Nothing here writes to NetSuite, and nothing here can.** Approving sets
`proposed_changes.state = 'APPROVED'` and stops; the write path is a separate
piece of work. `test_review_app.test_review_app_never_imports_netsuite_client`
proves the package does not even import `netsuite_client`, directly or through
anything it imports.

Requirements are `PO-Update-Automation-Phase3-Requirements.md`; each section of
the per-PO page cites the entry it implements.

    set PO_AGENT_REVIEWER=<the person whose decision this records>
    .venv\\Scripts\\python -m review_app --db sqlite:///<database> ^
        --blob-root %USERPROFILE%\\.po-agent\\attachments
"""
