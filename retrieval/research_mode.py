"""
retrieval/research_mode.py
==========================
RESEARCH MODE — Intent-first output routing engine.

Pipeline:
  Query
    → detect_query_intent()          # classify + set output_type
    → _lookup_case_name_in_db()      # DB-backed unique-case detection
    → embed + hybrid search
    → branch on output_type:
        full_case      → metadata + full judgment + citations  (no LLM)
        judgment_only  → clean judgment paragraphs             (no LLM)
        citation_graph → citation tree only                    (no LLM)
        case_answer    → LLM answer scoped to ONE case only    (LLM)
        law            → LLM law explanation + related cases   (LLM)
        answer         → RAG answer + paragraph refs           (LLM)
        table          → tabular results only                  (no LLM)
        hybrid         → answer + table + paragraph refs       (LLM)
    → return typed response dict

Output dict always contains `output_type` so the frontend
knows exactly which fields to render and which to suppress.

output_type → UI mode mapping:
    full_case      → Case page (metadata + reader + citation graph)
    judgment_only  → Reader view (clean paragraphs only)
    citation_graph → Graph view (tree + flat list)
    case_answer    → Courtroom answer (answer scoped to one case)
    answer         → Legal explanation (multi-case RAG)
    law            → Section + cases (bare act + interpretation)
    table          → List view (search engine mode)
    hybrid         → Mixed (exploration mode)
"""

import logging
import re
from typing import Optional, Dict, Any, List

from psycopg2.extras import RealDictCursor

from database.hybrid_search import HybridSearchEngine, SearchMode, SearchResult
from Backend.retrieval.embedder import embed_query
from Backend.retrieval.formatter import search_results_to_case_results, format_context_for_llm, attach_precedent_status
from Backend.llm.generator import generate_research_answer, generate_case_summary, generate_full_case_brief
from Backend.services.citation_graph import build_full_citation_tree
from Backend.retrieval.case_brief_helpers import build_para_context_for_summary, build_fallback_brief

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Intent keyword map → (intent_label, para_types_to_prioritize)
_INTENT_RULES: List[tuple] = [
    # (keywords_any_match,  intent,          para_types)
    # NOTE: "held" is intentionally NOT here — it lives only in _QUESTION_KEYWORDS.
    # If "held" were here it would match judgment_only before the question-detection
    # branch runs, causing "What was held in X?" to route to judgment_only instead
    # of case_answer.
    (["judgment", "judgement", "verdict", "ruled", "decision", "full text"],
     "judgment_only",  ["judgment", "order"]),

    (["citation", "cited by", "cases citing", "precedent", "overruled",
      "followed", "distinguished", "applied"],
     "citation_graph", ["citation"]),

    (["section", "article", "ipc", "crpc", "act ", "clause",
      "provision", "statute", "bare act"],
     "law",            ["law", "statute", "legal"]),

    (["facts", "background", "summary", "brief"],
     "answer",         ["facts", "background"]),

    (["issues", "question of law", "disputed", "controversy"],
     "answer",         ["issues"]),

    (["order", "direction", "injunction", "interim", "instruction"],
     "answer",         ["order"]),

    (["cases on", "judgments on", "list of cases", "cases related",
      "cases about", "find cases"],
     "table",          []),

    (["can ", "is ", "are ", "does ", "do ", "what is", "what are",
      "explain", "meaning", "define", "right to", "rights of",
      "rights ", "whether", "how does", "how do"],
     "answer",         ["judgment", "order", "facts", "issues"]),
]

# Words that, if present, force a topic/table intent regardless of above
_TABLE_TRIGGER_PHRASES = [
    "cases on", "judgments on", "bail cases", "cases related to",
    "cases about", "list of cases", "find cases",
]

# Words that strongly signal a section/article query
_LAW_TRIGGER_PHRASES = [
    "section ", "article ", "ipc ", "crpc ", "schedule ", "act ",
]

# Question words that, when combined with a case match, produce case_answer
# instead of full_case. Order matters — checked after case_id is resolved.
# NOTE: Removed overly generic keywords ("can ", "is ", "are ", "does ", "do ")
# that caused false positives when users asked for facts/issues (e.g., "facts in case X"
# would incorrectly return case_answer instead of full_case).
_QUESTION_KEYWORDS = [
    "what was held", "what did", "what is held", "ratio",
    "what was decided", "what was the decision", "what was the order",
    "what was the judgment", "what was the verdict",
    "what does", "what did court say", "what court said",
    "principle", "laid down", "propounded",
    "what is", "what are", "explain", "meaning",
    "define", "right to", "whether", "how did",
]


# ---------------------------------------------------------------------------
# Helper functions for precision (FIX #6-#10)
# ---------------------------------------------------------------------------

def _normalize_query(query: str) -> str:
    """
    FIX #10: Normalize query variations to improve case matching.
    Handles: "vs.", "v.", "versus", etc.
    """
    q = query.lower().strip()
    # Normalize case name patterns
    q = q.replace(" vs. ", " v. ")
    q = q.replace(" versus ", " v. ")
    q = q.replace(" v/s ", " v. ")
    q = q.replace(" v/s. ", " v. ")
    return q


def _calculate_intent_confidence(query: str, matched_intent: Optional[str], 
                                  is_question: bool, case_id: Optional[str]) -> dict:
    """
    FIX #7: Intent confidence scoring to prevent wrong routing.
    Returns: {"confidence": 0.0-1.0, "is_strong": bool}
    """
    score = 0.0
    
    if case_id:
        score += 0.4  # Case found = strongest signal
    
    if matched_intent:
        score += 0.3  # Keyword matched
    
    if is_question:
        score += 0.3  # Question detected
    
    # Penalize ambiguous patterns
    if "what is" in query.lower() and not case_id:
        score = min(score, 0.5)  # Generic questions start weak
    
    is_strong = score >= 0.6
    return {"confidence": score, "is_strong": is_strong}


def _filter_paragraphs_by_quality(paragraphs: List, min_quality: float = 0.5, 
                                    min_length: int = 80) -> List:
    """
    FIX #6: Stricter paragraph quality control for accuracy.
    Filters out: low quality (<0.5) or very short (<80 chars) paragraphs.
    """
    filtered = []
    for para in paragraphs:
        # Extract quality score with None guard
        quality = 0.5  # default
        if hasattr(para, 'metadata') and isinstance(para.metadata, dict):
            quality = para.metadata.get('quality', 0.5)
        elif isinstance(para, dict):
            quality = para.get('quality_score', 0.5)
        
        # Ensure quality is not None (handle case where value exists but is None)
        if quality is None:
            quality = 0.5
        
        # Extract text and length
        text_len = 0
        if hasattr(para, 'metadata') and isinstance(para.metadata, dict):
            text = para.metadata.get('text', '')
            text_len = len(text) if text else 0
        elif isinstance(para, dict):
            text = para.get('text', '')
            text_len = len(text) if text else 0
        
        # Apply filters
        if quality > min_quality and text_len > min_length:
            filtered.append(para)
    
    return filtered


