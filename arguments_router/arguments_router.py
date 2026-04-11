"""
Backend/retrieval/arguments_router.py
======================================
Legal Reasoning Engine — Argument Extraction + Caching
Mount in main.py:
    from Backend.retrieval.arguments_router import router as arguments_router
    app.include_router(arguments_router)

Endpoints:
    POST /api/cases/{case_id}/arguments      → extract + cache petitioner/respondent arguments
    GET  /api/cases/{case_id}/arguments      → fetch cached arguments (instant)
    POST /api/legal/issue-spot               → facts → legal issues + DB-backed case search
    POST /api/brief/multi                    → multi-case structured brief
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import asyncpg
import httpx
import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent / "database" / ".env"
if env_path.exists():
    load_dotenv(env_path)

log = logging.getLogger(__name__)

router = APIRouter(tags=["legal-reasoning"])

# ── Config ────────────────────────────────────────────────
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = int(os.getenv("DB_PORT", 5432))
DB_NAME     = os.getenv("DB_NAME", "legal_knowledge_graph")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
PG_DSN      = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")


# ── DB helpers ────────────────────────────────────────────

async def get_conn():
    return await asyncpg.connect(PG_DSN)


async def ensure_cache_tables(conn):
    """
    Create cache tables on first use.
    Safe to call on every request — IF NOT EXISTS is idempotent.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS case_arguments_cache (
            case_id         TEXT PRIMARY KEY,
            arguments_json  JSONB NOT NULL,
            generated_at    TIMESTAMPTZ DEFAULT NOW(),
            model_used      TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS case_brief_cache (
            cache_key       TEXT PRIMARY KEY,   -- case_id or "multi:{sorted_ids_hash}"
            brief_json      JSONB NOT NULL,
            generated_at    TIMESTAMPTZ DEFAULT NOW(),
            model_used      TEXT
        )
    """)


async def get_case_core(conn, case_id: str) -> dict:
    """Pull case metadata + ratio paragraphs + citations."""
    case = await conn.fetchrow("""
        SELECT case_id, case_name, citation, court, date_of_judgment,
               outcome, headnotes, ratio_decidendi, final_judgment,
               acts_sections, bench_strength, judges
        FROM cases WHERE case_id = $1
    """, case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case not found: {case_id}")

    paras = await conn.fetch("""
        SELECT para_number, para_text, para_type, is_ratio
        FROM case_paragraphs
        WHERE case_id = $1
        ORDER BY para_number
        LIMIT 50
    """, case_id)

    citations = await conn.fetch("""
        SELECT c.case_name, c.citation, cr.relationship_type
        FROM case_relations cr
        JOIN cases c ON c.case_id = cr.cited_case_id
        WHERE cr.citing_case_id = $1
        LIMIT 8
    """, case_id)

    return {
        "case":      dict(case),
        "paras":     [dict(p) for p in paras],
        "citations": [dict(c) for c in citations],
    }


def _build_case_text(ctx: dict, max_paras: int = 40) -> str:
    c = ctx["case"]
    paras_text = "\n".join(
        f"Para {p['para_number']} [{p['para_type'] or 'general'}]: {p['para_text']}"
        for p in ctx["paras"][:max_paras]
    )
    return (
        f"Case: {c['case_name']}\n"
        f"Citation: {c.get('citation', 'N/A')}\n"
        f"Court: {c['court']}  |  Date: {c.get('date_of_judgment', 'N/A')}\n"
        f"Outcome: {c.get('outcome', 'N/A')}\n"
        f"Acts/Sections: {c.get('acts_sections') or 'N/A'}\n"
        f"Ratio (DB): {(c.get('ratio_decidendi') or 'N/A')[:400]}\n\n"
        f"JUDGMENT TEXT:\n{paras_text}"
    )


async def _call_ollama_json(prompt: str, max_tokens: int = 2000) -> dict:
    """Accumulate full Ollama response and parse as JSON."""
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
                    except Exception:
                        continue

    # Extract first valid JSON object or array
    try:
        start = full.find("{")
        end   = full.rfind("}") + 1
        if start == -1:
            start, end = full.find("["), full.rfind("]") + 1
        return json.loads(full[start:end])
    except Exception:
        return {"raw": full, "parse_error": True}


async def _stream_ollama_text(prompt: str, max_tokens: int = 1500):
    """Token-by-token streaming generator."""
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
                    except Exception:
                        continue


# ══════════════════════════════════════════════════════════
# 2A. ARGUMENT EXTRACTOR — with DB caching
# POST /api/cases/{case_id}/arguments
# GET  /api/cases/{case_id}/arguments
# ══════════════════════════════════════════════════════════

@router.get("/api/cases/{case_id}/arguments")
async def get_arguments_cached(case_id: str):
    """
    Return cached arguments instantly.
    If not yet generated, returns 404 — client should POST to generate.
    This separation means the frontend can check cache first (fast),
    then fall back to generation (slow) without blocking the page load.
    """
    conn = await get_conn()
    try:
        await ensure_cache_tables(conn)
        row = await conn.fetchrow(
            "SELECT arguments_json, generated_at FROM case_arguments_cache WHERE case_id = $1",
            case_id
        )
    finally:
        await conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Arguments not yet generated. POST to generate.")

    return {
        "case_id":      case_id,
        "arguments":    dict(row["arguments_json"]),
        "generated_at": row["generated_at"].isoformat(),
        "cached":       True,
    }


@router.post("/api/cases/{case_id}/arguments")
async def generate_arguments(case_id: str, force_regenerate: bool = False):
    """
    Extract petitioner vs respondent arguments from a judgment.

    First checks the DB cache — returns instantly if already generated.
    If not cached (or force_regenerate=True), calls Ollama and caches the result.

    This is expensive (~60-90s on first call) but cached forever after.
    The frontend should:
      1. Try GET /api/cases/{id}/arguments first
      2. If 404, show "Analysing arguments..." and POST here
      3. Cache result is permanent — never regenerates unless force_regenerate=True
    """
    conn = await get_conn()
    try:
        await ensure_cache_tables(conn)

        # ── Check cache first ──────────────────────────────────────────────────
        if not force_regenerate:
            cached = await conn.fetchrow(
                "SELECT arguments_json FROM case_arguments_cache WHERE case_id = $1",
                case_id
            )
            if cached:
                async def serve_cached():
                    yield f"data: {json.dumps({'arguments': dict(cached['arguments_json']), 'cached': True, 'done': True})}\n\n"
                return StreamingResponse(serve_cached(), media_type="text/event-stream")

        # ── Fetch case data ────────────────────────────────────────────────────
        ctx = await get_case_core(conn, case_id)
        case_text = _build_case_text(ctx, max_paras=40)

    finally:
        await conn.close()

    # ── Build prompt ───────────────────────────────────────────────────────────
    prompt = f"""You are an expert Indian legal analyst extracting arguments from a judgment.

{case_text}

Extract every argument made by each side. Return ONLY this JSON (no markdown):
{{
  "petitioner_name": "Full name of petitioner / appellant",
  "respondent_name": "Full name of respondent",
  "petitioner_arguments": [
    {{
      "point": "Argument heading — 5-8 words",
      "detail": "The argument in 1-2 sentences, in legal language",
      "para_ref": 12,
      "strength": "strong|moderate|weak"
    }}
  ],
  "respondent_arguments": [
    {{
      "point": "Argument heading — 5-8 words",
      "detail": "The argument in 1-2 sentences",
      "para_ref": 18,
      "strength": "strong|moderate|weak"
    }}
  ],
  "court_finding": "What the court held and the core reasoning in 2-3 sentences",
  "winning_side": "petitioner|respondent|partial",
  "key_legal_test": "Name of multi-part test applied if any, or null",
  "test_parts": ["Part 1 — description", "Part 2 — description"]
}}

Rules:
- Extract 3-6 arguments per side
- para_ref must be an integer (para number) or null — never a string
- Only include arguments actually stated in the text — do not invent
- key_legal_test: e.g. "Three-part proportionality test" or null"""

    # ── Stream + cache on completion ───────────────────────────────────────────
    async def stream_and_cache():
        data = await _call_ollama_json(prompt, max_tokens=2000)

        # Save to DB cache
        if not data.get("parse_error"):
            conn2 = await get_conn()
            try:
                await ensure_cache_tables(conn2)
                await conn2.execute("""
                    INSERT INTO case_arguments_cache (case_id, arguments_json, model_used)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (case_id) DO UPDATE
                        SET arguments_json = EXCLUDED.arguments_json,
                            generated_at   = NOW(),
                            model_used     = EXCLUDED.model_used
                """, case_id, json.dumps(data), OLLAMA_MODEL)
                log.info(f"[ARGUMENTS] Cached for case_id={case_id}")
            except Exception as e:
                log.error(f"[ARGUMENTS] Cache write failed: {e}")
            finally:
                await conn2.close()

        yield f"data: {json.dumps({'arguments': data, 'cached': False, 'done': True})}\n\n"

    return StreamingResponse(stream_and_cache(), media_type="text/event-stream")


# ══════════════════════════════════════════════════════════
# 2B. ONE-LINER + 30-SECOND SUMMARY
# These are wired into the brief generation — see note below.
# Also exposed as a standalone endpoint for case viewer header.
# POST /api/cases/{case_id}/quick-summary
# ══════════════════════════════════════════════════════════

@router.post("/api/cases/{case_id}/quick-summary")
async def generate_quick_summary(case_id: str):
    """
    Generate one_liner and summary_30s for a case.
    Cached in case_brief_cache under key "summary:{case_id}".

    Frontend usage:
    - Call on case page load (background, non-blocking)
    - Display one_liner in the case header yellow box
    - Display summary_30s in the Brief tab above FIHR
    - Also shown in search result cards
    """
    conn = await get_conn()
    try:
        await ensure_cache_tables(conn)

        # Check cache
        cached = await conn.fetchrow(
            "SELECT brief_json FROM case_brief_cache WHERE cache_key = $1",
            f"summary:{case_id}"
        )
        if cached:
            async def serve_cached():
                yield f"data: {json.dumps({**dict(cached['brief_json']), 'cached': True, 'done': True})}\n\n"
            return StreamingResponse(serve_cached(), media_type="text/event-stream")

        case = await conn.fetchrow("""
            SELECT case_name, citation, court, date_of_judgment,
                   outcome, headnotes, ratio_decidendi, final_judgment
            FROM cases WHERE case_id = $1
        """, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        case = dict(case)

    finally:
        await conn.close()

    # ── DB-only fast path — if ratio and headnotes exist, skip LLM ────────────
    ratio   = (case.get("ratio_decidendi") or "").strip()
    headnotes = (case.get("headnotes") or "").strip()

    if ratio and len(ratio) > 40:
        # We have enough in the DB to build a good summary without LLM
        one_liner_db   = f"Held: {ratio[:120].rstrip('.,')}."
        summary_30s_db = headnotes[:350] if headnotes else ratio[:350]

        # Still try LLM for quality, but don't block — fire and cache async
        prompt = f"""You are a legal editor. Write two things about this Indian court case.

Case: {case['case_name']}
Citation: {case.get('citation', 'N/A')}
Court: {case.get('court', 'N/A')} | Date: {case.get('date_of_judgment', 'N/A')}
Outcome: {case.get('outcome', 'N/A')}
Ratio: {ratio[:500]}
Headnotes: {headnotes[:400]}

Return ONLY valid JSON (no markdown):
{{
  "one_liner": "Held: [core holding in max 18 words — specific, not generic]",
  "summary_30s": "3 sentences: (1) what the dispute was, (2) what court held and why, (3) the legal principle it established. No jargon."
}}

The one_liner MUST start with 'Held:'. Be specific — name the right, the test, or the principle."""

        async def stream_with_db_fallback():
            try:
                data = await _call_ollama_json(prompt, max_tokens=250)
                if data.get("parse_error") or not data.get("one_liner"):
                    # LLM failed — use DB values
                    data = {"one_liner": one_liner_db, "summary_30s": summary_30s_db}
            except Exception:
                data = {"one_liner": one_liner_db, "summary_30s": summary_30s_db}

            # Cache result
            conn3 = await get_conn()
            try:
                await ensure_cache_tables(conn3)
                await conn3.execute("""
                    INSERT INTO case_brief_cache (cache_key, brief_json, model_used)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (cache_key) DO UPDATE
                        SET brief_json = EXCLUDED.brief_json, generated_at = NOW()
                """, f"summary:{case_id}", json.dumps(data), OLLAMA_MODEL)
            except Exception as e:
                log.error(f"[QUICK-SUMMARY] Cache write failed: {e}")
            finally:
                await conn3.close()

            data["cached"] = False
            data["done"] = True
            yield f"data: {json.dumps(data)}\n\n"

        return StreamingResponse(stream_with_db_fallback(), media_type="text/event-stream")

    # ── No ratio in DB — LLM only path ────────────────────────────────────────
    # Fetch paragraphs to build context
    conn4 = await get_conn()
    try:
        paras = await conn4.fetch("""
            SELECT para_number, para_text, para_type FROM case_paragraphs
            WHERE case_id = $1 AND para_type IN ('judgment', 'ratio', 'order')
            ORDER BY para_number LIMIT 8
        """, case_id)
        para_text = " ".join(p["para_text"][:300] for p in paras)
    finally:
        await conn4.close()

    prompt = f"""Case: {case['case_name']}
Court: {case.get('court', 'N/A')} | Outcome: {case.get('outcome', 'N/A')}
Judgment excerpts: {para_text[:1200]}

Return ONLY valid JSON:
{{
  "one_liner": "Held: [core holding in max 18 words]",
  "summary_30s": "3 sentences covering the dispute, the holding, and the principle established."
}}"""

    async def stream_llm_only():
        data = await _call_ollama_json(prompt, max_tokens=250)
        if data.get("parse_error") or not data.get("one_liner"):
            data = {
                "one_liner": f"Held: {case['case_name']} — {case.get('outcome', 'decided')}",
                "summary_30s": headnotes[:300] if headnotes else "See judgment for full details."
            }

        conn5 = await get_conn()
        try:
            await ensure_cache_tables(conn5)
            await conn5.execute("""
                INSERT INTO case_brief_cache (cache_key, brief_json, model_used)
                VALUES ($1, $2, $3)
                ON CONFLICT (cache_key) DO UPDATE
                    SET brief_json = EXCLUDED.brief_json, generated_at = NOW()
            """, f"summary:{case_id}", json.dumps(data), OLLAMA_MODEL)
        except Exception as e:
            log.error(f"[QUICK-SUMMARY] Cache write failed: {e}")
        finally:
            await conn5.close()

        data["cached"] = False
        data["done"] = True
        yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(stream_llm_only(), media_type="text/event-stream")


# ══════════════════════════════════════════════════════════
# 2C. ISSUE SPOTTER — DB-backed case search
# POST /api/legal/issue-spot
# ══════════════════════════════════════════════════════════

class IssueSpotRequest(BaseModel):
    facts: str              # Client's factual situation in plain language
    context: str = ""       # Optional: jurisdiction, type of matter, stage of proceedings
    max_issues: int = 5     # 3-5 recommended

@router.post("/api/legal/issue-spot")
async def spot_issues(req: IssueSpotRequest):
    """
    Given a factual scenario, identify legal issues + search the DB for relevant cases.

    Pipeline:
      1. LLM identifies legal issues + acts from facts (fast, ~20s)
      2. DB search for real cases matching each issue/act (instant)
      3. Returns issues with actual case_ids so frontend can link directly

    This is different from study_router's issue-spot which returns LLM-invented case names.
    This version only returns cases that actually exist in your DB.
    """
    facts_text    = req.facts[:1800]
    context_block = f"\nAdditional context: {req.context}" if req.context else ""
    max_i         = min(req.max_issues, 6)

    # ── Step 1: LLM extracts issues + acts ────────────────────────────────────
    prompt = f"""You are a senior Indian advocate identifying legal issues from facts.

FACTS:
{facts_text}{context_block}

Identify the legal issues. Return ONLY this JSON (no markdown):
{{
  "issues": [
    {{
      "issue": "Concise legal issue — e.g. 'Violation of Article 22(2): detention beyond 24 hours'",
      "explanation": "Why this issue arises from the facts — 1 sentence",
      "applicable_acts": ["CrPC Section 57", "Article 22 Constitution"],
      "search_query": "Short query to find relevant cases — e.g. 'custodial detention 24 hours Article 22'",
      "priority": "high|medium|low",
      "relief_available": "Writ of habeas corpus / bail application / etc."
    }}
  ],
  "immediate_reliefs": ["Relief type 1", "Relief type 2"],
  "limitation_concern": "Any limitation period issue to flag, or null",
  "matter_type": "criminal|civil|constitutional|service|family|other"
}}

Identify {max_i} issues maximum. Only Indian law. Only actionable issues."""

    issues_data = await _call_ollama_json(prompt, max_tokens=1200)

    if issues_data.get("parse_error"):
        raise HTTPException(status_code=500, detail="Issue identification failed. Try again.")

    # ── Step 2: DB search for real cases per issue ────────────────────────────
    conn = await get_conn()
    try:
        for issue_obj in issues_data.get("issues", []):
            search_q = issue_obj.get("search_query", issue_obj.get("issue", ""))
            acts      = issue_obj.get("applicable_acts", [])

            # Search by acts/sections first (most precise), then keyword fallback
            relevant_cases = []

            if acts:
                # Try to find cases that cite these acts
                act_terms = [a.split(" ")[0] for a in acts[:2]]  # e.g. ["CrPC", "Article"]
                act_nums  = []
                for a in acts[:2]:
                    parts = a.split()
                    if len(parts) >= 2:
                        act_nums.append(parts[-1])  # e.g. "57", "22"

                if act_terms and act_nums:
                    rows = await conn.fetch("""
                        SELECT case_id, case_name, citation, court,
                               EXTRACT(YEAR FROM date_of_judgment)::int AS year,
                               outcome
                        FROM cases
                        WHERE acts_sections ILIKE ANY($1)
                        ORDER BY date_of_judgment DESC
                        LIMIT 4
                    """, [f"%{t}%{n}%" for t, n in zip(act_terms, act_nums)])
                    relevant_cases = [dict(r) for r in rows]

            # Keyword fallback if act search returned nothing
            if not relevant_cases and search_q:
                words = [w for w in search_q.split() if len(w) > 4][:4]
                if words:
                    like_clauses = " OR ".join(
                        f"(case_name ILIKE $%d OR headnotes ILIKE $%d)" % (i+1, i+1)
                        for i in range(len(words))
                    )
                    params = [f"%{w}%" for w in words]
                    rows = await conn.fetch(f"""
                        SELECT case_id, case_name, citation, court,
                               EXTRACT(YEAR FROM date_of_judgment)::int AS year,
                               outcome
                        FROM cases
                        WHERE {like_clauses}
                        ORDER BY date_of_judgment DESC
                        LIMIT 4
                    """, *params)
                    relevant_cases = [dict(r) for r in rows]

            issue_obj["relevant_cases"] = [
                {
                    "case_id":   str(c["case_id"]),
                    "case_name": c["case_name"],
                    "citation":  c.get("citation", ""),
                    "court":     c.get("court", ""),
                    "year":      c.get("year"),
                    "outcome":   c.get("outcome", ""),
                }
                for c in relevant_cases
            ]

    finally:
        await conn.close()

    async def stream():
        yield f"data: {json.dumps({'issues': issues_data, 'done': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ══════════════════════════════════════════════════════════
# 2D. MULTI-CASE BRIEF
# POST /api/brief/multi
# ══════════════════════════════════════════════════════════

class MultiBriefRequest(BaseModel):
    case_ids: list[str]     # 2–5 case IDs
    topic: str              # Legal topic connecting these cases — e.g. "Article 21"
    mode: str = "brief"     # "brief" | "evolution" | "conflict"

@router.post("/api/brief/multi")
async def multi_case_brief(req: MultiBriefRequest):
    """
    Generate a structured multi-case brief on a legal topic.

    Modes:
    - brief:     Side-by-side structured brief for each case + synthesis paragraph
    - evolution: How the legal principle evolved chronologically across these cases
    - conflict:  Where these cases agree and where they conflict

    Different from /api/study/synthesize — that streams free-form prose.
    This returns structured JSON: per-case data + a synthesis block.
    The frontend renders this as a comparison table or timeline.

    Cached in case_brief_cache under key "multi:{sorted_case_ids}:{topic_hash}".
    """
    if len(req.case_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 case IDs.")
    if len(req.case_ids) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 cases.")

    # ── Cache key ──────────────────────────────────────────────────────────────
    import hashlib
    sorted_ids = sorted(req.case_ids)
    topic_hash = hashlib.md5(req.topic.lower().encode()).hexdigest()[:8]
    cache_key  = f"multi:{':'.join(sorted_ids)}:{topic_hash}:{req.mode}"

    conn = await get_conn()
    try:
        await ensure_cache_tables(conn)

        cached = await conn.fetchrow(
            "SELECT brief_json FROM case_brief_cache WHERE cache_key = $1", cache_key
        )
        if cached:
            async def serve_cached():
                yield f"data: {json.dumps({**dict(cached['brief_json']), 'cached': True, 'done': True})}\n\n"
            return StreamingResponse(serve_cached(), media_type="text/event-stream")

        # ── Fetch all cases ────────────────────────────────────────────────────
        cases_data = []
        for cid in req.case_ids:
            try:
                ctx = await get_case_core(conn, cid)
                cases_data.append(ctx)
            except HTTPException:
                log.warning(f"[MULTI-BRIEF] Case not found: {cid}")
                continue

    finally:
        await conn.close()

    if len(cases_data) < 2:
        raise HTTPException(status_code=404, detail="Could not find at least 2 of the provided cases.")

    # ── Build case summaries for the prompt ────────────────────────────────────
    # Keep each case block tight — ratio + outcome + 3 key paras only
    case_blocks = []
    for i, ctx in enumerate(cases_data):
        c = ctx["case"]
        ratio_paras = [
            p for p in ctx["paras"]
            if p.get("para_type") in ("ratio", "judgment") or p.get("is_ratio")
        ][:3]
        para_text = " ".join(p["para_text"][:300] for p in ratio_paras)

        case_blocks.append(
            f"CASE {i+1}: {c['case_name']} ({c.get('citation', 'N/A')})\n"
            f"Court: {c['court']}  |  Year: {str(c.get('date_of_judgment', 'N/A'))[:4]}\n"
            f"Outcome: {c.get('outcome', 'N/A')}\n"
            f"Ratio (DB): {(c.get('ratio_decidendi') or '')[:300]}\n"
            f"Key paragraphs: {para_text[:500]}"
        )

    cases_block = "\n\n---\n\n".join(case_blocks)

    # ── Mode-specific prompt ───────────────────────────────────────────────────
    PROMPTS = {
        "brief": f"""You are a senior Indian advocate preparing a comparative case brief.

TOPIC: {req.topic}

CASES:
{cases_block}

Return ONLY this JSON (no markdown):
{{
  "topic": "{req.topic}",
  "mode": "brief",
  "cases": [
    {{
      "case_name": "full name",
      "citation": "citation",
      "year": "year",
      "court": "court",
      "key_facts": "2 sentences on the facts relevant to {req.topic}",
      "holding_on_topic": "What this case held specifically about {req.topic}",
      "ratio": "The binding principle this case established",
      "precedent_value": "high|medium|low"
    }}
  ],
  "synthesis": "3-4 sentences: What do these cases together establish about {req.topic}? What is the current legal position?",
  "key_principle": "The single clearest principle that emerges across all these cases — 1 sentence",
  "conflicts": "Where these cases conflict or create tension, or null if they are consistent"
}}""",

        "evolution": f"""You are tracing the evolution of a legal principle through Indian case law.

TOPIC: {req.topic}

CASES (read in order — they may be chronological):
{cases_block}

Return ONLY this JSON (no markdown):
{{
  "topic": "{req.topic}",
  "mode": "evolution",
  "starting_position": "What was the legal position before / what the earliest case here established",
  "timeline": [
    {{
      "case_name": "name",
      "year": "year",
      "development": "What this case added, changed, or clarified about {req.topic} — 2 sentences",
      "shift_type": "established|expanded|restricted|overruled|distinguished"
    }}
  ],
  "current_position": "What the law currently says about {req.topic} based on the latest case — 2 sentences",
  "key_turning_point": "Which case caused the biggest change and why — 1-2 sentences"
}}""",

        "conflict": f"""You are a legal analyst identifying consensus and conflict in Indian case law.

TOPIC: {req.topic}

CASES:
{cases_block}

Return ONLY this JSON (no markdown):
{{
  "topic": "{req.topic}",
  "mode": "conflict",
  "consensus_points": [
    "Principle all cases agree on — 1 sentence",
    "Second agreed principle"
  ],
  "conflict_points": [
    {{
      "point": "What the cases disagree about",
      "case_a": "Case name and its position",
      "case_b": "Case name and its position",
      "resolution": "Which case prevails and why, or 'unresolved'"
    }}
  ],
  "recommended_approach": "Given these cases, which position is strongest for a lawyer to argue — 2 sentences"
}}""",
    }

    prompt = PROMPTS.get(req.mode, PROMPTS["brief"])

    # ── Stream + cache ─────────────────────────────────────────────────────────
    async def stream_and_cache():
        data = await _call_ollama_json(prompt, max_tokens=2000)

        if not data.get("parse_error"):
            conn6 = await get_conn()
            try:
                await ensure_cache_tables(conn6)
                await conn6.execute("""
                    INSERT INTO case_brief_cache (cache_key, brief_json, model_used)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (cache_key) DO UPDATE
                        SET brief_json = EXCLUDED.brief_json, generated_at = NOW()
                """, cache_key, json.dumps(data), OLLAMA_MODEL)
                log.info(f"[MULTI-BRIEF] Cached key={cache_key}")
            except Exception as e:
                log.error(f"[MULTI-BRIEF] Cache write failed: {e}")
            finally:
                await conn6.close()

        data["cached"] = False
        data["done"]   = True
        yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(stream_and_cache(), media_type="text/event-stream")
