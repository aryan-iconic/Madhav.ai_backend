"""
main.py
=======
Madhav.AI — FastAPI Backend
3 Modes: Normal (search only) | Research (RAG) | Study (RAG + notes)

Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env in database directory
env_path = Path(__file__).parent.parent / "database" / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    # Fallback to project root .env
    load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import time
import asyncio
from pydantic import BaseModel
from typing import Optional

from Backend.db import get_connection, close_connection
from Backend.models import (
    SearchRequest, NormalModeResponse, ResearchModeResponse,
    StudyModeResponse, ChatSession, UploadResponse, CitationValidationResponse,
    SearchFilters
)
from psycopg2.extras import RealDictCursor
from Backend.retrieval.router import route_query
from Backend.documents.upload import process_document_upload
from Backend.services.citation_graph import validate_citation
from Backend.llm.generator import extract_judgment_paragraph, generate_case_summary
from Backend.drafting.drafting_router import router as drafting_router
from Backend.search.search_router import router as search_router
from Backend.retrieval.study_router import router as study_router
from Backend.retrieval.arguments_router import router as arguments_router
from Backend.retrieval.legal_reasoning_router import router as reasoning_router
from Backend.precedent.precedent_router import router as precedent_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# APP STARTUP / SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Madhav.AI Backend starting...")
    log.info("✅ Database connection pool ready")
    yield
    log.info("🛑 Shutting down...")
    close_connection()

app = FastAPI(
    title="Madhav.AI Legal Intelligence API",
    description="Legal search engine with RAG — Normal, Research, Study modes",
    version="1.0.0-mvp",
    lifespan=lifespan
)

# Allow frontend (React/Next) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include drafting engine router
app.include_router(drafting_router)

# Include search enhancements router
app.include_router(search_router)

# Include study mode router (5 AI-powered features)
app.include_router(study_router)

# Include legal reasoning router (argument extraction, multi-case briefs, issue spotting)
app.include_router(arguments_router)

# Include legal reasoning router (Days 1, 2, 4 — counter-arguments, strategy, fact-law separation)
app.include_router(reasoning_router)

# Include precedent intelligence router (Day 2 — precedent status & citation context)
app.include_router(precedent_router)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "Madhav.AI Backend", "version": "1.0.0-mvp"}


# ─────────────────────────────────────────────────────────────────────────────
# FRONTEND ADAPTER ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────
# Maps old frontend /chat endpoint to new /search/* modes

class ChatRequest(BaseModel):
    query: str
    mode: str = "normal"  # 'normal', 'research', 'study'
    filters: Optional[SearchFilters] = None
    case_context: Optional[str] = None

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Adapter endpoint for frontend compatibility.
    Maps the old /chat endpoint to the new /search/* modes.
    
    Accepts: { query, mode: 'normal'|'research'|'study', filters?, case_context? }
    Returns: { text, cases } or { sections } based on mode
    
    IMPORTANT: For exact field matches (court, case_id, year, etc.), returns ALL results.
    For general queries, returns up to 100 results to balance UX and performance.
    """
    start = time.time()
    mode = request.mode.lower()
    
    if mode not in ["normal", "research", "study"]:
        raise HTTPException(status_code=400, detail="Mode must be 'normal', 'research', or 'study'")
    
    try:
        search_request = SearchRequest(
            query=request.query,
            filters=request.filters,
            case_context=request.case_context
        )
        
        conn = get_connection()
        
        # Use higher limit for /chat endpoint to support exact field matches
        # (e.g., "supreme court" should return all 2997 cases, not just 10)
        result = route_query(
            mode=mode,
            query=request.query,
            conn=conn,
            filters=request.filters,
            limit=2000,  # Increased from hardcoded 10 to support exact field matches
            case_context=request.case_context
        )
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        return result
        
    except Exception as e:
        log.error(f"[CHAT] Error in {mode} mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/case/{case_id}")