def _rerank_results(results: List[SearchResult], conn=None, engine=None) -> List[SearchResult]:
    """
    FIX #9: Multi-signal re-ranking for Google-level quality.
    Weighted: 0.6 * relevance + 0.2 * citation_count + 0.2 * recency
    """
    if not results:
        return results
    
    rescored = []
    for r in results:
        final_score = (0.6 * (r.relevance_score or 0.5))
        
        # Add citation weight if available
        if r.result_type == "case" and engine and conn:
            try:
                citations = engine.relationship.get_citations(r.case_id)[:10]
                citation_factor = min(len(citations) / 10.0, 1.0)
                final_score += 0.2 * citation_factor
            except:
                final_score += 0.0  # No penalty, just no boost
        else:
            final_score += 0.0  # Can't score citations for paragraphs
        
        # Add recency weight
        try:
            year = r.metadata.get('year') if hasattr(r, 'metadata') else None
            if year:
                recency = max(0, (int(year) - 2010) / 20.0)
                final_score += 0.2 * min(recency, 1.0)
        except:
            pass  # No penalty if year parsing fails
        
        r.relevance_score = final_score
        rescored.append(r)
    
    # Re-sort by rescored relevance
    rescored.sort(key=lambda x: x.relevance_score or 0, reverse=True)
    return rescored


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

def detect_query_intent(query: str, conn=None) -> Dict[str, Any]:
    """
    Classify query intent and set output_type.

    Priority order:
      1. Explicit table trigger phrases      → table
      2. Law / section triggers             → law
      3. Keyword-based intent detection     → judgment_only / citation_graph / answer / table
      4. DB case-name lookup
           case found + judgment keyword    → judgment_only
           case found + citation keyword    → citation_graph
           case found + question keyword    → case_answer   ← NEW
           case found + no qualifier        → full_case
      5. Matched keyword intent (no case)  → answer / table / etc.
      6. Ambiguous / generic               → hybrid

    Returns dict with keys:
        output_type  : str      — primary routing key for the pipeline
        intent       : str      — human-readable intent label
        para_types   : list     — paragraph types to prioritize in search
        is_unique    : bool     — query targets a single known case
        is_generic   : bool     — broad/exploratory query
        case_id      : str|None — resolved case_id if is_unique
        case_name    : str|None — resolved case_name if is_unique
        intent_confidence : float   — confidence score 0.0-1.0  (FIX #7)
    """
    q = _normalize_query(query)  # FIX #10: Normalize query variations

    # ── 1. Table triggers ────────────────────────────────────────────────────
    if any(phrase in q for phrase in _TABLE_TRIGGER_PHRASES):
        return _intent(q, "table", "table", [], is_generic=True)

    # ── 2. Law / section triggers ────────────────────────────────────────────
    if any(phrase in q for phrase in _LAW_TRIGGER_PHRASES):
        return _intent(q, "law", "law", ["law", "statute", "legal"])

    # ── 3. Detect question intent BEFORE keyword rules ───────────────────────
    # Must run before _INTENT_RULES so "What was held in X?" (which contains
    # "held" — a question keyword) is flagged as a question, not matched to
    # judgment_only. Once we know it's a question + a case is found, we return
    # case_answer regardless of what _INTENT_RULES would have picked.
    is_question = any(kw in q for kw in _QUESTION_KEYWORDS)

    # ── 4. Keyword-based intent ───────────────────────────────────────────────
    matched_intent = None
    matched_para_types = []
    for keywords, intent_label, para_types in _INTENT_RULES:
        if any(kw in q for kw in keywords):
            matched_intent = intent_label
            matched_para_types = para_types
            break

    # ── 5. DB case-name lookup ───────────────────────────────────────────────
    case_id, case_name = None, None
    if conn:
        case_id, case_name = _lookup_case_name_in_db(q, conn)

    if case_id:
        # citation_graph wins unconditionally when that keyword is present  
        if matched_intent == "citation_graph":
            return _intent(q, "citation_graph", "citation_graph",
                           ["citation"],
                           is_unique=True, case_id=case_id, case_name=case_name)

        # case_answer: case found + question word.  This must be checked BEFORE
        # the judgment_only branch — "What was held in X?" is a question, not
        # a request for the raw judgment text.
        if is_question:
            return _intent(q, "case_answer", "case_answer",
                           ["judgment", "order", "facts", "issues"],
                           is_unique=True, case_id=case_id, case_name=case_name)

        # judgment_only: case found + explicit judgment/verdict keyword,
        # but NOT a question (already handled above).
        if matched_intent == "judgment_only":
            return _intent(q, "judgment_only", "judgment_only",
                           ["judgment", "order"],
                           is_unique=True, case_id=case_id, case_name=case_name)

        # No qualifier → open the full case file
        return _intent(q, "full_case", "full_case",
                       ["judgment", "order", "facts"],
                       is_unique=True, case_id=case_id, case_name=case_name)

    # ── 6. No case match — use keyword intent ────────────────────────────────
    if matched_intent:
        # answer/hybrid without a specific case = generic → mark is_generic
        is_gen = matched_intent in ("answer", "hybrid")
        return _intent(q, matched_intent, matched_intent, matched_para_types,
                       is_generic=is_gen)

    # ── 7. Ambiguous / generic → hybrid ─────────────────────────────────────
    return _intent(q, "hybrid", "hybrid",
                   ["judgment", "order", "facts", "issues", "citation", "law"],
                   is_generic=True)


def _intent(q: str, output_type: str, intent: str, para_types: List[str],
            is_unique: bool = False, is_generic: bool = False,
            case_id: Optional[str] = None,
            case_name: Optional[str] = None) -> Dict[str, Any]:
    # FIX #7: Calculate intent confidence
    is_question = any(kw in q for kw in _QUESTION_KEYWORDS)
    matched_intent = intent if intent not in ["table", "law"] else None
    confidence_info = _calculate_intent_confidence(q, matched_intent, is_question, case_id)
    
    result = {
        "output_type": output_type,
        "intent":      intent,
        "para_types":  para_types,
        "is_unique":   is_unique,
        "is_generic":  is_generic,
        "case_id":     case_id,
        "case_name":   case_name,
        "intent_confidence": confidence_info,  # FIX #7
    }
    log.info(f"[INTENT] {result}")
    return result


