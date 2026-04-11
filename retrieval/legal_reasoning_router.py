"""
Backend/retrieval/legal_reasoning_router.py
============================================
Legal Reasoning Engine — Sprint Features 1, 2 & 4

Endpoints:
    POST /api/cases/{case_id}/counter-arguments   — weaknesses in each side's position
    POST /api/cases/{case_id}/strategy            — case strategy for a given side
    POST /api/cases/{case_id}/fact-law-separation — tag each paragraph: fact / law / mixed
    GET  /api/cases/{case_id}/reasoning-full      — all three in one cached call (recommended)

Mount in main.py:
    from Backend.retrieval.legal_reasoning_router import router as reasoning_router
    app.include_router(reasoning_router)

Design notes:
  - All three endpoints share the same DB cache table: case_reasoning_cache
  - /reasoning-full fetches arguments from case_arguments_cache (built in arguments_router.py)
    and runs all three analyses in parallel — one LLM call each, cached together
  - Counter-arguments build on top of the extracted arguments — so arguments_router must
    have been called first (or arguments are regenerated inline if cache miss)
  - Fact/law separation reuses ratio-obiter paragraph data where available
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import asyncpg
import httpx
import json
import logging
import asyncio
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


# ════════════════════════════════════════════════════════
# DB HELPERS
# ════════════════════════════════════════════════════════

async def get_conn():
    return await asyncpg.connect(PG_DSN)


def safe_parse_jsonb(data):
    """
    Handle JSONB data from asyncpg.
    If it's a string (JSON), parse it. If it's already a dict, return it.
    """
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return data
    return data


async def ensure_cache_table(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS case_reasoning_cache (
            cache_key       TEXT PRIMARY KEY,
            data_json       JSONB NOT NULL,
            generated_at    TIMESTAMPTZ DEFAULT NOW(),
            model_used      TEXT
        )
    """)


async def get_cached(conn, key: str) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT data_json FROM case_reasoning_cache WHERE cache_key = $1", key
    )
    return safe_parse_jsonb(row["data_json"]) if row else None


async def set_cached(conn, key: str, data: dict):
    await conn.execute("""
        INSERT INTO case_reasoning_cache (cache_key, data_json, model_used)
        VALUES ($1, $2, $3)
        ON CONFLICT (cache_key) DO UPDATE
            SET data_json = EXCLUDED.data_json,
                generated_at = NOW(),
                model_used = EXCLUDED.model_used
    """, key, json.dumps(data), OLLAMA_MODEL)


async def fetch_case_core(conn, case_id: str) -> dict:
    case = await conn.fetchrow("""
        SELECT case_id, case_name, court, year,
               outcome, petitioner, respondent, judgment,
               acts_referred, subject_tags
        FROM legal_cases WHERE case_id = $1
    """, case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case not found: {case_id}")

    paras = await conn.fetch("""
        SELECT paragraph_id, para_no, text, para_type
        FROM legal_paragraphs
        WHERE case_id = $1
        ORDER BY para_no
        LIMIT 50
    """, case_id)

    return {"case": dict(case), "paras": [dict(p) for p in paras]}


async def fetch_cached_arguments(conn, case_id: str) -> Optional[dict]:
    """Pull from arguments_router's cache table if available."""
    try:
        row = await conn.fetchrow(
            "SELECT arguments_json FROM case_arguments_cache WHERE case_id = $1",
            case_id
        )
        return safe_parse_jsonb(row["arguments_json"]) if row else None
    except Exception:
        return None


def _compact_case_text(ctx: dict, max_paras: int = 35) -> str:
    c = ctx["case"]
    paras = "\n".join(
        f"Para {p['para_no']} [{p['para_type'] or 'general'}]: {p['text'][:350]}"
        for p in ctx["paras"][:max_paras]
    )
    acts = ", ".join(c.get('acts_referred', [])[:3]) if c.get('acts_referred') else "N/A"
    return (
        f"Case: {c['case_name']}\n"
        f"Petitioner: {c.get('petitioner','N/A')} | Respondent: {c.get('respondent','N/A')}\n"
        f"Court: {c['court']} | Year: {c['year']} | Outcome: {c.get('outcome','N/A')}\n"
        f"Acts/Sections: {acts}\n\n"
        f"PARAGRAPHS:\n{paras}"
    )


async def _stream_ollama_json(prompt: str, max_tokens: int = 1800):
    """Accumulate full response then parse as JSON and return."""
    full = ""
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": 0.2, "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=600.0) as client:
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
    try:
        start = full.find("{")
        end   = full.rfind("}") + 1
        if start == -1:
            start = full.find("[")
            end   = full.rfind("]") + 1
        return json.loads(full[start:end])
    except Exception:
        return {"raw": full, "parse_error": True}


