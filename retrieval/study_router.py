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
import logging
import asyncio
from dotenv import load_dotenv
from pathlib import Path

# Configure logging
log = logging.getLogger(__name__)

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

log.info(f"[STUDY-ROUTER] 📍 Configuration loaded:")
log.info(f"[STUDY-ROUTER]   - DB: postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
log.info(f"[STUDY-ROUTER]   - Ollama URL: {OLLAMA_URL}")
log.info(f"[STUDY-ROUTER]   - Ollama Model: {OLLAMA_MODEL}")

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
    log.info(f"[STREAM-OLLAMA] 🔗 Connecting to Ollama at {OLLAMA_URL}")
    log.info(f"[STREAM-OLLAMA] 🤖 Model: {OLLAMA_MODEL}, Max tokens: {max_tokens}")
    
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }
    
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            log.info(f"[STREAM-OLLAMA] 📤 Sending request to Ollama...")
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                log.info(f"[STREAM-OLLAMA] 📡 Response status: {resp.status_code}")
                
                if resp.status_code != 200:
                    log.error(f"[STREAM-OLLAMA] ❌ Bad response status: {resp.status_code}")
                    error_body = await resp.aread()
                    log.error(f"[STREAM-OLLAMA] ❌ Error body: {error_body[:500]}")
                    yield f"data: {json.dumps({'error': f'Ollama returned {resp.status_code}'})}\n\n"
                    return
                
                line_count = 0
                token_count = 0
                done = False
                
                async for line in resp.aiter_lines():
                    line_count += 1
                    if line:
                        log.debug(f"[STREAM-OLLAMA] 📥 Line {line_count}: {line[:100]}")
                        try:
                            chunk = json.loads(line)
                            token = chunk.get('response', '')
                            done = chunk.get('done', False)
                            
                            if token:
                                token_count += len(token.split())
                                log.debug(f"[STREAM-OLLAMA] 📨 Token (words: {len(token.split())}): {token[:50]}")
                            
                            yield f"data: {json.dumps({'token': token, 'done': done})}\n\n"
                            
                            if done:
                                log.info(f"[STREAM-OLLAMA] ✅ Stream complete ({line_count} lines, ~{token_count} tokens)")
                                break
                        except json.JSONDecodeError as e:
                            log.error(f"[STREAM-OLLAMA] ⚠️  JSON decode error on line {line_count}: {e}")
                            log.error(f"[STREAM-OLLAMA] ⚠️  Problem line: {line[:200]}")
                            continue
                        except Exception as e:
                            log.error(f"[STREAM-OLLAMA] ❌ Unexpected error on line {line_count}: {e}", exc_info=True)
                            continue
                
                if not done:
                    log.warning(f"[STREAM-OLLAMA] ⚠️  Stream ended without 'done' signal (lines: {line_count})")
    except asyncio.TimeoutError:
        log.error(f"[STREAM-OLLAMA] ⏱️  Ollama request timeout (180s)")
        yield f"data: {json.dumps({'error': 'Ollama timeout'})}\n\n"
    except Exception as e:
        log.error(f"[STREAM-OLLAMA] ❌ Connection error: {e}", exc_info=True)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"

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
    log.info(f"[LEGAL-TEST] 🔍 Endpoint called for case_id: {req.case_id}")
    
    conn = await get_conn()
    try:
        log.info(f"[LEGAL-TEST] ⏳ Fetching case context from DB...")
        ctx = await get_case_context(conn, req.case_id)
        log.info(f"[LEGAL-TEST] ✅ Case context fetched:")
        log.info(f"  - Case name: {ctx['case'].get('case_name', 'N/A')}")
        log.info(f"  - Court: {ctx['case'].get('court', 'N/A')}")
        log.info(f"  - Year: {ctx['case'].get('year', 'N/A')}")
        log.info(f"  - Citation: {ctx['case'].get('citation', 'N/A')}")
        log.info(f"  - Outcome: {ctx['case'].get('outcome', 'N/A')}")
        log.info(f"  - Total paragraphs: {len(ctx.get('paras', []))}")
    except Exception as e:
        log.error(f"[LEGAL-TEST] ❌ Error fetching case context: {e}", exc_info=True)
        raise
    finally:
        await conn.close()

    # Only send ratio/judgment paragraphs — tests are always in these
    log.info(f"[LEGAL-TEST] 🔎 Filtering paragraphs by type (ratio/judgment/issues)...")
    ratio_paras = [
        p for p in ctx.get("paras", [])
        if p.get("para_type") in ("ratio", "judgment", "issues") or p.get("is_ratio")
    ][:20]
    
    log.info(f"[LEGAL-TEST] Found {len(ratio_paras)} ratio/judgment paragraphs")
    
    if not ratio_paras:
        log.info(f"[LEGAL-TEST] ⚠️  No ratio paragraphs found, using ALL paragraphs instead")
        ratio_paras = ctx.get("paras", [])[:15]
        log.info(f"[LEGAL-TEST] Using first {len(ratio_paras)} paragraphs")

    paras_text = "\n".join(
        f"Para {p.get('para_no', '?')}: {(p.get('text') or '')[:350]}"
        for p in ratio_paras
    )
    
    log.info(f"[LEGAL-TEST] 📝 Paragraph text compiled ({len(paras_text)} chars)")
    log.info(f"[LEGAL-TEST] 🧠 Building prompt for Ollama...")

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

    log.info(f"[LEGAL-TEST] 📤 Prompt length: {len(prompt)} chars")
    log.info(f"[LEGAL-TEST] 🚀 Streaming to Ollama with max_tokens=800...")

    async def stream():
        log.info(f"[LEGAL-TEST] 📡 Stream generator started")
        chunk_count = 0
        try:
            async for data in stream_ollama(prompt, 800):
                chunk_count += 1
                log.debug(f"[LEGAL-TEST] 📨 Chunk {chunk_count}: {len(data)} bytes")
                yield data
            log.info(f"[LEGAL-TEST] ✅ Stream completed ({chunk_count} chunks)")
        except Exception as e:
            log.error(f"[LEGAL-TEST] ❌ Stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'has_legal_test': false, 'error': str(e)})}\n\n"

    log.info(f"[LEGAL-TEST] ✨ Returning StreamingResponse")
    return StreamingResponse(stream(), media_type="text/event-stream")
