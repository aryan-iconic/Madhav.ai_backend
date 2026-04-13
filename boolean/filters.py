"""
boolean/filters.py
==================
Filter normalisation, validation and SQL clause generation.

Responsibilities:
  1. Normalise raw filter input (strip whitespace, coerce types,
     resolve court aliases, normalise act names)
  2. Validate filter combinations (year_from <= year_to, etc.)
  3. Build the SQL WHERE clauses for the result wrapper query
  4. Provide court alias lookup (so "SC" resolves to "Supreme Court of India")
  5. Provide act name fuzzy-match suggestions

This module is imported by executor.py (for building SQL)
and router.py (for normalising incoming request filters).

No DB calls in this file — pure logic.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any

from .exceptions import InvalidFilterError


# ─────────────────────────────────────────────────────────────────────────────
# Court alias resolution
# ─────────────────────────────────────────────────────────────────────────────

# Maps common abbreviations / shorthand → exact DB court name substrings
# The executor uses ILIKE %value% so partial matches work fine.
COURT_ALIASES: dict[str, str] = {
    # Supreme Court
    "sc":                           "Supreme Court of India",
    "supreme court":                "Supreme Court of India",
    "supreme court of india":       "Supreme Court of India",
    "sci":                          "Supreme Court of India",

    # High Courts
    "hc delhi":                     "High Court of Delhi",
    "delhi hc":                     "High Court of Delhi",
    "delhi high court":             "High Court of Delhi",

    "hc bombay":                    "Bombay High Court",
    "bombay hc":                    "Bombay High Court",
    "bombay high court":            "Bombay High Court",

    "hc madras":                    "Madras High Court",
    "madras hc":                    "Madras High Court",

    "hc calcutta":                  "Calcutta High Court",
    "calcutta hc":                  "Calcutta High Court",

    "hc allahabad":                 "Allahabad High Court",
    "allahabad hc":                 "Allahabad High Court",

    "hc karnataka":                 "Karnataka High Court",
    "karnataka hc":                 "Karnataka High Court",

    "hc kerala":                    "Kerala High Court",
    "kerala hc":                    "Kerala High Court",

    "hc gujarat":                   "Gujarat High Court",
    "gujarat hc":                   "Gujarat High Court",

    "hc punjab":                    "Punjab and Haryana High Court",
    "punjab hc":                    "Punjab and Haryana High Court",
    "punjab haryana hc":            "Punjab and Haryana High Court",

    "hc rajasthan":                 "Rajasthan High Court",
    "rajasthan hc":                 "Rajasthan High Court",

    "hc mp":                        "Madhya Pradesh High Court",
    "mp hc":                        "Madhya Pradesh High Court",

    "hc patna":                     "Patna High Court",
    "patna hc":                     "Patna High Court",

    # Tribunals
    "nclat":                        "NCLAT",
    "ncdrc":                        "NCDRC",
    "sat":                          "SAT",
    "itat":                         "ITAT",
    "cestat":                       "CESTAT",
    "drat":                         "DRAT",
    "armed forces tribunal":        "Armed Forces Tribunal",
    "aft":                          "Armed Forces Tribunal",
    "ngt":                          "National Green Tribunal",
    "national green tribunal":      "National Green Tribunal",
}


def resolve_court(raw: str) -> str:
    """
    Resolve a court alias to its canonical form.
    Returns the canonical name if alias found, otherwise returns raw (stripped).

    Examples:
        resolve_court("SC")          → "Supreme Court of India"
        resolve_court("delhi hc")    → "High Court of Delhi"
        resolve_court("Bombay HC")   → "Bombay High Court"
        resolve_court("Custom Court")→ "Custom Court"  (passthrough)
    """
    cleaned = raw.strip().lower()
    return COURT_ALIASES.get(cleaned, raw.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Act name normalisation
# ─────────────────────────────────────────────────────────────────────────────

# Common short forms → canonical act name substrings for ILIKE matching
ACT_ALIASES: dict[str, str] = {
    "ipc":          "Indian Penal Code",
    "crpc":         "Code of Criminal Procedure",
    "cpc":          "Code of Civil Procedure",
    "coa":          "Companies Act",
    "it act":       "Information Technology Act",
    "it":           "Information Technology Act",
    "pocso":        "Protection of Children from Sexual Offences",
    "ndps":         "Narcotic Drugs and Psychotropic Substances",
    "rera":         "Real Estate (Regulation and Development) Act",
    "ibc":          "Insolvency and Bankruptcy Code",
    "posh":         "Sexual Harassment of Women at Workplace",
    "nia":          "National Investigation Agency Act",
    "uapa":         "Unlawful Activities Prevention Act",
    "pmla":         "Prevention of Money Laundering Act",
    "fema":         "Foreign Exchange Management Act",
    "rti":          "Right to Information Act",
    "mvact":        "Motor Vehicles Act",
    "mv act":       "Motor Vehicles Act",
    "negotiable instruments act": "Negotiable Instruments Act",
    "ni act":       "Negotiable Instruments Act",
    "constitution": "Constitution of India",
}


def resolve_act(raw: str) -> str:
    """
    Resolve act shorthand to canonical name.
    Returns canonical if alias found, otherwise raw (stripped).
    """
    cleaned = raw.strip().lower()
    return ACT_ALIASES.get(cleaned, raw.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Date parsing
# ─────────────────────────────────────────────────────────────────────────────

# Acceptable date/year input formats
_YEAR_ONLY    = re.compile(r"^\d{4}$")
_DATE_DMY     = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")
_DATE_YMD     = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")


def parse_year_input(raw: str | int | None) -> int | None:
    """
    Parse a year or date input and return just the year integer.

    Accepts:
        2015           → 2015
        "2015"         → 2015
        "01/01/2015"   → 2015
        "2015-01-01"   → 2015
        None           → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if _YEAR_ONLY.match(s):
        return int(s)
    m = _DATE_DMY.match(s)
    if m:
        return int(m.group(3))
    m = _DATE_YMD.match(s)
    if m:
        return int(m.group(1))
    raise InvalidFilterError(
        f"Invalid year/date format: {raw!r}",
        detail="Use YYYY, DD/MM/YYYY, or YYYY-MM-DD",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Normalised filter container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NormalisedFilters:
    """
    Fully normalised and validated filter set.
    All values are either None (not set) or in canonical form.
    """
    court:      str | None = None    # resolved via COURT_ALIASES
    year_from:  int | None = None    # integer year
    year_to:    int | None = None    # integer year
    act:        str | None = None    # resolved via ACT_ALIASES
    section:    str | None = None    # raw section string (ILIKE match)
    judge:      str | None = None    # raw judge name substring
    doc_type:   str | None = None    # "judgment" | "order" | None
    court_type: str | None = None    # "SC" | "HC" | "Tribunal" | None

    def is_empty(self) -> bool:
        """True if no filters are set."""
        return all(v is None for v in [
            self.court, self.year_from, self.year_to,
            self.act, self.section, self.judge,
            self.doc_type, self.court_type,
        ])

    def to_dict(self) -> dict:
        """Serialise to dict, omitting None values."""
        return {k: v for k, v in {
            "court":      self.court,
            "year_from":  self.year_from,
            "year_to":    self.year_to,
            "act":        self.act,
            "section":    self.section,
            "judge":      self.judge,
            "doc_type":   self.doc_type,
            "court_type": self.court_type,
        }.items() if v is not None}


def normalise_filters(raw: dict) -> NormalisedFilters:
    """
    Take a raw filter dict (from Pydantic model .model_dump())
    and return a NormalisedFilters with all values resolved and validated.

    Raises InvalidFilterError on semantic errors (e.g. year_from > year_to).
    """
    court_raw    = raw.get("court")
    year_from    = raw.get("year_from")
    year_to      = raw.get("year_to")
    act_raw      = raw.get("act")
    section      = raw.get("section")
    judge        = raw.get("judge")
    doc_type     = raw.get("doc_type")
    court_type   = raw.get("court_type")

    # ── Resolve aliases
    court = resolve_court(court_raw) if court_raw else None
    act   = resolve_act(act_raw)     if act_raw   else None

    # ── Parse years
    try:
        year_from = parse_year_input(year_from)
        year_to   = parse_year_input(year_to)
    except InvalidFilterError:
        raise

    # ── Cross-field validation
    if year_from and year_to and year_from > year_to:
        raise InvalidFilterError(
            f"year_from ({year_from}) cannot be greater than year_to ({year_to})",
            detail="Swap the values or set only one boundary",
        )

    # ── Reasonable year bounds
    current_year = 2025
    if year_from and year_from < 1800:
        raise InvalidFilterError(
            f"year_from {year_from} is before the earliest recorded Indian case law (1800)"
        )
    if year_to and year_to > current_year:
        raise InvalidFilterError(
            f"year_to {year_to} is in the future (current year: {current_year})"
        )

    # ── Normalise doc_type
    if doc_type:
        doc_type = doc_type.lower().strip()
        valid_doc_types = {"judgment", "order", "notification", "statute"}
        if doc_type not in valid_doc_types:
            raise InvalidFilterError(
                f"Unknown doc_type '{doc_type}' — valid values: {', '.join(sorted(valid_doc_types))}"
            )

    # ── Normalise judge name (title-case to improve ILIKE hit rate)
    if judge:
        judge = judge.strip()
        if len(judge) < 2:
            raise InvalidFilterError("Judge name must be at least 2 characters")

    # ── Normalise section
    if section:
        section = section.strip()

    # ── Resolve court_type
    if court_type:
        court_type = _normalise_court_type(court_type)

    return NormalisedFilters(
        court      = court,
        year_from  = year_from,
        year_to    = year_to,
        act        = act,
        section    = section,
        judge      = judge,
        doc_type   = doc_type,
        court_type = court_type,
    )


def _normalise_court_type(raw: str) -> str | None:
    """Normalise court_type shorthand."""
    mapping = {
        "sc":       "SC",
        "supreme":  "SC",
        "hc":       "HC",
        "high":     "HC",
        "tribunal": "Tribunal",
        "trib":     "Tribunal",
        "district": "District",
    }
    return mapping.get(raw.lower().strip(), raw.strip())


# ─────────────────────────────────────────────────────────────────────────────
# SQL clause builder (used by executor.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_filter_clauses(
    filters: NormalisedFilters,
    params:  list,
) -> list[str]:
    """
    Build a list of SQL WHERE clause fragments from a NormalisedFilters.
    Appends required values to the shared params list (psycopg2 %s style).

    All clauses assume the main table alias is 'lc' (legal_cases).
    JOIN aliases used:
      case_acts     → ca_f  (to avoid collision with main query's ca_agg)
      legal_paragraphs → lp_f

    Returns a list of SQL strings (joined with AND in the caller).
    """
    clauses: list[str] = []

    def add(val: Any) -> str:
        params.append(val)
        return "%s"

    # ── Court (ILIKE on legal_cases.court)
    if filters.court:
        p = add(f"%{filters.court}%")
        clauses.append(f"lc.court ILIKE {p}")

    # ── Court type (exact match on legal_cases.court_type)
    if filters.court_type:
        p = add(filters.court_type)
        clauses.append(f"lc.court_type = {p}")

    # ── Year range
    if filters.year_from:
        p = add(filters.year_from)
        clauses.append(f"lc.year >= {p}")

    if filters.year_to:
        p = add(filters.year_to)
        clauses.append(f"lc.year <= {p}")

    # ── Act (via case_acts JOIN)
    if filters.act:
        p = add(f"%{filters.act}%")
        clauses.append(f"""
            EXISTS (
                SELECT 1
                FROM case_acts ca_f
                WHERE ca_f.case_id = lc.case_id
                  AND ca_f.act_name ILIKE {p}
            )
        """)

    # ── Section (via case_acts JOIN — always paired with act if provided)
    if filters.section:
        p = add(f"%{filters.section}%")
        clauses.append(f"""
            EXISTS (
                SELECT 1
                FROM case_acts ca_f
                WHERE ca_f.case_id = lc.case_id
                  AND ca_f.section ILIKE {p}
            )
        """)

    # ── Judge (via legal_paragraphs.judges_mentioned array)
    if filters.judge:
        p = add(f"%{filters.judge}%")
        clauses.append(f"""
            EXISTS (
                SELECT 1
                FROM legal_paragraphs lp_f
                WHERE lp_f.case_id = lc.case_id
                  AND EXISTS (
                      SELECT 1
                      FROM unnest(lp_f.judges_mentioned) jm
                      WHERE jm ILIKE {p}
                  )
            )
        """)

    # ── Doc type (maps to outcome column — approximate)
    if filters.doc_type:
        p = add(filters.doc_type)
        clauses.append(f"LOWER(lc.outcome) LIKE LOWER({p})")

    return clauses


# ─────────────────────────────────────────────────────────────────────────────
# Utility: describe active filters for logging / response metadata
# ─────────────────────────────────────────────────────────────────────────────

def describe_filters(filters: NormalisedFilters) -> str:
    """
    Return a human-readable one-line summary of active filters.
    Used in log messages.

    Example: "court=Supreme Court of India | year=2010–2023 | act=Indian Penal Code"
    """
    parts: list[str] = []

    if filters.court:
        parts.append(f"court={filters.court}")
    if filters.year_from and filters.year_to:
        parts.append(f"year={filters.year_from}–{filters.year_to}")
    elif filters.year_from:
        parts.append(f"year≥{filters.year_from}")
    elif filters.year_to:
        parts.append(f"year≤{filters.year_to}")
    if filters.act:
        parts.append(f"act={filters.act}")
    if filters.section:
        parts.append(f"section={filters.section}")
    if filters.judge:
        parts.append(f"judge={filters.judge}")
    if filters.doc_type:
        parts.append(f"doc_type={filters.doc_type}")

    return " | ".join(parts) if parts else "no filters"