async def _call_ollama_json(prompt: str, max_tokens: int = 1800) -> dict:
    full = ""
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": 0.2, "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=600.0) as client:
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
    try:
        s = full.find("{"); e = full.rfind("}") + 1
        if s == -1:
            s = full.find("["); e = full.rfind("]") + 1
        return json.loads(full[s:e])
    except Exception:
        return {"raw": full[:500], "parse_error": True}


# ════════════════════════════════════════════════════════
# PROMPT BUILDERS — one per analysis type
# ════════════════════════════════════════════════════════

def _build_counter_args_prompt(case_text: str, args: Optional[dict]) -> str:
    """
    Takes extracted arguments (if available) and finds the weaknesses
    in each side's position + what the other side could exploit.
    """
    args_block = ""
    if args and not args.get("parse_error"):
        pet_args = args.get("petitioner_arguments", [])
        res_args = args.get("respondent_arguments", [])
        if pet_args or res_args:
            args_block = (
                "\n\nALREADY EXTRACTED ARGUMENTS (use these as base):\n"
                f"PETITIONER ({args.get('petitioner_name','')}):\n"
                + "\n".join(f"  - {a.get('point','')}: {a.get('detail','')}" for a in pet_args[:5])
                + f"\n\nRESPONDENT ({args.get('respondent_name','')}):\n"
                + "\n".join(f"  - {a.get('point','')}: {a.get('detail','')}" for a in res_args[:5])
            )

    return f"""You are a senior Indian advocate stress-testing legal arguments.

{case_text}{args_block}

Identify the weaknesses in each side's arguments and what counter-arguments could be raised.

Return ONLY this JSON (no markdown):
{{
  "petitioner_weaknesses": [
    {{
      "argument": "The petitioner's argument being challenged",
      "weakness": "Why this argument is vulnerable — specific legal or factual gap",
      "counter": "How the respondent can attack this — cite applicable law or precedent if possible",
      "severity": "fatal|serious|minor"
    }}
  ],
  "respondent_weaknesses": [
    {{
      "argument": "The respondent's argument being challenged",
      "weakness": "Why this argument is vulnerable",
      "counter": "How the petitioner can attack this",
      "severity": "fatal|serious|minor"
    }}
  ],
  "overall_assessment": {{
    "stronger_side": "petitioner|respondent|balanced",
    "decisive_issue": "The single issue that will likely determine the outcome — 1 sentence",
    "swing_factor": "What fact or law, if established, would change the outcome entirely"
  }}
}}

Rules:
- Be specific — cite paragraph numbers or legal provisions where possible
- fatal = argument likely fails if challenged; serious = significant risk; minor = manageable
- Only identify real weaknesses from the text — do not invent"""


