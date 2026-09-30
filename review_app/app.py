"""
The routes. Thin: every decision lives in `review_app.data`.

Local-only by design: `__main__` binds 127.0.0.1. There is no login, so two
things stand in for one. Every page and every recorded verdict names the
configured reviewer (`Settings.reviewer`). And every POST carries a per-process
token: without it, any web page open in the same browser could submit an
approval to 127.0.0.1.
"""

from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_app import data
from review_app.settings import Settings

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

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        context.update(request=request, reviewer=settings.reviewer, csrf=csrf)
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
            entries = [e for e in data.list_queue(conn) if e.view.no_action_possible]
        # DIAGNOSTIC ONLY: reachable by URL, linked from nowhere.
        return page(request, "queue.html", entries=entries, no_action_view=True,
                    heading="Diagnostic: POs with no open NetSuite line when processed "
                            "(not shown to the reviewer)")

    @app.get("/po/{shipment_po_id}", response_class=HTMLResponse)
    def po(request: Request, shipment_po_id: str):
        with engine.connect() as conn:
            view = data.load_po(conn, shipment_po_id)
        if view is None:
            return refuse(request, data.ReviewRefused(
                "This PO isn't in your review list.", 404, title="Not found",
                detail=f"no shipment_pos row {shipment_po_id!r}"))
        if view.refused_lines:
            return page(request, "refused.html", status=409, view=view)
        return page(request, "po.html", view=view)

    @app.post("/po/{shipment_po_id}/approve")
    def approve(request: Request, shipment_po_id: str, csrf_token: str = Form(""),
                fingerprint: str = Form(""), receipt_date: str = Form("")):
        try:
            check_csrf(csrf_token)
            data.approve(engine, shipment_po_id, reviewer=settings.reviewer,
                         receipt_date=receipt_date, fingerprint=fingerprint, now=clock())
        except data.ReviewRefused as exc:
            return refuse(request, exc)
        return RedirectResponse(f"/po/{shipment_po_id}", status_code=303)

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
