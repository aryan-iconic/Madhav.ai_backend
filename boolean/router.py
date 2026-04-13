"""
boolean/router.py
=================
FastAPI router for all Boolean search endpoints.

Endpoints:
  POST /boolean/search             — main search (query + filters + pagination)
  POST /boolean/validate           — validate query syntax only (no DB)
  POST /boolean/parse              — return AST as JSON (debug / frontend query tree)
  GET  /boolean/case/{case_id}     — full case detail
  GET  /boolean/suggestions        — autocomplete lists for courts, acts, judges
  GET  /boolean/health             — health check

Gaps fixed vs v1:
  ① filters.py now called — court/act aliases resolved before DB touch
  ② ranker.py now called — results re-ranked by 6-factor composite score
  ③ exceptions.py now used — structured errors everywhere, no bare HTTPException
  ④ CaseResult has relevance_score + score_breakdown fields
  ⑤ SearchResponse has filters_applied (resolved values shown to frontend)
  ⑥ GET /boolean/suggestions endpoint added
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

# ── Shared DB — adjust import path if your structure differs
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import get_dict_cursor

# ── Boolean module imports
from .validator   import validate_boolean_query
from .parser      import parse_boolean_query, ast_to_dict, ParseError
from .executor    import (
    BooleanExecutor,
    build_result_query,
    build_snippet_query,
    extract_search_terms,
    ExecutorError,
)
from .filters     import (
    normalise_filters,
    NormalisedFilters,
    describe_filters,
    COURT_ALIASES,
    ACT_ALIASES,
)
from .ranker      import rerank_results, build_para_count_query
from .highlighter import build_case_snippet, snippet_to_dict
from .exceptions  import (
    BooleanSearchError,
    QueryValidationError,
    QueryParseError,
    QueryExecutorError,
    DatabaseQueryError,
    DatabaseConnectionError,
    CaseNotFoundError,
    InvalidFilterError,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/boolean", tags=["Boolean Search"])


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────

class FilterParams(BaseModel):
    """
    Optional metadata filters. Applied AFTER boolean matching.
    Values are alias-resolved by filters.py before any DB touch.
    e.g. court="SC"  → "Supreme Court of India"
         act="ipc"   → "Indian Penal Code"
    """
    court:     str | None = Field(None, description="Court name or alias (SC, Delhi HC, ...)")
    year_from: int | None = Field(None, ge=1800, le=2100, description="Start year inclusive")
    year_to:   int | None = Field(None, ge=1800, le=2100, description="End year inclusive")
    act:       str | None = Field(None, description="Act name or alias (IPC, CrPC, ...)")
    section:   str | None = Field(None, description="Section number, e.g. '302'")
    judge:     str | None = Field(None, description="Judge name substring")
    doc_type:  str | None = Field(None, description="judgment | order | notification | statute")

    @field_validator("year_from", "year_to", mode="before")
    @classmethod
    def coerce_year(cls, v):
        if v is not None:
            return int(v)
        return v


class BooleanSearchRequest(BaseModel):
    """Request body for POST /boolean/search"""
    query:            str          = Field(
                                        ...,
                                        min_length=1,
                                        max_length=2000,
                                        description="Boolean query string",
                                    )
    filters:          FilterParams = Field(default_factory=FilterParams)
    sort_by:          str          = Field(
                                        "relevance",
                                        description="relevance | date_desc | date_asc | citations",
                                    )
    page:             int          = Field(1,  ge=1, le=1000)
    page_size:        int          = Field(25, ge=1, le=100)
    include_snippets: bool         = Field(True, description="Fetch KWIC snippets per result")

    @field_validator("sort_by")
    @classmethod
    def validate_sort(cls, v: str) -> str:
        valid = {"relevance", "date_desc", "date_asc", "citations"}
        if v not in valid:
            raise ValueError(f"sort_by must be one of: {', '.join(sorted(valid))}")
        return v


class ValidateRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class ParseRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

class ScoreBreakdownModel(BaseModel):
    """Individual factor scores (0.0–1.0 each) contributing to relevance_score."""
    authority:      float
    citation_count: float
    cited_by:       float
    term_density:   float
    recency:        float
    court:          float
    final:          float


class CaseResult(BaseModel):
    """One case in the search result list."""
    # ── Core identity
    case_id:                 str
    case_name:               str | None
    citation:                str | None
    # ── Court & date
    court:                   str | None
    court_type:              str | None
    year:                    int | None
    date_of_order:           str | None
    # ── Parties
    petitioner:              str | None
    respondent:              str | None
    # ── Outcome
    outcome:                 str | None
    outcome_summary:         str | None
    # ── Scoring signals
    authority_score:         float | None
    citation_count:          int | None
    cited_by_count:          int
    # ── Metadata arrays
    constitutional_articles: list[str] | None
    acts_referred:           list[str] | None
    subject_tags:            list[str] | None
    # ── Aggregated act/section strings (from case_acts join)
    acts_list:               str | None
    sections_list:           str | None
    # ── Gap 3 fix: relevance scoring
    relevance_score:         float | None = None   # 0–100 composite score for display
    score_breakdown:         ScoreBreakdownModel | None = None
    # ── KWIC snippets
    snippet:                 dict | None  = None


class FiltersApplied(BaseModel):
    """
    Gap 4 fix: resolved filter values actually used in the query.
    e.g. user sent court="SC", this shows court="Supreme Court of India".
    Frontend uses this to display "Searching in: Supreme Court of India".
    """
    court:       str | None = None
    year_from:   int | None = None
    year_to:     int | None = None
    act:         str | None = None
    section:     str | None = None
    judge:       str | None = None
    doc_type:    str | None = None
    description: str        = "no filters"


class SearchResponse(BaseModel):
    query:           str
    total_results:   int
    page:            int
    page_size:       int
    total_pages:     int
    sort_by:         str
    search_terms:    list[str]
    filters_applied: FiltersApplied        # Gap 4 fix
    results:         list[CaseResult]
    elapsed_ms:      float


class ValidateResponse(BaseModel):
    valid:  bool
    error:  str | None = None


class ParseResponse(BaseModel):
    query: str
    ast:   dict


# ─────────────────────────────────────────────────────────────────────────────
# Exception helper
# ─────────────────────────────────────────────────────────────────────────────

def _http(exc: Exception) -> HTTPException:
    """Convert a BooleanSearchError to an HTTPException with structured detail."""
    if isinstance(exc, BooleanSearchError):
        return HTTPException(status_code=exc.http_status, detail=exc.to_dict())
    return HTTPException(
        status_code=500,
        detail={"error": "INTERNAL_ERROR", "message": str(exc)}
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /boolean/search
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse, summary="Boolean case search")
def boolean_search(req: BooleanSearchRequest) -> SearchResponse:
    """
    Main Boolean search endpoint.

    Full pipeline:
      validate → parse → normalise filters → build SQL →
      execute → fetch para counts → re-rank → fetch snippets → return

    Example queries:
      "natural justice" AND "audi alteram partem"
      (murder OR "culpable homicide") AND intention AND NOT attempt
      constitu* AND article:21 AND court:"Supreme Court"
      "bail" W/5 "anticipatory" AND NOT dismissed
      atleast3(negligence) AND judge:Chandrachud
      act:IPC AND section:302 AND year:2020
    """
    t_start = time.perf_counter()
    query   = req.query.strip()

    # ── Step 1: Syntax validation (no DB, no parsing)
    validation = validate_boolean_query(query)
    if not validation:
        raise _http(QueryValidationError(validation.error))

    # ── Step 2: Parse → AST
    try:
        ast = parse_boolean_query(query)
    except ParseError as exc:
        log.warning("Parse error | query=%r | %s", query, exc)
        raise _http(QueryParseError(str(exc)))

    # ── Step 3: Normalise + validate filters  [Gap 1 fix]
    try:
        norm_filters: NormalisedFilters = normalise_filters(
            req.filters.model_dump(exclude_none=True)
        )
    except InvalidFilterError as exc:
        raise _http(exc)

    filters_dict = norm_filters.to_dict()   # alias-resolved, None-free dict

    # ── Step 4: Build boolean core SQL
    try:
        executor              = BooleanExecutor(filters=filters_dict)
        bool_sql, bool_params = executor.build(ast)
    except ExecutorError as exc:
        log.error("Executor error | query=%r | %s", query, exc)
        raise _http(QueryExecutorError(str(exc)))

    # ── Step 5: Wrap with joins, resolved filters, sort, pagination
    try:
        final_sql, final_params = build_result_query(
            boolean_sql    = bool_sql,
            boolean_params = bool_params,
            filters        = filters_dict,
            sort_by        = req.sort_by,
            page           = req.page,
            page_size      = req.page_size,
        )
    except Exception as exc:
        log.error("Result query build error | %s", exc)
        raise _http(QueryExecutorError(f"Result query construction failed: {exc}"))

    # ── Step 6: Execute main search query
    search_terms = extract_search_terms(ast)
    try:
        cursor = get_dict_cursor()
        cursor.execute(final_sql, final_params)
        rows = cursor.fetchall()
    except Exception as exc:
        log.error(
            "DB query failed | query=%r | error=%s | sql_head=%s",
            query, exc, final_sql[:300]
        )
        raise _http(DatabaseQueryError(exc, "boolean_search"))

    if not rows:
        total_count  = 0
        raw_results: list[dict] = []
    else:
        total_count = int(rows[0]["total_count"])
        raw_results = [dict(r) for r in rows]

    total_pages = max(1, -(-total_count // req.page_size))   # ceiling division

    # ── Step 7: Build paragraph match counts for term_density scoring  [Gap 2 fix]
    # Strategy: use total_paragraphs already in the result row (from legal_cases)
    # combined with a lightweight para count query scoped to matched case_ids.
    # This avoids re-running the full boolean SQL as a subquery.
    matched_para_counts: dict[str, int] = {}
    if raw_results:
        case_ids = [r["case_id"] for r in raw_results]
        try:
            pc_sql, pc_params = build_para_count_query(case_ids)
            cursor.execute(pc_sql, pc_params)
            for pc_row in cursor.fetchall():
                matched_para_counts[pc_row["case_id"]] = int(pc_row["para_count"])
        except Exception as exc:
            # Non-fatal — ranker falls back gracefully using total_paragraphs from row
            log.warning("Para count query failed (non-fatal): %s", exc)

    # ── Step 8: Re-rank with composite relevance score  [Gap 2 fix]
    ranked = rerank_results(
        results             = raw_results,
        matched_para_counts = matched_para_counts,
        sort_by             = req.sort_by,
    )

    # ── Step 9: Assemble CaseResult objects + optional KWIC snippets
    results: list[CaseResult] = []

    for row in ranked:
        snippet_dict = None
        if req.include_snippets:
            snippet_dict = _fetch_snippet(row["case_id"], search_terms)

        # Coerce score_breakdown dict → Pydantic model  [Gap 3 fix]
        sb_raw   = row.get("score_breakdown")
        sb_model = ScoreBreakdownModel(**sb_raw) if sb_raw else None

        results.append(CaseResult(
            case_id                 = row["case_id"],
            case_name               = row.get("case_name"),
            citation                = row.get("citation"),
            court                   = row.get("court"),
            court_type              = row.get("court_type"),
            year                    = row.get("year"),
            date_of_order           = str(row["date_of_order"]) if row.get("date_of_order") else None,
            petitioner              = row.get("petitioner"),
            respondent              = row.get("respondent"),
            outcome                 = row.get("outcome"),
            outcome_summary         = row.get("outcome_summary"),
            authority_score         = row.get("authority_score"),
            citation_count          = row.get("citation_count"),
            cited_by_count          = row.get("cited_by_count") or 0,
            constitutional_articles = row.get("constitutional_articles"),
            acts_referred           = row.get("acts_referred"),
            subject_tags            = row.get("subject_tags"),
            acts_list               = row.get("acts_list"),
            sections_list           = row.get("sections_list"),
            relevance_score         = row.get("relevance_score"),   # Gap 3 fix
            score_breakdown         = sb_model,                      # Gap 3 fix
            snippet                 = snippet_dict,
        ))

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 2)

    log.info(
        "Boolean search | query=%r | filters=%s | total=%d | page=%d/%d | %.1fms",
        query,
        describe_filters(norm_filters),
        total_count,
        req.page,
        total_pages,
        elapsed_ms,
    )

    # ── Gap 4 fix: FiltersApplied carries resolved values back to the frontend
    filters_applied = FiltersApplied(
        court       = norm_filters.court,
        year_from   = norm_filters.year_from,
        year_to     = norm_filters.year_to,
        act         = norm_filters.act,
        section     = norm_filters.section,
        judge       = norm_filters.judge,
        doc_type    = norm_filters.doc_type,
        description = describe_filters(norm_filters),
    )

    return SearchResponse(
        query           = query,
        total_results   = total_count,
        page            = req.page,
        page_size       = req.page_size,
        total_pages     = total_pages,
        sort_by         = req.sort_by,
        search_terms    = search_terms,
        filters_applied = filters_applied,
        results         = results,
        elapsed_ms      = elapsed_ms,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /boolean/validate
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/validate",
    response_model=ValidateResponse,
    summary="Validate Boolean query syntax (no DB)",
)
def validate_query(req: ValidateRequest) -> ValidateResponse:
    """
    Validate a Boolean query string without touching the database.
    Use for real-time frontend validation as the user types.
    Returns valid=True or valid=False with a human-readable error message.
    Typical response time: < 1ms.
    """
    result = validate_boolean_query(req.query)
    return ValidateResponse(valid=result.valid, error=result.error)


# ─────────────────────────────────────────────────────────────────────────────
# POST /boolean/parse
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Parse Boolean query → AST (debug / query tree visualiser)",
)
def parse_query(req: ParseRequest) -> ParseResponse:
    """
    Parse a Boolean query and return its Abstract Syntax Tree as JSON.
    Used by the frontend query tree visualiser and for debugging.
    No DB access.
    """
    validation = validate_boolean_query(req.query)
    if not validation:
        raise _http(QueryValidationError(validation.error))

    try:
        ast  = parse_boolean_query(req.query)
        tree = ast_to_dict(ast)
    except ParseError as exc:
        raise _http(QueryParseError(str(exc)))

    return ParseResponse(query=req.query, ast=tree)


# ─────────────────────────────────────────────────────────────────────────────
# GET /boolean/case/{case_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/case/{case_id}", summary="Get full case detail")
def get_case_detail(case_id: str) -> dict:
    """
    Fetch full metadata + acts + outbound citations + inbound citations
    + top paragraphs for one case.
    Called when the user expands / clicks a result card.
    """
    try:
        cursor = get_dict_cursor()
    except Exception as exc:
        raise _http(DatabaseConnectionError(exc))

    # ── Case metadata
    try:
        cursor.execute("""
            SELECT
                lc.*,
                COALESCE(
                    (SELECT COUNT(*)
                     FROM case_citations cc
                     WHERE cc.cited_case_id = lc.case_id),
                    0
                ) AS cited_by_count
            FROM legal_cases lc
            WHERE lc.case_id = %s
        """, (case_id,))
        case_row = cursor.fetchone()
    except Exception as exc:
        raise _http(DatabaseQueryError(exc, "get_case_detail/metadata"))

    if not case_row:
        raise _http(CaseNotFoundError(case_id))

    try:
        # Acts referred
        cursor.execute("""
            SELECT act_name, section, confidence
            FROM case_acts
            WHERE case_id = %s
            ORDER BY confidence DESC, act_name
        """, (case_id,))
        acts = cursor.fetchall()

        # Outbound citations — cases THIS case cites
        cursor.execute("""
            SELECT
                cc.cited_case_id,
                cc.target_citation,
                cc.relationship,
                cc.confidence,
                cc.context_sentence,
                lc2.case_name  AS cited_case_name,
                lc2.court      AS cited_court,
                lc2.year       AS cited_year
            FROM case_citations cc
            LEFT JOIN legal_cases lc2 ON lc2.case_id = cc.cited_case_id
            WHERE cc.source_case_id = %s
            ORDER BY cc.confidence DESC
            LIMIT 20
        """, (case_id,))
        citations_out = cursor.fetchall()

        # Inbound citations — cases that cite THIS case
        cursor.execute("""
            SELECT
                cc.source_case_id,
                cc.relationship,
                cc.confidence,
                lc2.case_name  AS citing_case_name,
                lc2.court      AS citing_court,
                lc2.year       AS citing_year
            FROM case_citations cc
            LEFT JOIN legal_cases lc2 ON lc2.case_id = cc.source_case_id
            WHERE cc.cited_case_id = %s
            ORDER BY lc2.authority_score DESC NULLS LAST, cc.confidence DESC
            LIMIT 20
        """, (case_id,))
        citations_in = cursor.fetchall()

        # Top paragraphs
        cursor.execute("""
            SELECT
                paragraph_id, para_no, page_no, text,
                para_type, word_count, quality_score
            FROM legal_paragraphs
            WHERE case_id = %s
            ORDER BY para_no ASC
            LIMIT 10
        """, (case_id,))
        paragraphs = cursor.fetchall()

    except Exception as exc:
        log.error("Case detail DB error for %s: %s", case_id, exc)
        raise _http(DatabaseQueryError(exc, f"get_case_detail/{case_id}"))

    # Serialise date/timestamp columns
    case_dict = dict(case_row)
    for key in ("date_of_order", "date_of_filing", "created_at", "updated_at"):
        if case_dict.get(key):
            case_dict[key] = str(case_dict[key])

    return {
        "case":         case_dict,
        "acts":         [dict(a) for a in acts],
        "citations":    [dict(c) for c in citations_out],   # outbound
        "cited_by":     [dict(c) for c in citations_in],    # inbound
        "paragraphs":   [dict(p) for p in paragraphs],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /boolean/suggestions   [Gap 5 fix]
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/suggestions", summary="Autocomplete data for query builder")
def get_suggestions() -> dict:
    """
    Returns all autocomplete data needed by the frontend query builder:

      courts         — distinct court names from DB (for dropdown)
      acts           — top 50 act names by frequency (for dropdown)
      judges         — top 50 judge names from paragraphs (for autocomplete)
      subject_tags   — top 50 subject tags (for keyword: field autocomplete)
      court_aliases  — map of shorthand → canonical (SC, Delhi HC, ...)
      act_aliases    — map of shorthand → canonical (IPC, CrPC, ...)
      fields         — valid field qualifiers with examples
      operators      — all Boolean operators with descriptions
      sort_options   — valid sort_by values

    This response is slow-changing. Frontend should cache it for the session.
    """
    try:
        cursor = get_dict_cursor()
    except Exception as exc:
        raise _http(DatabaseConnectionError(exc))

    # ── Distinct courts
    try:
        cursor.execute("""
            SELECT DISTINCT court
            FROM legal_cases
            WHERE court IS NOT NULL
            ORDER BY court
            LIMIT 100
        """)
        db_courts = [row["court"] for row in cursor.fetchall()]
    except Exception as exc:
        log.warning("Suggestions/courts failed (non-fatal): %s", exc)
        db_courts = []

    # ── Top acts by frequency
    try:
        cursor.execute("""
            SELECT act_name, COUNT(*) AS freq
            FROM case_acts
            WHERE act_name IS NOT NULL
            GROUP BY act_name
            ORDER BY freq DESC
            LIMIT 50
        """)
        db_acts = [row["act_name"] for row in cursor.fetchall()]
    except Exception as exc:
        log.warning("Suggestions/acts failed (non-fatal): %s", exc)
        db_acts = []

    # ── Top judge names
    try:
        cursor.execute("""
            SELECT j AS judge_name, COUNT(*) AS freq
            FROM legal_paragraphs,
                 LATERAL unnest(judges_mentioned) AS j
            WHERE j IS NOT NULL AND j != ''
            GROUP BY j
            ORDER BY freq DESC
            LIMIT 50
        """)
        db_judges = [row["judge_name"] for row in cursor.fetchall()]
    except Exception as exc:
        log.warning("Suggestions/judges failed (non-fatal): %s", exc)
        db_judges = []

    # ── Top subject tags
    try:
        cursor.execute("""
            SELECT t AS tag, COUNT(*) AS freq
            FROM legal_cases,
                 LATERAL unnest(subject_tags) AS t
            WHERE t IS NOT NULL AND t != ''
            GROUP BY t
            ORDER BY freq DESC
            LIMIT 50
        """)
        db_tags = [row["tag"] for row in cursor.fetchall()]
    except Exception as exc:
        log.warning("Suggestions/tags failed (non-fatal): %s", exc)
        db_tags = []

    # ── Static reference data
    fields = [
        {"field": "court:",       "description": "Court name",            "example": 'court:"Supreme Court"'},
        {"field": "judge:",       "description": "Judge name",            "example": "judge:Chandrachud"},
        {"field": "act:",         "description": "Act or legislation",    "example": "act:IPC"},
        {"field": "section:",     "description": "Section number",        "example": "section:302"},
        {"field": "article:",     "description": "Constitutional article","example": "article:21"},
        {"field": "year:",        "description": "Year of judgment",      "example": "year:2019"},
        {"field": "title:",       "description": "Case name",             "example": "title:puttaswamy"},
        {"field": "petitioner:",  "description": "Petitioner name",       "example": "petitioner:maneka"},
        {"field": "respondent:",  "description": "Respondent name",       "example": 'respondent:"Union of India"'},
        {"field": "keyword:",     "description": "Subject keyword / tag", "example": "keyword:privacy"},
        {"field": "citation:",    "description": "Case citation / number","example": "citation:494"},
    ]

    operators = [
        {"op": "AND",                 "type": "binary",     "description": "Both terms must appear"},
        {"op": "OR",                  "type": "binary",     "description": "Either term may appear"},
        {"op": "NOT",                 "type": "unary",      "description": "Exclude cases containing term"},
        {"op": "W/n",                 "type": "proximity",  "description": "Left precedes right within n words (ordered)"},
        {"op": "NEAR/n",              "type": "proximity",  "description": "Either term within n words (unordered)"},
        {"op": "PRE/n",               "type": "proximity",  "description": "Left precedes right within n words"},
        {"op": "/S",                  "type": "proximity",  "description": "Both terms in the same sentence"},
        {"op": "/P",                  "type": "proximity",  "description": "Both terms in the same paragraph"},
        {"op": '"..."',               "type": "phrase",     "description": "Exact phrase match"},
        {"op": "term*",               "type": "wildcard",   "description": "Prefix wildcard (constitu* → constitution, constitutional)"},
        {"op": "term?",               "type": "wildcard",   "description": "Single-char wildcard (wom?n → woman, women)"},
        {"op": "atleast<n>(<term>)",  "type": "occurrence", "description": "Term appears at least n times in a paragraph"},
    ]

    sort_options = [
        {"value": "relevance", "label": "Relevance (default)"},
        {"value": "date_desc", "label": "Date — newest first"},
        {"value": "date_asc",  "label": "Date — oldest first"},
        {"value": "citations", "label": "Most cited"},
    ]

    return {
        "courts":        db_courts,
        "acts":          db_acts,
        "judges":        db_judges,
        "subject_tags":  db_tags,
        "court_aliases": dict(COURT_ALIASES),
        "act_aliases":   dict(ACT_ALIASES),
        "fields":        fields,
        "operators":     operators,
        "sort_options":  sort_options,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /boolean/health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health", summary="Health check")
def health_check() -> dict:
    """
    Verifies DB is reachable and returns basic table counts.
    """
    try:
        cursor = get_dict_cursor()
        cursor.execute("""
            SELECT
                (SELECT COUNT(*) FROM legal_cases)      AS case_count,
                (SELECT COUNT(*) FROM legal_paragraphs) AS paragraph_count,
                (SELECT COUNT(*) FROM case_acts)        AS acts_count
        """)
        row = cursor.fetchone()
        return {
            "status":          "ok",
            "case_count":      row["case_count"]      if row else 0,
            "paragraph_count": row["paragraph_count"] if row else 0,
            "acts_count":      row["acts_count"]      if row else 0,
        }
    except Exception as exc:
        log.error("Health check failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "DATABASE_CONNECTION_ERROR", "message": str(exc)},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_snippet(case_id: str, search_terms: list[str]) -> dict | None:
    """
    Fetch and build KWIC snippet for one case.
    Non-fatal — snippet failure must never prevent a result from returning.
    Returns None silently on any error.
    """
    try:
        sql, params = build_snippet_query(case_id, search_terms)
        cursor      = get_dict_cursor()
        cursor.execute(sql, params)
        para_rows   = cursor.fetchall()

        if not para_rows:
            return None

        snippet = build_case_snippet(
            case_id      = case_id,
            para_rows    = [dict(r) for r in para_rows],
            search_terms = search_terms,
        )
        return snippet_to_dict(snippet)

    except Exception as exc:
        log.warning("Snippet fetch failed for %s (non-fatal): %s", case_id, exc)
        return None