def _build_strategy_prompt(case_text: str, args: Optional[dict], side: str) -> str:
    """
    Builds a litigation strategy for 'petitioner' or 'respondent'
    based on case facts, arguments, and identified weaknesses.
    """
    side_label = side.capitalize()
    opp_label  = "Respondent" if side == "petitioner" else "Petitioner"

    args_block = ""
    if args and not args.get("parse_error"):
        side_args = args.get(f"{side}_arguments", [])
        opp_args  = args.get(f"{'respondent' if side == 'petitioner' else 'petitioner'}_arguments", [])
        if side_args:
            args_block = (
                f"\n\n{side_label.upper()} ARGUMENTS:\n"
                + "\n".join(f"  [{a.get('strength','?')}] {a.get('point','')}: {a.get('detail','')}" for a in side_args[:5])
                + f"\n\n{opp_label.upper()} ARGUMENTS TO COUNTER:\n"
                + "\n".join(f"  - {a.get('point','')}: {a.get('detail','')}" for a in opp_args[:4])
            )

    return f"""You are a senior Indian advocate preparing litigation strategy.

{case_text}{args_block}

Build a complete litigation strategy for the {side_label}.

Return ONLY this JSON (no markdown):
{{
  "side": "{side}",
  "win_probability": "high|medium|low",
  "win_probability_reason": "One sentence explaining why",
  "primary_strategy": "The core strategic approach in 2 sentences — what theory of the case to run",
  "strongest_arguments": [
    {{
      "argument": "The argument to lead with",
      "why_strong": "Why this is your best point",
      "how_to_present": "How to frame it most effectively for the court",
      "supporting_law": "Key provision or precedent to cite"
    }}
  ],
  "arguments_to_avoid": [
    {{
      "argument": "An argument that should NOT be made",
      "reason": "Why it hurts more than it helps"
    }}
  ],
  "how_to_counter_opposition": [
    {{
      "their_point": "Opposition's strongest argument",
      "your_response": "How to neutralise it — specific legal or factual response"
    }}
  ],
  "evidence_to_establish": [
    "Key fact or document to prove — 1 sentence each"
  ],
  "reliefs_to_claim": [
    "Specific relief to pray for — 1 sentence each"
  ],
  "risk_factors": [
    {{
      "risk": "A significant risk in your position",
      "mitigation": "How to manage or minimise this risk"
    }}
  ],
  "alternative_routes": [
    "Alternative legal route if primary strategy fails — e.g. different forum, different ground"
  ]
}}

Be specific to this case — not generic advice. Reference actual paragraphs and provisions."""


def _build_fact_law_prompt(case_text: str, paras: list) -> str:
    """
    Classify each paragraph as finding of fact, question of law,
    mixed fact-law, procedural, or ratio.
    Also identifies burden of proof assignments.
    """
    # Only send first 40 paras, truncated
    para_list = json.dumps([
        {
            "para_number": p["para_no"],
            "text": p["text"][:400],
            "existing_type": p.get("para_type") or "unknown"
        }
        for p in paras[:40]
    ])

    return f"""You are an expert Indian legal analyst classifying judgment paragraphs.

Case info:
{case_text[:600]}

Paragraphs to classify:
{para_list}

For each paragraph, determine:
1. Whether it is a finding of FACT, a question of LAW, MIXED (both), PROCEDURAL, or RATIO
2. If it assigns burden of proof — who bears the burden and on what issue

Return ONLY this JSON (no markdown):
{{
  "classifications": [
    {{
      "para_number": 1,
      "type": "fact|law|mixed|procedural|ratio|order",
      "sub_type": "finding_of_fact|question_of_law|ratio_decidendi|obiter_dicta|procedural_history|final_order|null",
      "summary": "What this paragraph establishes in one clause — max 12 words",
      "burden_of_proof": {{
        "present": true,
        "party": "petitioner|respondent|null",
        "on_issue": "what they must prove — 1 sentence or null"
      }}
    }}
  ],
  "fact_law_summary": {{
    "key_facts_established": ["Fact 1 — Para X", "Fact 2 — Para Y"],
    "key_legal_questions": ["Legal question 1 — Para X"],
    "burden_summary": "Overall burden of proof distribution — 1-2 sentences",
    "contested_facts": ["Fact disputed between parties — 1 sentence each"]
  }}
}}

Rules:
- fact = what happened (events, documents, conduct)
- law = interpretation of statute or precedent
- mixed = court applying law to facts
- ratio = the binding rule the case establishes
- Be concise in summaries — 12 words max"""


# ════════════════════════════════════════════════════════
# 1. COUNTER-ARGUMENTS + WEAKNESS DETECTION
# POST /api/cases/{case_id}/counter-arguments
# ════════════════════════════════════════════════════════