async def get_case(case_id: str):
    """
    Fetch a single case with all details (for inline viewer).
    Uses LLM to generate proper judgment summary from actual judgment paragraphs.
    
    Returns: Case object with case_name, court, year, LLM-generated judgment summary, paragraphs, citations, etc.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get case header info
        cursor.execute("""
            SELECT case_id, case_name, court, year, date_of_order, date_of_filing,
                   petitioner, respondent, judgment, outcome_summary, authority_score,
                   citation_count, total_paragraphs, acts_referred, subject_tags
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))
        
        case = cursor.fetchone()
        if not case:
            cursor.close()
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
        
        # Convert to dict and get paragraphs with type info for LLM analysis
        case_dict = dict(case)
        
        # Get paragraphs - including para_type for judgment identification
        cursor.execute("""
            SELECT paragraph_id, para_no, page_no, text, word_count, quality_score, para_type
            FROM legal_paragraphs
            WHERE case_id = %s
            ORDER BY para_no
            LIMIT 100
        """, (case_id,))
        
        paragraphs = [dict(r) for r in cursor.fetchall()]
        case_dict['paragraphs'] = paragraphs
        
        # Get citations
        cursor.execute("""
            SELECT id, cited_case_id, target_citation, relationship, confidence, context_sentence
            FROM case_citations
            WHERE source_case_id = %s
            ORDER BY confidence DESC
            LIMIT 20
        """, (case_id,))
        
        citations = [dict(r) for r in cursor.fetchall()]
        case_dict['citations'] = citations
        
        cursor.close()
        
        # ── USE LLM TO GENERATE COMPREHENSIVE CASE OVERVIEW ──
        try:
            # Step 1: Extract the best judgment paragraph using LLM
            judgment_info = extract_judgment_paragraph(paragraphs)
            case_dict['judgment_paragraph'] = judgment_info  # Include for debugging
            
            # Step 2: Generate comprehensive LLM case overview from all available data
            llm_summary = generate_case_summary(
                case_name=case_dict.get('case_name', 'Unknown Case'),
                judgment_text=judgment_info.get('judgment_text', ''),
                acts=case_dict.get('acts_referred', []),
                all_paragraphs=paragraphs  # Pass all paragraphs for context extraction
            )
            
            # Store LLM-generated comprehensive case overview
            case_dict['llm_summary'] = llm_summary
            
            # Override judgment field with LLM-generated one if not already set
            if not case_dict.get('judgment') or case_dict.get('judgment') == 'null':
                case_dict['judgment'] = judgment_info.get('judgment_text', '')
            
            log.info(f"✅ Generated comprehensive LLM case overview for case {case_id}")
            
        except Exception as llm_err:
            # If LLM fails, fall back to database values
            log.warning(f"LLM overview generation failed for {case_id}: {llm_err}")
            case_dict['llm_summary'] = None
            case_dict['llm_error'] = str(llm_err)
        
        return case_dict
        
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[GET_CASE] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search")
async def search(
    q: str = Query(None, description="Search query"),
    court: str = Query(None, description="Filter by court"),
    year_from: int = Query(None, description="From year"),
    year_to: int = Query(None, description="To year"),
    act: str = Query(None, description="Filter by statute/act"),
    exclude: str = Query(None, description="Exclude terms (comma-separated)"),
    limit: int = Query(2000, le=5000, description="Result limit (frontend handles pagination)")
):
    """
    Adapter endpoint for traditional keyword search.
    Maps frontend /search query params to /search/normal mode.
    
    Returns: { cases: Case[], total: number }
    
    NOTE: Frontend handles pagination (15 items/page) - backend returns all matching results
    up to 2000 by default to support the full result set for client-side pagination.
    """
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")
    
    start = time.time()
    
    try:
        # Build filters from query params
        filters = SearchFilters(
            court=court,
            year_from=year_from,
            year_to=year_to,
            acts=[act] if act else None
        )
        
        conn = get_connection()
        result = route_query(
            mode="normal",
            query=q,
            conn=conn,
            filters=filters,
            limit=limit
        )
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        return {"cases": result.get("cases", []), "total": len(result.get("cases", []))}
        
    except Exception as e:
        log.error(f"[SEARCH] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/suggestions")
async def get_suggestions(q: str = Query("", description="Partial search term")):
    """
    Suggestions for token search autocomplete.
    Returns suggestions for courts, keywords, years, judges, acts.
    Deduplicates results to prevent showing same suggestion twice.
    """
    await asyncio.sleep(0)  # Simulate async work
    
    q = q.lower().strip()
    if not q:
        # Return default suggestions
        return {
            "suggestions": [
                {"label": "Supreme Court of India", "type": "court", "value": "supreme", "keywords": ["sc", "supreme"]},
                {"label": "Delhi High Court", "type": "court", "value": "delhi", "keywords": ["delhi"]},
                {"label": "Constitutional Law", "type": "keyword", "value": "constitutional", "keywords": ["constitution", "const"]},
                {"label": "Fundamental Rights", "type": "keyword", "value": "fundamental rights", "keywords": ["fundamental", "rights", "fr"]},
            ]
        }
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Search across cases table for matching courts, acts, tags
        cursor.execute("""
            SELECT DISTINCT court FROM legal_cases WHERE court ILIKE %s LIMIT 5
        """, (f"%{q}%",))
        courts = [{"label": c[0], "type": "court", "value": c[0].lower(), "keywords": [q]} for c in cursor.fetchall()]
        
        cursor.execute("""
            SELECT DISTINCT subject_tags FROM legal_cases WHERE subject_tags ILIKE %s LIMIT 5
        """, (f"%{q}%",))
        keywords = [
            {"label": tag, "type": "keyword", "value": tag.lower(), "keywords": [q]} 
            for row in cursor.fetchall() 
            for tag in (row[0] or [])
            if q in tag.lower()
        ][:5]
        
        cursor.close()
        
        # Deduplicate: prevent same label from appearing multiple times
        seen_labels = set()
        unique_suggestions = []
        for suggestion in courts + keywords:
            label_key = suggestion.get('label', '').lower()
            if label_key not in seen_labels:
                seen_labels.add(label_key)
                unique_suggestions.append(suggestion)
        
        return {"suggestions": unique_suggestions[:7]}  # Limit to 7 total
        
    except Exception as e:
        log.error(f"[SUGGESTIONS] Error: {e}")
        # Return empty suggestions on error instead of failing
        return {"suggestions": []}


