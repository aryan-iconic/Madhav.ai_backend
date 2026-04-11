"""
retrieval/study_mode.py
=======================
STUDY MODE — Intent-first learning engine for Madhav.AI.

Target users: Law students, UPSC/Judiciary/CLAT aspirants, junior lawyers,
              non-legal professionals who need clarity, not complexity.

Pipeline:
  Query
    → detect_study_intent()        # study-specific intent classification
    → _lookup_case_name_in_db()    # reused from research_mode (DB-backed)
    → embed + hybrid search        # same retrieval stack as research mode
    → branch on study_output_type:
        concept_explanation  → simple explanation + key points + cases    (LLM)
        case_explanation     → student-friendly case breakdown            (LLM)
        case_brief           → structured FIHR brief                      (LLM)
        notes                → exam-ready bullet notes under headings     (LLM)
        qa_mode              → Q&A pairs for viva/MCQ prep                (LLM)
        comparison           → structured difference table                (LLM)
        bare_act_simplified  → plain-language statute explanation         (LLM)
        deep_dive            → topic + evolution + landmark cases         (LLM)
    → return typed structured JSON

All output dicts contain `study_output_type` so the frontend
knows which card/template to render.

study_output_type → UI mapping:
    concept_explanation  → Concept card (title + explanation + bullets + case chips)
    case_explanation     → Case explainer card (facts / issues / held / importance)
    case_brief           → Brief card (FIHR format — printable)
    notes                → Notes view (collapsible headings + bullet points)
    qa_mode              → Flashcard / accordion (Q on front, A on back)
    comparison           → Side-by-side table
    bare_act_simplified  → Law card (section text + plain English + example)
    deep_dive            → Long-form article (intro + timeline + case list)
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from database.hybrid_search import HybridSearchEngine, SearchMode, SearchResult
from Backend.retrieval.embedder import embed_query
from Backend.retrieval.formatter import format_context_for_llm, search_results_to_case_results, attach_precedent_status
from Backend.llm.generator import generate_research_answer
from Backend.retrieval.research_mode import (
    _boost_case_name_matches,
    _build_paragraph_refs,
    _build_tabular_results,
    _case_name_fallback,
    _fetch_all_paragraphs_for_case,
    _fetch_case_metadata,
    _fetch_judgment_paragraphs,
    _fetch_paragraphs_for_cases,
    _filter_paragraphs_by_intent,
    _generate_pdf_link,
    _lookup_case_name_in_db,
    _row_to_search_result,
    _split_results,
)
from Backend.services.citation_graph import build_full_citation_tree

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: Serialize structured output to HTML/text
# ---------------------------------------------------------------------------

def _serialize_structured_output(structured: Dict[str, Any]) -> str:
    """
    Convert a structured output dict from LLM into HTML/text format
    that can be displayed in the frontend buildStudyOutput() function.
    
    Handles different study types:
    - concept_explanation → title + explanation + key_points
    - case_explanation → case_name, facts, issues, judgment, importance
    - case_brief → facts, issues, held, ratio_decidendi, obiter_dicta
    - notes → heading + content pairs
    - qa_mode → questions
    - comparison → comparison_table
    - bare_act_simplified → section + explanation + example
    - deep_dive → introduction + cases
    """
    if not structured:
        return ""
    
    # Try to preserve the structure as-is for frontend to parse
    # The frontend will iterate through the dict and render each field
    # Convert common fields to readable HTML
    html_parts = []
    
    for key, value in structured.items():
        if key in ("important_cases", "cases", "case_chips"):
            continue  # Skip complex nested structures
        
        if isinstance(value, str):
            # Format as paragraph
            html_parts.append(f"<p><strong>{key.replace('_', ' ').title()}:</strong> {value}</p>")
        elif isinstance(value, list) and value:
            if isinstance(value[0], str):
                # List of strings
                html_parts.append(f"<p><strong>{key.replace('_', ' ').title()}:</strong></p><ul>")
                for item in value:
                    html_parts.append(f"<li>{item}</li>")
                html_parts.append("</ul>")
            elif isinstance(value[0], dict):
                # List of dicts
                html_parts.append(f"<p><strong>{key.replace('_', ' ').title()}:</strong></p>")
                for item in value:
                    html_parts.append("<div style='margin-left:12px;'>" + str(item) + "</div>")
    
    return "\n".join(html_parts) or json.dumps(structured)


# ---------------------------------------------------------------------------
# Study intent constants
# ---------------------------------------------------------------------------

# Each rule: (trigger_keywords, study_output_type, para_types_to_prioritize)
# Rules are checked in order — first match wins.
_STUDY_INTENT_RULES: List[Tuple[List[str], str, List[str]]] = [
    # ── Case brief (must come before generic "explain" catch-all) ────────────
    (["case brief", "brief of", "fihr", "ratio decidendi", "obiter"],
     "case_brief",          ["judgment", "facts", "issues", "order"]),

    # ── Comparison ───────────────────────────────────────────────────────────
    (["difference between", "compare", "distinguish between",
      " vs ", " versus ", "similarities", "contrast"],
     "comparison",          ["judgment", "law", "issues"]),

    # ── Bare act simplification ──────────────────────────────────────────────
    (["simplify", "in simple terms", "simple language", "easy language",
      "layman", "plain english", "explain section", "explain article",
      "meaning of section", "meaning of article"],
     "bare_act_simplified", ["law", "statute", "legal"]),

    # ── Q&A / viva / MCQ ─────────────────────────────────────────────────────
    (["questions on", "mcq", "viva", "quiz", "practice questions",
      "important questions", "questions for exam", "questions about"],
     "qa_mode",             ["judgment", "facts", "issues", "law"]),

    # ── Notes ────────────────────────────────────────────────────────────────
    (["notes on", "make notes", "study notes", "revision notes",
      "short notes", "notes for exam", "bullet points on"],
     "notes",               ["judgment", "facts", "issues", "law"]),

    # ── Deep dive (topic-level, no specific case) ────────────────────────────
    (["evolution of", "history of", "development of", "right to privacy",
      "right to equality", "fundamental right", "landmark cases on",
      "overview of", "all about", "deep dive"],
     "deep_dive",           ["judgment", "facts", "issues", "law"]),

    # ── Case explanation (student-friendly) ──────────────────────────────────
    (["explain case", "explain the case", "summarise case",
      "summarize case", "tell me about case", "what happened in"],
     "case_explanation",    ["facts", "issues", "judgment", "order"]),

    # ── Concept explanation — broadest catch-all, checked last ───────────────
    (["what is", "what are", "define", "meaning of", "explain",
      "describe", "tell me about", "introduction to"],
     "concept_explanation", ["law", "judgment", "facts"]),
]

# Phrases that force study_output_type = "notes" regardless of other keywords
_NOTES_TRIGGERS = ["notes on", "make notes", "study notes", "revision notes",
                   "short notes", "notes for exam"]

# Phrases that force study_output_type = "comparison"
_COMPARISON_TRIGGERS = ["difference between", "compare", "distinguish between",
                        " vs ", " versus ", "difference of", "compare the", "campare"]


# ---------------------------------------------------------------------------
# Study intent detection
# ---------------------------------------------------------------------------

def detect_study_intent(query: str, conn=None) -> Dict[str, Any]:
    """
    Classify query into a study_output_type.

    Priority:
      1. Hard trigger phrases (notes, comparison) — HIGHEST PRIORITY
      2. DB case-name lookup  → case_brief / case_explanation
      3. Keyword rules        → concept / qa / deep_dive / bare_act / etc.
      4. Fallback             → concept_explanation

    Returns dict with:
        study_output_type : str        — primary routing key
        para_types        : list[str]  — paragraph types to prioritize
        is_case_query     : bool       — query resolved to a specific case
        case_id           : str|None
        case_name         : str|None
    """
    q = query.lower().strip()

    # ── 1. Hard triggers (HIGHEST PRIORITY) ────────────────────────────────────
    # These take precedence over all other analysis
    is_notes = any(t in q for t in _NOTES_TRIGGERS)
    is_comparison = any(t in q for t in _COMPARISON_TRIGGERS)
    
    if is_notes:
        return _sintent("notes", ["judgment", "facts", "issues", "law"])

    if is_comparison:
        # For comparison queries, don't restrict to single case
        # Return comparison intent and let the builder handle multiple cases
        return _sintent("comparison", ["judgment", "law", "issues"],
                       is_case_query=False)  # Treat as topic even if cases mentioned

    # ── 2. DB case-name lookup ────────────────────────────────────────────────
    case_id, case_name = None, None
    if conn:
        case_id, case_name = _lookup_case_name_in_db(q, conn)

    # ── 3. Keyword rules ──────────────────────────────────────────────────────
    matched_type, matched_para_types = None, []
    for keywords, study_type, para_types in _STUDY_INTENT_RULES:
        if any(kw in q for kw in keywords):
            matched_type = study_type
            matched_para_types = para_types
            break

    # ── If case found, upgrade/confirm output type (but preserve comparison) ────
    if case_id:
        # NEVER downgrade comparison to case_explanation
        if matched_type == "comparison":
            return _sintent("comparison", matched_para_types,
                           is_case_query=False)  # Comparison handles multiple cases
        
        # "case brief of X" → case_brief
        if matched_type == "case_brief":
            return _sintent("case_brief",
                            ["judgment", "facts", "issues", "order"],
                            is_case_query=True, case_id=case_id, case_name=case_name)
        # "explain X case" → case_explanation
        if matched_type == "case_explanation":
            return _sintent("case_explanation",
                            ["facts", "issues", "judgment", "order"],
                            is_case_query=True, case_id=case_id, case_name=case_name)
        # Any concept/explain + a known case → case_explanation
        if matched_type in ("concept_explanation", None):
            return _sintent("case_explanation",
                            ["facts", "issues", "judgment", "order"],
                            is_case_query=True, case_id=case_id, case_name=case_name)
        # All other study types with a case hit — keep type, mark as case query
        return _sintent(matched_type, matched_para_types,
                        is_case_query=True, case_id=case_id, case_name=case_name)

    # ── 4. Use keyword match or fall back ─────────────────────────────────────
    if matched_type:
        return _sintent(matched_type, matched_para_types)

    return _sintent("concept_explanation", ["law", "judgment", "facts"])


def _sintent(
    study_output_type: str,
    para_types: List[str],
    is_case_query: bool = False,
    case_id: Optional[str] = None,
    case_name: Optional[str] = None,
) -> Dict[str, Any]:
    result = {
        "study_output_type": study_output_type,
        "para_types":        para_types,
        "is_case_query":     is_case_query,
        "case_id":           case_id,
        "case_name":         case_name,
    }
    log.info(f"[STUDY-INTENT] {result}")
    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_study_search(
    query: str,
    conn,
    filters=None,
    limit: int = 10,
    case_context: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Study mode pipeline. Returns a typed response dict with `study_output_type`
    telling the frontend which learning card template to render.
    """
    log.info(f"[STUDY] Query: '{query}'")

    # ── Step 0: Study intent detection ───────────────────────────────────────
    intent = detect_study_intent(query, conn)
    study_type = intent["study_output_type"]
    resolved_case_id = case_context or intent.get("case_id")

    # ── Step 1: Embed ─────────────────────────────────────────────────────────
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

    # Deduplicate — keep highest score per case_id
    seen: Dict[str, SearchResult] = {}
    for r in all_results:
        score = r.relevance_score or 0
        if r.case_id not in seen or score > (seen[r.case_id].relevance_score or 0):
            seen[r.case_id] = r

    sorted_results = sorted(
        seen.values(), key=lambda x: x.relevance_score or 0, reverse=True
    )

    if filters:
        from Backend.retrieval.normal_mode import apply_filters_to_conn
        sorted_results = apply_filters_to_conn(conn, sorted_results, filters, limit * 3)

    sorted_results = _boost_case_name_matches(query, sorted_results)

    if len(sorted_results) < 3:
        sorted_results = _case_name_fallback(query, engine, seen, sorted_results, limit)

    final_results = sorted_results[:limit]

    # ── Step 3: Build paragraph context ──────────────────────────────────────
    para_results, case_results_list = _split_results(final_results)

    # For case queries, fetch paragraphs directly from the resolved case
    if resolved_case_id and intent["is_case_query"]:
        case_paras = _fetch_all_paragraphs_for_case(conn, resolved_case_id)
        # Merge — put case-specific paragraphs first
        existing_ids = {(p.metadata or {}).get("paragraph_id") for p in para_results}
        for p in case_paras:
            if (p.metadata or {}).get("paragraph_id") not in existing_ids:
                para_results.append(p)
    elif len(para_results) < 3 and case_results_list:
        extra = _fetch_paragraphs_for_cases(
            conn, [r.case_id for r in case_results_list[:3]], query
        )
        para_results.extend(extra)

    # Prioritize paragraphs matching the intent's para_types
    para_results = _filter_study_paragraphs(para_results, intent)
    
    # Case queries need more paragraphs for accurate briefs.
    # Topic/concept queries only need 3 — the LLM uses its own knowledge primarily.
    is_case_q = intent.get("is_case_query", False)
    top_paragraphs = para_results[:6] if is_case_q else para_results[:3]
    
    raw_context = format_context_for_llm(top_paragraphs, query)
    # Cap context to 1800 chars for all study prompts.
    # This is the single biggest driver of slow responses.
    # Fallback logic in each builder fills gaps if context is thin.
    context = raw_context[:1800] + ("..." if len(raw_context) > 1800 else "")

    # ── Step 4: Route to builder ──────────────────────────────────────────────
    builders = {
        "concept_explanation": _build_concept_explanation,
        "case_explanation":    _build_case_explanation,
        "case_brief":          _build_case_brief,
        "notes":               _build_notes,
        "qa_mode":             _build_qa_mode,
        "comparison":          _build_comparison,
        "bare_act_simplified": _build_bare_act_simplified,
        "deep_dive":           _build_deep_dive,
    }
    builder = builders.get(study_type, _build_concept_explanation)

    # ── Step 5: Shared metadata ────────────────────────────────────────────────
    case_metadata = {}
    if resolved_case_id:
        case_metadata = _fetch_case_metadata(conn, resolved_case_id)

    # ── Step 6: Call builder ──────────────────────────────────────────────────
    output = builder(
        query=query,
        context=context,
        intent=intent,
        case_metadata=case_metadata,
        top_paragraphs=top_paragraphs,
        final_results=final_results,
        conn=conn,
        limit=limit,
    )

    # ── Step 7: Convert structured_output to sections format for frontend ──────
    # Frontend expects: { sections: [{output_type, body, ...}, ...] }
    structured = output.get("structured_output", {})
    
    # Convert the single structured output to sections array format
    sections = [{
        "output_type": study_type,
        "body": _serialize_structured_output(structured),
        "text": _serialize_structured_output(structured),
        **structured
    }]
    
    case_results = search_results_to_case_results(final_results[:limit])
    case_results = attach_precedent_status(case_results, conn)
    output_final = {
        "query":               query,
        "mode":                "study",
        "study_output_type":   study_type,
        "is_case_query":       intent["is_case_query"],
        "case_id":             resolved_case_id,
        "case_name":           intent.get("case_name"),
        "case_metadata":       case_metadata,
        "paragraph_references": _build_paragraph_refs(top_paragraphs),
        "sections":            sections,       # ← Frontend looks for this!
        "results":             case_results,
        "tabular_results":     _build_study_tabular(case_results),
        "total_results":       len(case_results),
        "session_id":          session_id,
    }

    return output_final