@router.post("/api/cases/{case_id}/counter-arguments")
async def counter_arguments(case_id: str, force_regenerate: bool = False):
    """
    Identify weaknesses in each side's arguments and what counter-arguments apply.

    Builds on top of the argument extraction cache — call
    POST /api/cases/{id}/arguments first for best results.
    If arguments aren't cached, fetches case text directly and extracts inline.

    Cache: permanent in case_reasoning_cache under key "counter:{case_id}"

    Response includes:
    - Per-argument weaknesses for both sides, severity-rated
    - Specific counter-arguments with legal basis
    - Overall assessment: stronger side, decisive issue, swing factor
    """
    cache_key = f"counter:{case_id}"

    conn = await get_conn()
    try:
        await ensure_cache_table(conn)

        if not force_regenerate:
            cached = await get_cached(conn, cache_key)
            if cached:
                return JSONResponse({**cached, 'cached': True, 'done': True})

        ctx  = await fetch_case_core(conn, case_id)
        args = await fetch_cached_arguments(conn, case_id)

    finally:
        await conn.close()

    case_text = _compact_case_text(ctx)
    prompt    = _build_counter_args_prompt(case_text, args)

    # Await LLM response before handling response
    data = await _stream_ollama_json(prompt, max_tokens=1800)

    # Cache if successful
    if not data.get("parse_error"):
        c = await get_conn()
        try:
            await ensure_cache_table(c)
            await set_cached(c, cache_key, data)
            log.info(f"[COUNTER-ARGS] Cached {cache_key}")
        except Exception as e:
            log.error(f"[COUNTER-ARGS] Cache failed: {e}")
        finally:
            await c.close()

    return JSONResponse({**data, 'cached': False, 'done': True})


# ════════════════════════════════════════════════════════
# 2. CASE STRATEGY
# POST /api/cases/{case_id}/strategy
# ════════════════════════════════════════════════════════

class StrategyRequest(BaseModel):
    side: str = "petitioner"   # "petitioner" | "respondent"

@router.post("/api/cases/{case_id}/strategy")
async def case_strategy(case_id: str, req: StrategyRequest, force_regenerate: bool = False):
    """
    Generate a complete litigation strategy for a given side.

    Given the case facts, arguments, and weaknesses, answers:
    - What is the core theory of the case?
    - Which arguments to lead with and why?
    - Which arguments to avoid?
    - How to counter the opposition's strongest points?
    - What evidence/facts must be established?
    - What reliefs to claim?
    - What are the risks and how to mitigate them?
    - What alternative routes exist if primary strategy fails?

    Cache: permanent under "strategy:{case_id}:{side}"
    Use force_regenerate=true to rebuild.
    """
    side = req.side.lower()
    if side not in ("petitioner", "respondent"):
        raise HTTPException(status_code=400, detail="side must be 'petitioner' or 'respondent'")

    cache_key = f"strategy:{case_id}:{side}"

    conn = await get_conn()
    try:
        await ensure_cache_table(conn)

        if not force_regenerate:
            cached = await get_cached(conn, cache_key)
            if cached:
                return JSONResponse({**cached, 'cached': True, 'done': True})

        ctx  = await fetch_case_core(conn, case_id)
        args = await fetch_cached_arguments(conn, case_id)

    finally:
        await conn.close()

    case_text = _compact_case_text(ctx)
    prompt    = _build_strategy_prompt(case_text, args, side)

    # Await LLM response before handling response
    data = await _stream_ollama_json(prompt, max_tokens=2000)

    # Cache if successful
    if not data.get("parse_error"):
        c = await get_conn()
        try:
            await ensure_cache_table(c)
            await set_cached(c, cache_key, data)
            log.info(f"[STRATEGY] Cached {cache_key}")
        except Exception as e:
            log.error(f"[STRATEGY] Cache failed: {e}")
        finally:
            await c.close()

    return JSONResponse({**data, 'cached': False, 'done': True})


# ════════════════════════════════════════════════════════
# 4. FACT VS LAW SEPARATION
# POST /api/cases/{case_id}/fact-law-separation
# ════════════════════════════════════════════════════════

