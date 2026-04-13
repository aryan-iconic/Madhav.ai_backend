"""
madhav.ai — Drafting Engine v3.0 (Phase 3: Multi-Backend with Auto-Failover)
FastAPI router: mount this in your main app.py with:
    from drafting_router import router as drafting_router
    app.include_router(drafting_router)

Phase 3 Features:
  - 20+ Indian legal document types
  - **PHASE 3 MULTI-BACKEND:** Ollama (primary, self-hosted) + Groq (free-tier fallback) with auto-failover
  - **SMART FUZZY MATCHING:** Find templates by partial name (e.g., "bail" → "bail_application")
  - Multi-language output (English, Hindi, Marathi, Tamil, Telugu, Bengali)
  - Jurisdiction-aware formatting for all major High Courts + Supreme Court
  - BNS / BNSS / BSA aware (post-July 2024 new criminal codes)
  - DB prefill from case metadata
  - PDF export via WeasyPrint
  - Draft refinement endpoint
  - Comprehensive backend health monitoring
  
Phase 3 Architecture:
  User Request → Template Fuzzy Matcher → LLM Strategy Selector
                                          ├→ Ollama (primary, 180s timeout)
                                          └→ Groq API (fallback, free tier, 60s timeout)
  
Groq Free Tier ($0/month):
  - Rate limit: ~10 req/min
  - Token quota: ~30K tokens/min (sufficient for legal documents)
  - No credit card required initially
  - Model: mixtral-8x7b-32768 (default)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, field_validator, ValidationError
from typing import Optional, Literal
import httpx
import json
import tempfile
import os
import re

# Phase 3: Multi-backend strategy + fuzzy matching
from .llm_strategies import OllamaStrategy, GroqStrategy, StrategySelector
from .template_matcher import init_matcher, resolve_template, get_suggestions

# ── DB connection (your existing module) ──────────────────────────────────────
try:
    from psycopg2.extras import RealDictCursor
    from Backend.db import get_connection
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

# ── PDF generation ─────────────────────────────────────────────────────────────
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["drafting"])


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: MULTI-BACKEND LLM STRATEGY WITH AUTO-FAILOVER
# ══════════════════════════════════════════════════════════════════════════════

# Load LLM backend configuration
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "mixtral-8x7b-32768")

# Initialize backends
ollama_strategy = OllamaStrategy(OLLAMA_URL, OLLAMA_MODEL)
groq_strategy = GroqStrategy(GROQ_API_KEY, GROQ_MODEL) if GROQ_API_KEY else None

# Strategy selector with auto-failover
strategy_selector = StrategySelector(primary=ollama_strategy, fallback=groq_strategy)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ══════════════════════════════════════════════════════════════════════════════

class DraftRequest(BaseModel):
    template_type: str
    # ── Parties ──
    party_name: str
    opposite_party: str
    # ── Facts & relief ──
    facts: str
    relief_sought: str
    # ── Optional enrichment ──
    tone: str = "formal"
    act_sections: Optional[str] = ""
    case_citations: Optional[str] = ""
    court: Optional[str] = ""
    jurisdiction: Optional[str] = ""
    language: Optional[str] = "english"
    state: Optional[str] = ""
    # ── Criminal law extras ──
    fir_number: Optional[str] = ""
    police_station: Optional[str] = ""
    custody_since: Optional[str] = ""
    charge_sheet_filed: Optional[bool] = None
    # ── Civil extras ──
    suit_number: Optional[str] = ""
    valuation: Optional[str] = ""
    # ── Contract extras ──
    contract_date: Optional[str] = ""
    consideration: Optional[str] = ""
    # ── Misc ──
    advocate_name: Optional[str] = ""
    advocate_enroll: Optional[str] = ""
    additional_instructions: Optional[str] = ""

    @field_validator('party_name', 'opposite_party', 'facts', 'relief_sought', mode='before')
    @classmethod
    def validate_required_fields(cls, v):
        if not v or not str(v).strip():
            raise ValueError("Required fields cannot be empty")
        return str(v).strip()

    @field_validator('language', mode='before')
    @classmethod
    def validate_language(cls, v):
        valid = ['english', 'hindi', 'marathi', 'tamil', 'telugu', 'bengali']
        if v not in valid:
            raise ValueError(f"Language must be one of: {', '.join(valid)}")
        return v

    @field_validator('tone', mode='before')
    @classmethod
    def validate_tone(cls, v):
        valid = ['formal', 'aggressive', 'concise', 'consumer_friendly']
        if v not in valid:
            raise ValueError(f"Tone must be one of: {', '.join(valid)}")
        return v

    @field_validator('template_type', mode='before')
    @classmethod
    def validate_template_type(cls, v):
        # Note: TEMPLATES dict is defined later in the file
        # We'll do runtime validation in the endpoint
        if not v or not str(v).strip():
            raise ValueError("template_type cannot be empty")
        return str(v).strip()


class RefineRequest(BaseModel):
    draft: str
    instruction: str
    template_type: Optional[str] = ""

    @field_validator('draft', 'instruction', mode='before')
    @classmethod
    def validate_text_fields(cls, v):
        if not v or not str(v).strip():
            raise ValueError("Text fields cannot be empty")
        return str(v).strip()


class ExportPDFRequest(BaseModel):
    draft: str
    title: str = "Legal Draft"
    party_name: Optional[str] = ""
    opposite_party: Optional[str] = ""
    court: Optional[str] = ""

    @field_validator('draft', mode='before')
    @classmethod
    def validate_draft(cls, v):
        if not v or not str(v).strip():
            raise ValueError("Draft text cannot be empty")
        return str(v).strip()


class FuzzyMatchRequest(BaseModel):
    query: str

    @field_validator('query', mode='before')
    @classmethod
    def validate_query(cls, v):
        if not v or not str(v).strip():
            raise ValueError("Template query cannot be empty")
        return str(v).strip()


# ══════════════════════════════════════════════════════════════════════════════
# TONE INSTRUCTIONS
# ══════════════════════════════════════════════════════════════════════════════

TONE_INSTRUCTIONS = {
    "formal": (
        "Maintain the measured, precise register of a Senior Advocate filing before the Supreme Court. "
        "Use passive constructions where appropriate: 'It is respectfully submitted', 'It is humbly prayed'. "
        "Every factual assertion must be attributed ('The petitioner submits that…'). "
        "Avoid contractions, colloquialisms, and personal opinions. "
        "Use 'this Hon'ble Court' and 'the learned counsel for the respondent' throughout."
    ),
    "aggressive": (
        "Adopt the uncompromising posture of an advocate pressing a strong case. "
        "Emphasise breaches, violations, and consequences in clear terms. "
        "Use phrases like 'glaring violation', 'wilful disregard', 'irreparable prejudice', "
        "'blatant abuse of process', 'manifest illegality'. "
        "Stress urgency at every turn. Reserve formalities but let the strength of the argument dominate. "
        "Still maintain court-level professionalism — aggressive in argument, never discourteous."
    ),
    "concise": (
        "Draft in the compressed style of a well-crafted interlocutory application. "
        "Every sentence must carry weight. No ornamental language. "
        "Each paragraph: one legal point, maximum four lines. "
        "Sub-points in numbered lists, not prose. "
        "Omit recitals unless essential. Begin each section with the conclusion, then the reasoning."
    ),
    "consumer_friendly": (
        "Draft so that a lay client reading the document can follow the argument. "
        "Avoid Latin maxims without explanation. Define legal terms the first time they appear. "
        "Maintain full formality in headings and prayer but use plain English in fact paragraphs."
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT TEMPLATES  (20 types)
# ══════════════════════════════════════════════════════════════════════════════

TEMPLATES = {

    # ── Criminal ──────────────────────────────────────────────────────────────

    "bail_application": {
        "title": "Bail Application",
        "act_hint": "BNS 2023 / BNSS 2023 (formerly IPC/CrPC)",
        "sections": [
            "Court & Case Header",
            "Applicant / Petitioner Details",
            "FIR Details & Offence",
            "Custody Duration & Investigation Stage",
            "Grounds for Bail",
            "Personal Circumstances & Roots in Community",
            "Supporting Precedents (with principle extracted)",
            "Conditions Offered by Petitioner",
            "Prayer",
            "Verification",
        ],
    },

    "anticipatory_bail": {
        "title": "Anticipatory Bail Application",
        "act_hint": "Section 482 BNSS 2023 (formerly S.438 CrPC)",
        "sections": [
            "Court & Case Header",
            "Applicant Details",
            "Background & FIR / Complaint Details",
            "Apprehension of Arrest — Basis",
            "Grounds for Anticipatory Bail",
            "Absence of Flight Risk / Tampering Risk",
            "Supporting Precedents",
            "Conditions Offered",
            "Prayer",
            "Verification",
        ],
    },

    "quashing_petition": {
        "title": "Petition for Quashing of FIR / Proceedings",
        "act_hint": "Section 528 BNSS 2023 (formerly S.482 CrPC) / Article 226 Constitution",
        "sections": [
            "Court & Case Header",
            "Parties",
            "Jurisdiction",
            "Background & FIR / Proceedings",
            "Grounds for Quashing",
            "Prima Facie Case for Quashing",
            "Abuse of Process / No Cognizable Offence Disclosed",
            "Supporting Precedents",
            "Interim Stay Prayer (if any)",
            "Final Prayer",
            "Verification",
        ],
    },

    "discharge_application": {
        "title": "Application for Discharge",
        "act_hint": "Sections 250–252 BNSS 2023 (Sessions) / Section 263 BNSS (Magistrate)",
        "sections": [
            "Court & Case Header",
            "Accused / Applicant Details",
            "Charge Sheet / Chargeframe Details",
            "Grounds for Discharge",
            "Insufficient Material Against Accused",
            "No Prima Facie Case",
            "Supporting Precedents",
            "Prayer for Discharge",
            "Verification",
        ],
    },

    "revision_petition_criminal": {
        "title": "Criminal Revision Petition",
        "act_hint": "Sections 438–445 BNSS 2023 (formerly S.397–401 CrPC)",
        "sections": [
            "Court & Case Header",
            "Parties & Original Court Details",
            "Impugned Order / Judgment Details",
            "Grounds for Revision",
            "Illegality / Irregularity / Impropriety",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    # ── Civil ─────────────────────────────────────────────────────────────────

    "plaint": {
        "title": "Civil Plaint",
        "act_hint": "CPC 1908 — Order VII Rule 1",
        "sections": [
            "Court & Case Header",
            "Parties & Addresses",
            "Jurisdiction (Territorial, Pecuniary, Subject-matter)",
            "Cause of Action — When & Where Arose",
            "Facts of the Case (Chronological)",
            "Legal Basis & Applicable Provisions",
            "Valuation & Court Fees",
            "Reliefs Claimed",
            "List of Documents",
            "Prayer",
            "Verification",
        ],
    },

    "written_statement": {
        "title": "Written Statement",
        "act_hint": "CPC 1908 — Order VIII",
        "sections": [
            "Court & Case Header",
            "Defendant / Respondent Details",
            "Preliminary Objections",
            "Para-wise Reply to Plaint",
            "Additional Facts in Defence",
            "Set-Off / Counter-claim (if any)",
            "Legal Grounds",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    "injunction_application": {
        "title": "Application for Injunction (Interim / Permanent)",
        "act_hint": "Order XXXIX Rules 1 & 2 CPC / Section 37–42 Specific Relief Act 1963",
        "sections": [
            "Court & Case Header",
            "Applicant & Respondent Details",
            "Background & Facts",
            "Three-Part Test: Prima Facie Case",
            "Three-Part Test: Irreparable Injury",
            "Three-Part Test: Balance of Convenience",
            "Applicable Provisions",
            "Supporting Precedents",
            "Undertaking as to Damages",
            "Prayer for Injunction",
            "Verification",
        ],
    },

    "appeal_civil": {
        "title": "First Appeal / Memorandum of Appeal",
        "act_hint": "Order XLI CPC / Section 96–99 CPC",
        "sections": [
            "Court & Case Header",
            "Appellant & Respondent Details",
            "Original Decree / Judgment Details",
            "Grounds of Appeal",
            "Substantial Questions of Law",
            "Factual Errors in Judgment",
            "Legal Errors in Judgment",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    "execution_application": {
        "title": "Execution Application",
        "act_hint": "Order XXI CPC",
        "sections": [
            "Court & Case Header",
            "Decree Holder & Judgment Debtor Details",
            "Decree / Award Details",
            "Grounds for Execution",
            "Mode of Execution Sought",
            "Assets / Property (if known)",
            "Prayer",
            "Verification",
        ],
    },

    "counter_claim": {
        "title": "Counter-Claim",
        "act_hint": "Order VIII Rule 6A CPC",
        "sections": [
            "Court & Case Header",
            "Counter-Claimant (Defendant) Details",
            "Original Suit Background",
            "Grounds for Counter-Claim",
            "Counter-Claim Allegations (Para-wise)",
            "Relief Sought in Counter-Claim",
            "Applicable Provisions",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    # ── Constitutional / Writ ─────────────────────────────────────────────────

    "writ_petition_hc": {
        "title": "Writ Petition (High Court — Article 226)",
        "act_hint": "Article 226 Constitution of India",
        "sections": [
            "Court & Case Header",
            "Parties",
            "Jurisdiction under Article 226",
            "Facts of the Case",
            "Questions of Law",
            "Grounds — Violation of Fundamental / Legal Rights",
            "Failure of the State / Authority",
            "Supporting Precedents",
            "Interim Relief Prayer (if any)",
            "Final Prayer",
            "Verification",
        ],
    },

    "writ_petition_sc": {
        "title": "Writ Petition (Supreme Court — Article 32)",
        "act_hint": "Article 32 Constitution of India",
        "sections": [
            "Court & Case Header",
            "Parties",
            "Jurisdiction under Article 32",
            "Statement of Facts",
            "Questions of Law",
            "Grounds — Violation of Fundamental Rights",
            "Why High Court Relief is Inadequate / Not Approached",
            "Supporting Precedents",
            "Interim Relief Prayer",
            "Final Prayer",
            "Verification",
        ],
    },

    # ── Notices & Communications ──────────────────────────────────────────────

    "legal_notice": {
        "title": "Legal Notice",
        "act_hint": "As applicable to the cause of action",
        "sections": [
            "Advocate / Sender Details",
            "Addressee Details",
            "Subject Line",
            "Instructions of Client",
            "Background & Facts",
            "Legal Violations / Cause of Action",
            "Applicable Provisions",
            "Demand / Relief",
            "Time for Compliance",
            "Consequence of Non-Compliance",
            "Closing",
        ],
    },

    "reply_to_legal_notice": {
        "title": "Reply to Legal Notice",
        "act_hint": "General — no specific act",
        "sections": [
            "Advocate / Sender Details",
            "Original Notice Reference",
            "Denial of Allegations (Para-wise)",
            "Counter-Position of Client",
            "Legal Basis for Denial",
            "Demand / Counter-demand",
            "Closing Warning (if appropriate)",
        ],
    },

    # ── Family Law ────────────────────────────────────────────────────────────

    "divorce_petition": {
        "title": "Divorce Petition",
        "act_hint": "Hindu Marriage Act 1955 / Special Marriage Act 1954 / IDMA 2019",
        "sections": [
            "Court & Case Header",
            "Petitioner & Respondent Details",
            "Date & Place of Marriage",
            "Ground(s) for Divorce (specify: cruelty / desertion / adultery / etc.)",
            "Detailed Facts Supporting the Ground",
            "Children & Custody Details (if applicable)",
            "Matrimonial Property / Maintenance",
            "Applicable Provisions",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    "maintenance_application": {
        "title": "Application for Maintenance",
        "act_hint": "Section 144 BNSS / Section 125 CrPC / Section 24 HMA / PWDVA 2005",
        "sections": [
            "Court & Case Header",
            "Applicant & Respondent Details",
            "Relationship & Dependency",
            "Income & Financial Circumstances of Respondent",
            "Applicant's Needs & Expenses",
            "Grounds for Maintenance",
            "Supporting Precedents",
            "Interim Maintenance Prayer (if urgent)",
            "Final Prayer",
            "Verification",
        ],
    },

    # ── Property / Revenue ────────────────────────────────────────────────────

    "eviction_petition": {
        "title": "Eviction Petition",
        "act_hint": "Transfer of Property Act 1882 / State Rent Control Acts",
        "sections": [
            "Court & Case Header",
            "Landlord (Petitioner) & Tenant (Respondent) Details",
            "Property Description",
            "Tenancy History & Rent Agreed",
            "Ground(s) for Eviction",
            "Default / Breach Details",
            "Applicable Provisions (State Rent Act + TPA)",
            "Supporting Precedents",
            "Prayer",
            "Verification",
        ],
    },

    # ── Contracts & Commercial ────────────────────────────────────────────────

    "contract_agreement": {
        "title": "Contract / Agreement",
        "act_hint": "Indian Contract Act 1872",
        "sections": [
            "Title, Date & Place of Execution",
            "Parties & Addresses",
            "Recitals (WHEREAS clauses)",
            "Definitions & Interpretation",
            "Scope of Work / Subject Matter",
            "Consideration & Payment Terms",
            "Rights & Obligations of Each Party",
            "Representations & Warranties",
            "Confidentiality & IP Rights",
            "Indemnification & Limitation of Liability",
            "Dispute Resolution (Arbitration / Courts)",
            "Governing Law & Jurisdiction",
            "Term, Termination & Amendment",
            "Force Majeure",
            "Signature Block & Execution",
        ],
    },

    "affidavit": {
        "title": "Affidavit",
        "act_hint": "Notaries Act 1952 / Oaths Act 1969",
        "sections": [
            "Court / Authority Header",
            "Deponent Details",
            "Solemn Affirmation",
            "Numbered Statement of Facts",
            "Verification Clause",
            "Notary / Commissioner Block",
        ],
    },

    "consumer_complaint": {
        "title": "Consumer Complaint",
        "act_hint": "Consumer Protection Act 2019",
        "sections": [
            "Forum / Commission Header",
            "Complainant & Opposite Party Details",
            "Jurisdiction (Pecuniary & Territorial)",
            "Facts of the Case",
            "Deficiency in Service / Unfair Trade Practice",
            "Loss / Damage Suffered",
            "Applicable Provisions (CPA 2019)",
            "Reliefs Sought (Refund / Compensation / Penalty)",
            "List of Documents",
            "Prayer",
            "Verification",
        ],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# LANGUAGE SUPPORT
# ══════════════════════════════════════════════════════════════════════════════

LANGUAGE_PROMPTS = {
    "english":  "",
    "hindi":    "\n\nLANGUAGE INSTRUCTION: Translate the entire document into formal legal Hindi (Devanagari script). Use standard legal Hindi terminology (न्यायालय, याचिकाकर्ता, प्रतिवादी, आवेदन, etc.). Maintain all case citations in English.",
    "marathi":  "\n\nLANGUAGE INSTRUCTION: Translate the entire document into formal Marathi (Devanagari script). Use Maharashtra court-standard Marathi legal terminology. Maintain all case citations in English.",
    "tamil":    "\n\nLANGUAGE INSTRUCTION: Translate the entire document into formal Tamil (Tamil script). Use Madras High Court standard Tamil legal terminology. Maintain all case citations in English.",
    "telugu":   "\n\nLANGUAGE INSTRUCTION: Translate the entire document into formal Telugu (Telugu script). Use Andhra Pradesh / Telangana High Court standard legal terminology. Maintain all case citations in English.",
    "bengali":  "\n\nLANGUAGE INSTRUCTION: Translate the entire document into formal Bengali (Bengali script). Use Calcutta High Court standard legal terminology. Maintain all case citations in English.",
}


# ══════════════════════════════════════════════════════════════════════════════
# JURISDICTION RULES
# ══════════════════════════════════════════════════════════════════════════════

JURISDICTION_RULES = {
    "maharashtra":      "Follow Bombay High Court Original Side / Appellate Side Rules as applicable. Refer to Bombay Civil Manual. Stamp duty under Maharashtra Stamp Act 2017.",
    "delhi":            "Follow Delhi High Court (Original Side) Rules 2018. Refer to Delhi Courts Practice Directions. Include District Court Establishment details where relevant.",
    "karnataka":        "Follow Karnataka High Court Rules. Cite under Karnataka Civil Rules of Practice 1967 where applicable.",
    "tamil_nadu":       "Follow Madras High Court Original Side Rules 1956 / Appellate Side Rules. Reference Tamil Nadu Court Fees and Suits Valuation Act 1955.",
    "uttar_pradesh":    "Follow Allahabad High Court Rules 1952. Use proper Allahabad HC formatting with Roman/English cause-title conventions.",
    "west_bengal":      "Follow Calcutta High Court (Original Side) Rules 1914 / Appellate Side Rules. Reference Bengal, Agra and Assam Civil Courts Act 1887 where relevant.",
    "rajasthan":        "Follow Rajasthan High Court Rules 1952. Reference Rajasthan Stamp Act.",
    "gujarat":          "Follow Gujarat High Court Rules. Reference Bombay Stamp Act (as applicable in Gujarat).",
    "madhya_pradesh":   "Follow Madhya Pradesh High Court Rules. Reference MP Stamp Act.",
    "andhra_pradesh":   "Follow Andhra Pradesh High Court Rules. Reference AP Courts Act 1964.",
    "telangana":        "Follow Telangana High Court Rules. Reference Telangana Courts Act.",
    "kerala":           "Follow Kerala High Court Act 1958 and Rules. Reference Kerala Court Fees and Suits Valuation Act.",
    "punjab_haryana":   "Follow Punjab and Haryana High Court Rules and Orders. Reference Punjab Courts Act 1918.",
    "supreme_court":    "Follow Supreme Court Rules 2013. Reference the Constitution, Civil Procedure Code, and Supreme Court Practice & Procedure. Use SLP (Civil) / SLP (Criminal) format where applicable.",
}


# ══════════════════════════════════════════════════════════════════════════════
# CRIMINAL LAW TRANSITION NOTE (BNS / BNSS / BSA — effective 1 July 2024)
# ══════════════════════════════════════════════════════════════════════════════

BNS_NOTE = """
IMPORTANT — NEW CRIMINAL CODES (effective 1 July 2024):
- Bharatiya Nyaya Sanhita (BNS) 2023 replaces Indian Penal Code 1860.
- Bharatiya Nagarik Suraksha Sanhita (BNSS) 2023 replaces Code of Criminal Procedure 1973.
- Bharatiya Sakshya Adhiniyam (BSA) 2023 replaces Indian Evidence Act 1872.
- For offences committed ON OR AFTER 1 July 2024: cite BNS/BNSS/BSA sections.
- For offences committed BEFORE 1 July 2024: cite IPC/CrPC/Evidence Act.
- Always cross-reference both codes in transitional cases (e.g., "Section 318(4) BNS [erstwhile Section 420 IPC]").
"""


# ══════════════════════════════════════════════════════════════════════════════
# PROMPT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _criminal_extras(req: DraftRequest) -> str:
    parts = []
    if req.fir_number:
        parts.append(f"FIR Number: {req.fir_number}")
    if req.police_station:
        parts.append(f"Police Station: {req.police_station}")
    if req.custody_since:
        parts.append(f"Petitioner in custody since: {req.custody_since}")
    if req.charge_sheet_filed is not None:
        status = "filed" if req.charge_sheet_filed else "NOT yet filed"
        parts.append(f"Charge sheet status: {status}")
    return ("\nCRIMINAL MATTER DETAILS:\n" + "\n".join(parts)) if parts else ""


def _civil_extras(req: DraftRequest) -> str:
    parts = []
    if req.suit_number:
        parts.append(f"Suit / Case Number: {req.suit_number}")
    if req.valuation:
        parts.append(f"Suit Valuation / Court Fees: {req.valuation}")
    return ("\nCIVIL MATTER DETAILS:\n" + "\n".join(parts)) if parts else ""


def _contract_extras(req: DraftRequest) -> str:
    parts = []
    if req.contract_date:
        parts.append(f"Contract Date: {req.contract_date}")
    if req.consideration:
        parts.append(f"Consideration / Amount: {req.consideration}")
    return ("\nCONTRACT DETAILS:\n" + "\n".join(parts)) if parts else ""


def _advocate_block(req: DraftRequest) -> str:
    if req.advocate_name:
        enroll = f" (Enrolment No. {req.advocate_enroll})" if req.advocate_enroll else ""
        return f"\nADVOCATE ON RECORD:\n{req.advocate_name}{enroll}"
    return ""


def build_draft_prompt(req: DraftRequest) -> tuple:
    """Build prompt; returns (prompt_text, actual_template_key)."""
    # Initialize template matcher on first use
    if not hasattr(build_draft_prompt, '_initialized'):
        init_matcher(TEMPLATES)
        build_draft_prompt._initialized = True
    
    # Try exact match first, then fuzzy
    tmpl_key = req.template_type
    tmpl = TEMPLATES.get(tmpl_key)
    
    if not tmpl:
        # Use fuzzy matcher
        matched_key, score, method = resolve_template(req.template_type)
        if matched_key and score >= 0.6:
            tmpl_key = matched_key
            tmpl = TEMPLATES[matched_key]
        else:
            suggestions = get_suggestions(req.template_type)
            suggestion_text = ""
            if suggestions:
                suggestion_text = "\nDid you mean: " + ", ".join(s["template"] for s in suggestions) + "?"
            raise HTTPException(
                status_code=400,
                detail=f"Unknown template_type: '{req.template_type}'. "
                       f"Valid options: {list(TEMPLATES.keys())}{suggestion_text}"
            )

    tone_instr       = TONE_INSTRUCTIONS.get(req.tone, TONE_INSTRUCTIONS["formal"])
    sections_list    = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(tmpl["sections"]))
    language_instr   = LANGUAGE_PROMPTS.get((req.language or "english").lower(), "")
    jurisdiction_rule = JURISDICTION_RULES.get((req.state or "").lower().replace(" ", "_"), "")

    citations_block = (
        f"\nCASE CITATIONS TO WEAVE IN (explain principle for each — do not merely name-drop):\n{req.case_citations}"
        if req.case_citations else ""
    )
    acts_block = (
        f"\nAPPLICABLE LEGISLATION / SECTIONS (introduce fully on first mention; abbreviate thereafter):\n{req.act_sections}"
        if req.act_sections else ""
    )
    jurisdiction_block = (
        f"\nJURISDICTION-SPECIFIC RULES:\n{jurisdiction_rule}"
        if jurisdiction_rule else ""
    )
    additional_block = (
        f"\nADDITIONAL DRAFTING INSTRUCTIONS:\n{req.additional_instructions}"
        if req.additional_instructions else ""
    )

    # Decide whether to inject BNS transition note
    criminal_types = {
        "bail_application", "anticipatory_bail", "quashing_petition",
        "discharge_application", "revision_petition_criminal",
    }
    bns_block = BNS_NOTE if req.template_type in criminal_types else ""

    criminal_extras  = _criminal_extras(req)
    civil_extras     = _civil_extras(req)
    contract_extras  = _contract_extras(req)
    advocate_block   = _advocate_block(req)

    prompt = f"""You are Madhav — the drafting intelligence of Madhav.ai — modelled on a Senior Advocate with 25+ years of court experience across district courts, High Courts, and the Supreme Court of India.

