"""
madhav.ai — Precedent Intelligence Engine
Day 2 Sprint: Precedent Status + Citation Context

Endpoints:
    GET  /api/cases/{case_id}/precedent-status   → Is this case still good law?
    GET  /api/cases/{case_id}/citation-context   → Why was this case cited?
    POST /api/cases/bulk-precedent-status        → Status for multiple cases

Uses your DB schema:
    - legal_cases (case_id, case_name, judgment, citation_count, authority_score)
    - legal_paragraphs (paragraph_id, case_id, text, para_no)
    - case_citations (source_case_id, cited_case_id, target_citation, relationship)
    - precedent_status (case_id, status, strength, label, treatment_counts, citing_count)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import re
import httpx
import json
import logging
from psycopg2.extras import RealDictCursor

from Backend.db import get_connection

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cases", tags=["precedent"])

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.1:8b"


# ─────────────────────────────────────────────
# Trigger phrase dictionaries
# ─────────────────────────────────────────────

TREATMENT_PHRASES = {
    "overruled": [
        "is hereby overruled",
        "stands overruled",
        "is overruled",
        "are overruled",
        "overruled by",
        "overruled to the extent",
        "no longer good law",
        "cannot be considered good law",
        "not a good law",
        "is bad law",
    ],
    "distinguished": [
        "is distinguishable",
        "can be distinguished",
        "are distinguishable",
        "distinguished from",
        "distinguishable on facts",
        "distinguishable on the ground",
        "not applicable to the facts",
        "not applicable in the present case",
        "inapplicable here",
        "on different facts",
        "factually distinguishable",
    ],
    "followed": [
        "is squarely applicable",
        "is directly applicable",
        "we respectfully follow",
        "we follow the ratio",
        "ratio is followed",
        "fully applicable",
        "this court follows",
        "we are bound by",
        "applied in the present case",
        "relied upon",
        "we rely on",
        "reliance is placed",
    ],
    "affirmed": [
        "affirmed by",
        "upheld by",
        "confirmed by",
        "approved by",
        "approved the view",
    ],
    "doubted": [
        "is doubted",
        "expressed doubt",
        "with some doubt",
        "not entirely correct",
        "may not be correct",
    ],
}

# Compile all patterns once at import time
_COMPILED: Dict[str, List[re.Pattern]] = {
    treatment: [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in phrases]
    for treatment, phrases in TREATMENT_PHRASES.items()
}


# ─────────────────────────────────────────────
# Core detection logic (no DB needed)
# ─────────────────────────────────────────────

def detect_treatment_in_text(text: str) -> Dict[str, int]:
    """
    Scan a block of text for precedent treatment phrases.
    Returns { treatment_type: count, ... } for all found treatments.
    """
    found = {}
    for treatment, patterns in _COMPILED.items():
        count = sum(len(p.findall(text)) for p in patterns)
        if count:
            found[treatment] = count
    return found


def extract_context_window(text: str, citation_str: str, window: int = 300) -> Optional[str]:
    """
    Find citation_str in text and return surrounding context (window chars each side).
    Returns None if citation not found.
    """
    idx = text.lower().find(citation_str.lower())
    if idx == -1:
        return None
    start = max(0, idx - window)
    end   = min(len(text), idx + len(citation_str) + window)
    snippet = text[start:end].strip()
    # Clean up whitespace
    snippet = re.sub(r'\s+', ' ', snippet)
    return snippet


def score_precedent_strength(followed: int, distinguished: int, doubted: int, overruled: int) -> int:
    """
    Returns 0–100 strength score.
    Overruled → 0. Purely followed → 100. Mixed → weighted.
    """
    if overruled > 0:
        return 0
    total = followed + distinguished + doubted
    if total == 0:
        return 50  # unknown, neutral
    positive = followed
    negative = distinguished + doubted
    return min(100, int((positive / (positive + negative * 1.5)) * 100))


def determine_status_label(counts: Dict[str, int]) -> tuple[str, str]:
    """
    Given treatment counts across all citing cases, return (status, label).
    status: "good_law" | "overruled" | "distinguished" | "doubted" | "unknown"
    label:  human-readable description
    """
    overruled    = counts.get("overruled", 0)
    distinguished = counts.get("distinguished", 0)
    followed     = counts.get("followed", 0) + counts.get("affirmed", 0)
    doubted      = counts.get("doubted", 0)

    if overruled > 0:
        return "overruled", f"Overruled in {overruled} later case{'s' if overruled > 1 else ''}"
    if followed > 0 and distinguished == 0 and doubted == 0:
        return "good_law", f"Good law — followed in {followed} later case{'s' if followed > 1 else ''}"
    if followed > 0 and (distinguished > 0 or doubted > 0):
        return "good_law", f"Generally followed ({followed}×) but distinguished in some cases ({distinguished}×)"
    if distinguished > 0 and followed == 0:
        return "distinguished", f"Distinguished in {distinguished} later case{'s' if distinguished > 1 else ''} — verify applicability"
    if doubted > 0:
        return "doubted", f"Doubted in {doubted} later case{'s' if doubted > 1 else ''} — treat with caution"
    return "unknown", "No later treatment found — status unknown"


# ─────────────────────────────────────────────
# LLM: extract "cited for" proposition
# ─────────────────────────────────────────────

async def extract_cited_proposition(
    citing_case_name: str,
    target_citation: str,
    context_snippet: str
) -> str:
    """
    Ask Ollama: given this paragraph from a judgment, for what legal proposition
    was target_citation cited?
    Returns a 1–2 sentence string, or falls back to empty string on error.
    """
    prompt = f"""You are a legal research assistant analyzing Indian court judgments.

