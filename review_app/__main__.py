"""Run the review app locally: `python -m review_app --db <url> --blob-root <dir>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv=None) -> int:
    from review_app.settings import Settings, SettingsError, reviewer_from_env

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("PO_AGENT_DB_URL"),
                    help="database URL (default: PO_AGENT_DB_URL)")
    ap.add_argument("--blob-root", default=str(Path.home() / ".po-agent" / "attachments"),
                    help="attachment store; documents outside it are never served")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    try:
        if not args.db:
            raise SettingsError("no database: pass --db or set PO_AGENT_DB_URL")
        settings = Settings(db_url=args.db, blob_root=Path(args.blob_root),
                            reviewer=reviewer_from_env())
    except SettingsError as exc:
        print(f"REFUSED TO START: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from review_app.app import create_app

    print(f"Reviewing as {settings.reviewer} -- http://127.0.0.1:{args.port}/")
    # 127.0.0.1 only: no auth exists, so this must never listen on the network.
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