# ---------------------------------------------------------------------------
# Output builders — one per study_output_type
# ---------------------------------------------------------------------------

def _build_concept_explanation(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: concept_explanation
    Query: "What is Article 21?" / "Explain right to equality"

    Output JSON structure:
    {
      "title": "Article 21 — Right to Life and Personal Liberty",
      "explanation": "...",
      "key_points": ["...", "..."],
      "important_cases": [{"case_name": "...", "case_id": "...", "relevance": "..."}]
    }
    """
    prompt = _PROMPTS["concept_explanation"].format(query=query, context=context)
    raw = _call_llm(prompt, mode="study_concept")
    structured = _parse_json_output(raw, _DEFAULT_CONCEPT)

    # Fallback: If LLM didn't populate, fill from context and query
    if not structured.get("title"):
        structured["title"] = query
    if not structured.get("explanation") and context:
        structured["explanation"] = context[:300] + "..." if len(context) > 300 else context
    if not structured.get("key_points") or not structured["key_points"]:
        structured["key_points"] = [
            f"This is a fundamental concept in Indian law",
            f"Has been interpreted extensively by courts",
            f"Key principles are established through landmark cases",
            f"Important for exam preparation and legal practice"
        ]
    if not structured.get("exam_tip"):
        structured["exam_tip"] = f"Remember the key principles and landmark cases related to {query}"

    # Attach live case chips from search results
    structured["important_cases"] = _extract_case_chips(final_results, limit=5)

    return {"structured_output": structured}


def _build_case_explanation(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: case_explanation
    Query: "Explain Samatha vs State" / "What happened in Maneka Gandhi case"

    Output JSON structure:
    {
      "case_name": "...",
      "court": "...",
      "year": "...",
      "facts": "...",
      "issues": "...",
      "judgment": "...",
      "importance": "...",
      "key_takeaway": "..."
    }
    """
    prompt = _PROMPTS["case_explanation"].format(
        query=query,
        context=context,
        case_name=case_metadata.get("case_name", "the case"),
    )
    raw = _call_llm(prompt, mode="study_case")
    structured = _parse_json_output(raw, _DEFAULT_CASE_EXPLANATION)

    # Fill metadata fields from DB if LLM left them blank
    for field in ("case_name", "court", "year"):
        if not structured.get(field) and case_metadata.get(field):
            structured[field] = case_metadata[field]

    # Fallback: facts from context if LLM didn't provide
    if not structured.get("facts") and context:
        structured["facts"] = context[:400] + "..." if len(context) > 400 else context

    # Fallback: issues from query pattern "explain [case_name]"
    if not structured.get("issues"):
        structured["issues"] = f"The key issues involved in {case_metadata.get('case_name', 'this case')} and their legal implications"

    # Fallback: judgment from context or query
    if not structured.get("judgment"):
        if context:
            structured["judgment"] = context[200:600] + "..." if len(context) > 600 else context[200:]
        else:
            structured["judgment"] = f"The court's decision in {case_metadata.get('case_name', 'this case')} had significant legal implications"

    # Fallback: importance from case significance
    if not structured.get("importance"):
        structured["importance"] = f"This case established important precedent regarding {query}. It is frequently cited in legal practice and examination"

    # Fallback: key takeaway from judgment or context
    if not structured.get("key_takeaway"):
        if context:
            # Extract first meaningful sentence from context
            sentences = context.split('.')
            structured["key_takeaway"] = (sentences[0] + '.') if sentences[0] else f"Key principle: {query}"
        else:
            structured["key_takeaway"] = f"This case is essential for understanding {query} in Indian law"

    structured["pdf_link"] = _generate_pdf_link(
        intent.get("case_id") or ""
    )
    return {"structured_output": structured}


def _build_case_brief(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: case_brief
    Query: "Case brief of Nandini Sharma" / "FIHR of Kesavananda Bharati"

    Output JSON structure (FIHR format):
    {
      "case_name": "...",
      "citation": "...",
      "court": "...",
      "year": "...",
      "bench": "...",
      "facts": "...",
      "issues": ["...", "..."],
      "held": "...",
      "ratio_decidendi": "...",
      "obiter_dicta": "...",
      "importance": "..."
    }
    """
    prompt = _PROMPTS["case_brief"].format(
        query=query,
        context=context,
        case_name=case_metadata.get("case_name", "the case"),
    )
    raw = _call_llm(prompt, mode="study_brief")
    structured = _parse_json_output(raw, _DEFAULT_CASE_BRIEF)

    # Fill from DB metadata — these are more reliable than LLM extraction
    for field in ("case_name", "citation", "court", "year", "bench"):
        if case_metadata.get(field):
            structured[field] = case_metadata[field]

    # If LLM failed to populate, fill with meaningful fallback from context
    if not structured.get("facts") and context:
        structured["facts"] = context[:300] + "..." if len(context) > 300 else context
    if not structured.get("held") and case_metadata:
        structured["held"] = f"Case from {case_metadata.get('court', 'Court')} decided in {case_metadata.get('year', 'Year')}"
    if not structured.get("importance"):
        structured["importance"] = f"Landmark case related to {query}"
    
    structured["pdf_link"] = _generate_pdf_link(intent.get("case_id") or "")
    return {"structured_output": structured}


def _build_notes(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: notes
    Query: "Notes on Fundamental Rights" / "Study notes IPC"

    Output JSON structure:
    {
      "topic": "...",
      "headings": [
        {
          "title": "...",
          "points": ["...", "..."],
          "source_case": "..."   // optional
        }
      ]
    }
    """
    prompt = _PROMPTS["notes"].format(query=query, context=context)
    raw = _call_llm(prompt, mode="study_notes")
    structured = _parse_json_output(raw, _DEFAULT_NOTES)

    # Fallback: topic from query if not provided by LLM
    if not structured.get("topic"):
        structured["topic"] = query

    # Fallback: create default headings if LLM didn't provide
    if not structured.get("headings") or len(structured["headings"]) == 0:
        structured["headings"] = [
            {
                "title": f"Overview of {query}",
                "points": [
                    "Definition and scope",
                    "Legal framework and statutory provisions",
                    "Key principles established through case law"
                ],
                "source_case": ""
            },
            {
                "title": f"Important Cases and Interpretations",
                "points": [
                    "Landmark decisions regarding this topic",
                    "Evolution of legal interpretation",
                    "Current judicial approach"
                ],
                "source_case": ""
            },
            {
                "title": f"Practical Application and Exam Tips",
                "points": [
                    "Common examination questions",
                    "Key points to remember",
                    "Related topics for comprehensive understanding"
                ],
                "source_case": ""
            }
        ]

    # Attach source cases to headings where possible
    case_chips = _extract_case_chips(final_results, limit=len(structured.get("headings", [])))
    for i, heading in enumerate(structured.get("headings", [])):
        if i < len(case_chips) and not heading.get("source_case"):
            heading["source_case"] = case_chips[i].get("case_name", "")

    return {"structured_output": structured}


def _build_qa_mode(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: qa_mode
    Query: "Important questions on IPC 302" / "Viva questions on Article 21"

    Output JSON structure:
    {
      "topic": "...",
      "questions": [
        { "q": "...", "a": "...", "difficulty": "easy|medium|hard" }
      ]
    }
    """
    prompt = _PROMPTS["qa_mode"].format(query=query, context=context)
    raw = _call_llm(prompt, mode="study_qa")
    structured = _parse_json_output(raw, _DEFAULT_QA)

    # Fallback: topic from query if not provided
    if not structured.get("topic"):
        structured["topic"] = query

    # Fallback: generate default questions if LLM didn't provide
    if not structured.get("questions") or len(structured["questions"]) == 0:
        structured["questions"] = [
            {
                "q": f"What is the definition of {query}?",
                "a": f"{query} is a fundamental concept in Indian law that governs {query}.",
                "difficulty": "easy"
            },
            {
                "q": f"What are the key principles of {query}?",
                "a": f"The key principles include statutory interpretation, case law precedents, and their application in practice.",
                "difficulty": "medium"
            },
            {
                "q": f"How has {query} evolved through judicial interpretation?",
                "a": f"Landmark cases have shaped the interpretation and application of {query}. Courts have refined the scope and application over time.",
                "difficulty": "hard"
            },
            {
                "q": f"What is the practical significance of {query}?",
                "a": f"{query} has significant implications for Indian legal practice and is frequently tested in examinations.",
                "difficulty": "medium"
            }
        ]

    return {"structured_output": structured}


def _build_comparison(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: comparison
    Query: "Difference between Article 14 and Article 21" / "IPC vs CrPC"
           "Compare Kesavananda vs Indira Gandhi" (case comparison)

    Handles both:
    - Topic comparisons (concepts, articles, laws)
    - Case comparisons (compare two actual court cases)

    Output JSON structure:
    {
      "topic1": "...",
      "topic2": "...",
      "summary": "...",
      "differences": [
        { "point": "...", "topic1": "...", "topic2": "..." }
      ],
      "similarities": ["...", "..."]
    }
    """
    # ── Extract topic/case names from query ────────────────────────────────────
    topic1, topic2 = _extract_comparison_topics(query)
    
    # ── Try to find actual cases if this is a case comparison ────────────────────
    case1_data, case2_data = None, None
    if conn and ("vs" in query.lower() or "compare" in query.lower()):
        # Try to lookup first case
        case1_id, case1_name = _lookup_case_name_in_db(topic1, conn)
        case2_id, case2_name = _lookup_case_name_in_db(topic2, conn)
        
        if case1_id and case2_id:
            # Both cases found — this is a case comparison
            case1_data = _fetch_case_metadata(conn, case1_id)
            case2_data = _fetch_case_metadata(conn, case2_id)
            topic1 = case1_name or topic1
            topic2 = case2_name or topic2
    
    # ── Build LLM prompt with context about the comparison topic(s) ──────────────
    prompt = _PROMPTS["comparison"].format(
        query=query,
        context=context
    )
    raw = _call_llm(prompt, mode="study_compare")
    structured = _parse_json_output(raw, _DEFAULT_COMPARISON)
    
    # ── Populate topics from extracted values ───────────────────────────────────
    if not structured.get("topic1"):
        structured["topic1"] = topic1
    if not structured.get("topic2"):
        structured["topic2"] = topic2
    
    # ── Build summary ──────────────────────────────────────────────────────────
    if not structured.get("summary"):
        if case1_data and case2_data:
            structured["summary"] = (
                f"Legal comparison between {topic1} ({case1_data.get('year', 'N/A')}) "
                f"and {topic2} ({case2_data.get('year', 'N/A')}) in Indian jurisprudence"
            )
        else:
            structured["summary"] = (
                f"A comparative analysis of {topic1} and {topic2} in Indian legal context"
            )
    
    # ── Build differences ──────────────────────────────────────────────────────
    if not structured.get("differences") or len(structured["differences"]) == 0:
        differences = []
        
        if case1_data and case2_data:
            # Case-specific comparison points
            differences.extend([
                {
                    "point": "Court and Year",
                    "topic1": f"Case {topic1} ({case1_data.get('year', 'N/A')})",
                    "topic2": f"Case {topic2} ({case2_data.get('year', 'N/A')})"
                },
                {
                    "point": "Nature of Dispute",
                    "topic1": case1_data.get("case_type", "General civil dispute"),
                    "topic2": case2_data.get("case_type", "General civil dispute")
                },
                {
                    "point": "Core Legal Issue",
                    "topic1": f"Concerned with {topic1}'s principles",
                    "topic2": f"Concerned with {topic2}'s principles"
                }
            ])
        else:
            # Topic-specific comparison points
            differences = [
                {
                    "point": "Definition",
                    "topic1": f"{topic1} has specific meaning and scope in Indian law",
                    "topic2": f"{topic2} has distinct meaning and application"
                },
                {
                    "point": "Scope of Application",
                    "topic1": f"{topic1} applies in specific legal contexts",
                    "topic2": f"{topic2} applies in different legal circumstances"
                },
                {
                    "point": "Legal Framework",
                    "topic1": f"{topic1} is governed by relevant statutes and case law",
                    "topic2": f"{topic2} is governed by different legal provisions"
                },
                {
                    "point": "Constitutional/Statutory Basis",
                    "topic1": f"{topic1} derive authority from Indian Constitution/statutes",
                    "topic2": f"{topic2} derive authority from Indian legal framework"
                }
            ]
        
        structured["differences"] = differences
    
    # ── Build similarities ────────────────────────────────────────────────────
    if not structured.get("similarities") or len(structured["similarities"]) == 0:
        if case1_data and case2_data:
            # Case similarities
            structured["similarities"] = [
                f"Both {topic1} and {topic2} are landmark Supreme/High Court decisions",
                f"Both cases have shaped Indian legal principles and interpretation",
                f"Both are subject to extensive judicial reliance and scholarly analysis",
                f"Both establish precedent applicable in similar legal situations"
            ]
        else:
            # Topic similarities
            structured["similarities"] = [
                f"Both {topic1} and {topic2} are fundamental to Indian law",
                "Both have been subject to extensive judicial interpretation",
                "Both are significant topics in legal studies and examinations",
                "Both derive authority from Indian Constitution or statutory law"
            ]
    
    return {"structured_output": structured}


def _extract_comparison_topics(query: str) -> Tuple[str, str]:
    """
    Extract two topics/cases from a comparison query.
    Handles patterns: "between X and Y", "X vs Y", "X versus Y", "compare X Y"
    Returns: (topic1, topic2)
    """
    q = query.lower()
    
    # Pattern: "between X and Y"
    if " between " in q:
        parts = q.split(" between ", 1)
        if len(parts) > 1:
            rest = parts[1]
            if " and " in rest:
                topics = rest.split(" and ", 1)
                return topics[0].strip(), topics[1].strip()
    
    # Pattern: "X vs Y" or "X versus Y"
    for sep in [" vs ", " vs. ", " versus "]:
        if sep in q:
            parts = q.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    
    # Pattern: "compare X with Y" or "compare X and Y"
    if "compare " in q:
        after_compare = q.split("compare ", 1)[1]
        for sep in [" with ", " and "]:
            if sep in after_compare:
                topics = after_compare.split(sep, 1)
                return topics[0].strip(), topics[1].strip()
    
    # Fallback
    return "Topic 1", "Topic 2"


def _build_bare_act_simplified(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: bare_act_simplified
    Query: "Explain Section 302 IPC in simple terms" / "Simplify Article 21"

    Output JSON structure:
    {
      "section": "...",
      "act": "...",
      "original_text": "...",
      "simple_explanation": "...",
      "punishment": "...",
      "example": "...",
      "related_cases": [{"case_name": "...", "case_id": "..."}]
    }
    """
    prompt = _PROMPTS["bare_act_simplified"].format(query=query, context=context)
    raw = _call_llm(prompt, mode="study_bare_act")
    structured = _parse_json_output(raw, _DEFAULT_BARE_ACT)
    
    # Fallback: Extract section number from query if LLM didn't fill it
    if not structured.get("section"):
        import re
        match = re.search(r"(Article|Section)\s+(\d+[A-Z]?)", query, re.IGNORECASE)
        if match:
            structured["section"] = f"{match.group(1)} {match.group(2)}"
        structured["section"] = structured["section"] or query.split()[0]
    
    # Fill missing fields with meaningful fallback data
    if not structured.get("simple_explanation") and context:
        structured["simple_explanation"] = context[:250] + "..." if len(context) > 250 else context
    if not structured.get("act"):
        structured["act"] = "Indian Constitution / Indian Laws"
    if not structured.get("original_text") and context:
        structured["original_text"] = context.split(".")[0] if "." in context else context[:100]
    if not structured.get("who_it_applies_to"):
        structured["who_it_applies_to"] = "All persons/citizens of India"
    if not structured.get("example"):
        structured["example"] = f"This provision has been extensively applied in various landmark cases"
    if not structured.get("common_misconception"):
        structured["common_misconception"] = f"A common misunderstanding is that this only applies in specific circumstances"
    
    structured["related_cases"] = _extract_case_chips(final_results, limit=3)
    return {"structured_output": structured}


def _build_deep_dive(
    query, context, intent, case_metadata,
    top_paragraphs, final_results, conn, limit, **_
) -> Dict[str, Any]:
    """
    study_output_type: deep_dive
    Query: "Right to Privacy in India" / "Evolution of Article 21"

    Output JSON structure:
    {
      "topic": "...",
      "introduction": "...",
      "evolution": "...",
      "landmark_cases": [
        { "case_name": "...", "case_id": "...", "year": "...", "significance": "..." }
      ],
      "current_position": "...",
      "conclusion": "..."
    }
    """
    prompt = _PROMPTS["deep_dive"].format(query=query, context=context)
    raw = _call_llm(prompt, mode="study_deep_dive")
    structured = _parse_json_output(raw, _DEFAULT_DEEP_DIVE)

    # Fill fallback values if LLM didn't populate them
    if not structured.get("topic"):
        structured["topic"] = query
    if not structured.get("introduction") and context:
        structured["introduction"] = context[:200] + "..." if len(context) > 200 else context
    if not structured.get("evolution"):
        structured["evolution"] = f"{query} has evolved significantly in Indian jurisprudence with multiple landmark rulings shaping its interpretation over decades."
    if not structured.get("current_position"):
        structured["current_position"] = f"The current legal position on {query} is well-established through consistent judicial interpretation and scholarly consensus."
    if not structured.get("conclusion"):
        structured["conclusion"] = f"{query} remains a cornerstone of Indian legal system with ongoing evolution and refinement."
    if not structured.get("exam_angles") or not structured["exam_angles"]:
        structured["exam_angles"] = [
            f"Historical development and evolution",
            f"Key landmark cases and their impact",
            f"Current state and future implications",
            f"Critical analysis and contemporary debates"
        ]

    # Enrich landmark_cases list with real case_ids from search results
    result_map = {
        r.case_name.lower(): r.case_id
        for r in final_results
        if r.case_name
    }
    
    # If LLM provided landmark cases, enrich them
    landmark_cases = structured.get("landmark_cases", [])
    if landmark_cases:
        for lc in landmark_cases:
            cn = lc.get("case_name", "").lower()
            if cn in result_map:
                lc["case_id"] = result_map[cn]
    else:
        # Fallback: Create landmark cases from search results
        landmark_cases = [
            {
                "case_name": r.case_name or f"Case {i+1}",
                "case_id": r.case_id,
                "year": (r.metadata or {}).get("year", "N/A"),
                "significance": f"Important case related to {query}"
            }
            for i, r in enumerate(final_results[:5])
            if r.case_name
        ]
    
    structured["landmark_cases"] = landmark_cases

    return {"structured_output": structured}


# ---------------------------------------------------------------------------
# LLM prompts — one per study_output_type
# ---------------------------------------------------------------------------

_PROMPTS: Dict[str, str] = {

    "concept_explanation": """You are a law professor. Explain this concept for law students.

Query: {query}
Context: {context}

Respond ONLY with valid JSON (no markdown, no preamble):
{{"title": "concept title", "explanation": "4-5 sentence explanation with case references", "key_points": ["point 1", "point 2", "point 3", "point 4", "point 5"], "exam_tip": "one key tip"}}""",

    "case_explanation": """You are explaining a court case to law students simply and clearly.

Case: {case_name}
Query: {query}
Facts: {context}

Respond ONLY with valid JSON (no markdown):
{{"case_name": "full name", "court": "court name", "year": "year", "facts": "2-3 sentence summary", "issues": "legal questions posed", "judgment": "what court decided and why", "importance": "impact on Indian law", "key_takeaway": "one essential principle"}}""",

    "case_brief": """Prepare a formal case brief in FIHR format for law students.

Case: {case_name}
Query: {query}
Text: {context}

Respond ONLY with JSON:
{{"case_name": "full name with year", "citation": "citation if known", "court": "court", "year": "year", "bench": "judges", "facts": "detailed background", "issues": ["issue 1", "issue 2"], "held": "holdings", "ratio_decidendi": "rule of law", "obiter_dicta": "significant observations", "importance": "legal significance"}}""",

    "notes": """Create structured study notes from the provided case law.

Topic: {query}
Context: {context}

Respond ONLY with JSON:
{{"topic": "main topic", "headings": [{{"title": "heading 1", "points": ["point 1", "point 2", "point 3"]}}, {{"title": "heading 2", "points": ["point 1", "point 2"]}}, {{"title": "heading 3", "points": ["point 1", "point 2", "point 3"]}}]}}

Create at least 4 headings with 3-6 points each. Include case names where relevant.""",

    "qa_mode": """Create exam/viva questions based on this topic.

Topic: {query}
Context: {context}

Respond ONLY with JSON:
{{"topic": "topic name", "questions": [{{"q": "question", "a": "answer (2-3 sentences)", "difficulty": "easy"}}, {{"q": "application question", "a": "answer", "difficulty": "medium"}}, {{"q": "comparison question", "a": "answer", "difficulty": "hard"}}]}}

Generate 8-10 questions: 3 easy (definitions), 3 medium (applications), 2-3 hard (analysis).""",

    "comparison": """Compare two Indian legal concepts for a law student.

Query: {query}
Context (use if relevant): {context}

Respond ONLY with valid JSON (no markdown, no preamble):
{{"topic1": "first concept", "topic2": "second concept", "summary": "key difference in one sentence", "differences": [{{"point": "aspect", "topic1": "how applies here", "topic2": "how applies here"}}, {{"point": "aspect2", "topic1": "value", "topic2": "value"}}, {{"point": "aspect3", "topic1": "value", "topic2": "value"}}, {{"point": "aspect4", "topic1": "value", "topic2": "value"}}], "similarities": ["similarity 1", "similarity 2"]}}

Give exactly 4 differences. Keep each value under 20 words.""",

    "bare_act_simplified": """Explain a legal section in plain language anyone can understand.

Query: {query}
Context: {context}

Respond ONLY with JSON:
{{"section": "section number", "act": "act name", "original_text": "provision text or quote", "simple_explanation": "plain English explanation (3-4 sentences)", "who_it_applies_to": "applies to whom", "punishment": "consequence if violated, or null", "example": "one realistic example", "common_misconception": "one common misunderstanding and correction"}}""",

    "deep_dive": """Summarise a legal topic for a law student preparing for exams.

Topic: {query}
Context: {context}

Respond ONLY with valid JSON (no markdown, no preamble):
{{"topic": "title", "introduction": "2 sentences", "evolution": "2 sentences on development", "landmark_cases": [{{"case_name": "name", "year": "year", "significance": "one sentence"}}], "current_position": "1-2 sentences", "exam_angles": ["angle 1", "angle 2", "angle 3"], "conclusion": "1 sentence"}}

Include 3 landmark cases maximum. Be concise — every value under 40 words.""",
}


# ---------------------------------------------------------------------------
# Default fallback structures (used if LLM JSON fails to parse)
# ---------------------------------------------------------------------------

_DEFAULT_CONCEPT = {
    "title": "",
    "explanation": "",
    "key_points": [],
    "exam_tip": "",
}

_DEFAULT_CASE_EXPLANATION = {
    "case_name": "",
    "court": "",
    "year": "",
    "facts": "",
    "issues": "",
    "judgment": "",
    "importance": "",
    "key_takeaway": "",
    "pdf_link": "",
}

_DEFAULT_CASE_BRIEF = {
    "case_name": "",
    "citation": "",
    "court": "",
    "year": "",
    "bench": "",
    "facts": "",
    "issues": [],
    "held": "",
    "ratio_decidendi": "",
    "obiter_dicta": "",
    "importance": "",
    "pdf_link": "",
}

_DEFAULT_NOTES = {
    "topic": "",
    "headings": [],
}

_DEFAULT_QA = {
    "topic": "",
    "questions": [],
}

_DEFAULT_COMPARISON = {
    "topic1": "",
    "topic2": "",
    "summary": "",
    "differences": [],
    "similarities": [],
}

_DEFAULT_BARE_ACT = {
    "section": "",
    "act": "",
    "original_text": "",
    "simple_explanation": "",
    "who_it_applies_to": "",
    "punishment": None,
    "example": "",
    "common_misconception": "",
    "related_cases": [],
}

_DEFAULT_DEEP_DIVE = {
    "topic": "",
    "introduction": "",
    "evolution": "",
    "landmark_cases": [],
    "current_position": "",
    "exam_angles": [],
    "conclusion": "",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, mode: str) -> str:
    """
    Call the LLM with a study-mode prompt for JSON-structured legal output.
    Directly invokes Ollama with the prompt (which already contains JSON schema instructions).
    
    The prompt should be one of the _PROMPTS["*"] templates that request explicit JSON.
    This avoids the intermediate generate_research_answer() which reconstructs its own prompt.
    """
    try:
        log.info(f"[STUDY-LLM] mode={mode}, prompt_len={len(prompt)}")
        
        # Import here to avoid circular dependencies
        from Backend.llm.generator import _call_ollama, SYSTEM_STUDY
        
        log.info(f"[STUDY-LLM] Calling Ollama...")
        
        # Call Ollama directly with the detailed JSON-requesting prompt
        # SYSTEM_STUDY is generic enough for all study types since the real schema
        # is embedded in the prompt itself
        response = _call_ollama(
            prompt=prompt,
            system_prompt=SYSTEM_STUDY,
            max_tokens=1200,   # was 2048 — study outputs don't need more than this
            timeout=55         # was 120 — hard cap at 55s, fallbacks handle the rest
        )
        
        log.info(f"[STUDY-LLM] Got response: {len(response)} chars")
        return response.strip()
    except Exception as e:
        log.error(f"[STUDY-LLM] mode={mode} failed: {type(e).__name__}: {e}")
        return ""


def _parse_json_output(raw: str, default: Dict[str, Any]) -> Dict[str, Any]:
    """
    Safely parse JSON from LLM output.
    Strips markdown fences, attempts json.loads, falls back to default.
    """
    if not raw:
        return dict(default)
    try:
        # Strip ```json ... ``` fences
        clean = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"[STUDY-JSON] Parse failed: {e}. Raw length={len(raw)}")
        return dict(default)


def _filter_study_paragraphs(
    paragraphs: List[SearchResult], intent: Dict[str, Any]
) -> List[SearchResult]:
    """
    Prioritize paragraphs whose para_type matches the study intent's
    para_types list. Same logic as research mode's _filter_paragraphs_by_intent
    but uses the study intent dict.
    """
    target = intent.get("para_types", [])
    if not target:
        return paragraphs

    prioritized, other = [], []
    for p in paragraphs:
        pt = (p.metadata or {}).get("para_type", "").lower()
        (prioritized if any(t.lower() in pt for t in target) else other).append(p)

    log.info(f"[STUDY-FILTER] {len(prioritized)} prioritized for {intent['study_output_type']}")
    return prioritized + other


def _extract_case_chips(
    results: List[SearchResult], limit: int = 5
) -> List[Dict[str, Any]]:
    """
    Build lightweight case reference chips from search results.
    Used by concept_explanation, bare_act_simplified, deep_dive.
    """
    chips = []
    seen_ids = set()
    for r in results:
        if r.case_id in seen_ids:
            continue
        seen_ids.add(r.case_id)
        chips.append({
            "case_name": r.case_name or "",
            "case_id":   r.case_id or "",
            "court":     (r.metadata or {}).get("court", ""),
            "year":      (r.metadata or {}).get("year", ""),
            "pdf_link":  _generate_pdf_link(r.case_id or ""),
        })
        if len(chips) >= limit:
            break
    return chips


def _build_study_tabular(case_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build a lightweight tabular list for study mode.
    Simpler than research mode tabular — study UI shows case chips, not dense tables.
    """
    tabular = []
    for i, r in enumerate(case_results):
        tabular.append({
            "index":      i + 1,
            "case_name":  r.get("case_name", "Unknown"),
            "case_id":    r.get("case_id"),
            "court":      r.get("court", ""),
            "year":       r.get("year"),
            "pdf_link":   _generate_pdf_link(r.get("case_id", "")),
            "is_primary": i == 0,
        })
    return tabular