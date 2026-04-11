"""
models.py
=========
Pydantic request/response schemas for all 3 modes.
These define exactly what the frontend sends and what it receives.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODEL (shared by all 3 modes)
# ─────────────────────────────────────────────────────────────────────────────

class SearchFilters(BaseModel):
    """Optional filters the user can apply"""
    court: Optional[str] = None           # e.g. "Supreme Court of India"
    court_code: Optional[str] = None      # e.g. "SC", "HC-DEL"
    year_from: Optional[int] = None       # e.g. 2020
    year_to: Optional[int] = None         # e.g. 2024
    acts: Optional[List[str]] = None      # e.g. ["IPC", "NDPS Act"]
    subject_tags: Optional[List[str]] = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, description="The legal query (min 2 chars for abbreviations like 'SC')")
    filters: Optional[SearchFilters] = None
    limit: Optional[int] = Field(default=10, le=50)
    case_context: Optional[str] = None   # case_id for citation graph expansion
    session_id: Optional[str] = None     # for chat history sidebar


# ─────────────────────────────────────────────────────────────────────────────
# SHARED SUB-MODELS
# ─────────────────────────────────────────────────────────────────────────────

class CaseResult(BaseModel):
    """A matched case from search"""
    case_id: str
    case_name: str
    court: Optional[str]
    year: Optional[int]
    relevance_score: float
    result_type: str                      # 'case' or 'paragraph'
    paragraph_text: Optional[str] = None  # snippet if paragraph result
    paragraph_id: Optional[str] = None
    citation_count: Optional[int] = None
    authority_score: Optional[float] = None
    outcome_summary: Optional[str] = None
    acts_referred: Optional[List[str]] = None
    search_mode: str                      # 'structured', 'semantic', 'hybrid'


class CitationTreeNode(BaseModel):
    """One node in the citation tree"""
    case_id: str
    case_name: str
    depth: int
    relationship: str   # 'cited', 'relied_on', 'overruled', 'distinguished'
    confidence: float
    children: Optional[List['CitationTreeNode']] = []

CitationTreeNode.model_rebuild()  # Required for self-referencing models


class CitationInfo(BaseModel):
    """Flat citation record"""
    source_case_id: str
    cited_case_id: Optional[str]
    target_citation: Optional[str]
    relationship: Optional[str]
    confidence: Optional[float]
    case_name: Optional[str]
    year: Optional[int]
    court: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# MODE 1: NORMAL MODE RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

class NormalModeResponse(BaseModel):
    """
    Normal Mode — No LLM. Pure search intelligence.
    Returns ranked cases + citation tree.
    """
    query: str
    mode: str = "normal"
    total_results: int
    results: List[CaseResult]
    citation_tree: Optional[CitationTreeNode] = None  # If case_context given
    citations_flat: Optional[List[CitationInfo]] = None
    filters_applied: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# MODE 2: RESEARCH MODE RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

class ParagraphReference(BaseModel):
    """A specific paragraph cited in the LLM answer"""
    paragraph_id: str
    case_id: str
    case_name: str
    text_snippet: str       # First 300 chars
    relevance_score: float
    page_no: Optional[int]
    para_no: Optional[int]


class ResearchModeResponse(BaseModel):
    """
    Research Mode — RAG answer with citations.
    LLM generates answer grounded in retrieved paragraphs.
    """
    query: str
    mode: str = "research"
    answer: str                              # LLM-generated answer
    citations: List[CaseResult]             # Top supporting cases
    paragraph_references: List[ParagraphReference]  # Exact paragraphs used
    total_results: int
    session_id: Optional[str] = None
    latency_ms: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# MODE 3: STUDY MODE RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

class StudyNote(BaseModel):
    """Auto-generated study note from case content"""
    heading: str
    content: str
    source_case_id: Optional[str] = None
    source_case_name: Optional[str] = None


class StudyModeResponse(BaseModel):
    """
    Study Mode — Research + simplified explanation + study notes.
    Great for law students and junior lawyers.
    """
    query: str
    mode: str = "study"
    answer: str                              # Simplified LLM answer
    simplified_explanation: str             # ELI5-style explanation
    key_notes: List[StudyNote]             # Bullet-point study notes
    case_summaries: List[Dict[str, Any]]   # 2-line summaries of top cases
    citations: List[CaseResult]
    paragraph_references: List[ParagraphReference]
    total_results: int
    session_id: Optional[str] = None
    latency_ms: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    success: bool
    document_id: str
    filename: str
    chunks_created: int
    embeddings_generated: int
    message: str


# ─────────────────────────────────────────────────────────────────────────────
# CITATION VALIDATION (PHASE 3)
# ─────────────────────────────────────────────────────────────────────────────

class CitationValidationResponse(BaseModel):
    case_id: str
    case_name: Optional[str]
    is_overruled: bool
    overruled_by: Optional[str] = None
    citation_count: int
    authority_score: Optional[float]
    latest_status: str   # "valid", "overruled", "distinguished", "unknown"
    court: Optional[str]
    year: Optional[int]


# ─────────────────────────────────────────────────────────────────────────────
# CHAT SESSION (sidebar history)
# ─────────────────────────────────────────────────────────────────────────────

class ChatSession(BaseModel):
    session_id: str
    title: str          # "Bail in NDPS cases"
    mode: str           # "research", "study", "normal"
    created_at: str
    last_query: Optional[str] = None