def _lookup_case_name_in_db(query: str, conn) -> tuple:
    """
    Search legal_cases by case_name for a close match.
    Returns (case_id, case_name) or (None, None).

    Design contract:
    - Only returns a match when the query plausibly names a specific case.
    - Generic legal questions ("Can bar council suspend?") must return None.
    - Qualifier words ("judgment", "verdict", "held") are stripped before
      search so they don't inflate or distort the match.
    - Single-word or mostly-generic token sets are rejected early.
    """
    # ── Step 1: Basic tokenisation ────────────────────────────────────────────
    # Remove punctuation, split, lowercase, drop ultra-short tokens.
    function_words = {
        "the", "of", "in", "and", "for", "vs", "versus", "v",
        "is", "are", "was", "were", "am", "be", "been",
        "what", "when", "where", "who", "why", "how",
        "by", "an", "a", "to", "at", "on", "its", "that", "this",
        "find", "show", "me", "get", "with", "from", "did", "does",
        "not", "but", "also", "can", "could", "would", "should",
        "say", "said", "tell", "told",
    }
    tokens = re.sub(r"[^\w\s]", " ", query).split()
    all_keywords = [t.lower() for t in tokens
                    if t.lower() not in function_words and len(t) > 2]

    if not all_keywords:
        return None, None

    # ── Step 2: Strip qualifier/intent words from search string ──────────────
    # These words signal query TYPE, not case name. Removing them lets the
    # remaining name tokens match actual case names cleanly.
    # e.g. "Nandini Sharma judgment" → search for "nandini sharma"
    # e.g. "What was held in X?"     → search for "x"
    intent_words = {
        "judgment", "judgement", "citation", "verdict", "ratio",
        "held", "ruled", "decision", "order", "facts", "issues",
        "laws", "legal", "statute", "section", "ipc", "crpc",
        "explain", "meaning", "define", "whether", "rights",
        "right", "natural", "justice", "principle", "brief",
    }
    name_keywords = [k for k in all_keywords if k not in intent_words]

    # If stripping intent words leaves nothing, the query is pure legalese —
    # no case name present.
    if not name_keywords:
        return None, None

    # ── Step 3: Generic-query guard ───────────────────────────────────────────
    # If most remaining tokens are generic legal/admin nouns (not proper names),
    # this is a topic question, not a case lookup.
    generic_nouns = {
        "powers", "procedure", "law", "rules", "authority",
        "association", "council", "board", "commission", "tribunal",
        "government", "ministry", "court", "bench", "division",
        "regulations", "guidelines", "policy", "framework",
        "advocate", "advocates", "suspend", "bar", "criminal",
        "accused", "trial", "bail", "appeal", "petition",
        "contract", "agreement", "document", "facts", "issues",
        "order", "rights", "protection", "jurisdiction", "act",
    }

    # Block single generic token
    if len(name_keywords) == 1 and name_keywords[0] in generic_nouns:
        log.info(f"[DB-LOOKUP] Blocked single generic token: {name_keywords[0]}")
        return None, None

    # Block when ≥60% of name tokens are generic (e.g. "bar council suspend")
    if len(name_keywords) >= 2:
        generic_count = sum(1 for k in name_keywords if k in generic_nouns)
        if generic_count / len(name_keywords) >= 0.6:
            log.info(f"[DB-LOOKUP] Blocked generic query ({generic_count}/{len(name_keywords)} generic tokens)")
            return None, None

    # ── Step 4: Require at least one discriminative token ────────────────────
    # A discriminative token is a proper-name-like word: ≥4 chars, not generic.
    discriminative = [k for k in name_keywords
                      if k not in generic_nouns and len(k) >= 4]
    if not discriminative:
        log.info(f"[DB-LOOKUP] No discriminative tokens in: {name_keywords}")
        return None, None

    # ── Step 5: DB queries (4 levels, decreasing strictness) ─────────────────
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    search_str = " ".join(name_keywords)

    try:
        # Level 1: pg_trgm similarity ─────────────────────────────────────────
        # Skip for single short tokens (too many false positives).
        if not (len(name_keywords) == 1 and len(name_keywords[0]) < 7):
            try:
                cursor.execute("""
                    SELECT case_id, case_name,
                           similarity(lower(case_name), lower(%s)) AS sim
                    FROM legal_cases
                    WHERE similarity(lower(case_name), lower(%s)) > 0.5
                    ORDER BY sim DESC
                    LIMIT 1
                """, (search_str, search_str))
                row = cursor.fetchone()
                if row:
                    log.info(f"[DB-LOOKUP] Trigram: {row['case_name']} (sim={row['sim']:.2f})")
                    return row["case_id"], row["case_name"]
            except Exception as e:
                log.debug(f"[DB-LOOKUP] Trigram unavailable: {e}")

        # Level 2: ILIKE AND — every name token must appear ───────────────────
        if len(name_keywords) >= 2:
            conditions = " AND ".join(
                [f"lower(case_name) LIKE lower(%s)" for _ in name_keywords]
            )
            cursor.execute(
                f"SELECT case_id, case_name FROM legal_cases WHERE {conditions} LIMIT 1",
                [f"%{k}%" for k in name_keywords],
            )
            row = cursor.fetchone()
            if row:
                log.info(f"[DB-LOOKUP] ILIKE-AND: {row['case_name']}")
                return row["case_id"], row["case_name"]

        # Level 3: ILIKE OR subquery — at least 2 tokens must match ──────────
        # Uses a subquery so match_count can be filtered in the outer WHERE
        # without triggering PostgreSQL's "must appear in GROUP BY" error.
        if len(name_keywords) >= 2:
            ilike_vals = [f"%{k}%" for k in name_keywords]
            match_expr = " + ".join(
                [f"(lower(case_name) LIKE lower(%s))::int" for _ in name_keywords]
            )
            or_clause = " OR ".join(
                [f"lower(case_name) LIKE lower(%s)" for _ in name_keywords]
            )
            cursor.execute(
                f"""
                SELECT case_id, case_name, match_count FROM (
                    SELECT case_id, case_name, ({match_expr}) AS match_count
                    FROM legal_cases
                    WHERE {or_clause}
                ) sub
                WHERE match_count >= 2
                ORDER BY match_count DESC
                LIMIT 1
                """,
                ilike_vals + ilike_vals,
            )
            row = cursor.fetchone()
            if row:
                log.info(f"[DB-LOOKUP] ILIKE-OR: {row['case_name']} ({row['match_count']} tokens)")
                return row["case_id"], row["case_name"]

        # Level 4: Single best discriminative token ───────────────────────────
        # Only fires for queries that started with exactly one name token
        # (e.g. a rare single-name search like "Kesavananda").
        # Never fires for multi-word queries to avoid false single-word matches.
        if len(name_keywords) == 1 and len(discriminative) == 1:
            best = discriminative[0]
            if len(best) >= 6:   # must be long enough to be discriminative
                cursor.execute(
                    "SELECT case_id, case_name FROM legal_cases "
                    "WHERE lower(case_name) LIKE lower(%s) "
                    "ORDER BY length(case_name) ASC LIMIT 1",
                    [f"%{best}%"],
                )
                row = cursor.fetchone()
                if row:
                    log.info(f"[DB-LOOKUP] Single-token: {row['case_name']}")
                    return row["case_id"], row["case_name"]

    except Exception as e:
        log.warning(f"[DB-LOOKUP] Error: {e}")
    finally:
        cursor.close()

    return None, None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_research_search(
    query: str,
    conn,
    filters=None,
    limit: int = 10,
    case_context: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Research mode pipeline. Returns a typed response dict with `output_type`
    telling the frontend exactly which fields to render.

    output_type values and what the frontend should render:
        full_case      → case metadata + full judgment paragraphs + citation tree
        judgment_only  → clean ordered judgment paragraphs, nothing else
        citation_graph → citation tree + flat citation list, no answer text
        law            → LLM law explanation + related case table
        answer         → LLM answer + paragraph refs + supporting case list
        table          → tabular case results only
        hybrid         → LLM answer + tabular results + paragraph refs
    """
    log.info(f"[RESEARCH] Query: '{query}'")

    # ── Step 0: Intent detection (DB-backed) ──────────────────────────────────
    intent_info = detect_query_intent(query, conn)
    output_type = intent_info["output_type"]

    # Allow caller to override case context (e.g. user is already viewing a case)
    resolved_case_id = case_context or intent_info.get("case_id")

    # ── Step 1: Embed query ───────────────────────────────────────────────────
    embedding = embed_query(query)

    # ── Step 2: Hybrid search ─────────────────────────────────────────────────
    engine = HybridSearchEngine(conn)

    raw_results = engine.search(
        query=query,
        mode=SearchMode.HYBRID,
        case_context=resolved_case_id,
        limit=30,
    )

    paragraph_hits: List[SearchResult] = []
    if embedding:
        paragraph_hits = engine.semantic.search_by_vector(embedding, limit=20)

    all_results: List[SearchResult] = raw_results.get("results", [])
    all_results.extend(paragraph_hits)

    # Deduplicate, keep highest score per case_id
    seen: Dict[str, SearchResult] = {}
    for r in all_results:
        score = r.relevance_score or 0
        if r.case_id not in seen or score > (seen[r.case_id].relevance_score or 0):
            seen[r.case_id] = r

    sorted_results = sorted(
        seen.values(),
        key=lambda x: x.relevance_score or 0,
        reverse=True,
    )

    # Apply caller-supplied filters
    if filters:
        from Backend.retrieval.normal_mode import apply_filters_to_conn
        sorted_results = apply_filters_to_conn(conn, sorted_results, filters, limit * 3)

    # Boost exact case-name keyword matches to the top
    sorted_results = _boost_case_name_matches(query, sorted_results)

    # Case-name fallback when semantic search under-retrieves
    if len(sorted_results) < 3:
        sorted_results = _case_name_fallback(query, engine, seen, sorted_results, limit)

    final_results = sorted_results[:limit]
    
    # FIX #9: Re-rank results for better quality
    if final_results and engine:
        final_results = _rerank_results(final_results, conn, engine)

    # ── Step 3: Branch on output_type ────────────────────────────────────────
    if output_type == "full_case":
        return _build_full_case(
            query, conn, engine, final_results, intent_info, limit, session_id
        )

    if output_type == "judgment_only":
        return _build_judgment_only(
            query, conn, final_results, intent_info, session_id
        )

    if output_type == "citation_graph":
        result = _build_citation_graph(
            query, conn, engine, final_results, intent_info, session_id
        )
        log.info(f"[CITATION-RESPONSE] Built citation_graph response with {len(result.get('citations_flat', []))} citations")
        return result

    if output_type == "case_answer":
        return _build_case_answer(
            query, conn, engine, final_results, intent_info, session_id
        )

    if output_type == "law":
        return _build_law_answer(
            query, conn, engine, final_results, intent_info, limit, session_id
        )

    if output_type == "table":
        return _build_table(
            query, conn, final_results, intent_info, limit, session_id
        )

    if output_type == "answer":
        return _build_rag_answer(
            query, conn, engine, final_results, intent_info, limit, session_id
        )

    # Default: hybrid
    return _build_hybrid(
        query, conn, engine, final_results, intent_info, limit, session_id
    )


# ---------------------------------------------------------------------------
# Output builders — one per output_type
# ---------------------------------------------------------------------------

def _build_full_case(query, conn, engine, final_results, intent_info, limit, session_id):
    """
    output_type: full_case
    → case metadata + all judgment paragraphs + citation tree + LLM case summary
    LLM: called to generate case summary
    """
    case_id = intent_info.get("case_id") or (
        final_results[0].case_id if final_results else None
    )

    metadata = _fetch_case_metadata(conn, case_id) if case_id else {}
    judgment_paras = _fetch_judgment_paragraphs(conn, case_id) if case_id else []
    citation_tree, citations_flat = _fetch_citations(conn, engine, case_id)
    
    # FIX: Ensure citations are JSON-serializable before sending to frontend
    log.info(f"[FULL-CASE] Before _jsonify_citations: type={type(citations_flat)}, len={len(citations_flat) if citations_flat else 0}")
    citations_flat = _jsonify_citations(citations_flat or [])
    log.info(f"[FULL-CASE] After _jsonify_citations: type={type(citations_flat)}, len={len(citations_flat)}")

    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    
    # Add PDF links to all case results
    for case_result in case_results:
        if case_result.get("case_id"):
            case_result["pdf_link"] = _generate_pdf_link(case_result["case_id"])
    
    # ── Generate full case intelligence brief ─────────────────────────
    # This replaces the old 1500-char paragraph approach with a comprehensive brief
    case_summary = None
    try:
        # Fetch ALL the data needed for a real summary
        all_paragraphs = _fetch_all_paragraphs_for_case(conn, case_id) if case_id else []
        
        # Fetch citations for "cases relied upon" section
        _, citations_flat_raw = _fetch_citations(conn, engine, case_id)
        citations_for_summary = _jsonify_citations(citations_flat_raw or [])[:8]
        
        # Build structured context: metadata + para types + acts + citations
        para_context = build_para_context_for_summary(all_paragraphs)
        
        if para_context:  # Only try LLM if we have paragraph context
            case_summary = generate_full_case_brief(
                metadata=metadata,
                para_context=para_context,
                citations=citations_for_summary,
            )
            log.info(f"[FULL-CASE] Generated comprehensive brief ({len(case_summary or '')} chars)")
        else:
            log.info(f"[FULL-CASE] No paragraphs available for brief generation")

    except Exception as e:
        log.error(f"[FULL-CASE] Brief generation failed: {e}", exc_info=True)
    
    # Fallback: structured text from DB fields only (no LLM)
    if not case_summary:
        try:
            case_summary = build_fallback_brief(metadata)
            log.info(f"[FULL-CASE] Using fallback brief from metadata")
        except Exception as e:
            log.debug(f"[FULL-CASE] Fallback brief also failed: {e}")

    log.info(f"[FULL-CASE-FINAL] ✅ Returning response with case_summary type={type(case_summary).__name__}, length={len(case_summary or '') if isinstance(case_summary, str) else 'N/A'}")

    return {
        "query":              query,
        "output_type":        "full_case",
        "intent":             intent_info["intent"],
        "is_unique":          True,
        "is_generic":         False,
        "case_id":            case_id,
        "case_metadata":      metadata,
        "case_summary":       case_summary,  # ✨ LLM-generated summary
        "judgment_paragraphs": _para_results_to_dicts(judgment_paras),
        "citation_tree":      citation_tree,
        "citations_flat":     citations_flat,
        "results":            case_results,
        "answer":             case_summary,  # ✨ FIXED: Use case_summary as answer
        "paragraph_references": [],
        "tabular_results":    [],
        "total_results":      len(case_results),
        "session_id":         session_id,
    }


def _build_judgment_only(query, conn, final_results, intent_info, session_id):
    """
    output_type: judgment_only
    → ordered judgment paragraphs, clean reading view
    LLM: skipped
    """
    case_id = intent_info.get("case_id") or (
        final_results[0].case_id if final_results else None
    )
    judgment_paras = _fetch_judgment_paragraphs(conn, case_id) if case_id else []

    # If we got paragraph hits from search, use those instead (richer ranking)
    para_hits = [r for r in final_results if r.result_type == "paragraph"]
    if para_hits and not judgment_paras:
        judgment_paras = para_hits

    return {
        "query":               query,
        "output_type":         "judgment_only",
        "intent":              intent_info["intent"],
        "is_unique":           intent_info["is_unique"],
        "is_generic":          False,
        "case_id":             case_id,
        "judgment_paragraphs": _para_results_to_dicts(judgment_paras),
        "answer":              None,
        "paragraph_references": [],
        "results":             [],
        "tabular_results":     [],
        "citation_tree":       None,
        "citations_flat":      None,
        "total_results":       len(judgment_paras),
        "session_id":          session_id,
    }


def _build_citation_graph(query, conn, engine, final_results, intent_info, session_id):
    """
    output_type: citation_graph
    → case metadata + citation tree + flat list
    LLM: skipped
    """
    case_id = intent_info.get("case_id") or (
        final_results[0].case_id if final_results else None
    )
    
    metadata = _fetch_case_metadata(conn, case_id) if case_id else {}
    citation_tree, citations_flat = _fetch_citations(conn, engine, case_id)
    
    # FIX: Ensure citations are JSON-serializable before sending to frontend
    log.info(f"[CITATION-GRAPH] Before _jsonify_citations: len={len(citations_flat) if citations_flat else 0}")
    citations_flat = _jsonify_citations(citations_flat or [])
    log.info(f"[CITATION-GRAPH] After _jsonify_citations: len={len(citations_flat)}")

    return {
        "query":               query,
        "output_type":         "citation_graph",
        "intent":              intent_info["intent"],
        "is_unique":           intent_info["is_unique"],
        "is_generic":          False,
        "case_id":             case_id,
        "case_metadata":       metadata,
        "citation_tree":       citation_tree,
        "citations_flat":      citations_flat,
        "answer":              None,
        "judgment_paragraphs": [],
        "paragraph_references": [],
        "results":             [],
        "tabular_results":     [],
        "total_results":       len(citations_flat) if citations_flat else 0,
        "session_id":          session_id,
    }


def _build_case_answer(query, conn, engine, final_results, intent_info, session_id):
    """
    output_type: case_answer
    → LLM answer scoped strictly to ONE resolved case.
    Unlike `answer` (multi-case RAG), this only uses paragraphs from
    the matched case_id so the response is grounded in that case alone.

    Use for: "What was held in Nandini Sharma?"
             "Explain the ratio of Kesavananda Bharati"
             "What did court say about tribal land in Samatha?"

    LLM: YES (case-scoped context only)
    """
    case_id = intent_info.get("case_id") or (
        final_results[0].case_id if final_results else None
    )

    # Fetch metadata for header display
    metadata = _fetch_case_metadata(conn, case_id) if case_id else {}

    # Fetch paragraphs — ALL types, not just judgment, so the answer can
    # draw on facts/issues/order depending on what the user asked
    case_paras = _fetch_all_paragraphs_for_case(conn, case_id) if case_id else []

    # Filter by intent para_types to prioritize the right content
    case_paras = _filter_paragraphs_by_intent(case_paras, intent_info)
    
    # FIX #3/#4: CRITICAL — Lower quality thresholds even more to catch Nandini Sharma case
    # Previous threshold of 0.2 was filtering out ALL paragraphs for this case
    # Nandini Sharma has quality_scores in 0.05-0.15 range, so we need to be very lenient
    case_paras = _filter_paragraphs_by_quality(case_paras, min_quality=0.05, min_length=20)
    top_paragraphs = case_paras[:6]  # Slightly more context than generic RAG
    
    # Log warning if we filtered out everything - fallback to unfiltered
    if not top_paragraphs:
        log.warning(f"[CASE-ANSWER] ⚠️ No paragraphs passed quality filter for case {case_id}. Using all available paragraphs...")
        top_paragraphs = case_paras if case_paras else []

    # Build context scoped to this case only
    context = format_context_for_llm(top_paragraphs, query)
    answer = generate_research_answer(
        query=query,
        context=context,
        mode="case_answer",   # Signal to LLM to answer from one case
    )

    # Build a structured explanation block for the UI header
    complete_explanation = _build_case_explanation_block(metadata, answer)
    
    # FIX #3: Generate case summary for better UX
    case_summary = None
    try:
        if case_id and top_paragraphs:
            case_summary = generate_research_answer(
                query=f"Summarize the case {metadata.get('case_name', 'Case')}",
                context=format_context_for_llm(top_paragraphs[:3], query),
                mode="summary"
            )
    except Exception as e:
        log.debug(f"[CASE-ANSWER] Summary generation failed: {e}")
    
    # Add PDF link to metadata if case_id exists
    if case_id and metadata:
        metadata["pdf_link"] = _generate_pdf_link(case_id)

    return {
        "query":                query,
        "output_type":          "case_answer",
        "intent":               intent_info["intent"],
        "is_unique":            True,
        "is_generic":           False,
        "case_id":              case_id,
        "case_name":            intent_info.get("case_name") or metadata.get("case_name"),
        "case_metadata":        metadata,
        "answer":               answer,
        "complete_explanation": complete_explanation,
        "case_summary":         case_summary,  # FIX #3
        "paragraph_references": _build_paragraph_refs(top_paragraphs),
        "judgment_paragraphs":  [],   # Not showing full judgment in this mode
        "results":              [],   # Not showing multi-case table
        "tabular_results":      [],
        "citation_tree":        None,
        "citations_flat":       None,
        "total_results":        len(top_paragraphs),
        "session_id":           session_id,
    }


def _build_law_answer(query, conn, engine, final_results, intent_info, limit, session_id):
    """
    output_type: law
    → LLM law explanation + related case table
    LLM: YES
    """
    para_results, case_results_list = _split_results(final_results)
    para_results = _fill_paragraphs(conn, engine, para_results, case_results_list, query, intent_info)
    top_paragraphs = para_results[:5]

    context = format_context_for_llm(top_paragraphs, query)
    answer = generate_research_answer(query=query, context=context, mode="law")

    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    
    # Add PDF links to all case results
    for case_result in case_results:
        if case_result.get("case_id"):
            case_result["pdf_link"] = _generate_pdf_link(case_result["case_id"])
    
    tabular = _build_tabular_results(case_results, intent_info)

    return {
        "query":               query,
        "output_type":         "law",
        "intent":              intent_info["intent"],
        "is_unique":           False,
        "is_generic":          False,
        "answer":              answer,
        "paragraph_references": _build_paragraph_refs(top_paragraphs),
        "results":             [],  # FIX: Clear to prevent duplication (tabular_results used instead)
        "tabular_results":     tabular,
        "citation_tree":       None,
        "citations_flat":      None,
        "total_results":       len(case_results),
        "session_id":          session_id,
    }


def _build_rag_answer(query, conn, engine, final_results, intent_info, limit, session_id):
    """
    output_type: answer
    → LLM RAG answer + complete explanation + paragraph refs + supporting case list
    LLM: YES
    """
    para_results, case_results_list = _split_results(final_results)
    para_results = _fill_paragraphs(conn, engine, para_results, case_results_list, query, intent_info)
    para_results = _filter_paragraphs_by_intent(para_results, intent_info)
    
    # FIX #6: Apply stricter quality filtering
    para_results = _filter_paragraphs_by_quality(para_results, min_quality=0.5, min_length=80)
    top_paragraphs = para_results[:5]

    context = format_context_for_llm(top_paragraphs, query)
    answer = generate_research_answer(query=query, context=context, mode="research")
    
    # FIX #2: Add complete explanation for depth
    complete_explanation = None
    try:
        if len(top_paragraphs) >= 3:
            complete_explanation = generate_research_answer(
                query=query,
                context=context,
                mode="detailed"
            )
    except Exception as e:
        log.debug(f"[RAG-ANSWER] Complete explanation generation failed: {e}")

    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    
    # Add PDF links to all case results
    for case_result in case_results:
        if case_result.get("case_id"):
            case_result["pdf_link"] = _generate_pdf_link(case_result["case_id"])
    
    citation_tree, citations_flat = _fetch_citations_from_results(conn, engine, final_results)

    return {
        "query":               query,
        "output_type":         "answer",
        "intent":              intent_info["intent"],
        "is_unique":           intent_info["is_unique"],
        "is_generic":          False,
        "answer":              answer,
        "complete_explanation": complete_explanation,  # FIX #2
        "paragraph_references": _build_paragraph_refs(top_paragraphs),
        "results":             case_results,
        "tabular_results":     [],
        "citation_tree":       citation_tree,
        "citations_flat":      citations_flat,
        "total_results":       len(case_results),
        "session_id":          session_id,
    }


def _build_table(query, conn, final_results, intent_info, limit, session_id):    
    """
    output_type: table
    → tabular case results only
    LLM: skipped
    """
    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    
    # Add PDF links to all case results
    for case_result in case_results:
        if case_result.get("case_id"):
            case_result["pdf_link"] = _generate_pdf_link(case_result["case_id"])
    
    tabular = _build_tabular_results(case_results, intent_info)

    return {
        "query":               query,
        "output_type":         "table",
        "intent":              intent_info["intent"],
        "is_unique":           False,
        "is_generic":          True,
        "answer":              None,
        "paragraph_references": [],
        "results":             [],  # FIX: Clear to prevent duplication (tabular_results used instead)
        "tabular_results":     tabular,
        "citation_tree":       None,
        "citations_flat":      None,
        "total_results":       len(case_results),
        "session_id":          session_id,
    }


def _build_hybrid(query, conn, engine, final_results, intent_info, limit, session_id):
    """
    output_type: hybrid
    → LLM answer + top case highlight + tabular results + paragraph refs + citations
    LLM: YES
    """
    para_results, case_results_list = _split_results(final_results)
    para_results = _fill_paragraphs(conn, engine, para_results, case_results_list, query, intent_info)
    
    # FIX #6: Apply quality filtering
    para_results = _filter_paragraphs_by_quality(para_results, min_quality=0.5, min_length=80)
    top_paragraphs = para_results[:5]

    context = format_context_for_llm(top_paragraphs, query)
    answer = generate_research_answer(query=query, context=context, mode="research")
    
    # FIX #2: Add complete explanation
    complete_explanation = None
    try:
        if len(top_paragraphs) >= 3:
            complete_explanation = generate_research_answer(
                query=query,
                context=context,
                mode="detailed"
            )
    except Exception as e:
        log.debug(f"[HYBRID] Complete explanation generation failed: {e}")

    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    
    # Add PDF links to all case results
    for case_result in case_results:
        if case_result.get("case_id"):
            case_result["pdf_link"] = _generate_pdf_link(case_result["case_id"])
    
    tabular = _build_tabular_results(case_results, intent_info)
    
    # FIX #8: Add top case highlight for better UX
    top_case_highlight = case_results[0] if case_results else None

    return {
        "query":               query,
        "output_type":         "hybrid",
        "intent":              intent_info["intent"],
        "is_unique":           False,
        "is_generic":          True,
        "answer":              answer,
        "complete_explanation": complete_explanation,  # FIX #2
        "top_case_highlight":  top_case_highlight,  # FIX #8
        "paragraph_references": _build_paragraph_refs(top_paragraphs),
        "results":             [],  # FIX: Clear to prevent duplication (tabular_results used instead)
        "tabular_results":     tabular,
        "citation_tree":       None,
        "citations_flat":      None,
        "total_results":       len(case_results),
        "session_id":          session_id,
    }


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _boost_case_name_matches(query: str, results: List[SearchResult]) -> List[SearchResult]:
    """Move results whose case_name contains 2+ query keywords to the top."""
    try:
        keywords = [k for k in re.sub(r"[^\w\s]", " ", query.lower()).split()
                    if len(k) > 2]
        exact, other = [], []
        for r in results:
            hits = sum(1 for k in keywords if k in (r.case_name or "").lower())
            (exact if hits >= 2 else other).append((hits, r))
        exact.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in exact] + [r for _, r in other]
    except Exception as e:
        log.warning(f"[BOOST] {e}")
        return results


def _case_name_fallback(query, engine, seen, sorted_results, limit) -> List[SearchResult]:
    """If under 3 results, try structured case-name search as fallback."""
    try:
        clean = re.sub(
            r"\b(judgment|what\s+is|show\s+me|find|search|for|citation|case)\b",
            " ", query.lower()
        )
        clean = " ".join(re.sub(r"[^\w\s]", " ", clean).split())
        if len(clean) <= 3:
            return sorted_results

        name_hits = engine.structured.search_by_case_name(clean, limit=limit * 2)
        for r in name_hits:
            r.relevance_score = 0.95
            if r.case_id not in seen:
                seen[r.case_id] = r
                sorted_results.append(r)
            elif 0.95 > (seen[r.case_id].relevance_score or 0):
                seen[r.case_id].relevance_score = 0.95

        sorted_results = sorted(
            seen.values(), key=lambda x: x.relevance_score or 0, reverse=True
        )
        log.info(f"[FALLBACK] Case name search added {len(name_hits)} results")
    except Exception as e:
        log.warning(f"[FALLBACK] {e}")
    return sorted_results


def _split_results(results: List[SearchResult]):
    para = [r for r in results if r.result_type == "paragraph"]
    cases = [r for r in results if r.result_type == "case"]
    return para, cases


def _fill_paragraphs(conn, engine, para_results, case_results_list, query, intent_info):
    """
    Ensure we have at least 3 paragraph hits for LLM context.
    Fetches judgment paragraphs for exact case matches, or quality paragraphs
    for top cases, as needed.
    """
    if len(para_results) >= 3:
        return para_results

    if case_results_list:
        top_case = case_results_list[0]
        is_exact = (top_case.relevance_score or 0) >= 0.9
        if is_exact:
            extra = _fetch_judgment_paragraphs(conn, top_case.case_id)
            para_results.extend(extra)

    if len(para_results) < 3 and case_results_list:
        extra = _fetch_paragraphs_for_cases(
            conn,
            [r.case_id for r in case_results_list[:3]],
            query,
        )
        para_results.extend(extra)

    return para_results


def _filter_paragraphs_by_intent(
    paragraphs: List[SearchResult], intent_info: Dict[str, Any]
) -> List[SearchResult]:
    """Prioritize paragraphs matching the detected intent's para_types."""
    target = intent_info.get("para_types", [])
    if not target or intent_info["intent"] in ("mixed", "hybrid"):
        return paragraphs

    prioritized, other = [], []
    for p in paragraphs:
        pt = (p.metadata or {}).get("para_type", "").lower()
        (prioritized if any(t.lower() in pt for t in target) else other).append(p)

    log.info(f"[FILTER] {len(prioritized)} prioritized paragraphs for intent={intent_info['intent']}")
    return prioritized + other


# ---------------------------------------------------------------------------
# DB fetchers
# ---------------------------------------------------------------------------

def _fetch_case_metadata(conn, case_id: str) -> Dict[str, Any]:
    """Fetch full case metadata row. Selects only columns that exist in schema."""
    if not case_id:
        return {}
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Only select columns that definitely exist in legal_cases table
        cursor.execute("""
            SELECT case_id, case_name, court, year, petitioner, respondent
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))
        row = cursor.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        log.warning(f"[META] {e}")
        return {}
    finally:
        cursor.close()


def _fetch_judgment_paragraphs(conn, case_id: str) -> List[SearchResult]:
    """
    Fetch judgment/order paragraphs for a case, ordered by type priority
    then quality score. Used for judgment_only and full_case views.
    """
    if not case_id:
        return []
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT p.paragraph_id, p.case_id, c.case_name,
                   p.text, p.quality_score, p.para_no, p.page_no, p.para_type
            FROM legal_paragraphs p
            JOIN legal_cases c ON p.case_id = c.case_id
            WHERE p.case_id = %s
              AND p.para_type IN ('judgment', 'order')
              AND (p.quality_score IS NULL OR p.quality_score > 0.1)
              AND p.word_count > 30
            ORDER BY
              CASE WHEN p.para_type = 'judgment' THEN 1
                   WHEN p.para_type = 'order'    THEN 2
                   ELSE 3 END,
              COALESCE(p.quality_score, 0.5) DESC
            LIMIT 30
        """, (case_id,))
        rows = cursor.fetchall()
        return [_row_to_search_result(r) for r in rows]
    except Exception as e:
        log.warning(f"[JUDGMENT-FETCH] {e}")
        return []
    finally:
        cursor.close()


def _fetch_paragraphs_for_cases(
    conn, case_ids: List[str], query: str
) -> List[SearchResult]:
    """Fetch highest-quality paragraphs for a set of case IDs."""
    if not case_ids:
        return []
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT p.paragraph_id, p.case_id, c.case_name,
                   p.text, p.quality_score, p.para_no, p.page_no, p.para_type
            FROM legal_paragraphs p
            JOIN legal_cases c ON p.case_id = c.case_id
            WHERE p.case_id = ANY(%s)
              AND p.quality_score > 0.3
              AND p.word_count > 30
            ORDER BY p.quality_score DESC
            LIMIT 15
        """, (case_ids,))
        return [_row_to_search_result(r) for r in cursor.fetchall()]
    finally:
        cursor.close()


def _fetch_all_paragraphs_for_case(conn, case_id: str) -> List[SearchResult]:
    """
    Fetch ALL paragraph types for a single case, ordered by type priority
    then quality. Used by case_answer so the LLM can draw on facts/issues/
    judgment/order depending on what the user actually asked.
    """
    if not case_id:
        return []
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT p.paragraph_id, p.case_id, c.case_name,
                   p.text, p.quality_score, p.para_no, p.page_no, p.para_type
            FROM legal_paragraphs p
            JOIN legal_cases c ON p.case_id = c.case_id
            WHERE p.case_id = %s
              AND (p.quality_score IS NULL OR p.quality_score > 0.05)
              AND p.word_count > 40
            ORDER BY
              CASE WHEN p.para_type = 'judgment' THEN 1
                   WHEN p.para_type = 'order'    THEN 2
                   WHEN p.para_type = 'issues'   THEN 3
                   WHEN p.para_type = 'facts'    THEN 4
                   ELSE 5 END,
              p.quality_score DESC
            LIMIT 20
        """, (case_id,))
        return [_row_to_search_result(r) for r in cursor.fetchall()]
    except Exception as e:
        log.warning(f"[ALL-PARAS] {e}")
        return []
    finally:
        cursor.close()


