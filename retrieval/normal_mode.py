"""
retrieval/normal_mode.py
========================
NORMAL MODE — No LLM. Fast. Pure search intelligence.
Pipeline: Query → Embed → Hybrid Search → Citation Tree → Return

Competes with: Manupatra, SCC Online
"""

import logging
from typing import Optional, Dict, Any, List
from psycopg2.extras import RealDictCursor

from database.hybrid_search import HybridSearchEngine, SearchMode, SearchResult
from Backend.services.citation_graph import build_full_citation_tree
from Backend.retrieval.embedder import embed_query
from Backend.retrieval.formatter import search_results_to_case_results, attach_precedent_status
from Backend.search.phrase_matcher import FieldAwareMatcher

log = logging.getLogger(__name__)

# Initialize field matcher for exact matches (court, case_id, year, etc.)
_field_matcher = FieldAwareMatcher()


def run_normal_search(
    query: str,
    conn,
    filters=None,
    limit: int = 20,
    case_context: Optional[str] = None
) -> Dict[str, Any]:
    """
    Normal Mode search pipeline:
    1. Check for exact field matches (court, case_id, year, etc.)
    2. If exact match found, return all matching cases
    3. Otherwise, generate embedding and run hybrid search
    4. Apply any filters (court, year, act)
    5. Build citation tree if a specific case is in context
    6. Return results — NO LLM involved
    """

    log.info(f"[NORMAL] Starting search: '{query}'")

    # ── Step 0: Check for exact field matches first ───────────────────────────
    # This handles: "supreme court", "SC", "2015" → return all matching cases
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    all_results = []
    search_type = "semantic"
    
    try:
        field_matches = _field_matcher.match_query_to_field(query)
        
        for field_name, value, confidence, match_type in field_matches:
            if confidence >= 0.85:  # High confidence match
                log.info(f"[NORMAL] Exact field match: {field_name}={value} (confidence={confidence})")
                
                if field_name == "court":
                    # Court match - return all cases with this court
                    court_query = """
                    SELECT case_id, case_name, court, year, 
                           COALESCE(authority_score, 0) as relevance_score,
                           appeal_no, petitioner, respondent, outcome
                    FROM legal_cases
                    WHERE court = %s
                    ORDER BY COALESCE(authority_score, 0) DESC, case_id
                    LIMIT 5000
                    """
                    cursor.execute(court_query, (value,))
                    field_results = cursor.fetchall()
                    
                    if field_results:
                        # Convert to SearchResult objects
                        for row in field_results:
                            result = SearchResult(
                                case_id=row['case_id'],
                                case_name=row['case_name'],
                                relevance_score=row['relevance_score'],
                                result_type='case',
                                search_mode='exact_field',
                                metadata={
                                    'court': row['court'],
                                    'year': row['year'],
                                    'petitioner': row['petitioner'],
                                    'respondent': row['respondent'],
                                    'outcome': row['outcome'],
                                }
                            )
                            all_results.append(result)
                        search_type = "exact_court"
                        log.info(f"[NORMAL] Found {len(all_results)} cases for court '{value}'")
                        break  # Don't try other field matches if we found exact court match
                    
                elif field_name == "case_id":
                    # Case ID match - find the exact case
                    case_query = """
                    SELECT case_id, case_name, court, year,
                           COALESCE(authority_score, 0) as relevance_score,
                           appeal_no, petitioner, respondent, outcome
                    FROM legal_cases
                    WHERE case_id = %s OR case_id ILIKE %s
                    LIMIT 1
                    """
                    cursor.execute(case_query, (value, f"{value}%"))
                    case_row = cursor.fetchone()
                    
                    if case_row:
                        result = SearchResult(
                            case_id=case_row['case_id'],
                            case_name=case_row['case_name'],
                            relevance_score=case_row['relevance_score'],
                            result_type='case',
                            search_mode='exact_field',
                            metadata={'court': case_row['court'], 'year': case_row['year']}
                        )
                        all_results.append(result)
                        search_type = "exact_case_id"
                        break
    except Exception as e:
        log.warning(f"[NORMAL] Field matching failed: {e}, falling back to semantic search")
    
    # If we found exact field matches, return them (skip semantic search)
    if all_results and search_type.startswith("exact_"):
        cursor.close()
        final_results = all_results[:limit]
        
        # ── Build citation tree ───────────────────────────────────────────────
        citation_tree = None
        citations_flat = None
        
        target_case = case_context
        if not target_case and final_results:
            top_case = next((r for r in final_results if r.result_type == 'case'), None)
            if top_case:
                target_case = top_case.case_id
        
        if target_case:
            log.info(f"[NORMAL] Building citation tree for {target_case}")
            try:
                citation_tree = build_full_citation_tree(conn, target_case, max_depth=2)
                # Also get flat citations list
                engine = HybridSearchEngine(conn)
                rel_search = engine.relationship
                citations_flat = rel_search.get_citations(target_case)[:20]
            except Exception as e:
                log.warning(f"[NORMAL] Citation tree failed: {e}")
                citations_flat = []
        
        # Format response
        formatted = search_results_to_case_results(final_results)
        formatted = attach_precedent_status(formatted, conn)
        
        return {
            "query": query,
            "mode": "normal",
            "total_results": len(final_results),
            "results": formatted,
            "cases": formatted,
            "search_type": search_type,
            "citation_tree": citation_tree,
            "citations_flat": citations_flat or [],
        }

    # ── Step 1: Generate query embedding ──────────────────────────────────────
    embedding = embed_query(query)

    # ── Step 2: Hybrid search (structured + semantic) ─────────────────────────
    engine = HybridSearchEngine(conn)

    # Use HYBRID mode for best coverage (case names + semantic paragraphs)
    raw_results = engine.search(
        query=query,
        mode=SearchMode.HYBRID,
        case_context=case_context,
        limit=limit * 2  # Over-fetch so filters can trim
    )

    # ── Step 3: Also run vector search if embedding succeeded ─────────────────
    paragraph_results = []
    if embedding:
        paragraph_results = engine.semantic.search_by_vector(embedding, limit=limit)

    # Merge: combine structured+semantic hits with pure vector hits
    all_results: List[SearchResult] = raw_results.get('results', [])
    all_results.extend(paragraph_results)

    # Deduplicate by case_id (keep highest score)
    seen = {}
    for r in all_results:
        key = r.case_id
        # Handle None scores: treat None as 0
        r_score = r.relevance_score if r.relevance_score is not None else 0
        if key not in seen:
            seen[key] = r
        else:
            # Compare with existing
            existing_score = seen[key].relevance_score if seen[key].relevance_score is not None else 0
            if r_score > existing_score:
                seen[key] = r
    # Sort with None-safe key function
    deduped = sorted(seen.values(), key=lambda x: x.relevance_score if x.relevance_score is not None else 0, reverse=True)

    # ── Step 4: Apply filters ─────────────────────────────────────────────────
    if filters:
        deduped = apply_filters_to_conn(conn, deduped, filters, limit)

    final_results = deduped[:limit]

    # ── Step 5: Build citation tree ───────────────────────────────────────────
    citation_tree = None
    citations_flat = None

    # If user clicked on a specific case OR top result is a case
    target_case = case_context
    if not target_case and final_results:
        # Auto-use the top case result for citation tree
        top_case = next((r for r in final_results if r.result_type == 'case'), None)
        if top_case:
            target_case = top_case.case_id

    if target_case:
        log.info(f"[NORMAL] Building citation tree for {target_case}")
        try:
            citation_tree = build_full_citation_tree(conn, target_case, max_depth=2)
            # Also get flat citations list
            rel_search = engine.relationship
            citations_flat = rel_search.get_citations(target_case)[:20]
        except Exception as e:
            log.warning(f"[NORMAL] Citation tree failed: {e}")

    # ── Step 6: Format response ───────────────────────────────────────────────
    case_results = search_results_to_case_results(final_results)
    
    # ── Step 7: Fetch and attach precedent status for each case ─────────────
    case_results = attach_precedent_status(case_results, conn)

    return {
        "query": query,
        "mode": "normal",
        "total_results": len(case_results),
        "results": case_results,
        "citation_tree": citation_tree,
        "citations_flat": citations_flat,
        "filters_applied": filters.model_dump() if filters else None
    }


