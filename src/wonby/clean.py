"""Pure cleaning / normalisation helpers — no I/O, no DuckDB.

See CLAUDE.md > Business rules for the exact semantics.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def parse_award_date(raw: str | None) -> date | None:
    """Day-first `D/M/YYYY` (no zero padding) -> date. `None` on anything else."""
    if not raw:
        return None
    m = _DATE_RE.match(raw)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def fiscal_year(d: date) -> int:
    """Singapore FY runs 1 Apr -> 31 Mar, named by its starting year."""
    return d.year if d.month >= 4 else d.year - 1


def parse_amount(raw: str | None) -> Decimal | None:
    """API amount string -> Decimal. `None` on anything that doesn't parse."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


# --- supplier name normalisation -------------------------------------------------

# Applied, in order, to the trailing tokens of the name *after* punctuation has
# been stripped and whitespace collapsed. Longest / most specific first so e.g.
# "PRIVATE LIMITED" is canonicalised before a bare "LIMITED" rule could fire.
_SUFFIX_CANON_RULES: tuple[tuple[str, str], ...] = (
    ("PRIVATE LIMITED", "PTE LTD"),
    ("PTE LIMITED", "PTE LTD"),
    ("LIMITED", "LTD"),
)

# Legal-form suffixes stripped (with the preceding word boundary) to get the
# prospecting "group" name. Only suffixes the normaliser itself canonicalises —
# LLP / CORPORATION / INC are left alone, they're distinct legal forms, not
# variants of the same one.
_GROUP_SUFFIXES: tuple[str, ...] = ("PTE LTD", "SDN BHD", "LTD")

# Country markers stripped for the group name.
_COUNTRY_MARKERS: tuple[str, ...] = ("(SINGAPORE)", "(SG)", "(S)")


def _strip_trailing_token(name: str, token: str) -> str | None:
    """Remove `token` from the end of `name` if it ends with it on a word
    boundary (preceded by whitespace or the token is the whole name). Returns
    None if there's no match, so callers can chain checks cheaply."""
    if name == token:
        return ""
    suffix = " " + token
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return None


def normalize_supplier_name(name: str) -> str:
    """Unicode NFKC, upper case, `.`/`,` removed, whitespace collapsed, legal
    suffix canonicalised. Suffix rules only ever strip/rewrite a whole trailing
    word — a firm literally named "... LIMITED EDITION" is untouched (LIMITED
    isn't at the end), and "INCOME" never gets mangled by an "INC" rule since
    there is no such substring rule, only whole-trailing-word ones."""
    text = unicodedata.normalize("NFKC", name or "")
    text = text.upper()
    text = text.replace(".", "").replace(",", "")
    text = re.sub(r"\s+", " ", text).strip()

    for token, canon in _SUFFIX_CANON_RULES:
        stripped = _strip_trailing_token(text, token)
        if stripped is not None:
            text = (stripped + " " + canon).strip() if stripped else canon
            break

    return text


def supplier_group(norm_name: str) -> str:
    """`supplier_name_norm` without its legal suffix or country marker — the
    prospecting key (before alias merges, see `apply_aliases`)."""
    group = norm_name
    changed = True
    while changed:
        changed = False
        for suffix in _GROUP_SUFFIXES:
            stripped = _strip_trailing_token(group, suffix)
            if stripped is not None:
                group = stripped
                changed = True
                break
        for marker in _COUNTRY_MARKERS:
            stripped = _strip_trailing_token(group, marker)
            if stripped is not None:
                group = stripped
                changed = True
                break
    return group


def apply_aliases(group: str, aliases: dict[str, str] | None) -> str:
    """Map a `supplier_group` through the manual alias table
    (`config/supplier_aliases.yaml`), unchanged if there's no entry."""
    if not aliases:
        return group
    return aliases.get(group, group)
