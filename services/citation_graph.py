"""
services/citation_graph.py
==========================
Citation graph service for Madhav.AI.
Two main jobs:
1. build_full_citation_tree()  — for Normal mode display
2. validate_citation()         — for Phase 3 citation validation
"""

import logging
from typing import Optional, Dict, Any
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)


def build_full_citation_tree(conn, case_id: str, max_depth: int = 2) -> Optional[Dict[str, Any]]:
    """
    Build a citation tree for a case — wraps the existing
    RelationshipSearch.build_citation_tree() with our response format.

    Returns a dict matching CitationTreeNode schema (serializable).
    """
    try:
        from database.hybrid_search import HybridSearchEngine
        engine = HybridSearchEngine(conn)
        tree_node = engine.relationship.build_citation_tree(case_id, max_depth=max_depth)
        return engine._serialize_tree(tree_node)
    except Exception as e:
        log.error(f"[CITATION TREE] Failed for {case_id}: {e}")
        return None


def validate_citation(conn, case_id: str) -> Dict[str, Any]:
    """
    PHASE 3 — Check if a case is still valid law.

    Checks:
    - Is it overruled? (relationship = 'overruled_by' in case_citations)
    - How many times cited? (citation_count in legal_cases)
    - Authority score
    - Basic case metadata

    Returns: CitationValidationResponse dict
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # ── Get basic case info ───────────────────────────────────────────────────
    cursor.execute("""
        SELECT case_id, case_name, court, year, citation_count, authority_score
        FROM legal_cases
        WHERE case_id = %s
    """, (case_id,))

    case = cursor.fetchone()

    if not case:
        cursor.close()
        return {
            "case_id": case_id,
            "case_name": None,
            "is_overruled": False,
            "overruled_by": None,
            "citation_count": 0,
            "authority_score": None,
            "latest_status": "unknown",
            "court": None,
            "year": None
        }

    # ── Check if overruled ────────────────────────────────────────────────────
    cursor.execute("""
        SELECT cc.source_case_id, c.case_name as overruling_case
        FROM case_citations cc
        LEFT JOIN legal_cases c ON cc.source_case_id = c.case_id
        WHERE cc.cited_case_id = %s
          AND LOWER(cc.relationship) IN ('overruled', 'overruled_by', 'overrules')
        LIMIT 1
    """, (case_id,))

    overruling = cursor.fetchone()
    cursor.close()

    is_overruled = overruling is not None
    overruled_by = overruling['overruling_case'] if overruling else None

    # Determine status
    if is_overruled:
        status = "overruled"
    elif (case['citation_count'] or 0) > 50:
        status = "highly_cited"
    elif (case['citation_count'] or 0) > 10:
        status = "valid"
    elif (case['authority_score'] or 0) > 0.7:
        status = "valid"
    else:
        status = "valid"  # Default — assume valid unless overruled

    return {
        "case_id": case_id,
        "case_name": case['case_name'],
        "is_overruled": is_overruled,
        "overruled_by": overruled_by,
        "citation_count": case['citation_count'] or 0,
        "authority_score": case['authority_score'],
        "latest_status": status,
        "court": case['court'],
        "year": case['year']
    }


def get_citing_cases_summary(conn, case_id: str, limit: int = 10) -> list:
    """
    Get summary of cases that cite this case.
    Useful for 'Cited By' panel in the UI.
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT cc.source_case_id, c.case_name, c.year, c.court,
               cc.relationship, cc.confidence, c.authority_score
        FROM case_citations cc
        JOIN legal_cases c ON cc.source_case_id = c.case_id
        WHERE cc.cited_case_id = %s
        ORDER BY c.authority_score DESC NULLS LAST
        LIMIT %s
    """, (case_id, limit))

    results = cursor.fetchall()
    cursor.close()
    return [dict(r) for r in results]


def get_relied_on_cases(conn, case_id: str, limit: int = 10) -> list:
    """
    Get cases that this case relied on (outgoing citations).
    Useful for 'Relied On' panel in the UI.
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT cc.cited_case_id, c.case_name, c.year, c.court,
               cc.relationship, cc.confidence, cc.context_sentence
        FROM case_citations cc
        LEFT JOIN legal_cases c ON cc.cited_case_id = c.case_id
        WHERE cc.source_case_id = %s
          AND LOWER(cc.relationship) IN ('relied_on', 'followed', 'applied', 'cited')
        ORDER BY cc.confidence DESC
        LIMIT %s
    """, (case_id, limit))

    results = cursor.fetchall()
    cursor.close()
    return [dict(r) for r in results]