Your task is to generate a COMPLETE, COURT-READY {tmpl["title"]} that could be filed as-is (after filling placeholders).

════════════════════════════════════════════
DOCUMENT TYPE: {tmpl["title"]}
RELEVANT LAW HINT: {tmpl["act_hint"]}
════════════════════════════════════════════

TONE & STYLE:
{tone_instr}

PARTIES:
- Petitioner / Applicant / Plaintiff / Claimant: {req.party_name}
- Respondent / Opposite Party / Defendant: {req.opposite_party}
- Court: {req.court or "[TO BE SPECIFIED]"}
- Jurisdiction / State: {req.jurisdiction or req.state or "India"}
{advocate_block}

FACTS (use these as the factual core — expand with legal framing):
{req.facts}

RELIEF SOUGHT:
{req.relief_sought}
{acts_block}
{citations_block}
{criminal_extras}
{civil_extras}
{contract_extras}
{jurisdiction_block}
{bns_block}
{additional_block}

════════════════════════════════════════════
MANDATORY STRUCTURE — generate EXACTLY these sections in this order:
{sections_list}
════════════════════════════════════════════

FORMATTING RULES FOR INDIAN COURTS (NON-NEGOTIABLE):

1. DOCUMENT HEADER
   - Court name in FULL CAPS, centred: "IN THE HIGH COURT OF [STATE] AT [CITY]"
   - Case number line: e.g., "BAIL APPLICATION NO. [CASE_NO] OF [YEAR]"
   - Parties block: centred, with "...PETITIONER" and "...RESPONDENT" on separate lines
   - Horizontal rule separating header from body

