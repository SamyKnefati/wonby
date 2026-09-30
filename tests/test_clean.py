from datetime import date
from decimal import Decimal

import pytest

from wonby.clean import (
    apply_aliases,
    fiscal_year,
    normalize_supplier_name,
    parse_amount,
    parse_award_date,
    supplier_group,
)


# --- parse_award_date ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("6/9/2021", date(2021, 9, 6)),  # day-first, no zero padding: 6 Sept, not June
        ("06/09/2021", date(2021, 9, 6)),
        ("1/4/2021", date(2021, 4, 1)),
        ("31/3/2026", date(2026, 3, 31)),
    ],
)
def test_parse_award_date_valid(raw, expected):
    assert parse_award_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "not a date", "31/2/2021", "2021-09-06", "6/9/21", "  "],
)
def test_parse_award_date_garbage(raw):
    assert parse_award_date(raw) is None


# --- fiscal_year ---------------------------------------------------------------


@pytest.mark.parametrize(
    "d, expected_fy",
    [
        (date(2021, 4, 1), 2021),
        (date(2021, 9, 6), 2021),
        (date(2022, 3, 31), 2021),
        (date(2022, 4, 1), 2022),
    ],
)
def test_fiscal_year(d, expected_fy):
    assert fiscal_year(d) == expected_fy


# --- parse_amount --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2321600", Decimal("2321600")),
        ("310676.63", Decimal("310676.63")),
        ("0", Decimal("0")),
        ("1", Decimal("1")),
        ("16.53", Decimal("16.53")),
    ],
)
def test_parse_amount_valid(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "not a number", "N/A", "  "])
def test_parse_amount_garbage(raw):
    assert parse_amount(raw) is None


# --- normalize_supplier_name: suffix canonicalisation table -------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ALPHA ZETTA PTE. LTD.", "ALPHA ZETTA PTE LTD"),
        ("NXGEN COMMUNICATIONS PTE LTD", "NXGEN COMMUNICATIONS PTE LTD"),
        ("E M SERVICES PRIVATE LIMITED", "E M SERVICES PTE LTD"),
        ("ANALYSYS MASON PTE. LIMITED", "ANALYSYS MASON PTE LTD"),
        ("ANALYSYS MASON LIMITED", "ANALYSYS MASON LTD"),
        ("Cybiant Asia Sdn. Bhd.", "CYBIANT ASIA SDN BHD"),
        ("DT Economics LLP", "DT ECONOMICS LLP"),
        ("KPMG SERVICES PTE. LTD.", "KPMG SERVICES PTE LTD"),
        ("ZENITH INFOTECH (S) PTE LTD.", "ZENITH INFOTECH (S) PTE LTD"),
        ("INCOME INSURANCE LIMITED", "INCOME INSURANCE LTD"),  # not "OME..."
        ("BR LAW CORPORATION", "BR LAW CORPORATION"),
        ("Ernst & Young Advisory PTE. LTD.", "ERNST & YOUNG ADVISORY PTE LTD"),
        ("Frontier Economics Pty Ltd", "FRONTIER ECONOMICS PTY LTD"),
    ],
)
def test_normalize_supplier_name_suffix_table(raw, expected):
    assert normalize_supplier_name(raw) == expected


def test_normalize_supplier_name_collapses_whitespace_and_punctuation():
    assert normalize_supplier_name("  Foo,   Bar.  Pte.  Ltd.  ") == "FOO BAR PTE LTD"


def test_normalize_supplier_name_does_not_strip_mid_word():
    # "LIMITED" only at the very end, on a word boundary, gets canonicalised —
    # a firm literally named "... LIMITED EDITION" is untouched.
    assert normalize_supplier_name("WIDGETS LIMITED EDITION PTE LTD") == "WIDGETS LIMITED EDITION PTE LTD"


def test_normalize_supplier_name_does_not_mangle_income():
    # Regression: a naive substring-based "INC" suffix rule would turn
    # "INCOME" into "OME ..." (dropping the leading "INC"). There is no such
    # rule — only whole-trailing-word ones — so "INCOME" survives intact as
    # its own word. (Note "OME" is trivially a substring of "INCOME" itself;
    # the real check is that the word isn't split/truncated.)
    result = normalize_supplier_name("INCOME INSURANCE LIMITED")
    assert result == "INCOME INSURANCE LTD"
    assert "INCOME" in result.split()


# --- supplier_group -------------------------------------------------------------


def test_supplier_group_analysys_mason_pair():
    # The two Analysys Mason entities from IMD000ETT23000009 must collapse to
    # the same prospecting group.
    a = supplier_group(normalize_supplier_name("ANALYSYS MASON LIMITED"))
    b = supplier_group(normalize_supplier_name("ANALYSYS MASON PTE. LIMITED"))
    assert a == b == "ANALYSYS MASON"


@pytest.mark.parametrize(
    "raw, expected_group",
    [
        ("ALPHA ZETTA PTE. LTD.", "ALPHA ZETTA"),
        ("ZENITH INFOTECH (S) PTE LTD.", "ZENITH INFOTECH"),
        ("HUAY ARCHITECTS", "HUAY ARCHITECTS"),
        ("HUAY ARCHITECTS PTE. LTD.", "HUAY ARCHITECTS"),
        ("DT Economics LLP", "DT ECONOMICS LLP"),  # LLP not stripped, distinct legal form
        ("BR LAW CORPORATION", "BR LAW CORPORATION"),
        ("Cybiant Asia Sdn. Bhd.", "CYBIANT ASIA"),
        ("FTI CONSULTING (SINGAPORE) PTE. LTD.", "FTI CONSULTING"),
    ],
)
def test_supplier_group(raw, expected_group):
    assert supplier_group(normalize_supplier_name(raw)) == expected_group


def test_supplier_group_does_not_strip_country_name_inside_name():
    # Bare "SINGAPORE" (no parens) is not a marker per CLAUDE.md — only
    # "(S)", "(SINGAPORE)", "(SG)" are.
    group = supplier_group(normalize_supplier_name("SINGAPORE TELECOM PTE LTD"))
    assert group == "SINGAPORE TELECOM"


# --- apply_aliases ---------------------------------------------------------------


def test_apply_aliases_maps_known_group():
    aliases = {"ACCENTURE SG SERVICES": "ACCENTURE"}
    assert apply_aliases("ACCENTURE SG SERVICES", aliases) == "ACCENTURE"


def test_apply_aliases_passthrough_for_unknown_group():
    assert apply_aliases("SOME OTHER FIRM", {"X": "Y"}) == "SOME OTHER FIRM"


def test_apply_aliases_empty_table():
    assert apply_aliases("SOME FIRM", {}) == "SOME FIRM"
    assert apply_aliases("SOME FIRM", None) == "SOME FIRM"
