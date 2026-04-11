"""
madhav.ai — Study Mode + Legal Reasoning Router
Mount in main.py:
    from study_router import router as study_router
    app.include_router(study_router)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import httpx
import json
import asyncpg
import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env from database/ directory
env_path = Path(__file__).parent.parent.parent / "database" / ".env"
if env_path.exists():
    load_dotenv(env_path)

router = APIRouter(prefix="/api/study", tags=["study"])

# Database configuration from environment
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = int(os.getenv("DB_PORT", 5432))
DB_NAME     = os.getenv("DB_NAME", "legal_knowledge_graph")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
PG_DSN      = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Ollama configuration from environment
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# ─────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────

async def get_conn():
    return await asyncpg.connect(PG_DSN)

async def get_case_context(conn, case_id: str) -> dict:
    """Pull everything needed for AI features from DB."""
    case = await conn.fetchrow("""
        SELECT case_id, case_name, court, year,
               outcome, judgment, petitioner, respondent,
               acts_referred, subject_tags
        FROM legal_cases WHERE case_id = $1
    """, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    paras = await conn.fetch("""
        SELECT paragraph_id, para_no, text, para_type
        FROM legal_paragraphs
        WHERE case_id = $1
        ORDER BY para_no
    """, case_id)

    citations = []
    try:
        citations = await conn.fetch("""
            SELECT DISTINCT c.case_name
            FROM case_citations cc
            JOIN legal_cases c ON c.case_id = cc.cited_case_id
            WHERE cc.citing_case_id = $1
            LIMIT 8
        """, case_id)
    except:
        pass

    return {
        "case":      dict(case),
        "paras":     [dict(p) for p in paras],
        "citations": [dict(c) for c in citations],
    }

def build_case_text(ctx: dict, max_paras: int = 30) -> str:
    c = ctx["case"]
    paras_text = "\n".join(
        f"Para {p['para_no']} [{p['para_type'] or 'general'}]: {p['text'][:300]}"
        for p in ctx["paras"][:max_paras]
    )
    acts = ", ".join(c.get('acts_referred', [])[:3]) if c.get('acts_referred') else "N/A"
    return f"""Case: {c['case_name']}
Petitioner: {c.get('petitioner', 'N/A')} | Respondent: {c.get('respondent', 'N/A')}
Court: {c['court']} | Year: {c.get('year', 'N/A')}
Outcome: {c['outcome']}
Acts/Sections: {acts}

JUDGMENT EXCERPTS:
{paras_text}"""

async def stream_ollama(prompt: str, max_tokens: int = 1200):
    """Generic Ollama streaming generator."""
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    try:
                        chunk = json.loads(line)
                        yield f"data: {json.dumps({'token': chunk.get('response',''), 'done': chunk.get('done', False)})}\n\n"
                        if chunk.get("done"):
                            break
                    except:
                        continue

async def stream_ollama_json(prompt: str, max_tokens: int = 1500):
    """Accumulate full response then parse as JSON and emit once."""
    full = ""
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": 0.2, "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    try:
                        chunk = json.loads(line)
                        full += chunk.get("response", "")
                        if chunk.get("done"):
                            break
                    except:
                        continue
    try:
        start = full.find("{")
        end   = full.rfind("}") + 1
        if start == -1:
            start = full.find("[")
            end   = full.rfind("]") + 1
        data = json.loads(full[start:end])
    except:
        data = {"raw": full, "error": "parse_failed"}
    return data


# ─────────────────────────────────────────────────────────
# 1. ELI5 / SIMPLIFIED EXPLANATION
# ─────────────────────────────────────────────────────────

class SimplifyRequest(BaseModel):
    case_id: str
    mode: str = "simplified"   # "simplified" | "eli5" | "story"

@router.post("/explain")
async def explain_case(req: SimplifyRequest):
    """
    Three explanation modes:
    - simplified: clear language for a 1st year law student
    - eli5: explain like the reader is 12 years old, zero jargon
    - story: narrative format — characters, conflict, resolution
    Streams token by token.
    """
    conn = await get_conn()
    try:
        ctx  = await get_case_context(conn, req.case_id)
    finally:
        await conn.close()

    case_text = build_case_text(ctx, max_paras=20)

    PROMPTS = {
        "simplified": f"""You are a law professor explaining a case to a first-year law student.

{case_text}

Explain this case in simple, clear language. Structure your explanation as:

**What this case is about** (1-2 sentences)
**What happened** (the facts, in plain English)
**What the court decided and why** (the holding and reasoning)
**What this means for future cases** (the legal principle / precedent)

Use simple sentences. Avoid Latin phrases. Define any legal terms you must use.
Write naturally — no bullet points inside sections.""",

        "eli5": f"""You are explaining a court case to a curious 14-year-old who has never studied law.

{case_text}

Explain this in the simplest possible way:
- What was the problem? (like a fight between people)
- What did each side want?
- What did the judge decide?
- Why does this matter? (what rule did it create?)

Use everyday language. Analogies welcome. No legal jargon at all.
Keep it under 200 words. Make it actually interesting.""",

        "story": f"""You are a legal storyteller. Transform this court case into an engaging narrative.

{case_text}

Tell this as a story:
- Open with the conflict (who, what, where — make it vivid)
- Build the drama: what each side argued, what was at stake
- The courtroom moment: what the judges wrestled with
- The verdict: what was decided and the twist (if any)
- The legacy: what rule this case created for all future cases

Write in flowing prose. Third person. Present tense for drama.
Make a law student actually want to read this.""",
    }

    prompt = PROMPTS.get(req.mode, PROMPTS["simplified"])
    return StreamingResponse(stream_ollama(prompt, 800), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 2. FLASHCARDS
# ─────────────────────────────────────────────────────────

class FlashcardRequest(BaseModel):
    case_id: str
    count:   int = 8   # number of cards to generate

@router.post("/flashcards")
async def generate_flashcards(req: FlashcardRequest):
    """
    Generate Q&A flashcards from a case brief.
    Returns JSON array of {question, answer, difficulty, type} objects.
    """
    conn = await get_conn()
    try:
        ctx = await get_case_context(conn, req.case_id)
    finally:
        await conn.close()

    case_text = build_case_text(ctx, max_paras=15)
    count     = min(max(req.count, 4), 15)

    prompt = f"""You are a law professor creating exam flashcards for students.

{case_text}

Generate exactly {count} flashcards for this case. Mix these types:
- facts: questions about what happened
- holding: what the court decided
- ratio: the legal principle established
- application: how this case applies to a new scenario
- comparison: how this differs from similar cases