2. SECTION HEADINGS
   - Bold, UPPERCASE, with ordinal number: "1. FACTS OF THE CASE"
   - Sub-sections: "1.1", "1.2" etc.

3. PARAGRAPH NUMBERING
   - Every substantive paragraph gets a number
   - No bullet points in the body — only numbered paragraphs or sub-paragraphs
   - Each paragraph: 4–6 lines maximum

4. LEGAL REFERENCES
   - Full name on first mention: "Section 482 of the Code of Criminal Procedure, 1973 (hereinafter 'CrPC')"
   - Case citations: "[Case Name] [(Year)] [Volume] [Reporter] [Page]" e.g., "Arnesh Kumar v. State of Bihar (2014) 8 SCC 273"
   - For every case cited: state the HOLDING or PRINCIPLE in 1–2 sentences, not just the name

5. FORMAL LANGUAGE
   - "It is most respectfully submitted that…"
   - "This Hon'ble Court may be pleased to…"
   - "It is humbly prayed that…"
   - Never "please", never "kindly"

6. PRAYER CLAUSE
   - Start: "WHEREFORE, IT IS MOST RESPECTFULLY PRAYED THAT THIS HON'BLE COURT MAY BE PLEASED TO:"
   - Numbered prayer items: (i), (ii), (iii)…
   - End: "AND FOR SUCH OTHER AND FURTHER RELIEF(S) AS THIS HON'BLE COURT MAY DEEM FIT AND PROPER IN THE FACTS AND CIRCUMSTANCES OF THE CASE."
   - Final line: "AND AS IN DUTY BOUND, THE PETITIONER SHALL EVER PRAY."

