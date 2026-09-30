"""Configuration for the review app. Everything explicit; nothing defaults to a person."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REVIEWER_ENV = "PO_AGENT_REVIEWER"


class SettingsError(RuntimeError):
    """The app was asked to start without something it must not guess."""


@dataclass(frozen=True)
class Settings:
    db_url: str
    #: Where attachment bytes live. A source document is served only if its
    #: stored path resolves INSIDE this directory.
    blob_root: Path
    #: Whose decision a verdict records. Required, no default: with no auth this is
    #: the only attribution the audit trail gets, and a default would put a real
    #: person's name on clicks they did not make.
    reviewer: str


def reviewer_from_env() -> str:
    name = (os.environ.get(REVIEWER_ENV) or "").strip()
    if not name:
        raise SettingsError(
            f"{REVIEWER_ENV} is not set. The review app records every decision against "
            "this name, and there is no login to supply it, so it will not start without "
            "one. Set it to the person whose decisions are being recorded -- not to "
            "whoever happens to be at the keyboard."
        )
    return name