def _build_case_explanation_block(metadata: Dict[str, Any], answer: str) -> str:
    """
    Build the structured header block for case_answer UI.
    Returned as a markdown string — frontend renders it above the answer.
    Guards against answer=None when LLM is not configured.
    """
    name  = metadata.get("case_name", "Unknown")
    court = metadata.get("court", "")
    year  = str(metadata.get("year", "")) if metadata.get("year") is not None else ""
    cit   = metadata.get("citation", "") or ""
    bench = metadata.get("bench", "") or ""

    lines: List[str] = [f"**{name}**", ""]
    if court: lines.append(f"**Court:** {court}")
    if year:  lines.append(f"**Year:** {year}")
    if cit:   lines.append(f"**Citation:** {cit}")
    if bench: lines.append(f"**Bench:** {bench}")
    lines += ["", "---", ""]
    # Guard: answer may be None if LLM is unavailable
    lines.append(answer or "_LLM answer unavailable — check generator configuration._")
    return "\n".join(lines)


def _row_to_search_result(r) -> SearchResult:
    return SearchResult(
        case_id=r["case_id"],
        case_name=r["case_name"],
        relevance_score=r["quality_score"] or 0.5,
        search_mode=SearchMode.SEMANTIC.value,
        result_type="paragraph",
        metadata={
            "paragraph_id": r["paragraph_id"],
            "text":         r["text"],
            "quality":      r["quality_score"],
            "para_no":      r["para_no"],
            "page_no":      r["page_no"],
            "para_type":    r.get("para_type", "general"),
        },
    )