7. VERIFICATION CLAUSE
   - "VERIFICATION: I, [NAME], [son/daughter] of [FATHER'S NAME], aged [AGE] years, resident of [ADDRESS], do hereby solemnly verify that the contents of paragraphs [X] to [Y] above are true and correct to my personal knowledge, and that nothing material has been concealed therefrom. I have not suppressed any material fact."
   - Signed at [PLACE] on [DATE]

8. SIGNATURE BLOCK
   - Right-aligned:
     [PLACE], [DATE]
     _________________________
     {req.advocate_name or "[ADVOCATE'S NAME]"}
     Advocate for the Petitioner
     Enrolment No.: {req.advocate_enroll or "[ENROLMENT_NO]"}

9. PLACEHOLDERS
   - Use [SQUARE_BRACKET_PLACEHOLDERS] for every value not provided
   - Examples: [CASE_NO], [DATE_OF_ARREST], [POLICE_STATION], [FIR_NO], [AGE], [ADDRESS]
   - Do NOT explain placeholders in the text

10. DO NOT INCLUDE
    - Any preamble or "Here is the draft…" commentary
    - AI disclaimers or meta-text of any kind
    - Anything after the signature block
    - Suggestions or editorial notes

════════════════════════════════════════════
BEGIN THE DOCUMENT NOW. START DIRECTLY WITH THE COURT HEADER:
════════════════════════════════════════════
{language_instr}"""
    
    return (prompt, tmpl_key)


def build_refine_prompt(req: RefineRequest) -> str:
    tmpl_hint = ""
    if req.template_type and req.template_type in TEMPLATES:
        tmpl_hint = f"\nDocument type: {TEMPLATES[req.template_type]['title']}"

    return f"""You are Madhav — an expert Indian legal document refiner.{tmpl_hint}

