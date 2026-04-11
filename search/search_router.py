"""
madhav.ai — Enhanced Search Router
Mount in your main app.py:
    from search_router import router as search_router
    app.include_router(search_router)

Uses your existing PostgreSQL + pgvector Hybrid Search Engine.
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional
from database.hybrid_search import HybridSearchEngine, SearchMode, SearchResult
from Backend.db import get_connection
from psycopg2.extras import RealDictCursor
from .search_enhancements import (
    expand_synonyms,
    parse_boolean_query,
)
from .search_pipeline import (
    SearchPipeline,
    FuzzySearchBuilder,
)
from .phrase_matcher import (
    PhraseMatcher,
    FieldAwareMatcher,
    SectionDetector,
)

router = APIRouter(prefix="/api/search", tags=["search"])

# Initialize search pipeline for spell correction, fuzzy matching
_search_pipeline = SearchPipeline()
_fuzzy_builder = FuzzySearchBuilder()
_phrase_matcher = PhraseMatcher()
_field_matcher = FieldAwareMatcher()
_section_detector = SectionDetector()


# ─────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    mode: str = "normal"            # "normal" | "research"
    page: int = 1
    page_size: int = 20

    # ── All advanced filters ──
    court:            Optional[str] = None
    judge:            Optional[str] = None
    year_from:        Optional[int] = None
    year_to:          Optional[int] = None
    bench_strength:   Optional[int] = None
    act_section:      Optional[str] = None
    party_name:       Optional[str] = None
    case_type:        Optional[str] = None   # civil/criminal/writ/tax/company/consumer
    outcome:          Optional[str] = None   # allowed/dismissed/acquitted/convicted
    precedent_status: Optional[str] = None   # good_law/overruled/distinguished/followed


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def format_results(results: list, total: int, page: int, page_size: int) -> dict:
    """Format search results for API response"""
    cases = []
    for r in results:
        meta = r.metadata or {}
        cases.append({
            "case_id": r.case_id,
            "case_name": r.case_name,
            "court": meta.get('court', ''),
            "year": meta.get('year'),
            "relevance_score": r.relevance_score,
            "search_mode": r.search_mode,
            "result_type": r.result_type,
            "citation": r.case_id,  # Fallback
            "paragraph_text": None,
            "para_type": meta.get('para_type', 'general'),
        })

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     max(1, -(-total // page_size)),
        "results":   cases,  # ← Changed from "cases" to "results" to match frontend expectation
    }


# ─────────────────────────────────────────────────────────
# MAIN SEARCH ENDPOINT
# ─────────────────────────────────────────────────────────

@router.post("/")
async def search(req: SearchRequest):
    """
    Enhanced search endpoint using PostgreSQL + pgvector.
    - Spell correction for Indian legal queries
    - Fuzzy matching with pg_trgm
    - Native term detection and boosting
    - Exact field matching (court, case_id, year, outcome, etc.)
    - Returns ALL matching cases for exact field matches
    - Falls back to hybrid semantic search for general queries
    """
    try:
        # ════════════════════════════════════════════════════════════════════════
        # PHASE 1: QUERY ENHANCEMENT PIPELINE
        # ════════════════════════════════════════════════════════════════════════
        # Process query through spell correction, native term detection, variants
        
        pipeline_result = _search_pipeline.process(req.query)
        corrected_query = pipeline_result.get('corrected_query', req.query)
        
        # Track pipeline enhancements
        pipeline_metadata = {
            "original_query": req.query,
            "corrected_query": corrected_query,
            "corrections_applied": pipeline_result.get('corrections', []),
            "native_terms_detected": pipeline_result.get('native_terms_detected', []),
            "spell_corrected": len(pipeline_result.get('corrections', [])) > 0,
        }
        
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        offset = (req.page - 1) * req.page_size

        all_results = []
        search_type = "semantic"  # Track what type of search was performed
        search_query = corrected_query  # Use corrected query for field detection

        # ════════════════════════════════════════════════════════════════════════
        # SMART FIELD DETECTION: Check common searchable fields for exact matches
        # ════════════════════════════════════════════════════════════════════════

        field_searches = []

        # PHRASE-AWARE MATCHING: Try to detect multiword phrases first
        # This handles: "supreme court", "SC", "supeme court" → all to same canonical form
        phrase_court = None
        try:
            # Try to detect phrases and sections - ask for court specifically
            section_matches = _field_matcher.match_query_to_field(search_query)
            
            for field_name, value, confidence, match_type in section_matches:
                if confidence >= 0.85:  # High confidence match
                    # This is a phrase/section match - add it to field searches
                    if field_name == "court" and confidence >= 0.90:
                        field_searches.append(("court", value))  # ← Use canonical form directly
                        phrase_court = value  # Store for later use
                    elif field_name == "acts_referred" or field_name == "constitutional_articles":
                        field_searches.append(("section", value))
        except Exception as e:
            log.warning(f"[SEARCH] Phrase matching failed: {e}")

        # 1. Check if court name (exact match - use phrase-detected form if available)
        if not phrase_court:
            # No phrase detected - try direct court match
            court_check_query = """
            SELECT court FROM legal_cases 
            WHERE LOWER(court) = LOWER(%s)
            LIMIT 1
            """
            try:
                cursor.execute(court_check_query, (search_query,))
                court_match = cursor.fetchone()
                if court_match:
                    field_searches.append(("court", court_match['court']))
            except:
                pass

        # 2. Check if case_id (exact or prefix match)
        if len(search_query) >= 3:  # At least 3 characters
            case_id_query = """
            SELECT case_id FROM legal_cases 
            WHERE case_id = %s OR case_id ILIKE %s
            LIMIT 1
            """
            try:
                cursor.execute(case_id_query, (search_query, f"{search_query}%"))
                case_match = cursor.fetchone()
                if case_match:
                    field_searches.append(("case_id", search_query))
            except:
                pass

        # 3. Check if year (numeric)
        try:
            year_val = int(search_query)
            if 1950 <= year_val <= 2030:
                year_check_query = """
                SELECT COUNT(*) as cnt FROM legal_cases WHERE year = %s
                """
                cursor.execute(year_check_query, (year_val,))
                year_count = cursor.fetchone()
                if year_count and year_count['cnt'] > 0:
                    field_searches.append(("year", year_val))
        except (ValueError, TypeError):
            pass

        # 4. Check if outcome (exact match)
        outcome_check_query = """
        SELECT COUNT(*) as cnt FROM legal_cases 
        WHERE LOWER(outcome) = LOWER(%s)
        LIMIT 1
        """
        try:
            cursor.execute(outcome_check_query, (search_query,))
            outcome_result = cursor.fetchone()
            if outcome_result and outcome_result['cnt'] > 0:
                field_searches.append(("outcome", search_query))
        except:
            pass

        # 5. Check if petitioner or respondent (exact match in court cases)
        party_check_query = """
        SELECT COUNT(*) as cnt FROM legal_cases
        WHERE (LOWER(petitioner) = LOWER(%s) OR LOWER(respondent) = LOWER(%s))
        AND (petitioner IS NOT NULL OR respondent IS NOT NULL)
        LIMIT 1
        """
        try:
            cursor.execute(party_check_query, (search_query, search_query))
            party_result = cursor.fetchone()
            if party_result and party_result['cnt'] > 0:
                field_searches.append(("party", search_query))
        except:
            pass

        # ════════════════════════════════════════════════════════════════════════
        # EXECUTE EXACT FIELD SEARCH IF MATCHED
        # ════════════════════════════════════════════════════════════════════════

        if field_searches:
            # Use the first matching field search
            field_type, field_value = field_searches[0]
            search_type = f"exact_{field_type}"

            if field_type == "court":  # ← Changed from "court_phrase" to "court"
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE court = %s
                ORDER BY COALESCE(authority_score, 0) DESC, case_id
                """
                cursor.execute(query, (field_value,))

            elif field_type == "case_id":
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE case_id = %s OR case_id ILIKE %s
                ORDER BY COALESCE(authority_score, 0) DESC
                """
                cursor.execute(query, (field_value, f"{field_value}%"))

            elif field_type == "year":
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE year = %s
                ORDER BY COALESCE(authority_score, 0) DESC, case_id
                """
                cursor.execute(query, (field_value,))

            elif field_type == "outcome":
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE LOWER(outcome) = LOWER(%s)
                ORDER BY COALESCE(authority_score, 0) DESC, case_id
                """
                cursor.execute(query, (field_value,))

            elif field_type == "party":
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE (LOWER(petitioner) = LOWER(%s) OR LOWER(respondent) = LOWER(%s))
                ORDER BY COALESCE(authority_score, 0) DESC, case_id
                """
                cursor.execute(query, (field_value, field_value))

            elif field_type == "section":
                query = """
                SELECT case_id, case_name, court, year, 
                       COALESCE(authority_score, 0) as relevance_score,
                       appeal_no, petitioner, respondent, outcome, court_type
                FROM legal_cases
                WHERE acts_referred ILIKE %s OR constitutional_articles ILIKE %s
                ORDER BY COALESCE(authority_score, 0) DESC, case_id
                """
                section_pattern = f"%{field_value}%"
                cursor.execute(query, (section_pattern, section_pattern))

            # Convert all rows to SearchResult objects
            rows = cursor.fetchall()
            for row in rows:
                result = SearchResult(
                    case_id=row['case_id'],
                    case_name=row['case_name'],
                    relevance_score=row['relevance_score'],
                    search_mode='structured',
                    result_type='case',
                    metadata={
                        'court': row['court'],
                        'year': row['year'],
                        'appeal_no': row['appeal_no'],
                        'petitioner': row['petitioner'],
                        'respondent': row['respondent'],
                        'outcome': row['outcome'],
                        'court_type': row['court_type'],
                    }
                )
                all_results.append(result)

        # ════════════════════════════════════════════════════════════════════════
        # FALLBACK: If no exact field match, use hybrid semantic search
        # ════════════════════════════════════════════════════════════════════════

        if not all_results:
            search_type = "semantic"
            engine = HybridSearchEngine(conn)

            # Expand query with legal synonyms
            expanded_query = search_query
            synonyms = expand_synonyms(search_query)
            if synonyms:
                expanded_query = f"{search_query} {' '.join(synonyms)}"

            # Run hybrid search
            search_result = engine.search(
                query=expanded_query,
                mode=SearchMode.HYBRID,
                limit=500,  # Get more results for filtering
            )

            all_results = search_result.get('results', [])

        # ════════════════════════════════════════════════════════════════════════
        # APPLY FILTERS
        # ════════════════════════════════════════════════════════════════════════

        filtered_results = all_results

        # Filter by court if specified
        if req.court:
            filtered_results = [
                r for r in filtered_results 
                if r.metadata and r.metadata.get('court') == req.court
            ]

        # Filter by year range if specified
        if req.year_from or req.year_to:
            year_from = req.year_from or 0
            year_to = req.year_to or 9999
            filtered_results = [
                r for r in filtered_results
                if r.metadata and year_from <= (r.metadata.get('year') or 0) <= year_to
            ]

        # Filter by case type if specified
        if req.case_type:
            filtered_results = [
                r for r in filtered_results
                if r.metadata and r.metadata.get('court_type') == req.case_type
            ]

        # Apply pagination
        paginated_results = filtered_results[offset:offset + req.page_size]
        total = len(filtered_results)

        return {
            **format_results(paginated_results, total, req.page, req.page_size),
            "query_enhanced": {
                "original": pipeline_metadata['original_query'],
                "corrected": pipeline_metadata['corrected_query'],
                "corrections": pipeline_metadata['corrections_applied'],
                "native_terms": pipeline_metadata['native_terms_detected'],
                "spell_corrected": pipeline_metadata['spell_corrected'],
            },
            "query_expanded": search_query,
            "suggestion": None,
            "filters_applied": {
                "court": req.court,
                "year_from": req.year_from,
                "year_to": req.year_to,
                "case_type": req.case_type,
            },
            "search_type": search_type,
            "field_searches_tried": [s[0] for s in field_searches] if field_searches else []
        }
    except Exception as e:
        return {
            "error": str(e),
            "total": 0,
            "page": req.page,
            "page_size": req.page_size,
            "pages": 0,
            "cases": [],
        }