# ─────────────────────────────────────────────────────────────────────────────
# CORE SEARCH ENDPOINTS (3 MODES)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/search/normal", response_model=NormalModeResponse)
async def normal_search(request: SearchRequest):
    """
    NORMAL MODE — Fast legal search, NO LLM.
    Returns: matched cases, relevant paragraphs, citation tree.
    Competes with: Manupatra / SCC Online
    """
    start = time.time()
    log.info(f"[NORMAL] Query: '{request.query}'")

    try:
        conn = get_connection()
        result = route_query(
            mode="normal",
            query=request.query,
            conn=conn,
            filters=request.filters,
            limit=request.limit or 20,
            case_context=request.case_context
        )
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        return result

    except Exception as e:
        log.error(f"[NORMAL] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search/research")  # Remove response_model to allow full dict return with case_summary, metadata, etc.
async def research_search(request: SearchRequest):
    """
    RESEARCH MODE — Hybrid retrieval + LLM (Ollama RAG).
    Returns: AI answer + supporting case citations + paragraph references.
    Competes with: JurixAI
    """
    start = time.time()
    log.info(f"[RESEARCH] Query: '{request.query}'")

    try:
        conn = get_connection()
        result = route_query(
            mode="research",
            query=request.query,
            conn=conn,
            filters=request.filters,
            limit=request.limit or 10,
            case_context=request.case_context,
            session_id=request.session_id
        )
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        
        # CRITICAL: Log what we're about to return
        log.info(f"[RESEARCH-RESPONSE] Output type: {result.get('output_type')}")
        log.info(f"[RESEARCH-RESPONSE] Has case_summary: {'case_summary' in result}")
        if 'case_summary' in result:
            case_sum = result.get('case_summary')
            log.info(f"[RESEARCH-RESPONSE] case_summary type: {type(case_sum).__name__}, length: {len(case_sum) if isinstance(case_sum, str) else 'N/A'}")
        
        return result

    except Exception as e:
        log.error(f"[RESEARCH] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search/study", response_model=StudyModeResponse)
async def study_search(request: SearchRequest):
    """
    STUDY MODE — Research mode + simplified explanations + auto notes.
    Returns: AI answer + notes + case summaries + citations.
    Unique differentiator — beats everyone here.
    """
    start = time.time()
    log.info(f"[STUDY] Query: '{request.query}'")

    try:
        conn = get_connection()
        result = route_query(
            mode="study",
            query=request.query,
            conn=conn,
            filters=request.filters,
            limit=request.limit or 10,
            case_context=request.case_context,
            session_id=request.session_id
        )
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        return result

    except Exception as e:
        log.error(f"[STUDY] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# CITATION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/citation/{case_id}/validate", response_model=CitationValidationResponse)
async def validate_case_citation(case_id: str):
    """
    PHASE 3 — Citation validation.
    Is this case overruled? How many times cited? Current legal status?
    """
    try:
        conn = get_connection()
        result = validate_citation(conn, case_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/citation/{case_id}/tree")
async def get_citation_tree(
    case_id: str,
    depth: int = Query(default=2, le=3, description="Max tree depth (1-3)")
):
    """Get full citation tree for a case — cited by / relied on / overruled by"""
    try:
        conn = get_connection()
        from Backend.services.citation_graph import build_full_citation_tree
        tree = build_full_citation_tree(conn, case_id, max_depth=depth)
        return {"case_id": case_id, "tree": tree}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT UPLOAD (PHASE 2)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/documents/upload", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    session_id: str = Query(default=None, description="Link doc to a chat session")
):
    """
    PHASE 2 — Upload PDF/DOCX.
    Chunks it, generates embeddings, stores in legal_paragraphs table.
    Immediately searchable in Research mode.
    """
    if not file.filename.endswith(('.pdf', '.docx', '.txt')):
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, TXT supported")

    try:
        conn = get_connection()
        content = await file.read()
        result = process_document_upload(
            conn=conn,
            filename=file.filename,
            content=content,
            session_id=session_id
        )
        return result
    except Exception as e:
        log.error(f"[UPLOAD] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# CASE DETAIL ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/case/{case_id}")
async def get_case_detail(case_id: str):
    """Get full case details + all paragraphs + citations"""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT case_id, case_name, petitioner, respondent, court,
                   court_code, year, date_of_order, outcome_summary,
                   acts_referred, subject_tags, citation_count,
                   authority_score, total_paragraphs, pdf_url
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))

        from psycopg2.extras import RealDictCursor
        conn2 = get_connection()
        cur2 = conn2.cursor(cursor_factory=RealDictCursor)
        cur2.execute("""
            SELECT case_id, case_name, petitioner, respondent, court,
                   court_code, year, date_of_order, outcome_summary,
                   acts_referred, subject_tags, citation_count,
                   authority_score, total_paragraphs, pdf_url
            FROM legal_cases WHERE case_id = %s
        """, (case_id,))
        case = cur2.fetchone()
        cur2.close()

        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        return {"case": dict(case)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