REFINEMENT INSTRUCTION:
"{req.instruction}"

RULES DURING REFINEMENT:
1. Apply the instruction SURGICALLY — modify only what is asked.
2. Maintain existing paragraph numbering and structure.
3. Preserve all case citations, section numbers, and placeholders.
4. Do NOT add meta-commentary, explanations, or AI disclaimers.
5. Output the COMPLETE revised document — not just the changed portion.
6. If asked to ADD content, integrate it naturally into the relevant section.
7. If asked to REMOVE content, maintain sequential paragraph numbering.
8. Maintain the same tone, formatting, and language throughout.
9. Do NOT alter the Verification or Signature block unless explicitly asked.
10. Follow all Indian court formatting rules (uppercase headings, numbered paras, proper prayer).

ORIGINAL DRAFT:
{req.draft}

OUTPUT THE FULLY REFINED DOCUMENT (COMPLETE AND COURT-READY — NO PREAMBLE):"""


# ══════════════════════════════════════════════════════════════════════════════
# LLM STREAMING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _stream_ollama(prompt: str):
    """Yield SSE tokens from Ollama."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": 0.25,   # Low variance = consistent legal text
            "num_predict": 4096,   # Longer documents
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        },
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", OLLAMA_URL, json=payload) as response:
            if response.status_code != 200:
                yield f"data: {json.dumps({'error': f'Ollama HTTP {response.status_code}'})}\n\n"
                return
            async for line in response.aiter_lines():
                if line:
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        done  = chunk.get("done", False)
                        yield f"data: {json.dumps({'token': token, 'done': done})}\n\n"
                        if done:
                            break
                    except json.JSONDecodeError:
                        continue