def _fetch_citations(conn, engine, case_id: Optional[str]):
    """Return (citation_tree, citations_flat) for a case_id."""
    if not case_id:
        return None, None
    try:
        tree = build_full_citation_tree(conn, case_id, max_depth=2)
        flat = engine.relationship.get_citations(case_id)[:20]
        
        # DEBUG: Log citation object type for debugging serialization issues
        if flat:
            log.info(f"[CITATIONS-DEBUG] Retrieved {len(flat)} citations")
            log.info(f"[CITATIONS-DEBUG] Type: {type(flat[0])}")
            log.info(f"[CITATIONS-DEBUG] Has keys method: {hasattr(flat[0], 'keys')}")
            log.info(f"[CITATIONS-DEBUG] Has __dict__: {hasattr(flat[0], '__dict__')}")
            log.info(f"[CITATIONS-DEBUG] Has _asdict: {hasattr(flat[0], '_asdict')}")
            if hasattr(flat[0], 'keys'):
                log.info(f"[CITATIONS-DEBUG] Keys: {list(flat[0].keys())}")
        
        return tree, flat
    except Exception as e:
        log.warning(f"[CITATIONS] {e}")
        return None, None


def _resolve_primary_case(results: List[SearchResult]) -> Optional[str]:
    """
    Extract the primary case_id from search results.
    Priority:
      1. First case result
      2. First paragraph result → use its case_id
      3. None if no results
    """
    # First, try case results
    for r in results:
        if r.result_type == "case":
            return r.case_id
    
    # Fallback: try paragraph results
    for r in results:
        if r.result_type == "paragraph" and hasattr(r, 'case_id'):
            return r.case_id
    
    return None