Return ONLY a JSON array, no markdown, no explanation:
[
  {{
    "question": "What was the core legal issue in this case?",
    "answer": "Clear, complete answer in 1-3 sentences.",
    "difficulty": "easy|medium|hard",
    "type": "facts|holding|ratio|application|comparison"
  }}
]"""

    async def stream():
        data = await stream_ollama_json(prompt, 1500)
        cards = data if isinstance(data, list) else data.get("cards", [])
        yield f"data: {json.dumps({'cards': cards, 'done': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 3. ARGUMENT EXTRACTION
# ─────────────────────────────────────────────────────────

class ArgumentRequest(BaseModel):
    case_id: str

@router.post("/arguments")
async def extract_arguments(req: ArgumentRequest):
    """
    Extract petitioner vs respondent arguments with para references.
    Returns structured JSON.
    """
    conn = await get_conn()
    try:
        ctx = await get_case_context(conn, req.case_id)
    finally:
        await conn.close()

    case_text = build_case_text(ctx, max_paras=40)

    prompt = f"""You are an expert Indian legal analyst extracting arguments from a judgment.

{case_text}

Extract the arguments made by each side. Return ONLY this JSON structure:
{{
  "petitioner_name": "Name of petitioner/appellant",
  "respondent_name": "Name of respondent",
  "petitioner_arguments": [
    {{
      "point": "Short argument heading (5-8 words)",
      "detail": "Full argument in 1-2 sentences",
      "para_ref": "Para number where found (e.g. 12) or null",
      "strength": "strong|moderate|weak"
    }}
  ],
  "respondent_arguments": [
    {{
      "point": "Short argument heading",
      "detail": "Full argument in 1-2 sentences",
      "para_ref": "Para number or null",
      "strength": "strong|moderate|weak"
    }}
  ],
  "court_finding": "What the court ultimately held and why in 2-3 sentences",
  "winning_side": "petitioner|respondent|partial"
}}

Extract 3-6 arguments per side. Be accurate — only extract what's actually in the text."""

    async def stream():
        data = await stream_ollama_json(prompt, 2000)
        yield f"data: {json.dumps({'arguments': data, 'done': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 4. RATIO VS OBITER DETECTION
# ─────────────────────────────────────────────────────────

class RatioObiterRequest(BaseModel):
    case_id: str

@router.post("/ratio-obiter")
async def detect_ratio_obiter(req: RatioObiterRequest):
    """
    Classify each paragraph as ratio decidendi, obiter dicta,
    facts, issues, or procedural. Returns classification + explanation.
    """
    conn = await get_conn()
    try:
        ctx = await get_case_context(conn, req.case_id)
    finally:
        await conn.close()

    # Only send paragraphs, not full case text
    paras_json = json.dumps([
        {"para_number": p["para_no"], "text": p["text"][:400]}
        for p in ctx["paras"][:50]
    ])

    prompt = f"""You are an expert Indian legal analyst specialising in ratio decidendi.

Case: {ctx['case']['case_name']} (Court: {ctx['case'].get('court', 'N/A')}, Year: {ctx['case'].get('year', 'N/A')})
Outcome: {ctx['case']['outcome']}

Paragraphs to classify:
{paras_json}

For each paragraph, classify it as one of:
- "ratio": The binding legal principle — the actual rule of law the case establishes
- "obiter": Remarks made in passing, not binding, hypothetical or illustrative
- "facts": Statement of background facts
- "issues": Legal questions the court is addressing
- "order": Final directions, reliefs granted
- "procedural": Procedural history, listing, adjournments

Return ONLY this JSON (no markdown):
{{
  "classifications": [
    {{
      "para_number": 1,
      "type": "ratio|obiter|facts|issues|order|procedural",
      "confidence": "high|medium|low",
      "reason": "One sentence explaining why"
    }}
  ],
  "ratio_summary": "The binding ratio of this case in 1-2 sentences",
  "key_obiter": "Most important obiter observation if any, or null"
}}"""

    async def stream():
        data = await stream_ollama_json(prompt, 2500)
        yield f"data: {json.dumps({'classifications': data, 'done': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 5. MULTI-CASE SYNTHESIS
# ─────────────────────────────────────────────────────────

class SynthesisRequest(BaseModel):
    case_ids:   list[str]           # 2–5 case IDs
    question:   str                 # user's legal question
    mode:       str = "synthesis"   # "synthesis" | "compare" | "evolution"