In the case "{citing_case_name}", the following text appears near the citation "{target_citation}":

---
{context_snippet}
---

In ONE sentence (max 25 words), state the specific legal proposition for which "{target_citation}" was cited.
Do NOT start with "The court" or "It was held". Start directly with the proposition.
Example: "Right to privacy is a fundamental right under Article 21 of the Constitution."

Proposition:"""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 60},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(OLLAMA_URL, json=payload)
            data = resp.json()
            proposition = data.get("response", "").strip()
            # Strip any "Proposition:" prefix the model sometimes repeats
            proposition = re.sub(r'^proposition\s*:\s*', '', proposition, flags=re.IGNORECASE).strip()
            return proposition[:200]  # hard cap
    except Exception:
        return ""


# ─────────────────────────────────────────────
# Endpoint 1: Precedent Status
# ─────────────────────────────────────────────

@router.get("/{case_id}/precedent-status")
async def get_precedent_status(case_id: str):
    """
    For a given case, scan all cases that cite it and determine:
    - Is it still good law?
    - How many times followed / distinguished / overruled?
    - Precedent strength score (0–100)

    Response:
    {
        "case_id": "...",
        "case_name": "...",
        "status": "good_law" | "overruled" | "distinguished" | "doubted" | "unknown",
        "status_label": "Good law — followed in 23 later cases",
        "strength_score": 87,
        "treatment_counts": { "followed": 23, "distinguished": 4, ... },
        "citing_cases_scanned": 27,
        "top_citing_cases": [ { "case_name", "citation", "treatment", "year" }, ... ]
    }
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # ── Fetch the target case's details ──
        cursor.execute("""
            SELECT case_id, case_name, case_name as citation, year
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))

        case_row = cursor.fetchone()
        if not case_row:
            cursor.close()
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        target_case_name = case_row['case_name']

        # ── Fetch all cases that cite this case ──
        cursor.execute("""
            SELECT 
                c.case_id,
                c.case_name,
                c.year,
                c.judgment
            FROM legal_cases c
            INNER JOIN case_citations cc ON cc.source_case_id = c.case_id
            WHERE cc.cited_case_id = %s
            LIMIT 200
        """, (case_id,))

        citing_cases = cursor.fetchall()
        cursor.close()

        # ── Scan every citing case for treatment phrases ──
        total_counts: Dict[str, int] = {}
        top_citing: List[Dict[str, Any]] = []

        for case in citing_cases:
            judgment_text = case.get("judgment", "")
            if not judgment_text:
                continue

            # Only scan a window around the citation (faster, more accurate)
            local_window = extract_context_window(judgment_text, target_case_name, window=1000) or judgment_text[:3000]
            case_counts = detect_treatment_in_text(local_window)

            for treatment, count in case_counts.items():
                total_counts[treatment] = total_counts.get(treatment, 0) + count

            # Track top treatments for this citing case
            if case_counts:
                dominant = max(case_counts, key=lambda t: case_counts[t])
                top_citing.append({
                    "case_name": case["case_name"],
                    "citation": f"{case['case_name']} ({case['year']})" if case['year'] else case['case_name'],
                    "year": case.get("year"),
                    "treatment": dominant,
                })

        # ── Derive status ──
        status, status_label = determine_status_label(total_counts)
        strength_score = score_precedent_strength(
            followed     = total_counts.get("followed", 0) + total_counts.get("affirmed", 0),
            distinguished = total_counts.get("distinguished", 0),
            doubted      = total_counts.get("doubted", 0),
            overruled    = total_counts.get("overruled", 0),
        )

        return {
            "case_id": case_id,
            "case_name": target_case_name,
            "status": status,
            "status_label": status_label,
            "strength_score": strength_score,
            "treatment_counts": total_counts,
            "citing_cases_scanned": len(citing_cases),
            "top_citing_cases": top_citing[:10],
        }

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[PRECEDENT STATUS] Error for {case_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Endpoint 2: Citation Context ("Why Cited")
# ─────────────────────────────────────────────

@router.get("/{case_id}/citation-context")
async def get_citation_context(case_id: str, use_ai: bool = False):
    """
    For a given case, return the context in which each of its citations was used.
    i.e. WHY was each cited case cited, not just that it was cited.

    Response:
    {
        "case_id": "...",
        "case_name": "...",
        "citations": [
            {
                "cited_case_name": "Maneka Gandhi v. UoI",
                "year": 1978,
                "paragraph": "Para 34",
                "context_snippet": "...the court held that Article 21 cannot be read...",
                "cited_for": "Right to travel abroad is part of personal liberty under Article 21",
                "treatment": "followed"
            },
            ...
        ]
    }

    use_ai=True  → uses Ollama to generate a clean "cited_for" proposition (slower, better)
    use_ai=False → returns raw snippet only (instant, good enough for launch)
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # ── Fetch this case's name ──
        cursor.execute("""
            SELECT case_id, case_name
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))

        case_row = cursor.fetchone()
        if not case_row:
            cursor.close()
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        case_name = case_row['case_name']

        # ── Fetch all cases this case cites ──
        cursor.execute("""
            SELECT 
                cc.id,
                cc.cited_case_id,
                cc.target_citation,
                cc.context_sentence,
                cc.paragraph_id,
                c.case_name as cited_case_name,
                c.year,
                lp.para_no
            FROM case_citations cc
            LEFT JOIN legal_cases c ON c.case_id = cc.cited_case_id
            LEFT JOIN legal_paragraphs lp ON lp.paragraph_id = cc.paragraph_id
            WHERE cc.source_case_id = %s
            ORDER BY lp.para_no NULLS LAST
            LIMIT 100
        """, (case_id,))

        cited_cases = cursor.fetchall()
        
        # ── Fetch full judgment for this case (if needed for detailed context) ──
        cursor.execute("""
            SELECT judgment FROM legal_cases WHERE case_id = %s
        """, (case_id,))
        
        judgment_row = cursor.fetchone()
        full_judgment = judgment_row['judgment'] if judgment_row else ""
        cursor.close()

        results = []

        for cited in cited_cases:
            context_snippet = cited.get("context_sentence", "")
            
            # If no context sentence stored, extract from judgment
            if not context_snippet and cited.get("target_citation") and full_judgment:
                context_snippet = extract_context_window(
                    full_judgment, 
                    cited["target_citation"], 
                    window=200
                ) or ""

            cited_for = ""
            treatment = "cited"

            # Use LLM to extract proposition if requested
            if use_ai and context_snippet and cited.get("cited_case_name"):
                cited_for = await extract_cited_proposition(
                    case_name,
                    cited.get("target_citation") or cited["cited_case_name"],
                    context_snippet
                )
            elif context_snippet:
                # Fallback: extract next sentence
                cited_for = _extract_fallback_proposition(context_snippet, cited.get("target_citation", ""))

            # Detect treatment from snippet
            if context_snippet:
                treatments = detect_treatment_in_text(context_snippet)
                if treatments:
                    treatment = max(treatments, key=lambda t: treatments[t])

            results.append({
                "cited_case_name": cited.get("cited_case_name", ""),
                "cited_case_id": cited.get("cited_case_id"),
                "year": cited.get("year"),
                "paragraph": f"Para {cited['para_no']}" if cited.get("para_no") else None,
                "context_snippet": context_snippet[:300] if context_snippet else "",
                "cited_for": cited_for,
                "treatment": treatment,
            })

        return {
            "case_id": case_id,
            "case_name": case_name,
            "citations": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[CITATION CONTEXT] Error for {case_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _extract_fallback_proposition(snippet: str, citation_str: str) -> str:
    """
    Without LLM: find the citation in the snippet and return the next sentence.
    Used when use_ai=False or when Ollama call fails.
    """
    idx = snippet.lower().find(citation_str.lower())
    if idx == -1:
        return ""
    after = snippet[idx + len(citation_str):].strip()
    # Take text up to the first period
    sentences = re.split(r'(?<=[.!?])\s+', after)
    if sentences:
        s = sentences[0].strip()
        # Remove leading conjunctions/punctuation
        s = re.sub(r'^[,;\-–—:]+\s*', '', s).strip()
        return s[:200] if len(s) > 10 else ""
    return ""


# ─────────────────────────────────────────────
# Endpoint 3: Bulk Status (for search results)
# ─────────────────────────────────────────────

class BulkStatusRequest(BaseModel):
    case_ids: List[str]


@router.post("/bulk-precedent-status")
async def bulk_precedent_status(req: BulkStatusRequest):
    """
    Get precedent status for multiple cases at once.
    Useful for search results — show status badge next to each result.

    Request: { "case_ids": ["case_1", "case_2", ...] }
    Response: { "statuses": { "case_1": { "status", "strength", "label" }, ... } }
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # ── Fetch status from cache ──
        placeholders = ",".join(["%s"] * len(req.case_ids))
        cursor.execute(f"""
            SELECT case_id, status, strength, label, treatment_counts, citing_count
            FROM precedent_status
            WHERE case_id IN ({placeholders})
        """, req.case_ids)

        statuses_dict = {}
        for row in cursor.fetchall():
            statuses_dict[row['case_id']] = {
                "status": row['status'],
                "strength": row['strength'],
                "label": row['label'],
                "treatment_counts": row.get('treatment_counts', {}),
                "citing_count": row.get('citing_count', 0),
            }

        cursor.close()

        # ── For cases not in cache, return default ──
        for case_id in req.case_ids:
            if case_id not in statuses_dict:
                statuses_dict[case_id] = {
                    "status": "unknown",
                    "strength": 50,
                    "label": "Status not yet computed (run processor)",
                    "treatment_counts": {},
                    "citing_count": 0,
                }

        return {
            "statuses": statuses_dict,
            "from_cache": True,  # These are precomputed — instant lookup
        }

    except Exception as e:
        log.error(f"[BULK STATUS] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
