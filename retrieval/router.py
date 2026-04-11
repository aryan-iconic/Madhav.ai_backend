"""
retrieval/router.py
===================
Central dispatcher — reads the mode and routes to the right handler.
This is the brain that connects main.py to normal/research/study modes.
"""

import logging
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)


def route_query(
    mode: str,
    query: str,
    conn,
    filters=None,
    limit: int = 10,
    case_context: Optional[str] = None,
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Route the query to the correct mode handler.

    Args:
        mode:         'normal' | 'research' | 'study'
        query:        The user's legal question
        conn:         PostgreSQL connection
        filters:      SearchFilters object (court, year, acts, etc.)
        limit:        Max results to return
        case_context: Optional case_id to expand citation tree
        session_id:   Chat session ID for history sidebar

    Returns:
        Dict matching the response model for that mode
    """
    log.info(f"[ROUTER] Mode={mode}, Query='{query[:60]}...'")

    if mode == "normal":
        from Backend.retrieval.normal_mode import run_normal_search
        return run_normal_search(
            query=query,
            conn=conn,
            filters=filters,
            limit=limit,
            case_context=case_context
        )

    elif mode == "research":
        from Backend.retrieval.research_mode import run_research_search
        return run_research_search(
            query=query,
            conn=conn,
            filters=filters,
            limit=limit,
            case_context=case_context,
            session_id=session_id
        )

    elif mode == "study":
        from Backend.retrieval.study_mode import run_study_search
        return run_study_search(
            query=query,
            conn=conn,
            filters=filters,
            limit=limit,
            case_context=case_context,
            session_id=session_id
        )

    else:
        raise ValueError(f"Unknown mode: '{mode}'. Must be normal | research | study")