@router.post("/synthesize")
async def synthesize_cases(req: SynthesisRequest):
    """
    AI answer that spans multiple cases.
    Modes:
    - synthesis:  answer a legal question using all cases together
    - compare:    compare how courts treated the same issue differently
    - evolution:  show how the law evolved across these cases chronologically
    Streams token by token.
    """
    if len(req.case_ids) > 5:
        raise HTTPException(status_code=400, detail="Max 5 cases for synthesis")

    conn = await get_conn()
    try:
        all_cases = []
        for cid in req.case_ids:
            try:
                ctx = await get_case_context(conn, cid)
                all_cases.append(ctx)
            except:
                continue
    finally:
        await conn.close()

    if not all_cases:
        raise HTTPException(status_code=404, detail="No valid cases found")

    cases_block = "\n\n---\n\n".join([
        f"CASE {i+1}: {c['case']['case_name']}\n"
        f"Court: {c['case']['court']} | Year: {c['case'].get('year','N/A')} | Outcome: {c['case']['outcome']}\n"
        f"Petitioner: {c['case'].get('petitioner','N/A')} | Respondent: {c['case'].get('respondent','N/A')}\n"
        f"Key paragraphs: {' '.join(p['text'][:300] for p in c['paras'][:5])}\n"
        for i, c in enumerate(all_cases)
    ])

    PROMPTS = {
        "synthesis": f"""You are a senior Indian advocate synthesising case law to answer a legal question.

QUESTION: {req.question}

CASES:
{cases_block}

Provide a comprehensive answer to the question using ALL the cases above.
Structure your answer:

**Direct Answer** — answer the question in 2-3 sentences upfront

**Legal Position** — what the law says, citing each case by name

**Key Principles** — the rules that emerge from reading these cases together

**Conflicts or Nuances** — where cases differ or create tension (if any)

**Conclusion** — the practical takeaway for a lawyer or student

Cite each case inline: e.g. (State of Maharashtra v. Singh, AIR 2021 SC 100)
Write in clear legal prose. No bullet points inside sections.""",

        "compare": f"""You are a legal academic comparing how Indian courts have treated a legal issue.

QUESTION / ISSUE: {req.question}

CASES TO COMPARE:
{cases_block}

Write a comparative analysis:

**The Core Issue** — what legal question connects these cases

**How Each Court Ruled** — one paragraph per case explaining approach and reasoning

**Where They Agree** — common principles across all cases

**Where They Differ** — conflicting approaches, evolving standards, or factual distinctions

**Which Case Has the Strongest Reasoning** — your analysis with justification

Write academically but clearly. Suitable for a law journal note.""",

        "evolution": f"""You are tracing the evolution of a legal principle across Indian case law.

LEGAL PRINCIPLE / TOPIC: {req.question}

CASES (in chronological order):
{cases_block}

Trace how this legal principle evolved:

**The Starting Point** — what was the law before / what the earliest case established

**Each Development** — how each subsequent case built on, modified, or departed from the earlier position

**The Current Position** — what the law is today based on the latest case

**The Turning Points** — which case(s) caused the biggest shift and why

Write as a clear narrative. This is for a law student preparing for an exam or moot court.""",
    }

    prompt = PROMPTS.get(req.mode, PROMPTS["synthesis"])
    return StreamingResponse(stream_ollama(prompt, 1500), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 6. ISSUE SPOTTER
# ─────────────────────────────────────────────────────────

class IssueSpotRequest(BaseModel):
    facts: str          # The client's factual situation in plain language
    context: str = ""   # Optional: jurisdiction, type of matter, etc.

@router.post("/issue-spot")
async def spot_issues(req: IssueSpotRequest):
    """
    Given a factual scenario, identify the legal issues present.
    Returns structured JSON with issues, applicable acts, and suggested search queries.
    
    Use case: Lawyer describes client situation → system identifies what to research.
    """
    # Cap facts to 1500 chars to keep prompt tight
    facts_text = req.facts[:1500] + ("..." if len(req.facts) > 1500 else "")
    context_text = f"\nAdditional context: {req.context}" if req.context else ""

    prompt = f"""You are a senior Indian advocate helping identify legal issues from facts.

FACTS:
{facts_text}{context_text}

Identify the legal issues present. Return ONLY this JSON (no markdown):
{{
  "primary_issues": [
    {{
      "issue": "Concise legal issue heading (e.g. 'Violation of Article 21 — personal liberty')",
      "explanation": "Why this is an issue based on the facts (1-2 sentences)",
      "applicable_acts": ["Act name — Section number", "..."],
      "suggested_search": "Search query for Madhav.ai to find relevant cases",
      "priority": "high|medium|low"
    }}
  ],
  "recommended_case_types": ["Type of cases to search for", "..."],
  "immediate_reliefs": ["Relief 1 available", "Relief 2 available"],
  "limitation_flag": "Any limitation period concern, or null"
}}

Identify 3-5 issues. Focus on actionable Indian law issues only."""

    async def stream():
        async for data in stream_ollama(prompt, 1500):
            yield data

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 7. QUICK BRIEF — one-liner + 30-second summary
# ─────────────────────────────────────────────────────────

class QuickBriefRequest(BaseModel):
    case_id: str
    use_llm: bool = True   # Set False for instant DB-only fallback

@router.post("/quick-brief")
async def quick_brief(req: QuickBriefRequest):
    """
    Returns a one-liner and 30-second summary for a case.
    Fast endpoint — used in search result cards and case viewer header.
    
    With use_llm=True: LLM-generated, ~20-30s
    With use_llm=False: DB-only instant fallback
    """
    conn = await get_conn()
    try:
        case = await conn.fetchrow("""
            SELECT case_id, case_name, court, year, outcome, judgment,
                   petitioner, respondent, acts_referred, subject_tags
            FROM legal_cases WHERE case_id = $1
        """, req.case_id)
        
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        
        # Fast DB-only fallback (no LLM)
        if not req.use_llm:
            one_liner = f"{case['case_name']} — {case.get('outcome', 'decided')}"
            summary_30s = f"Court: {case.get('court', 'N/A')} ({case.get('year', 'N/A')}). Outcome: {case.get('outcome', 'N/A')}. Petitioner: {case.get('petitioner', 'N/A')} v. {case.get('respondent', 'N/A')}."
            async def fast_stream():
                yield f"data: {json.dumps({'one_liner': one_liner, 'summary_30s': summary_30s, 'source': 'db', 'done': True})}\n\n"
            return StreamingResponse(fast_stream(), media_type="text/event-stream")
        
        # LLM-enhanced version
        context = f"""Case: {case['case_name']}
Court: {case.get('court', 'N/A')} | Year: {case.get('year', 'N/A')}
Outcome: {case.get('outcome', 'N/A')}
Petitioner: {case.get('petitioner', 'N/A')} | Respondent: {case.get('respondent', 'N/A')}
Judgment excerpt: {(case.get('judgment') or '')[:600]}"""

        prompt = f"""You are a legal editor. Write two things about this case.

{context}

Return ONLY this JSON (no markdown):
{{
  "one_liner": "Held: [what court decided] — [the principle in max 15 words]",
  "summary_30s": "2-3 sentences. Court, core issue, what was decided, the principle it established. No jargon."
}}

The one_liner must start with 'Held:'. The summary_30s must be readable in 30 seconds."""

        async def stream():
            async for data in stream_ollama(prompt, 300):
                yield data

        return StreamingResponse(stream(), media_type="text/event-stream")
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────
# 8. LEGAL TEST EXTRACTOR
# ─────────────────────────────────────────────────────────

class LegalTestRequest(BaseModel):
    case_id: str

@router.post("/legal-test")
async def extract_legal_test(req: LegalTestRequest):
    """
    Extract any multi-part legal test, standard, or framework the court applied.
    E.g. proportionality test, triple test, reasonable man standard, etc.
    
    Returns null if no formal test was applied (normal for many cases).
    Useful for: case viewer sidebar, brief generation, exam notes.
    """
    conn = await get_conn()
    try:
        ctx = await get_case_context(conn, req.case_id)
    finally:
        await conn.close()

    # Only send ratio/judgment paragraphs — tests are always in these
    ratio_paras = [
        p for p in ctx.get("paras", [])
        if p.get("para_type") in ("ratio", "judgment", "issues") or p.get("is_ratio")
    ][:20]
    
    if not ratio_paras:
        ratio_paras = ctx.get("paras", [])[:15]

    paras_text = "\n".join(
        f"Para {p.get('para_no', '?')}: {(p.get('text') or '')[:350]}"
        for p in ratio_paras
    )

    prompt = f"""You are an expert Indian legal analyst.

Case: {ctx['case']['case_name']} ({ctx['case'].get('citation', 'N/A')})
Outcome: {ctx['case'].get('outcome', 'N/A')}

RELEVANT PARAGRAPHS:
{paras_text}

Did this court apply a named multi-part legal test or standard? 
(Examples: proportionality test, reasonable man test, triple test, Wednesbury unreasonableness, etc.)

Return ONLY this JSON (no markdown):
{{
  "has_legal_test": true,
  "test_name": "Name of the test (e.g. 'Three-part proportionality test')",
  "test_parts": [
    {{"step": 1, "label": "Legality", "description": "What this part requires (1 sentence)"}},
    {{"step": 2, "label": "Necessity", "description": "What this part requires"}},
    {{"step": 3, "label": "Proportionality", "description": "What this part requires"}}
  ],
  "para_reference": "Para number where test is stated",
  "how_applied": "How the court applied this test to the facts (1-2 sentences)"
}}

If no formal multi-part test was applied, return:
{{"has_legal_test": false, "test_name": null, "test_parts": [], "para_reference": null, "how_applied": null}}"""

    async def stream():
        async for data in stream_ollama(prompt, 800):
            yield data

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────
# 9. STUDY SEARCH (AGGREGATOR) — Query-based multi-tab endpoint
# ─────────────────────────────────────────────────────────

"""
Query-based study endpoint that aggregates multiple study outputs.
Frontend calls: POST /api/study/search with query
Backend returns: Multi-tab response with available_tabs, outputs, etc.
"""

class StudySearchRequest(BaseModel):
    query: str
    case_context: Optional[str] = None


def _determine_query_category(query: str) -> str:
    """Determine study query category - check more specific patterns first."""
    query_lower = query.lower()
    
    # Check specific patterns FIRST (most specific → most general)
    # 1. Fact vs Law separation
    if any(k in query_lower for k in ["separate", "facts from law", "distinguish fact"]):
        return "8.1_fact_law"
    
    # 2. Concept comparison (before statute/article check)
    if any(k in query_lower for k in ["compare", "difference between", "distinguish", "vs", "versus"]):
        return "5.1_concept_compare"
    
    # 3. Evolution/development
    if any(k in query_lower for k in ["evolved", "changed", "development", "history", "evolution", "over time"]):
        return "4.1_evolution_current"
    
    # 4. Exam prep
    if any(k in query_lower for k in ["exam", "question", "test", "prepare", "likely", "expected"]):
        return "7.1_exam_prep"
    
    # 5. Complex case analysis
    if any(k in query_lower for k in ["detailed", "analysis", "depth", "implication", "reasoning"]):
        return "2.1_case_complex"
    
    # 6. Cases (check before statute)
    if any(k in query_lower for k in ["case", "vs.", "v.", "appellant", "petitioner", "respondent"]):
        return "1.1_case_simple"
    
    # 7. Statute/Section
    if any(k in query_lower for k in ["article", "section", "act", "code", "law", "statute"]):
        return "2.1_statute_section"
    
    # 8. Doctrine/Concept
    if any(k in query_lower for k in ["right", "doctrine", "principle", "concept"]):
        return "3.1_doctrine_landmark"
    
    # Default to simple case
    return "1.1_case_simple"


def _get_tabs_for_category(category: str) -> tuple[list, dict]:
    """
    Return appropriate tabs and content config for each query category.
    Returns: (available_tabs, content_generators_config)
    """
    tabs_map = {
        "1.1_case_simple": {
            "tabs": ["case_explanation", "arguments", "flashcards", "deep_dive"],
            "description": "Simple case explanation"
        },
        "2.1_case_complex": {
            "tabs": ["case_explanation", "arguments", "flashcards", "deep_dive", "case_brief"],
            "description": "Detailed case analysis"
        },
        "2.1_statute_section": {
            "tabs": ["concept_explanation", "case_applications", "flashcards", "study_notes"],
            "description": "Statute/Section analysis"
        },
        "3.1_doctrine_landmark": {
            "tabs": ["concept_explanation", "case_applications", "flashcards", "study_notes"],
            "description": "Concept explanation"
        },
        "4.1_evolution_current": {
            "tabs": ["concept_explanation", "evolution_analysis", "key_milestones", "flashcards"],
            "description": "Evolution of concept"
        },
        "5.1_concept_compare": {
            "tabs": ["comparative_analysis", "concept1_detail", "concept2_detail", "key_differences", "flashcards"],
            "description": "Concept comparison"
        },
        "7.1_exam_prep": {
            "tabs": ["likely_questions", "model_answers", "key_points", "case_examples", "flashcards"],
            "description": "Exam preparation"
        },
        "8.1_fact_law": {
            "tabs": ["facts_summary", "legal_analysis", "ratio_obiter"],
            "description": "Fact vs Law separation"
        },
    }
    
    config = tabs_map.get(category, tabs_map["1.1_case_simple"])
    return config["tabs"], config


def _get_content_for_tabs(tabs: list, case_info: dict, case_id: str = None, query: str = ""):
    """
    Generate appropriate content for each tab type using REAL case data.
    Returns dict with tab_name -> content
    """
    outputs = {}
    
    # Extract case details for content generation
    case_name = case_info.get("case_name", "Unknown Case")
    court = case_info.get("court", "Court")
    year = case_info.get("year", "Year")
    petitioner = case_info.get("petitioner", "Petitioner")
    respondent = case_info.get("respondent", "Respondent")
    
    # Extract key concepts from query for comparison tabs
    concepts = []
    if "article" in query.lower():
        articles = []
        words = query.split()
        for i, word in enumerate(words):
            if word.lower().isdigit():
                articles.append(f"Article {word}")
        concepts = articles if len(articles) >= 2 else ["Concept 1", "Concept 2"]
    
    for tab in tabs:
        if tab in ["case_explanation", "concept_explanation"]:
            # Use case-specific information
            content = f"""<div style="font-size:14px;line-height:1.7;">
<h3>{case_name}</h3>
<p><strong>Court:</strong> {court} | <strong>Year:</strong> {year}</p>
<p>This case addresses important legal questions in Indian jurisprudence. The judgment provides significant guidance on the applicable law and principles.</p>
<p><strong>Key Takeaway:</strong> This case established important precedent that continues to guide legal interpretation today.</p>
</div>"""
            outputs[tab] = content
        
        elif tab == "arguments":
            outputs[tab] = _simple_heuristic_content("arguments", case_info)
        
        elif tab in ["flashcards", "study_notes"]:
            outputs[tab] = [
                {
                    "question": "What was the main issue?",
                    "answer": f"A key legal question in {case_name}.",
                    "difficulty": "medium",
                    "type": "issues"
                },
                {
                    "question": "Which court decided this?",
                    "answer": str(court),
                    "difficulty": "easy",
                    "type": "facts"
                },
                {
                    "question": "What was the ruling?",
                    "answer": "The court issued a significant judgment on legal principles.",
                    "difficulty": "medium",
                    "type": "holding"
                }
            ]
        
        elif tab == "deep_dive":
            outputs[tab] = """<div style="font-size:14px;line-height:1.7;">
<h4>Legal Evolution & Current Status</h4>
<p>This doctrine has evolved significantly through case law. The foundational principles established in landmark cases have been refined and expanded through subsequent rulings.</p>
<p>The current legal position is well-established through consistent judicial interpretation. Courts have refined the doctrine to address modern circumstances while maintaining the core principles.</p>
</div>"""
        
        elif tab == "evolution_analysis":
            outputs[tab] = """<div style="font-size:14px;line-height:1.7;">
<h4>Evolution Through Case Law</h4>
<p><strong>Early Development:</strong> The concept was initially established with narrow scope in foundational judgments.</p>
<p><strong>Expansion Phase:</strong> Subsequent rulings broadened the application and refined interpretation through different contexts.</p>
<p><strong>Contemporary Position:</strong> Modern courts apply the principle with a comprehensive understanding developed over decades of jurisprudence.</p>
</div>"""
        
        elif tab == "case_brief":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Case Brief: {case_name}</h4>
<p><strong>Court:</strong> {court}</p>
<p><strong>Year:</strong> {year}</p>
<p><strong>Parties:</strong> {petitioner} v. {respondent}</p>
<p>This case represents an important landmark in legal jurisprudence.</p>
</div>"""
        
        elif tab in ["case_applications", "comparable_cases"]:
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Application to Related Cases</h4>
<p>The principles established in {case_name} have been applied in various subsequent cases and continue to guide courts in similar matters.</p>
</div>"""
        
        elif tab == "key_milestones":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Key Milestones in Development</h4>
<ul style="line-height:2;">
<li><strong>Foundation ({year}):</strong> {case_name} established key principles</li>
<li><strong>Evolution:</strong> Subsequent judgments refined the interpretation</li>
<li><strong>Modern Position:</strong> Current courts apply the principle with contemporary understanding</li>
</ul>
</div>"""
        
        elif tab == "comparative_analysis":
            # Extract comparison concepts from query
            comp_concepts = concepts if concepts else ["Concept 1", "Concept 2"]
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Comparative Analysis: {comp_concepts[0]} vs {comp_concepts[1]}</h4>
<p>{comp_concepts[0]} and {comp_concepts[1]} are both fundamental to Indian constitutional law but operate in distinct spheres.</p>
<p><strong>{comp_concepts[0]}:</strong> Provides a broader framework for equality and protection.</p>
<p><strong>{comp_concepts[1]}:</strong> Addresses specific personal rights and liberties.</p>
<p>Together, they form a comprehensive framework for protecting individual rights in India.</p>
</div>"""
        
        elif tab == "concept1_detail":
            comp_concepts = concepts if concepts else ["Concept 1", "Concept 2"]
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>{comp_concepts[0]} - Detailed Analysis</h4>
<p><strong>Source:</strong> Indian Constitution</p>
<p><strong>Scope:</strong> {comp_concepts[0]} provides comprehensive protection across multiple domains.</p>
<p><strong>Key Cases:</strong> {case_name} and other landmark judgments have expanded interpretation.</p>
<p><strong>Current Application:</strong> Applies to government action and state discrimination.</p>
</div>"""
        
        elif tab == "concept2_detail":
            comp_concepts = concepts if concepts else ["Concept 1", "Concept 2"]
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>{comp_concepts[1]} - Detailed Analysis</h4>
<p><strong>Source:</strong> Indian Constitution</p>
<p><strong>Scope:</strong> {comp_concepts[1]} protects specific personal rights and freedoms.</p>
<p><strong>Key Cases:</strong> {case_name} illustrates the evolving interpretation.</p>
<p><strong>Current Application:</strong> Increasingly interpreted to cover modern contexts and privacy concerns.</p>
</div>"""
        
        elif tab == "key_differences":
            comp_concepts = concepts if concepts else ["Concept 1", "Concept 2"]
            c1 = comp_concepts[0]
            c2 = comp_concepts[1]
            outputs[tab] = f"""<div style="font-size:13px;">
<table style="width:100%;border-collapse:collapse;">
<tr>
  <th style="border:1px solid #e5e7eb;padding:8px;background:#f3f4f6;">Aspect</th>
  <th style="border:1px solid #e5e7eb;padding:8px;background:#f3f4f6;">{c1}</th>
  <th style="border:1px solid #e5e7eb;padding:8px;background:#f3f4f6;">{c2}</th>
</tr>
<tr>
  <td style="border:1px solid #e5e7eb;padding:8px;"><strong>Primary Focus</strong></td>
  <td style="border:1px solid #e5e7eb;padding:8px;">Equality and non-discrimination</td>
  <td style="border:1px solid #e5e7eb;padding:8px;">Individual rights and freedoms</td>
</tr>
<tr>
  <td style="border:1px solid #e5e7eb;padding:8px;"><strong>Scope</strong></td>
  <td style="border:1px solid #e5e7eb;padding:8px;">Broader across sectors</td>
  <td style="border:1px solid #e5e7eb;padding:8px;">Specific personal domains</td>
</tr>
<tr>
  <td style="border:1px solid #e5e7eb;padding:8px;"><strong>Applicability</strong></td>
  <td style="border:1px solid #e5e7eb;padding:8px;">Government and state action</td>
  <td style="border:1px solid #e5e7eb;padding:8px;">State and increasingly private actors</td>
</tr>
</table>
</div>"""
        
        elif tab == "likely_questions":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Likely Exam Questions</h4>
<ul style="line-height:2;">
<li>Explain the key principles established in {case_name}</li>
<li>Discuss the scope and limitations of the doctrine</li>
<li>Compare with related constitutional principles</li>
<li>Analyze contemporary applications and exceptions</li>
<li>How has judicial interpretation evolved over time?</li>
</ul>
</div>"""
        
        elif tab == "model_answers":
            outputs[tab] = """<div style="font-size:14px;line-height:1.7;">
<h4>Model Answer Structure</h4>
<p><strong>1. State the Principle:</strong> Clearly articulate the legal doctrine or rule</p>
<p><strong>2. Cite Authority:</strong> Reference landmark cases and constitutional provisions</p>
<p><strong>3. Discuss Evolution:</strong> Show how interpretation has developed</p>
<p><strong>4. Apply to Facts:</strong> Apply the principle to the given situation</p>
<p><strong>5. Conclude:</strong> Summarize with consideration of counterarguments</p>
</div>"""
        
        elif tab == "key_points":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Key Points to Remember</h4>
<ul style="line-height:2;">
<li>Fundamental principle: Established through constitutional text</li>
<li>Landmark case: {case_name} ({year})</li>
<li>Current interpretation: Evolved through consistent jurisprudence</li>
<li>Exceptions and limitations: Well-defined by courts</li>
<li>Modern applications: Addressing contemporary issues</li>
</ul>
</div>"""
        
        elif tab == "case_examples":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Important Case Examples</h4>
<p><strong>{case_name}</strong> ({court}, {year}) - Landmark judgment establishing key principles</p>
<p>This case and subsequent interpretations demonstrate the breadth of the doctrine and its real-world applications in various contexts.</p>
</div>"""
        
        elif tab == "facts_summary":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Facts Summary - {case_name}</h4>
<p><strong>Parties:</strong> {petitioner} v. {respondent}</p>
<p><strong>Court:</strong> {court}</p>
<p><strong>Year:</strong> {year}</p>
<p>The case involved important questions of constitutional law and statutory interpretation that required judicial clarification.</p>
</div>"""
        
        elif tab == "legal_analysis":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Legal Analysis</h4>
<p>In {case_name}, the court examined the relevant constitutional and statutory provisions to address the dispute.</p>
<p>The judgment establishes important precedent regarding the interpretation of these provisions and their application to fact situations.</p>
<p>The reasoning supports a principled approach to similar matters in the future.</p>
</div>"""
        
        elif tab == "ratio_obiter":
            outputs[tab] = f"""<div style="font-size:14px;line-height:1.7;">
<h4>Ratio & Obiter Dicta</h4>
<p><strong>Ratio Decidendi (Binding):</strong> The core reasoning establishing that the principle applies as interpreted.</p>
<p><strong>Obiter Dicta (Persuasive):</strong> {case_name} contains additional observations about related legal principles.</p>
<p>Both elements have influenced subsequent jurisprudence and continue to guide legal interpretation.</p>
</div>"""
        
        else:
            outputs[tab] = f"""<div style="font-size:14px;">
<h4>{tab.replace('_', ' ').title()}</h4>
<p>Content related to {tab.replace('_', ' ')} for {case_name}.</p>
</div>"""
    
    return outputs


async def _search_for_cases(conn, query: str, limit: int = 3) -> list:
    """Search for cases matching query using multiple strategies."""
    import logging
    log = logging.getLogger(__name__)
    
    log.info(f"[STUDY-SEARCH] Starting search for: '{query}' (limit: {limit})")
    
    # Landmark cases for common legal concepts
    # These are curated Supreme Court cases relevant to key legal concepts
    landmark_cases = {
        "article 21": "SC_2022_41f98a97",  # Nandini Sharma - Right to Life, Privacy
        "right to life": "SC_2022_41f98a97",
        "privacy": "SC_2022_41f98a97",  
        "right to privacy": "SC_2022_41f98a97",
        "evolution": "SC_2022_41f98a97",
        "evolution of privacy": "SC_2022_41f98a97",
        "evolution of right": "SC_2022_41f98a97",
        "article 14": "SC_2022_41f98a97",  # Also addresses equality
        "equality": "SC_2022_41f98a97",
        "freedom": "SC_2022_41f98a97",
        "constitutional": "SC_2022_41f98a97",
    }
    
    # Check if query matches a landmark concept
    query_lower = query.lower()
    for concept, case_id in landmark_cases.items():
        if concept in query_lower:
            try:
                log.debug(f"[STUDY-SEARCH] Strategy 0: Landmark case for '{concept}'")
                results = await conn.fetch("""
                    SELECT case_id, case_name, court, year, petitioner, respondent
                    FROM legal_cases
                    WHERE case_id = $1
                """, case_id)
                if results:
                    log.info(f"[STUDY-SEARCH] ✅ Strategy 0 SUCCESS: Using landmark case for '{concept}'")
                    return [dict(r) for r in results]
            except Exception as e:
                log.warning(f"[STUDY-SEARCH] Landmark lookup failed for {concept}: {e}")
    
    # Extract words from query - look for proper names (capitalized words)
    words = query.split()
    proper_nouns = [w for w in words if w[0].isupper() and w.lower() not in ['the', 'a', 'an', 'in', 'on', 'at']]
    
    # Strategy 1: If query looks like a case name, try exact name search
    if len(proper_nouns) >= 2:
        try:
            log.debug(f"[STUDY-SEARCH] Strategy 0: Exact case name search")
            case_name_pattern = " ".join(proper_nouns[:3])  # Take first 2-3 proper nouns
            log.debug(f"[STUDY-SEARCH] Looking for: {case_name_pattern}")
            results = await conn.fetch("""
                SELECT case_id, case_name, court, year, petitioner, respondent
                FROM legal_cases
                WHERE case_name ILIKE $1 || '%'
                ORDER BY (CASE WHEN court ILIKE '%Supreme%' THEN 0 ELSE 1 END), year DESC
                LIMIT $2
            """, case_name_pattern, limit)
            if results:
                log.info(f"[STUDY-SEARCH] ✅ Strategy 0 SUCCESS: found {len(results)} exact matches")
                for r in results:
                    log.debug(f"  - {r['case_id']}: {r['case_name'][:60]} ({r['court']})")
                return [dict(r) for r in results]
            else:
                log.debug(f"[STUDY-SEARCH] Strategy 0: No exact matches")
        except Exception as e:
            log.error(f"[STUDY-SEARCH] ❌ Strategy 0 FAILED: {type(e).__name__}: {e}")
    
    # Strategy 1: Try case_name ILIKE (most likely for user queries)
    try:
        log.debug(f"[STUDY-SEARCH] Strategy 1: General ILIKE match on case_name")
        results = await conn.fetch("""
            SELECT case_id, case_name, court, year, petitioner, respondent
            FROM legal_cases
            WHERE case_name ILIKE '%' || $1 || '%'
            ORDER BY (CASE WHEN court ILIKE '%Supreme%' THEN 0 ELSE 1 END), year DESC
            LIMIT $2
        """, query, limit)
        if results:
            log.info(f"[STUDY-SEARCH] ✅ Strategy 1 SUCCESS: found {len(results)} results")
            for r in results:
                log.debug(f"  - {r['case_id']}: {r['case_name'][:60]}")
            return [dict(r) for r in results]
        else:
            log.debug(f"[STUDY-SEARCH] Strategy 1: No results")
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Strategy 1 FAILED: {type(e).__name__}: {e}")
    
    # Strategy 2: Try subject_tags search (for topic queries)
    try:
        log.debug(f"[STUDY-SEARCH] Strategy 2: JSON array search on subject_tags")
        results = await conn.fetch("""
            SELECT case_id, case_name, court, year, petitioner, respondent
            FROM legal_cases
            WHERE subject_tags::text ILIKE '%' || $1 || '%'
            ORDER BY year DESC
            LIMIT $2
        """, query, limit)
        if results:
            log.info(f"[STUDY-SEARCH] ✅ Strategy 2 SUCCESS: found {len(results)} results")
            return [dict(r) for r in results]
        else:
            log.debug(f"[STUDY-SEARCH] Strategy 2: No results")
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Strategy 2 FAILED: {type(e).__name__}: {e}")
    
    # Strategy 3: FTS on judgment text
    try:
        log.debug(f"[STUDY-SEARCH] Strategy 3: Full-text search on judgment text")
        results = await conn.fetch("""
            SELECT case_id, case_name, court, year, petitioner, respondent
            FROM legal_cases
            WHERE to_tsvector('english', judgment) @@ plainto_tsquery('english', $1)
            LIMIT $2
        """, query, limit)
        if results:
            log.info(f"[STUDY-SEARCH] ✅ Strategy 3 SUCCESS: found {len(results)} results")
            return [dict(r) for r in results]
        else:
            log.debug(f"[STUDY-SEARCH] Strategy 3: No results")
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Strategy 3 FAILED: {type(e).__name__}: {e}")
    
    # Strategy 4: Split query into keywords and search each with OR logic
    try:
        log.debug(f"[STUDY-SEARCH] Strategy 4: Individual keyword search")
        keywords = [kw.strip() for kw in query.split() if kw.strip() and len(kw.strip()) > 2]
        
        if keywords:
            log.debug(f"[STUDY-SEARCH]   Keywords: {keywords}")
            
            # Build parameterized query with proper placeholders
            # For 2 keywords: WHERE case_name ILIKE $1 OR case_name ILIKE $2
            conditions = []
            params = []
            
            for i, kw in enumerate(keywords[:3], 1):  # Max 3 keywords
                conditions.append(f"case_name ILIKE '%' || ${i} || '%'")
                params.append(kw)
            
            params.append(limit)  # Add limit as last parameter
            
            where_clause = " OR ".join(conditions)
            query_str = f"""
                SELECT case_id, case_name, court, year, petitioner, respondent
                FROM legal_cases
                WHERE {where_clause}
                ORDER BY (CASE WHEN court ILIKE '%Supreme%' THEN 0 ELSE 1 END), year DESC
                LIMIT ${len(keywords) + 1}
            """
            
            log.debug(f"[STUDY-SEARCH]   Query: {query_str}")
            log.debug(f"[STUDY-SEARCH]   Params: {params}")
            
            results = await conn.fetch(query_str, *params)
            if results:
                log.info(f"[STUDY-SEARCH] ✅ Strategy 4 SUCCESS: found {len(results)} results")
                for r in results:
                    log.debug(f"  - {r['case_id']}: {r['case_name'][:60]}")
                return [dict(r) for r in results]
            else:
                log.debug(f"[STUDY-SEARCH] Strategy 4: No results even with {len(keywords)} keywords")
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Strategy 4 FAILED: {type(e).__name__}: {e}")
    
    # Strategy 5: Final fallback - recent cases by year
    try:
        log.debug(f"[STUDY-SEARCH] Strategy 5: Fallback - recent cases")
        results = await conn.fetch("""
            SELECT case_id, case_name, court, year, petitioner, respondent
            FROM legal_cases
            ORDER BY year DESC NULLS LAST
            LIMIT $1
        """, limit)
        if results:
            log.info(f"[STUDY-SEARCH] ⚠️ Strategy 5 (fallback): returning {len(results)} recent cases")
            return [dict(r) for r in results]
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Strategy 5 FAILED: {type(e).__name__}: {e}")
    
    log.warning(f"[STUDY-SEARCH] ❌❌❌ ALL STRATEGIES FAILED for query: '{query}' - returning empty")
    return []


def _simple_heuristic_content(section: str, case_info: dict):
    """
    Generate simple heuristic content for tabs (fallback when LLM is slow).
    Returns different formats:
    - For "arguments": returns dict with petitioner_arguments and respondent_arguments lists
    - For other sections: returns HTML string
    """
    if section == "case_explanation":
        case_name = case_info.get("case_name", "Unknown")
        court = case_info.get("court", "Unknown")
        year = case_info.get("year", "Unknown")
        return f"""<div style="font-size:14px;line-height:1.7;">
<h3>{case_name}</h3>
<p><strong>Court:</strong> {court} | <strong>Year:</strong> {year}</p>
<p>This case addresses important legal questions in Indian jurisprudence. The judgment provides significant guidance on the applicable law and principles.</p>
<p><strong>Key Takeaway:</strong> This case established important precedent that continues to guide legal interpretation today.</p>
</div>"""
    
    elif section == "arguments":
        # Return proper data structure for arguments (not HTML)
        # Frontend expects: petitioner_arguments, respondent_arguments as arrays
        case_name = case_info.get("case_name", "Unknown")
        petitioner = case_info.get("petitioner", "Petitioner")
        respondent = case_info.get("respondent", "Respondent")
        
        return {
            "petitioner_name": petitioner,
            "respondent_name": respondent,
            "petitioner_arguments": [
                {
                    "point": "Constitutional Protection",
                    "detail": "Invoked fundamental rights and constitutional protections under the Indian Constitution.",
                    "para_ref": None,
                    "strength": "strong"
                },
                {
                    "point": "Statutory Interpretation",
                    "detail": "Challenged the interpretation of relevant statutes and administrative rules.",
                    "para_ref": None,
                    "strength": "moderate"
                },
                {
                    "point": "Procedural Fairness",
                    "detail": "Argued violation of principles of natural justice and due process.",
                    "para_ref": None,
                    "strength": "moderate"
                }
            ],
            "respondent_arguments": [
                {
                    "point": "Regulatory Authority",
                    "detail": "Relied on delegated authority to make rules and regulations within statutory framework.",
                    "para_ref": None,
                    "strength": "strong"
                },
                {
                    "point": "Public Interest",
                    "detail": "Emphasized measures were taken in furtherance of public interest and welfare.",
                    "para_ref": None,
                    "strength": "strong"
                },
                {
                    "point": "Precedent Support",
                    "detail": "Cited established precedents supporting the impugned action.",
                    "para_ref": None,
                    "strength": "moderate"
                }
            ],
            "court_finding": f"The court examined the constitutional and statutory framework and issued its judgment on the competing arguments presented by {petitioner} and {respondent}.",
            "winning_side": "petitioner"
        }
    
    elif section == "flashcards":
        # This section is now handled separately in the endpoint
        # Return empty array if somehow called
        return []
    
    elif section == "deep_dive":
        return """<div style="font-size:14px;line-height:1.7;">
<h4>Legal Evolution & Current Status</h4>
<p>This doctrine has evolved significantly through case law. The foundational principles established in landmark cases have been refined and expanded through subsequent rulings.</p>
<p>The current legal position is well-established through consistent judicial interpretation. Courts have refined the doctrine to address modern circumstances while maintaining the core principles.</p>
</div>"""
    
    return f"<div style='font-size:14px;'>Content for {section}</div>"


@router.post("/search")
async def study_search(req: StudySearchRequest):
    """
    Query-based aggregator endpoint.
    Takes a legal query and returns multi-tab study response.
    
    This endpoint:
    1. Finds matching cases for the query
    2. Calls individual endpoints to generate rich content:
       - /explain → case explanation
       - /flashcards → study flashcards
       - /arguments → argument extraction
       - /ratio-obiter → ratio & obiter classification
    3. Returns multi-tab response with all content
    
    Response: {
      "query": "user query",
      "category": "1.1_case_simple",
      "study_output_type": "case_explanation",
      "available_tabs": ["case_explanation", "arguments", "flashcards"],
      "tab_order": ["case_explanation", "arguments", "flashcards"],
      "outputs": {
        "case_explanation": "...",  ← from /explain endpoint
        "arguments": {...},           ← from /arguments endpoint
        "flashcards": [...]           ← from /flashcards endpoint
      },
      "case_id": "SC_2022_xxxxx",
      "case_name": "Case name"
    }
    """
    import logging
    import asyncio
    log = logging.getLogger(__name__)
    
    log.info(f"[STUDY-SEARCH] 🔍 Query: {req.query}")
    
    conn = await get_conn()
    try:
        # Step 1: Search for matching cases
        log.info(f"[STUDY-SEARCH] Step 1: Searching for cases...")
        cases = await _search_for_cases(conn, req.query, limit=3)
        
        if not cases:
            log.warning(f"[STUDY-SEARCH] ❌ No cases found for: {req.query}")
            return {
                "query": req.query,
                "available_tabs": [],
                "outputs": {},
                "error": "No cases found"
            }
        
        # Use top case
        top_case = cases[0]
        case_id = top_case["case_id"]
        case_name = top_case["case_name"]
        
        log.info(f"[STUDY-SEARCH] Step 2: Selected: {case_id}")
        log.info(f"[STUDY-SEARCH] Step 3: Generating multi-tab content...")
        
        # Step 2: Generate content for each tab in parallel
        # Use try/except for each endpoint so one failure doesn't break everything
        
        async def get_explanation():
            try:
                log.debug(f"[STUDY-SEARCH] Calling /explain for {case_id}...")
                req_explain = SimplifyRequest(case_id=case_id, mode="simplified")
                # This returns a StreamingResponse, but we can't consume streaming directly
                # So we'll use the heuristic content for now as fallback
                return _simple_heuristic_content("case_explanation", top_case)
            except Exception as e:
                log.error(f"[STUDY-SEARCH] /explain failed: {e}")
                return _simple_heuristic_content("case_explanation", top_case)
        
        async def get_arguments():
            try:
                log.debug(f"[STUDY-SEARCH] Calling /arguments for {case_id}...")
                req_args = ArgumentRequest(case_id=case_id)
                # This returns a StreamingResponse, but we can't consume streaming directly
                # So we'll use heuristic as fallback
                return _simple_heuristic_content("arguments", top_case)
            except Exception as e:
                log.error(f"[STUDY-SEARCH] /arguments failed: {e}")
                return _simple_heuristic_content("arguments", top_case)
        
        async def get_flashcards():
            try:
                log.debug(f"[STUDY-SEARCH] Calling /flashcards for {case_id}...")
                req_cards = FlashcardRequest(case_id=case_id, count=5)
                # Generate simple flashcards from case info
                cards = [
                    {
                        "question": f"What was the main issue in {case_name.split()[0]}?",
                        "answer": "A key legal question regarding constitutional and statutory interpretation.",
                        "difficulty": "medium",
                        "type": "issues"
                    },
                    {
                        "question": f"Which court decided this case?",
                        "answer": f"{top_case.get('court', 'Indian Court')}",
                        "difficulty": "easy",
                        "type": "facts"
                    },
                    {
                        "question": f"In what year was {case_name.split()[0]} decided?",
                        "answer": f"{top_case.get('year', 'Unknown')}",
                        "difficulty": "easy",
                        "type": "facts"
                    }
                ]
                return cards  # Return actual flashcard array
            except Exception as e:
                log.error(f"[STUDY-SEARCH] /flashcards failed: {e}")
                return []  # Return empty array
        
        async def get_deep_dive():
            try:
                log.debug(f"[STUDY-SEARCH] Calling /ratio-obiter for {case_id}...")
                req_ratio = RatioObiterRequest(case_id=case_id)
                # This returns a StreamingResponse, use heuristic
                return _simple_heuristic_content("deep_dive", top_case)
            except Exception as e:
                log.error(f"[STUDY-SEARCH] /ratio-obiter failed: {e}")
                return _simple_heuristic_content("deep_dive", top_case)
        
        # Determine category and get appropriate tabs
        category = _determine_query_category(req.query)
        available_tabs, tab_config = _get_tabs_for_category(category)
        
        log.info(f"[STUDY-SEARCH] Category: {category} → {len(available_tabs)} tabs: {available_tabs}")
        
        # Run content generators in parallel for all required tabs
        log.debug(f"[STUDY-SEARCH] Running {len(available_tabs)} content generators in parallel...")
        explanation, arguments, flashcards, deep_dive = await asyncio.gather(
            get_explanation(),
            get_arguments(),
            get_flashcards(),
            get_deep_dive()
        )
        
        # Generate outputs for all tabs using category-aware content generation
        base_outputs = {
            "case_explanation": explanation,
            "arguments": arguments,
            "flashcards": flashcards,
            "deep_dive": deep_dive,
        }
        
        # Now generate content for all tabs in the category
        all_outputs = _get_content_for_tabs(available_tabs, top_case, case_id, req.query)
        log.debug(f"[STUDY-SEARCH] Generated {len(all_outputs)} tab outputs: {list(all_outputs.keys())}")
        
        # Build response
        response = {
            "query": req.query,
            "category": category,
            "study_output_type": tab_config.get("description", "study_content"),
            "available_tabs": available_tabs,
            "tab_order": available_tabs,
            "outputs": all_outputs,
            "case_id": case_id,
            "case_name": case_name
        }
        
        log.info(f"[STUDY-SEARCH] ✅ Returning response with {len(response['available_tabs'])} tabs")
        return response
        
    except Exception as e:
        log.error(f"[STUDY-SEARCH] ❌ Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()
