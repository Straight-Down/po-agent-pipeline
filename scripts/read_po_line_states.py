"""
What state would each of a shipment's proposed lines be in against THIS account?

Re-runs the matcher for one PO's proposals from a run database against the PO as
it stands in the NetSuite account named by an env file, and prints every line's
state beside the figures that decide it (`quantity`, `quantity_received`,
`isOpen`, `isClosed`). Built for the production re-verification of PO 1624: its
28 states depend on `quantity_received`, and every figure so far is a SANDBOX one.

    .venv\\Scripts\\python scripts\\read_po_line_states.py --po 1624 ^
        --db sqlite:///<run>.db --env-file <production>.env --expect production

**Read-only, enforced rather than promised.** The client's `update_po_line` is
replaced by one that raises, and every HTTP request is refused unless it is a GET
or the SuiteQL POST (a SELECT that resolves the PO number). Nothing here can
write to NetSuite, whatever account it points at.

**`--expect` is required and checked against the account id.** A sandbox figure
printed under a production heading is the failure this script exists to end, so
a mismatch refuses before any request is made.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _read_only(client) -> None:
    """Make `client` incapable of writing: no update method, no non-read request."""
    from netsuite_client import NetSuiteError

    def refuse_update(*_args, **_kwargs):
        raise NetSuiteError("read_po_line_states is read-only: update_po_line refused")

    original = client._request
    suiteql_url = client._require_live("read").suiteql_url

    def guarded(method, url, *args, **kwargs):
        if method.upper() == "GET" or (method.upper() == "POST" and url == suiteql_url):
            return original(method, url, *args, **kwargs)
        raise NetSuiteError(f"read_po_line_states is read-only: {method} {url} refused")

    client.update_po_line = refuse_update
    client._request = guarded


def _lines_from_run(engine, po_key: str) -> list[dict]:
    """The PO's proposed lines from the run database, as the matcher's input dicts."""
    from sqlalchemy import select

    from schema import proposed_changes, shipment_pos

    with engine.connect() as conn:
        rows = conn.execute(
            select(proposed_changes)
            .join(shipment_pos, proposed_changes.c.shipment_po_id == shipment_pos.c.id)
            .where(shipment_pos.c.po_number_key == po_key)
            .order_by(proposed_changes.c.key_style, proposed_changes.c.key_color,
                      proposed_changes.c.key_size, proposed_changes.c.key_recap_label)
        ).all()
    return [{
        "po_number": po_key, "style_number": r.src_style_text, "color": r.src_color_text,
        "size": r.src_size_text, "quantity": float(r.src_quantity_text),
        "confidence": r.extraction_confidence or "high", "note": r.extraction_note or "",
        "source_hint": r.source_hint, "recap_label": r.src_recap_label or "",
    } for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--po", required=True, help="PO number as printed, e.g. 1624")
    ap.add_argument("--db", required=True, help="the run database holding the proposals")
    ap.add_argument("--env-file", default=".env", help="NetSuite credentials for the account")
    ap.add_argument("--expect", required=True, choices=("production", "sandbox"))
    args = ap.parse_args(argv)

    from sqlalchemy import create_engine

    import ingest as ing
    import matcher as mt
    from netsuite_client import NetSuiteClient, NetSuiteConfig, po_number_key

    config = NetSuiteConfig.from_env(args.env_file)
    # `load_env_file` never overrides a variable already in the environment, so a
    # value exported in the shell (or loaded earlier from another file) silently
    # beats --env-file. The account check below would catch a sandbox ACCOUNT, but
    # not a production account paired with another account's client or key. So:
    # every required value must be the one the named file holds. Names only are
    # printed -- never a value.
    from dotenv import dotenv_values

    in_file = dotenv_values(args.env_file)
    overridden = [name for name in ("NS_ACCOUNT_ID", "NS_CLIENT_ID", "NS_CERTIFICATE_ID",
                                    "NS_PRIVATE_KEY_PATH")
                  if (os.environ.get(name) or "") != (in_file.get(name) or "")]
    if overridden:
        print(f"REFUSED: {', '.join(overridden)} in the environment differ from "
              f"{args.env_file} -- an exported variable is overriding the file. Unset "
              "it and re-run. No request made.")
        return 2
    actual = "sandbox" if config.is_sandbox else "production"
    if actual != args.expect:
        print(f"REFUSED: --expect {args.expect} but {args.env_file} names account "
              f"{config.account_id} ({actual}). No request made.")
        return 2
    client = NetSuiteClient(config=config)
    _read_only(client)

    engine = create_engine(args.db)
    key = po_number_key(args.po)
    lines = _lines_from_run(engine, key)
    if not lines:
        print(f"no proposals for PO {args.po} in {args.db}")
        return 1
    ns_lines, resolution = ing._fetch_po_lines(client, [key])
    info = resolution[key]
    print(f"ACCOUNT {config.account_id} ({actual.upper()}) -- PO {args.po} -> "
          f"{info['ns_tranid']} internal id {info['ns_internal_id']} [{info['status']}]"
          + (f": {info['detail']}" if info.get("detail") else ""))
    colour_lookups = {key: mt.build_colour_lookup(client, ns_lines.get(key, []), cache={})}
    # EMPTY history, deliberately. The run database's history is its own ingest
    # of these very lines, and `_line_history` counts an observed line as seen --
    # which silenced PRE_EXISTING_RECEIPT on all 28 PO 1624 lines when this was
    # first run. It also belongs to whichever account that run read. The question
    # here is "what would a first sight of this account's PO say", so: no history.
    changes = mt.build_proposed_changes(
        lines, NetSuiteClient(mock_data=ns_lines), colour_lookups=colour_lookups,
        line_history={})
    counts: dict[str, int] = {}
    print(f"{'style/colour/size':24} {'recap':7} {'slip':>5} {'line':>4} {'ns qty':>6} "
          f"{'recv':>5} {'open':>5} {'closed':>6} {'proposed':>8}  state / reason")
    for line, change in zip(lines, changes):
        counts[change.status] = counts.get(change.status, 0) + 1
        proposed = "-" if change.proposed_quantity is None else f"{change.proposed_quantity:g}"
        # EVERY NetSuite line matching this key, open or not -- the matcher's own
        # lookup. Showing only the line it chose printed "-" whenever no line was
        # open, and a closed line still has a quantity and a received quantity.
        # The line the matcher targeted, if any, is marked `*`.
        matches, *_ = mt._find_matching_lines(line, ns_lines.get(key, []),
                                              colour_lookups.get(key))
        rows = [(f"{m.line_id}{'*' if m.line_id == change.line_id else ''}",
                 f"{m.quantity:g}", f"{m.quantity_received or 0:g}", str(m.is_open),
                 str(m.closed)) for m in matches] or [("-", "-", "-", "-", "-")]
        for i, (line_id, qty, recv, is_open, closed) in enumerate(rows):
            first = i == 0
            label = line['style_number'] + '/' + line['color'] + '/' + line['size']
            print(f"{label if first else '':24} {line['recap_label'][:7] if first else '':7} "
                  f"{line['quantity'] if first else '':>5} {line_id:>4} {qty:>6} {recv:>5} "
                  f"{is_open:>5} {closed:>6} {proposed if first else '':>8}"
                  + (f"  {change.status}"
                     + (f" -- {change.attention_reason}" if change.attention_reason else "")
                     if first else ""))
    print(f"\n{len(lines)} line(s): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