def apply_filters_to_conn(conn, results: List[SearchResult], filters, limit: int) -> List[SearchResult]:
    """
    Post-filter results based on user-selected filters.
    For Normal Mode we filter in Python after retrieval (fast enough for MVP).
    For production, push filters into the SQL query.
    """
    from psycopg2.extras import RealDictCursor

    if not results:
        return results

    case_ids = [r.case_id for r in results]

    # Build dynamic SQL to get case metadata for these IDs
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    query = "SELECT case_id, court_code, court, year, acts_referred FROM legal_cases WHERE case_id = ANY(%s)"
    cursor.execute(query, (case_ids,))
    case_meta = {r['case_id']: dict(r) for r in cursor.fetchall()}
    cursor.close()

    filtered = []
    for r in results:
        meta = case_meta.get(r.case_id, {})

        # Court filter
        if filters.court_code and meta.get('court_code') != filters.court_code:
            continue
        if filters.court and filters.court.lower() not in (meta.get('court') or '').lower():
            continue

        # Year filter
        year = meta.get('year')
        if filters.year_from and year and year < filters.year_from:
            continue
        if filters.year_to and year and year > filters.year_to:
            continue

        # Acts filter
        if filters.acts:
            case_acts = meta.get('acts_referred') or []
            if not any(act.lower() in str(case_acts).lower() for act in filters.acts):
                continue

        filtered.append(r)

    log.info(f"[FILTER] {len(results)} → {len(filtered)} results after filtering")
    return filtered[:limit]
