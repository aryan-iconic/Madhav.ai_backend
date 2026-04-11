"""
case_brief_helpers.py
=====================
Helpers for generating comprehensive case intelligence briefs.
Supports full_case output_type in research mode.
"""

import logging
from typing import List, Dict, Any

from database.hybrid_search import SearchResult

log = logging.getLogger(__name__)


def build_para_context_for_summary(paragraphs: List[SearchResult]) -> str:
    """
    Build rich paragraph context grouped by type for the LLM.
    Groups: facts → issues → judgment → order
    Includes para_no so LLM can reference them.
    
    Returns structured markdown-like text with para refs for LLM prompt.
    """
    groups = {"facts": [], "issues": [], "judgment": [], "order": [], "general": []}
    
    for p in paragraphs:
        meta = p.metadata or {}
        ptype = meta.get("para_type", "general").lower()
        para_no = meta.get("para_no", "?")
        text = (meta.get("text", "") or "").strip()
        if not text:
            continue
        
        # Trim each para to 600 chars max but keep para_no reference
        snippet = text[:600] + ("..." if len(text) > 600 else "")
        group = ptype if ptype in groups else "general"
        groups[group].append(f"[Para {para_no}] {snippet}")
    
    parts = []
    labels = {
        "facts":    "FACTS OF THE CASE",
        "issues":   "ISSUES / QUESTIONS OF LAW",
        "judgment": "JUDGMENT / RATIO",
        "order":    "FINAL ORDER",
        "general":  "OTHER PARAGRAPHS",
    }
    
    for key in ["facts", "issues", "judgment", "order", "general"]:
        items = groups[key]
        if items:
            parts.append(f"=== {labels[key]} ===")
            # Include up to 4 paras per type to control token count
            parts.extend(items[:4])
    
    return "\n\n".join(parts)


def build_fallback_brief(metadata: dict) -> str:
    """
    No-LLM fallback — builds a structured brief from DB fields only.
    Used when LLM is unavailable or times out.
    
    Returns formatted text suitable for display in Summary tab.
    """
    lines = []
    name = metadata.get("case_name", "Unknown")
    court = metadata.get("court", "")
    year = metadata.get("year", "")
    petitioner = metadata.get("petitioner", "")
    respondent = metadata.get("respondent", "")
    outcome = metadata.get("outcome_summary", "")
    acts = metadata.get("acts_referred") or []
    
    lines.append(f"**{name}**")
    if court or year:
        lines.append(f"**Court:** {court}  |  **Year:** {year}")
    if petitioner:
        lines.append(f"**Petitioner:** {petitioner}")
    if respondent:
        lines.append(f"**Respondent:** {respondent}")
    if acts:
        if isinstance(acts, list):
            acts_str = ", ".join(str(a) for a in acts[:5])
        else:
            acts_str = str(acts)
        lines.append(f"**Acts:** {acts_str}")
    if outcome:
        lines.append(f"\n**Outcome:** {outcome}")
    
    return "\n".join(lines) if lines else None
