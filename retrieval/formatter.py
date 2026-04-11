"""
retrieval/formatter.py
======================
Converts raw SearchResult dataclass objects into clean API response dicts.
Also formats paragraph context for the LLM prompt.
"""

import logging
from typing import List, Dict, Any
from psycopg2.extras import RealDictCursor

from database.hybrid_search import SearchResult

log = logging.getLogger(__name__)


def search_results_to_case_results(results: List[SearchResult]) -> List[Dict[str, Any]]:
    """
    Convert SearchResult dataclass list → list of CaseResult dicts
    (matching the CaseResult Pydantic model in models.py)
    """
    output = []
    for r in results:
        meta = r.metadata or {}

        # Extract paragraph text snippet if available
        para_text = meta.get('text', '')
        if para_text and len(para_text) > 300:
            para_text = para_text[:300] + '...'

        output.append({
            "case_id": r.case_id,
            "case_name": r.case_name or "Unknown Case",
            "court": meta.get('court', ''),
            "year": meta.get('year'),
            "relevance_score": round(float(r.relevance_score or 0), 4),
            "result_type": r.result_type,
            "paragraph_text": para_text if r.result_type == 'paragraph' else None,
            "paragraph_id": meta.get('paragraph_id'),
            "para_type": meta.get('para_type', 'general'),  # NEW: paragraph type for intent filtering
            "citation_count": meta.get('citation_count'),
            "authority_score": meta.get('authority_score') or meta.get('quality'),
            "outcome_summary": meta.get('outcome', ''),
            "acts_referred": None,  # Not in SearchResult metadata (fetch separately if needed)
            "search_mode": r.search_mode or 'hybrid'
        })

    return output


def attach_precedent_status(case_results: List[Dict[str, Any]], conn) -> List[Dict[str, Any]]:
    """
    Fetch and attach precedent_status and precedent_strength to case results.
    
    Args:
        case_results: List of case result dicts
        conn: Database connection
        
    Returns:
        case_results with precedent_status and precedent_strength added
    """
    try:
        case_ids = [r["case_id"] for r in case_results if r.get("case_id")]
        if not case_ids:
            return case_results
            
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        # Batch fetch all precedent statuses
        placeholders = ','.join(['%s'] * len(case_ids))
        cursor.execute(f"""
            SELECT case_id, status, strength 
            FROM precedent_status 
            WHERE case_id IN ({placeholders})
        """, case_ids)
        status_rows = cursor.fetchall()
        cursor.close()
        
        status_map = {row['case_id']: {
            'precedent_status': row['status'] or 'unknown',
            'precedent_strength': row['strength'] or 0
        } for row in status_rows}
        
        # Attach status to each result
        for result in case_results:
            if result["case_id"] in status_map:
                result.update(status_map[result["case_id"]])
            else:
                result["precedent_status"] = "unknown"
                result["precedent_strength"] = 0
                
        return case_results
    except Exception as e:
        log.warning(f"[FORMATTER] Failed to attach precedent status: {e}")
        # Fallback: add default unknown status
        for result in case_results:
            if "precedent_status" not in result:
                result["precedent_status"] = "unknown"
                result["precedent_strength"] = 0
        return case_results


def format_context_for_llm(paragraph_results: List[SearchResult], query: str) -> str:
    """
    Format top paragraph results into a context string for the LLM prompt.
    This is what goes into the RAG prompt as "evidence".

    Structure:
        [Case: XYZ vs ABC (SC 2022)] — Score: 0.87
        "...paragraph text..."
        ---
    """
    if not paragraph_results:
        return "No relevant legal paragraphs found in database."

    context_parts = []
    for i, r in enumerate(paragraph_results, 1):
        meta = r.metadata or {}
        text = meta.get('text', '')

        # Clean up the text
        text = text.strip().replace('\n\n\n', '\n\n')
        if len(text) > 800:
            text = text[:800] + '...'

        year = meta.get('year', '')
        court = meta.get('court', '')
        score = round(float(r.relevance_score or 0), 3)

        header = f"[{i}] Case: {r.case_name}"
        if court or year:
            header += f" ({court} {year})".strip()
        header += f" | Relevance: {score}"

        context_parts.append(f"{header}\n{text}")

    return "\n\n---\n\n".join(context_parts)


def format_tabular_results(results: List[Dict[str, Any]]) -> str:
    """
    Format results as a human-readable table (for study mode notes).
    Used when multiple cases need to be compared.
    """
    if not results:
        return "No results found."

    lines = ["| Case | Court | Year | Relevance |", "|------|-------|------|-----------|"]
    for r in results[:10]:
        name = (r.get('case_name') or 'Unknown')[:40]
        court = (r.get('court') or '-')[:20]
        year = str(r.get('year') or '-')
        score = str(round(r.get('relevance_score', 0), 3))
        lines.append(f"| {name} | {court} | {year} | {score} |")

    return "\n".join(lines)
