"""
The routes. Thin: every decision lives in `review_app.data`.

Local-only by design: `__main__` binds 127.0.0.1. There is no login, so two
things stand in for one. Every page and every recorded verdict names the
configured reviewer (`Settings.reviewer`). And every POST carries a per-process
token: without it, any web page open in the same browser could submit an
approval to 127.0.0.1.

**Approve writes to NetSuite** (2026-11-02): it records the verdicts, then calls
`write_po_batch` -- the ONE function this package may take from the write side
(`test_review_app_never_imports_netsuite_client`) -- while the reviewer waits,
then redirects to the PO's result page, which is rebuilt from the database.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_app import data
from review_app.settings import Settings
from write_path import write_po_batch

logger = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parent / "templates"


def _qty(value) -> str:
    if value is None:
        return "–"
    text = format(value, "f") if not isinstance(value, str) else value
    return text.rstrip("0").rstrip(".") if "." in text else text


def create_app(settings: Settings, engine=None, now=None) -> FastAPI:
    import schema as sc

    engine = engine or sc.connect(settings.db_url)
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0))
    csrf = secrets.token_urlsafe(32)
    app = FastAPI(title="PO review", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["qty"] = _qty
    app.state.csrf = csrf

    def unrecorded_landed() -> list:
        """Writes that reached -- or may have reached -- NetSuite with no record: the
        worst outcome there is, so EVERY page says so until the file is reconciled.
        An entry that will not parse counts too: it might be one."""
        import config

        return [e for e in config.read_unrecorded_writes(settings.unrecorded_writes)
                if e.get("reached_netsuite") in (True, "unknown") or "unparsed" in e]

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        context.update(request=request, reviewer=settings.reviewer, csrf=csrf,
                       unrecorded=unrecorded_landed(),
                       unrecorded_file=settings.unrecorded_writes)
        return templates.TemplateResponse(request, name, context, status_code=status)

    def refuse(request: Request, exc: data.ReviewRefused) -> HTMLResponse:
        return page(request, "message.html", status=exc.status, title=exc.title,
                    message=str(exc), detail=exc.detail)

    def check_csrf(token: Optional[str]) -> None:
        if not token or not secrets.compare_digest(token, csrf):
            raise data.ReviewRefused(
                "This page has expired, so nothing was saved. Reload it and try again.", 403,
                detail="form token missing or not issued by this server process")

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request):
        with engine.connect() as conn:
            entries = data.list_queue(conn)
        # A PO with no open line at processing time was never actionable: it is
        # left out entirely, and nothing in the interface links to where it went.
        main = [e for e in entries if not e.view.no_action_possible]
        return page(request, "queue.html", entries=main, heading="Purchase orders to review")

    @app.get("/no-action", response_class=HTMLResponse)
    def no_action(request: Request):
        with engine.connect() as conn:
            everything = data.list_queue(conn)
        entries = [e for e in everything if e.view.no_action_possible]
        # Already received with every figure agreeing: on ANY PO, including ones still
        # in the queue for their other lines. The reviewer never sees these lines.
        no_action_lines = [(e.view, ln) for e in everything for ln in e.view.no_action_lines]
        # DIAGNOSTIC ONLY: reachable by URL, linked from nowhere.
        return page(request, "queue.html", entries=entries, no_action_view=True,
                    no_action_lines=no_action_lines,
                    heading="Diagnostic: nothing for the reviewer to do "
                            "(not shown to the reviewer)")

    @app.get("/po/{shipment_po_id}", response_class=HTMLResponse)
    def po(request: Request, shipment_po_id: str):
        with engine.connect() as conn:
            view = data.load_po(conn, shipment_po_id)
        if view is None:
            return refuse(request, data.ReviewRefused(
                "This PO isn't in your review list.", 404, title="Not found",
                detail=f"no shipment_pos row {shipment_po_id!r}"))
        with engine.connect() as conn:
            has_results = data.has_write_results(conn, shipment_po_id)
        return page(request, "po.html", view=view, has_results=has_results)

    def write_now(shipment_po_id: str) -> Optional[data.ReviewRefused]:
        """Write the PO's approved lines. None on a normal outcome -- refusals and
        NetSuite failures included, since those are per-line results, all of them
        recorded in the database (a refusal before NetSuite was contacted as an
        audit event), so the result page needs nothing from this call. A refusal
        to show only if the write path RAISED, which is a bug, not a NetSuite
        problem."""
        try:
            write_po_batch(engine, shipment_po_id)
        except Exception as exc:  # noqa: BLE001 -- shown and logged, never swallowed
            logger.exception("write_po_batch raised for shipment_po %s", shipment_po_id)
            return data.ReviewRefused(
                "Your approval is saved, but the tool hit an error while updating NetSuite, "
                "so it can't say which lines changed there. Open this PO and check its "
                "results before doing anything else, and let Kiko know.", 500,
                title="NetSuite may not have been updated",
                detail=f"{type(exc).__name__}: {exc}")
        return None

    @app.post("/po/{shipment_po_id}/approve")
    def approve(request: Request, shipment_po_id: str, csrf_token: str = Form(""),
                fingerprint: str = Form("")):
        try:
            check_csrf(csrf_token)
            data.approve(engine, shipment_po_id, reviewer=settings.reviewer,
                         fingerprint=fingerprint, now=clock())
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        failure = write_now(shipment_po_id)
        if failure is not None:
            return refuse(request, failure)
        return RedirectResponse(f"/po/{shipment_po_id}/result", status_code=303)

    @app.post("/po/{shipment_po_id}/write")
    def write_again(request: Request, shipment_po_id: str, csrf_token: str = Form("")):
        """Send the approved lines that aren't written yet. Per-line retry, no
        re-approval (partial-failure ruling, Paula 2026-09-16)."""
        try:
            check_csrf(csrf_token)
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        failure = write_now(shipment_po_id)
        if failure is not None:
            return refuse(request, failure)
        return RedirectResponse(f"/po/{shipment_po_id}/result", status_code=303)

    @app.post("/po/{shipment_po_id}/give-up")
    def give_up(request: Request, shipment_po_id: str, csrf_token: str = Form("")):
        """Leave the unwritten lines as they are in NetSuite: the reviewer's decision."""
        try:
            check_csrf(csrf_token)
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        data.give_up(engine, shipment_po_id, reviewer=settings.reviewer, now=clock())
        return RedirectResponse(f"/po/{shipment_po_id}/result", status_code=303)

    @app.get("/po/{shipment_po_id}/result", response_class=HTMLResponse)
    def result(request: Request, shipment_po_id: str):
        with engine.connect() as conn:
            view = data.load_po(conn, shipment_po_id)
            if view is None:
                return refuse(request, data.ReviewRefused(
                    "This PO isn't in your review list.", 404, title="Not found",
                    detail=f"no shipment_pos row {shipment_po_id!r}"))
            results = data.write_results(conn, shipment_po_id)
            retry = data.retryable_count(conn, shipment_po_id)
            unwritten = data.unwritten_count(conn, shipment_po_id)
        groups = [(key, heading, [r for r in results if r.group == key])
                  for key, heading in data.RESULT_GROUPS]
        return page(request, "result.html", view=view, retry=retry, unwritten=unwritten,
                    groups=[g for g in groups if g[2]])

    @app.post("/po/{shipment_po_id}/reject")
    def reject(request: Request, shipment_po_id: str, csrf_token: str = Form(""),
               fingerprint: str = Form(""), reason: str = Form("")):
        try:
            check_csrf(csrf_token)
            data.reject(engine, shipment_po_id, reviewer=settings.reviewer, reason=reason,
                        fingerprint=fingerprint, now=clock())
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        return RedirectResponse(f"/po/{shipment_po_id}", status_code=303)

    @app.get("/source/{change_id}")
    def source(request: Request, change_id: str):
        try:
            with engine.connect() as conn:
                path, name = data.resolve_source(conn, change_id, settings.blob_root)
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        return FileResponse(path, media_type="application/octet-stream", headers={
            "Content-Disposition": data.content_disposition(name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        })

    return app