def get_streamer(prompt: str):
    """Return Ollama streamer (Phase 1-2: Ollama-only backend)."""
    return _stream_ollama(prompt)


# ══════════════════════════════════════════════════════════════════════════════
# PDF HTML TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

def _build_pdf_html(title: str, draft_text: str, party_name: str = "",
                    opposite_party: str = "", court: str = "") -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: A4;
    margin: 1in 1.25in 1in 1.25in;
    @bottom-center {{
      content: "Page " counter(page) " of " counter(pages);
      font-size: 9pt;
      color: #666;
    }}
  }}
  @page :first {{ @bottom-center {{ content: ""; }} }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: "Cambria", "Times New Roman", Georgia, serif;
    font-size: 12pt;
    line-height: 1.6;
    color: #111;
    text-align: justify;
    text-justify: inter-word;
  }}
  .court-header {{
    text-align: center;
    font-weight: bold;
    font-size: 13pt;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 0.6rem;
  }}
  .parties-block {{
    text-align: center;
    font-size: 11.5pt;
    margin: 0.8rem 0;
    line-height: 1.8;
  }}
  hr.header-rule {{
    border: none;
    border-top: 1.5px solid #111;
    margin: 0.8rem 0;
  }}
  h2 {{
    font-size: 11.5pt;
    text-transform: uppercase;
    font-weight: bold;
    margin-top: 1rem;
    margin-bottom: 0.4rem;
    page-break-after: avoid;
  }}
  p {{
    margin: 0.4rem 0;
    text-indent: 0.5in;
    orphans: 3;
    widows: 3;
  }}
  p.no-indent {{ text-indent: 0; }}
  p.centered {{ text-align: center; text-indent: 0; }}
  p.right-align {{ text-align: right; text-indent: 0; }}
  .signature-block {{
    margin-top: 2rem;
    text-align: right;
  }}
  .sig-line {{
    display: inline-block;
    border-top: 1px solid #111;
    width: 2.5in;
    margin-top: 1.5rem;
    padding-top: 0.2rem;
    text-align: center;
    font-size: 10pt;
  }}
  .verification {{
    margin-top: 1.5rem;
    border-top: 1px solid #111;
    padding-top: 0.8rem;
    font-size: 11pt;
    page-break-inside: avoid;
  }}
  .placeholder {{
    background-color: #fffde7;
    border-bottom: 1px dotted #e65100;
    padding: 1px 3px;
    font-weight: bold;
  }}
  pre {{
    white-space: pre-wrap;
    word-break: break-word;
    font-family: "Cambria", "Times New Roman", Georgia, serif;
    font-size: 12pt;
    line-height: 1.6;
  }}
  .footer {{
    margin-top: 2rem;
    border-top: 1px solid #ccc;
    padding-top: 0.6rem;
    font-size: 8.5pt;
    color: #888;
    text-align: center;
  }}
  .page-break {{ page-break-after: always; }}