@router.post("/api/cases/{case_id}/fact-law-separation")
async def fact_law_separation(case_id: str, force_regenerate: bool = False):
    """
    Tag each paragraph of a judgment as:
      - fact          : finding of fact (what happened)
      - law           : question of law (statute/precedent interpretation)
      - mixed         : court applying law to facts
      - ratio         : the binding legal principle established
      - procedural    : listing, adjournments, procedural history
      - order         : final directions / reliefs granted

    Also identifies burden of proof assignments — which party bears
    burden on which specific issue.

    Why this matters for lawyers:
    - Findings of FACT are much harder to overturn on appeal than questions of LAW
    - Knowing the burden of proof distribution reveals where a case is won or lost
    - Separating ratio from obiter shows what's binding vs persuasive

    Cache: permanent under "factlaw:{case_id}"
    """
    cache_key = f"factlaw:{case_id}"

    conn = await get_conn()
    try:
        await ensure_cache_table(conn)

        if not force_regenerate:
            cached = await get_cached(conn, cache_key)
            if cached:
                return JSONResponse({**cached, 'cached': True, 'done': True})

        ctx = await fetch_case_core(conn, case_id)

    finally:
        await conn.close()

    # Build a compact case header (no paragraphs — they go in the prompt separately)
    c = ctx["case"]
    case_header = (
        f"Case: {c['case_name']}\n"
        f"Court: {c['court']} | Year: {c.get('year','N/A')} | Outcome: {c.get('outcome','N/A')}"
    )
    prompt = _build_fact_law_prompt(case_header, ctx["paras"])

    # Await LLM response before handling response
    data = await _stream_ollama_json(prompt, max_tokens=2500)

    # Cache if successful
    if not data.get("parse_error"):
        conn2 = await get_conn()
        try:
            await ensure_cache_table(conn2)
            await set_cached(conn2, cache_key, data)
            log.info(f"[FACT-LAW] Cached {cache_key}")
        except Exception as e:
            log.error(f"[FACT-LAW] Cache failed: {e}")
        finally:
            await conn2.close()

    return JSONResponse({**data, 'cached': False, 'done': True})


# ════════════════════════════════════════════════════════
# BONUS: FULL REASONING BUNDLE
# GET /api/cases/{case_id}/reasoning-full
#
# Returns all cached reasoning data in one call.
# Frontend hits this on case page load — if everything is cached
# it returns instantly. If nothing is cached, returns what exists
# and tells client which analyses are pending.
# ════════════════════════════════════════════════════════

@router.get("/api/cases/{case_id}/reasoning-full")
async def reasoning_full(case_id: str):
    """
    Return all cached reasoning for a case in one response.

    Returns whatever is already cached — does NOT trigger generation.
    The client uses this to:
      1. Check what's available before page render
      2. Know which endpoints to POST to for missing analyses

    Response shape:
    {
      "arguments":         {...} | null,
      "counter_arguments": {...} | null,
      "strategy_pet":      {...} | null,
      "strategy_res":      {...} | null,
      "fact_law":          {...} | null,
      "quick_summary":     {...} | null,
      "pending": ["counter_arguments", "strategy_pet"]  ← what needs generation
    }
    """
    conn = await get_conn()
    try:
        await ensure_cache_table(conn)

        # Fetch from arguments_router cache
        args_row = await conn.fetchrow(
            "SELECT arguments_json FROM case_arguments_cache WHERE case_id = $1",
            case_id
        )

        # Fetch from brief cache (quick summary)
        summary_row = await conn.fetchrow(
            "SELECT brief_json FROM case_brief_cache WHERE cache_key = $1",
            f"summary:{case_id}"
        )

        # Fetch reasoning cache items
        reasoning_rows = await conn.fetch(
            "SELECT cache_key, data_json FROM case_reasoning_cache WHERE cache_key = ANY($1)",
            [
                f"counter:{case_id}",
                f"strategy:{case_id}:petitioner",
                f"strategy:{case_id}:respondent",
                f"factlaw:{case_id}",
            ]
        )
        reasoning_map = {r["cache_key"]: safe_parse_jsonb(r["data_json"]) for r in reasoning_rows}

    finally:
        await conn.close()

    arguments        = args_row["arguments_json"]    if args_row    else None
    quick_summary    = summary_row["brief_json"]     if summary_row else None
    counter_args     = reasoning_map.get(f"counter:{case_id}")
    strategy_pet     = reasoning_map.get(f"strategy:{case_id}:petitioner")
    strategy_res     = reasoning_map.get(f"strategy:{case_id}:respondent")
    fact_law         = reasoning_map.get(f"factlaw:{case_id}")

    pending = []
    if not arguments:        pending.append("arguments")
    if not counter_args:     pending.append("counter_arguments")
    if not strategy_pet:     pending.append("strategy_petitioner")
    if not strategy_res:     pending.append("strategy_respondent")
    if not fact_law:         pending.append("fact_law_separation")
    if not quick_summary:    pending.append("quick_summary")

    return {
        "case_id":           case_id,
        "arguments":         arguments,
        "counter_arguments": counter_args,
        "strategy_petitioner": strategy_pet,
        "strategy_respondent": strategy_res,
        "fact_law":          fact_law,
        "quick_summary":     quick_summary,
        "pending":           pending,
        "all_cached":        len(pending) == 0,
    }