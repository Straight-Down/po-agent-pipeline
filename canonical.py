"""
One canonical form for comparing vendor text to NetSuite text.

**A displayed value and a comparison key are different things.** Vendor strings
are emitted verbatim everywhere they are shown to a human or stored for audit —
"emit values exactly as printed" still holds. This module derives the *key* used
for grouping, deduplication and matching. Nothing here ever overwrites a source
value.

## Why a shared canonical form rather than a whitespace fix

The bug that prompted this was one double space: Symmetry's detail PDF prints a
colour as `NEW  INDIGO`, the extractor sometimes emitted it verbatim and
sometimes collapsed it, and `.strip().upper()` does not normalise internal
whitespace — so the same document produced different keys on different runs, and
the colour would fail to match NetSuite depending on the run.

That is one instance of a class. Others are already in this corpus or arriving:

  - **Full-width comma** — Legendz writes `PO#1657，M630018` (U+FF0C).
  - **Non-breaking space** — routinely produced by PDF text extraction.
  - **Ideographic space** — U+3000, in the CJK headers of two vendors' files.
  - **En-dash / em-dash / minus sign** — will hit the waist–inseam sizes
    (`32-34`) as soon as a numerically-sized style appears; NetSuite's list holds
    the ASCII-hyphen form.
  - **Full-width digits and letters** — `２Ｘ` vs `2X`.

Fixing only the double space guarantees a repeat, so the pipeline addresses the
class.

## The pipeline, and one documented departure from the original sketch

  1. **NFKC** — folds full-width forms to ASCII (retiring the full-width-comma
     and full-width-digit cases) and maps non-breaking space to a plain space.
  2. **Dash folding** — en-dash, em-dash, figure/horizontal bars, minus sign and
     their small/full-width variants all become ASCII hyphen-minus.
  3. **Zero-width folding** — the zero-width family becomes a space.
  4. **Whitespace collapse** — any run of whitespace becomes one space.
  5. **Strip**, then **casefold**.

Steps 2 and 3 are additions to the originally specified `NFKC -> collapse
whitespace -> strip -> casefold`. They are here because that sketch was
**verified not to cover two of its own stated targets**: NFKC leaves en-dash,
em-dash and minus sign untouched (`32–34` and `32-34` stay different), and leaves
zero-width characters untouched. Without these steps the waist–inseam sizes would
mismatch the moment they appear.

Zero-width handling is a judgement call worth stating: U+200B and friends are
mapped to a space rather than deleted, because in PDF-extracted vendor text they
appear where a visual gap exists, so `NEW​INDIGO`, `NEW INDIGO` and
`NEW  INDIGO` all canonicalise alike. If the true source really had no gap, the
result is a failed match, which routes to human review — the safe direction.

Casefolding means canonical forms are lowercase. They are keys, never displayed.
"""

from __future__ import annotations

import re
import unicodedata

#: Dash-like characters that all mean "hyphen" in a size or code. NFKC does NOT
#: fold these, which is why the map is explicit.
_DASHES = {
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus sign
    "﹘": "-",  # small em dash
    "﹣": "-",  # small hyphen-minus
    "－": "-",  # fullwidth hyphen-minus (NFKC handles this, kept for clarity)
}

#: Zero-width / invisible characters, mapped to a space. See the module docstring
#: for why a space rather than deletion.
_ZERO_WIDTH = {
    "​": " ",  # zero width space
    "‌": " ",  # zero width non-joiner
    "‍": " ",  # zero width joiner
    "⁠": " ",  # word joiner
    "﻿": " ",  # zero width no-break space / BOM
}

_TRANSLATION = str.maketrans({**_DASHES, **_ZERO_WIDTH})

_WHITESPACE_RUN = re.compile(r"\s+")


def canonical(value: object) -> str:
    """
    The comparison key for a vendor or NetSuite string.

    Never use the result as a displayed value — it is casefolded and
    whitespace-collapsed. Use it only to group, deduplicate or match.

    >>> canonical("NEW  INDIGO") == canonical("NEW INDIGO")
    True
    >>> canonical("32\\u201334") == canonical("32-34")
    True
    >>> canonical("PO#1657\\uff0cM630018")
    'po#1657,m630018'
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.translate(_TRANSLATION)
    text = _WHITESPACE_RUN.sub(" ", text)
    return text.strip().casefold()


def canonical_key(*values: object) -> tuple[str, ...]:
    """
    Canonical form of several fields as one tuple, for use as a dict key or a
    sort key. Order of arguments is the caller's contract.
    """
    return tuple(canonical(v) for v in values)


def same(a: object, b: object) -> bool:
    """Whether two strings are equal once canonicalised. Reads better than `==`
    at call sites that are asking a question about equivalence."""
    return canonical(a) == canonical(b)