</style>
</head>
<body>
  <div class="court-header">{court or "[COURT NAME]"}</div>
  <div class="parties-block">
    <strong>{party_name or "[PETITIONER / APPLICANT]"}</strong> &nbsp;...Petitioner<br>
    <em>Versus</em><br>
    <strong>{opposite_party or "[RESPONDENT / OPPOSITE PARTY]"}</strong> &nbsp;...Respondent
  </div>
  <hr class="header-rule">
  <pre>{draft_text}</pre>
  <div class="footer">
    Generated by Madhav.ai &mdash; Legal Intelligence Platform &nbsp;|&nbsp;
    This draft requires review by a qualified advocate before filing. &nbsp;|&nbsp;
    Fill all [PLACEHOLDERS] before submission.
  </div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/draft")
async def generate_draft(req: DraftRequest):
    """
    Generate a legal document draft (streaming SSE).
    Phase 3: Auto-routes to Ollama (primary) or Groq (fallback).
    Supports fuzzy template matching.
    
    Streams tokens: { "token": "...", "done": false }
    Final chunk: { "token": "", "done": true }
    """
    try:
        prompt, actual_template_key = build_draft_prompt(req)
        return StreamingResponse(
            strategy_selector.get_streamer(prompt),
            media_type="text/event-stream",
            headers={"X-Template-Used": actual_template_key}
        )
    except ValidationError as e:
        error_details = "; ".join([f"{err['loc'][0]}: {err['msg']}" for err in e.errors()])
        raise HTTPException(
            status_code=422,
            detail=f"Validation error: {error_details}"
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Internal error: {str(e)}"
        )