# ─────────────────────────────────────────────────────────
# AUTOCOMPLETE ENDPOINT
# ─────────────────────────────────────────────────────────

@router.get("/autocomplete")
async def autocomplete(
    q: str = Query(..., min_length=2),
    size: int = Query(default=8, le=20)
):
    """
    Autocomplete for case names using PostgreSQL full-text search.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        
        query = """
            SELECT DISTINCT case_id, case_name, citation, court, year
            FROM legal_cases
            WHERE to_tsvector('english', case_name) @@ plainto_tsquery('english', %s)
            ORDER BY case_name
            LIMIT %s
        """
        cursor.execute(query, (q, size))
        rows = cursor.fetchall()

        results = [
            {
                "case_id": row.get("case_id"),
                "case_name": row.get("case_name"),
                "citation": row.get("citation"),
                "court": row.get("court"),
                "year": row.get("year"),
                "highlight": {},
            }
            for row in rows
        ]

        return {"suggestions": results, "query": q}
    except Exception as e:
        return {"suggestions": [], "query": q, "error": str(e)}


# ─────────────────────────────────────────────────────────
# SEARCH WITHIN RESULTS (refine live)
# ─────────────────────────────────────────────────────────

@router.post("/refine")
async def refine_results(
    case_ids: list[str],
    refine_query: str,
    page: int = 1,
    page_size: int = 20,
):
    """
    Search within a specific set of case IDs.
    """
    try:
        conn = get_connection()
        engine = HybridSearchEngine(conn)
        offset = (page - 1) * page_size

        # Filter semantic search results by case_ids
        search_result = engine.search(
            query=refine_query,
            mode=SearchMode.HYBRID,
            limit=500,  # Get more to filter
        )
        
        all_results = search_result.get('results', [])
        
        # Filter by case_ids
        filtered_results = [r for r in all_results if r.case_id in case_ids]
        
        # Apply pagination
        paginated_results = filtered_results[offset:offset + page_size]
        total = len(filtered_results)

        return format_results(paginated_results, total, page, page_size)
    except Exception as e:
        return {"error": str(e), "cases": [], "total": 0, "page": page, "page_size": page_size}


# ─────────────────────────────────────────────────────────
# FILTER OPTIONS (populate dropdowns dynamically)
# ─────────────────────────────────────────────────────────

@router.get("/filters")
async def get_filter_options():
    """Returns all available filter values for dropdowns."""
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Get courts
        cursor.execute("SELECT DISTINCT court FROM legal_cases WHERE court IS NOT NULL ORDER BY court LIMIT 50")
        courts = [row["court"] for row in cursor.fetchall()]

        # Get case types
        cursor.execute("SELECT DISTINCT case_type FROM legal_cases WHERE case_type IS NOT NULL ORDER BY case_type LIMIT 20")
        case_types = [row["case_type"] for row in cursor.fetchall()]

        # Get outcomes
        cursor.execute("SELECT DISTINCT outcome_summary FROM legal_cases WHERE outcome_summary IS NOT NULL ORDER BY outcome_summary LIMIT 20")
        outcomes = [row["outcome_summary"] for row in cursor.fetchall()]

        # Get year range
        cursor.execute("SELECT MIN(year) as min_year, MAX(year) as max_year FROM legal_cases")
        year_range = cursor.fetchone() or {"min_year": 1950, "max_year": 2025}

        return {
            "courts": courts,
            "case_types": case_types,
            "outcomes": outcomes,
            "statuses": ["good_law", "overruled", "distinguished", "followed"],
            "year_min": year_range["min_year"] or 1950,
            "year_max": year_range["max_year"] or 2025,
        }
    except Exception as e:
        return {
            "courts": [],
            "case_types": [],
            "outcomes": [],
            "statuses": [],
            "year_min": 1950,
            "year_max": 2025,
            "error": str(e),
        }