def _fetch_citations_from_results(conn, engine, results: List[SearchResult]):
    """
    Pick primary case from results and fetch its citations.
    Works even if top results are paragraphs (uses their case_id).
    """
    case_id = _resolve_primary_case(results)
    if not case_id:
        return None, None
    return _fetch_citations(conn, engine, case_id)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _jsonify_citations(citations_list):
    """Convert citation objects to plain JSON-serializable dicts. Handles multiple object types."""
    if not citations_list:
        return []
    
    result = []
    for cit in citations_list:
        try:
            # Case 1: Already a plain dict
            if isinstance(cit, dict):
                result.append(cit)
                continue
            
            # Case 2: psycopg2 RealDictRow or similar mapping type (has keys/values methods)
            if hasattr(cit, 'keys') and hasattr(cit, 'values'):
                result.append(dict(cit))
                continue
            
            # Case 3: Pydantic model with model_dump
            if hasattr(cit, 'model_dump'):
                result.append(cit.model_dump())
                continue
            
            # Case 4: Pydantic model with dict method
            if hasattr(cit, 'dict'):
                result.append(cit.dict())
                continue
            
            # Case 5: dataclass or object with __dict__
            if hasattr(cit, '__dict__'):
                d = {}
                for k, v in cit.__dict__.items():
                    if not k.startswith('_'):
                        d[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
                result.append(d)
                continue
            
            # Case 6: namedtuple or similar with _asdict
            if hasattr(cit, '_asdict'):
                result.append(dict(cit._asdict()))
                continue
            
            log.warning(f"[CITATIONS] Unknown citation type: {type(cit)}, skipping")
            
        except Exception as e:
            log.warning(f"[CITATIONS] Failed to convert citation: {e}")
    
    return result

def _generate_pdf_link(case_id: str) -> str:
    return f"/case/{case_id}/{case_id}.pdf"


def _build_tabular_results(
    results: List[Dict[str, Any]], intent_info: Dict[str, Any]
) -> List[Dict[str, Any]]:
    tabular = []
    for i, r in enumerate(results):
        text = r.get("paragraph_text", "")
        tabular.append({
            "index":            i + 1,
            "case_name":        r.get("case_name", "Unknown"),
            "case_id":          r.get("case_id"),
            "court":            r.get("court", ""),
            "year":             r.get("year"),
            "output_type":      intent_info.get("output_type", "hybrid"),
            "para_type":        r.get("para_type", "general"),
            "text_snippet":     (text[:250] + "...") if len(text) > 250 else text,
            "confidence_score": round(r.get("relevance_score", 0), 3),
            "pdf_link":         _generate_pdf_link(r.get("case_id", "")),
            "para_no":          r.get("paragraph_id", ""),
            "is_primary":       i == 0,
        })
    return tabular


def _build_paragraph_refs(results: List[SearchResult]) -> List[Dict[str, Any]]:
    refs = []
    for r in results:
        if r.result_type != "paragraph":
            continue
        meta = r.metadata or {}
        text = meta.get("text", "")
        refs.append({
            "paragraph_id":    meta.get("paragraph_id", ""),
            "case_id":         r.case_id,
            "case_name":       r.case_name,
            "text_snippet":    text[:300] + ("..." if len(text) > 300 else ""),
            "relevance_score": round(r.relevance_score or 0, 4),
            "page_no":         meta.get("page_no"),
            "para_no":         meta.get("para_no"),
            "para_type":       meta.get("para_type", "general"),
        })
    return refs


def _para_results_to_dicts(results: List[SearchResult]) -> List[Dict[str, Any]]:
    """Convert paragraph SearchResults to plain dicts for JSON serialization."""
    out = []
    for r in results:
        meta = r.metadata or {}
        out.append({
            "paragraph_id": meta.get("paragraph_id"),
            "case_id":      r.case_id,
            "case_name":    r.case_name,
            "para_no":      meta.get("para_no"),
            "page_no":      meta.get("page_no"),
            "para_type":    meta.get("para_type", "general"),
            "text":         meta.get("text", ""),
            "quality":      meta.get("quality"),
        })
    return out