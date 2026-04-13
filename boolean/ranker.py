"""
boolean/ranker.py
=================
Relevance scoring and result re-ranking for Boolean search.

Boolean search via SQL (tsvector / INTERSECT / UNION) returns a raw
set of matching case_ids but has no built-in relevance ordering.
This module computes a composite relevance score for each case AFTER
the DB returns results, giving a better ranked list than pure
authority_score or citation_count alone.

Scoring factors (all normalised 0.0–1.0, then weighted):

  Factor                  Weight    Source column(s)
  ──────────────────────  ──────    ───────────────────────────────────────
  authority_score          0.30     legal_cases.authority_score
  citation_count           0.20     legal_cases.citation_count  (log-scaled)
  cited_by_count           0.15     case_citations aggregate
  term_density             0.15     matched_paragraph_count / total_paragraphs
  recency                  0.10     legal_cases.year  (newer = higher)
  court_hierarchy          0.10     legal_cases.court_type  (SC > HC > Tribunal)

The final score is stored as `relevance_score` (float 0.0–1.0) on each
result dict before it is serialised.

No DB calls — operates entirely on data already fetched by executor.py.
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Factor weights — must sum to 1.0
WEIGHT_AUTHORITY      = 0.30
WEIGHT_CITATION_COUNT = 0.20
WEIGHT_CITED_BY       = 0.15
WEIGHT_TERM_DENSITY   = 0.15
WEIGHT_RECENCY        = 0.10
WEIGHT_COURT          = 0.10

assert abs(
    WEIGHT_AUTHORITY + WEIGHT_CITATION_COUNT + WEIGHT_CITED_BY
    + WEIGHT_TERM_DENSITY + WEIGHT_RECENCY + WEIGHT_COURT - 1.0
) < 1e-9, "Ranker weights must sum to 1.0"

# Court hierarchy scores (SC = highest)
COURT_SCORES: dict[str, float] = {
    "SC":       1.00,
    "HC":       0.70,
    "Tribunal": 0.40,
    "District": 0.20,
}
COURT_SCORE_DEFAULT = 0.30

# Year range for recency normalisation
YEAR_MIN = 1950
YEAR_MAX = 2025

# Log base for citation count normalisation
CITATION_LOG_BASE = 10_000    # cases with ~10k citations = score of 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Scoring helper functions
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _score_authority(authority_score: float | None) -> float:
    """authority_score is already 0.0–1.0 from DB constraint."""
    if authority_score is None:
        return 0.3    # neutral default
    return _clamp(float(authority_score))


def _score_citation_count(count: int | None) -> float:
    """
    Log-scale normalise citation_count.
    0 → 0.0,  100 → ~0.50,  1000 → ~0.75,  10000 → 1.0
    """
    if not count or count <= 0:
        return 0.0
    return _clamp(math.log1p(count) / math.log1p(CITATION_LOG_BASE))


def _score_cited_by(cited_by: int) -> float:
    """Same log-scale as citation_count."""
    return _score_citation_count(cited_by)


def _score_term_density(matched_para_count: int, total_paragraphs: int | None) -> float:
    """
    Fraction of paragraphs that matched the boolean query.
    Cases where the query terms appear densely = more relevant.
    """
    if not total_paragraphs or total_paragraphs <= 0:
        return 0.3    # neutral if unknown
    density = matched_para_count / total_paragraphs
    return _clamp(density * 5.0)    # amplify — even 20% density = full score


def _score_recency(year: int | None) -> float:
    """
    Linear normalise year between YEAR_MIN and YEAR_MAX.
    2025 → 1.0,  1950 → 0.0,  None → 0.5
    """
    if year is None:
        return 0.5
    year = max(YEAR_MIN, min(YEAR_MAX, year))
    return (year - YEAR_MIN) / (YEAR_MAX - YEAR_MIN)


def _score_court(court_type: str | None) -> float:
    """Map court_type code to hierarchy score."""
    if not court_type:
        return COURT_SCORE_DEFAULT
    return COURT_SCORES.get(court_type, COURT_SCORE_DEFAULT)


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring function
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    """Individual factor scores for transparency / debug endpoint."""
    authority:      float
    citation_count: float
    cited_by:       float
    term_density:   float
    recency:        float
    court:          float
    final:          float

    def to_dict(self) -> dict:
        return {
            "authority":      round(self.authority,      4),
            "citation_count": round(self.citation_count, 4),
            "cited_by":       round(self.cited_by,       4),
            "term_density":   round(self.term_density,   4),
            "recency":        round(self.recency,        4),
            "court":          round(self.court,          4),
            "final":          round(self.final,          4),
        }


def compute_relevance_score(
    authority_score:     float | None,
    citation_count:      int | None,
    cited_by_count:      int,
    matched_para_count:  int,
    total_paragraphs:    int | None,
    year:                int | None,
    court_type:          str | None,
) -> ScoreBreakdown:
    """
    Compute composite relevance score for one case result.

    Args:
        authority_score:    From legal_cases.authority_score (0–1)
        citation_count:     From legal_cases.citation_count
        cited_by_count:     From case_citations aggregate
        matched_para_count: Number of paragraphs that matched the boolean query
        total_paragraphs:   From legal_cases.total_paragraphs
        year:               From legal_cases.year
        court_type:         From legal_cases.court_type ("SC", "HC", "Tribunal")

    Returns:
        ScoreBreakdown with individual factors and final weighted score
    """
    s_authority      = _score_authority(authority_score)
    s_citation_count = _score_citation_count(citation_count)
    s_cited_by       = _score_cited_by(cited_by_count)
    s_term_density   = _score_term_density(matched_para_count, total_paragraphs)
    s_recency        = _score_recency(year)
    s_court          = _score_court(court_type)

    final = (
        WEIGHT_AUTHORITY      * s_authority
        + WEIGHT_CITATION_COUNT * s_citation_count
        + WEIGHT_CITED_BY       * s_cited_by
        + WEIGHT_TERM_DENSITY   * s_term_density
        + WEIGHT_RECENCY        * s_recency
        + WEIGHT_COURT          * s_court
    )

    return ScoreBreakdown(
        authority      = s_authority,
        citation_count = s_citation_count,
        cited_by       = s_cited_by,
        term_density   = s_term_density,
        recency        = s_recency,
        court          = s_court,
        final          = _clamp(final),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch re-ranker
# ─────────────────────────────────────────────────────────────────────────────

def rerank_results(
    results:             list[dict],
    matched_para_counts: dict[str, int],    # case_id → paragraph match count
    sort_by:             str = "relevance",
) -> list[dict]:
    """
    Re-rank a list of case result dicts by composite relevance score.

    Args:
        results:             List of dicts from build_result_query
        matched_para_counts: Mapping of case_id → how many paragraphs matched
                             (fetched separately in router for accuracy)
        sort_by:             "relevance" | "date_desc" | "date_asc" | "citations"

    Returns:
        Sorted list of result dicts with 'relevance_score' and
        'score_breakdown' fields added.

    Note: Non-relevance sorts still compute scores (stored on result)
    but sort by the requested field instead.
    """
    for r in results:
        cid       = r.get("case_id", "")
        para_hits = matched_para_counts.get(cid, 0)

        breakdown = compute_relevance_score(
            authority_score    = r.get("authority_score"),
            citation_count     = r.get("citation_count"),
            cited_by_count     = r.get("cited_by_count") or 0,
            matched_para_count = para_hits,
            total_paragraphs   = r.get("total_paragraphs"),
            year               = r.get("year"),
            court_type         = r.get("court_type"),
        )
        r["relevance_score"]  = round(breakdown.final * 100, 1)   # 0–100 for display
        r["score_breakdown"]  = breakdown.to_dict()

    # ── Sort
    if sort_by == "relevance":
        results.sort(key=lambda r: r["relevance_score"], reverse=True)
    elif sort_by == "date_desc":
        results.sort(
            key=lambda r: (r.get("year") or 0, str(r.get("date_of_order") or "")),
            reverse=True,
        )
    elif sort_by == "date_asc":
        results.sort(
            key=lambda r: (r.get("year") or 0, str(r.get("date_of_order") or "")),
        )
    elif sort_by == "citations":
        results.sort(key=lambda r: r.get("cited_by_count") or 0, reverse=True)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Utility: fetch paragraph match counts for a set of case_ids
# ─────────────────────────────────────────────────────────────────────────────

def build_para_count_query(case_ids: list[str]) -> tuple[str, list]:
    """
    Build a SQL query that counts total paragraphs per case for the
    matched case_ids. Used by router.py to feed term_density into ranker.

    NOTE: We count ALL paragraphs per case (not just matched ones) because
    the boolean matching already guarantees these cases are relevant.
    The ratio matched_para_count / total_paragraphs is approximated using
    legal_cases.total_paragraphs (already stored on the case row).

    This simpler approach avoids embedding the boolean SQL as a subquery
    (which would require passing bool_params a second time and creates
    param-ordering issues with psycopg2).

    For more accurate term_density, the router uses legal_cases.total_paragraphs
    directly from the result rows — no extra query needed.
    This function is kept for backward compatibility but returns a lightweight
    paragraph count query scoped only to the given case_ids.

    Returns (sql, params) where params is the list of case_ids.
    The SQL returns rows of (case_id, para_count).
    """
    if not case_ids:
        return "SELECT NULL::text AS case_id, 0 AS para_count WHERE FALSE", []

    placeholders = ", ".join(["%s"] * len(case_ids))

    sql = f"""
        SELECT
            p.case_id,
            COUNT(*) AS para_count
        FROM legal_paragraphs p
        WHERE p.case_id IN ({placeholders})
        GROUP BY p.case_id
    """
    return sql, list(case_ids)