@router.post("/draft/refine")
async def refine_draft(req: RefineRequest):
    """
    Refine an existing draft with a specific instruction (streaming SSE).
    Phase 3: Auto-routes to Ollama (primary) or Groq (fallback).
    E.g., "Add a ground about parity with co-accused" or "Translate to Hindi".
    """
    try:
        prompt = build_refine_prompt(req)
        return StreamingResponse(
            strategy_selector.get_streamer(prompt),
            media_type="text/event-stream"
        )
    except ValidationError as e:
        error_details = "; ".join([f"{err['loc'][0]}: {err['msg']}" for err in e.errors()])
        raise HTTPException(
            status_code=422,
            detail=f"Validation error: {error_details}"
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Internal error: {str(e)}"
        )


@router.post("/draft/export-pdf")
async def export_pdf(req: ExportPDFRequest):
    """
    Generate a court-formatted PDF from draft text.
    Returns PDF file if WeasyPrint is available, else returns fallback hint.
    """
    if not req.draft:
        raise HTTPException(status_code=400, detail="Draft text is required.")

    if not WEASYPRINT_AVAILABLE:
        return {
            "status": "fallback",
            "message": "WeasyPrint not installed on this server. Use frontend PDF export (jsPDF / print-to-PDF).",
            "hint": "pip install weasyprint",
        }

    try:
        html_content = _build_pdf_html(
            title          = req.title,
            draft_text     = req.draft,
            party_name     = req.party_name or "",
            opposite_party = req.opposite_party or "",
            court          = req.court or "",
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        HTML(string=html_content).write_pdf(pdf_path)
        filename = re.sub(r"[^a-zA-Z0-9\s_-]", "", req.title).replace(" ", "_") + ".pdf"
        return FileResponse(
            path=pdf_path,
            filename=filename,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}")


@router.get("/draft/templates")
async def list_templates():
    """Return all available document types with their sections and law hints."""
    return {
        key: {
            "title":    tmpl["title"],
            "act_hint": tmpl["act_hint"],
            "sections": tmpl["sections"],
        }
        for key, tmpl in TEMPLATES.items()
    }


@router.get("/draft/templates/{template_type}")
async def get_template(template_type: str):
    """Return a specific template's details."""
    tmpl = TEMPLATES.get(template_type)
    if not tmpl:
        raise HTTPException(status_code=404,
                            detail=f"Template '{template_type}' not found. "
                                   f"Available: {list(TEMPLATES.keys())}")
    return {"key": template_type, **tmpl}


@router.get("/draft/prefill/{case_id}")
async def get_draft_prefill(case_id: str):
    """
    Fetch case metadata from DB and format for the drafting form.
    Queries: petitioner, respondent, court, acts_referred, outcome_summary.
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="Database module not available in this deployment.")
    try:
        conn   = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT case_name, petitioner, respondent, court,
                   acts_referred, outcome_summary, judgment
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))
        case = cursor.fetchone()
        cursor.close()
        if not case:
            raise HTTPException(status_code=404, detail=f"Case '{case_id}' not found.")

        court_name = case.get("court") or ""
        jurisdiction = "India"
        if "High Court" in court_name:
            parts = court_name.split()
            idx = next((i for i, p in enumerate(parts) if p == "of"), -1)
            if idx != -1 and idx + 1 < len(parts):
                jurisdiction = parts[idx + 1]

        acts_list = case.get("acts_referred") or []
        acts_str  = ", ".join(acts_list) if isinstance(acts_list, list) else str(acts_list)

        facts_hint = (case.get("outcome_summary") or case.get("case_name") or "")[:600]

        return {
            "party_name":     case.get("petitioner") or "",
            "opposite_party": case.get("respondent") or "",
            "court":          court_name,
            "jurisdiction":   jurisdiction,
            "act_sections":   acts_str,
            "case_citations": case_id,
            "facts_hint":     facts_hint,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@router.get("/draft/health")
async def health_check():
    """Health check — PHASE 3: Multi-backend status with Groq fallback."""
    backend_status = await strategy_selector.health_status()
    return {
        "status":           "ok",
        "version":          "Phase 3 (Multi-backend with auto-failover)",
        "templates":        len(TEMPLATES),
        "templates_list":   list(TEMPLATES.keys()),
        "fuzzy_matching":   True,
        "fuzzy_info":       "Use /draft/templates/[partial_name] for fuzzy search",
        "pdf_engine":       "weasyprint" if WEASYPRINT_AVAILABLE else "frontend-fallback",
        "db":               "connected" if DB_AVAILABLE else "unavailable",
        "backends":         backend_status,
        "primary":          "ollama (self-hosted)",
        "fallback":         "groq" if GROQ_API_KEY else "disabled",
        "auto_failover":    "enabled" if groq_strategy else "disabled",
        "note":             "On Ollama timeout/error → auto-switches to Groq free tier ($0/month)",
    }


@router.post("/draft/test-fuzzy")
async def test_fuzzy_matching(req: FuzzyMatchRequest):
    """Test fuzzy template matching (returns closest match and score)."""
    try:
        # Initialize matcher on first use
        if not hasattr(test_fuzzy_matching, '_initialized'):
            init_matcher(TEMPLATES)
            test_fuzzy_matching._initialized = True
        
        template_input = req.query
        matched_key, score, method = resolve_template(template_input)
        if not matched_key:
            suggestions = get_suggestions(template_input)
            if suggestions:
                return {
                    "input": template_input,
                    "matched": False,
                    "suggestions": suggestions,
                    "hint": "No match above 60% threshold. Try one of the suggestions.",
                }
            raise HTTPException(status_code=404, detail=f"No template match for '{template_input}'")
        
        return {
            "input": template_input,
            "matched": True,
            "matched_template": matched_key,
            "resolution_method": method,
            "score": f"{score:.1%}",
            "title": TEMPLATES[matched_key]["title"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fuzzy matching error: {str(e)}")