"""
madhav.ai — Drafting Engine
FastAPI router: mount this in your main app.py with:
    from drafting_router import router as drafting_router
    app.include_router(drafting_router)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional
import httpx
import json
import tempfile
import os
import re
from psycopg2.extras import RealDictCursor
from Backend.db import get_connection  # Import your DB connection manager

# Optional: WeasyPrint for backend PDF generation (install: pip install weasyprint)
try:
    from weasyprint import HTML, CSS
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["drafting"])

# ── Change this if your Ollama runs on a different port ──
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.1:8b"   # swap to whatever model you have pulled (check: ollama list)


# ─────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────

class DraftRequest(BaseModel):
    template_type: str          # "legal_notice" | "petition" | "bail_application" | "affidavit"
    tone: str = "formal"        # "formal" | "aggressive" | "concise"
    facts: str                  # User-provided facts / situation
    party_name: str             # Sender / petitioner name
    opposite_party: str         # Opposite party name
    relief_sought: str          # What they want
    act_sections: Optional[str] = ""   # Relevant acts/sections
    case_citations: Optional[str] = "" # Case law to insert
    court: Optional[str] = ""          # Court name (for petitions)
    jurisdiction: Optional[str] = ""   # Jurisdiction (state/country)
    language: Optional[str] = "english" # "english" | "hindi" | "marathi" | "tamil"
    state: Optional[str] = ""           # State for jurisdiction-specific formatting


class RefineRequest(BaseModel):
    draft: str           # existing draft text
    instruction: str     # e.g. "make it more aggressive", "add section 420 IPC"


# ─────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────

TONE_INSTRUCTIONS = {
    "formal":     "Use formal legal language. Be precise, measured, and professional.",
    "aggressive": "Use assertive, strong legal language. Emphasize consequences and urgency.",
    "concise":    "Be brief and direct. Use short sentences. Avoid unnecessary elaboration.",
}

TEMPLATES = {
    "legal_notice": {
        "title": "Legal Notice",
        "sections": [
            "Sender & Addressee Details",
            "Subject of Notice",
            "Background & Facts",
            "Legal Violations / Cause of Action",
            "Applicable Laws & Sections",
            "Supporting Case Law",
            "Relief / Demand",
            "Consequence of Non-Compliance",
        ],
    },
    "petition": {
        "title": "Writ Petition",
        "sections": [
            "Court & Case Header",
            "Parties",
            "Jurisdiction",
            "Facts of the Case",
            "Questions of Law",
            "Grounds",
            "Supporting Precedents",
            "Prayer / Relief Sought",
        ],
    },
    "bail_application": {
        "title": "Bail Application",
        "sections": [
            "Court & Case Details",
            "Applicant Details",
            "FIR / Offence Details",
            "Grounds for Bail",
            "Personal Circumstances",
            "Supporting Case Law",
            "Conditions Offered",
            "Prayer",
        ],
    },
    "affidavit": {
        "title": "Affidavit",
        "sections": [
            "Deponent Details",
            "Court / Authority",
            "Statement of Facts",
            "Verification Clause",
        ],
    },
    "written_statement": {
        "title": "Written Statement",
        "sections": [
            "Court & Case Header",
            "Defendant Details",
            "Preliminary Objections",
            "Reply to Facts (Para-wise)",
            "Additional Facts / Defence",
            "Legal Grounds",
            "Supporting Case Law",
            "Prayer",
        ],
    },
    "reply_to_plaint": {
        "title": "Reply to Plaint",
        "sections": [
            "Court & Case Header",
            "Defendant / Respondent Details",
            "Denial of Major Allegations",
            "Para-wise Reply to Plaint",
            "Affirmative Defence",
            "Counter-allegations (if any)",
            "Applicable Law & Sections",
            "Supporting Case Law",
            "Prayer / Relief Sought",
        ],
    },
    "counter_claim": {
        "title": "Counter-claim",
        "sections": [
            "Court & Case Header",
            "Claimant Details (Defendant as Counter-claimant)",
            "Brief History of Original Suit",
            "Grounds for Counter-claim",
            "Counter-claim Allegations (Para-wise)",
            "Relief Sought in Counter-claim",
            "Applicable Laws & Sections",
            "Supporting Case Law",
            "Prayer for Counter-claim",
        ],
    },
    "contracts": {
        "title": "Contracts",
        "sections": [
            "Title & Date",
            "Parties & Addresses",
            "Recitals (WHEREAS clauses)",
            "Definitions",
            "Terms & Conditions",
            "Consideration / Payment Terms",
            "Rights & Obligations of Parties",
            "Confidentiality & IP Rights",
            "Dispute Resolution & Jurisdiction",
            "Termination & Amendment",
            "Signature Block & Execution",
        ],
    },
    "petition_revision": {
        "title": "Petition for Revision / Review",
        "sections": [
            "Court & Case Header",
            "Parties & Representation",
            "Original Judgment Details",
            "Grounds for Revision / Review",
            "Substantial Question of Law / Fact",
            "Arguments & Legal Analysis",
            "Supporting Case Law",
            "Why Judgment is Wrong / Unjust",
            "Alternate Relief Sought",
            "Prayer for Revision / Review",
        ],
    },
    "motion": {
        "title": "Motion / Interlocutory Application",
        "sections": [
            "Court & Case Header",
            "Applicant Details",
            "Case Background",
            "Grounds for the Motion",
            "Factual Basis & Context",
            "Legal Grounds (Acts/Sections)",
            "Prejudice to Applicant (if not granted)",
            "Supporting Case Law & Precedents",
            "Prayer / Relief Sought",
            "Verification & Signature",
        ],
    },
    "injunction": {
        "title": "Application for Injunction",
        "sections": [
            "Court & Case Header",
            "Applicant & Respondent Details",
            "Background & Facts of the Case",
            "Prima Facie Case (probability of success)",
            "Irreparable Injury / Balance of Convenience",
            "Why Interim/Permanent Relief is Needed",
            "Acts / Sections Invoked (CPC, IP Act, etc)",
            "Supporting Case Law",
            "Undertaking to Court",
            "Prayer for Injunction",
        ],
    },
    "appeal": {
        "title": "Appeal / Memorandum of Appeal",
        "sections": [
            "Court & Case Header",
            "Appellant & Respondent Details",
            "Original Judgment / Order Details",
            "Grounds of Appeal",
            "Substantial Question of Law / Fact",
            "Errors in Judgment (factual & legal)",
            "Why Judgment is Unjust / Illegal",
            "Supporting Case Law & Precedents",
            "Alternative Reliefs Sought",
            "Prayer for Appeal",
        ],
    },
}


# ─────────────────────────────────────────────
# Multi-Language & Jurisdiction Support
# ─────────────────────────────────────────────

LANGUAGE_PROMPTS = {
    "english": "",  # Default, no extra instruction
    "hindi": "\nTranslate the entire document into formal Hindi (Devanagari script).",
    "marathi": "\nTranslate the entire document into formal Marathi (Devanagari script).",
    "tamil": "\nTranslate the entire document into formal Tamil (Tamil script).",
}

JURISDICTION_RULES = {
    "maharashtra": {
        "state": "Maharashtra",
        "court_suffix": "High Court of Bombay",
        "rules": "Follow Bombay High Court procedures and Maharashtra state-specific rules.",
    },
    "delhi": {
        "state": "Delhi",
        "court_suffix": "High Court of Delhi",
        "rules": "Follow Delhi High Court procedures and Delhi-specific rules.",
    },
    "karnataka": {
        "state": "Karnataka",
        "court_suffix": "High Court of Karnataka",
        "rules": "Follow Karnataka High Court procedures and Karnataka state-specific rules.",
    },
    "tamil_nadu": {
        "state": "Tamil Nadu",
        "court_suffix": "High Court of Madras",
        "rules": "Follow Madras High Court procedures and Tamil Nadu state-specific rules.",
    },
    "uttar_pradesh": {
        "state": "Uttar Pradesh",
        "court_suffix": "High Court of Allahabad",
        "rules": "Follow Allahabad High Court procedures and Uttar Pradesh state-specific rules.",
    },
    "west_bengal": {
        "state": "West Bengal",
        "court_suffix": "High Court of Calcutta",
        "rules": "Follow Calcutta High Court procedures and West Bengal state-specific rules.",
    },
    "supreme_court": {
        "state": "India",
        "court_suffix": "Supreme Court of India",
        "rules": "Follow Supreme Court rules (Rule of Court, Constitution procedures).",
    },
}

def get_language_instruction(language: Optional[str]) -> str:
    """Get additional prompt instruction for selected language."""
    lang = (language or "english").lower()
    return LANGUAGE_PROMPTS.get(lang, "")

def get_jurisdiction_rules(state: Optional[str]) -> str:
    """Get jurisdiction-specific formatting rules."""
    if not state:
        return ""
    
    state_key = state.lower().replace(" ", "_")
    rules = JURISDICTION_RULES.get(state_key, {})
    return rules.get("rules", "")


def build_draft_prompt(req: DraftRequest) -> str:
    tmpl = TEMPLATES.get(req.template_type)
    if not tmpl:
        raise HTTPException(status_code=400, detail=f"Unknown template: {req.template_type}")

    tone_instr = TONE_INSTRUCTIONS.get(req.tone, TONE_INSTRUCTIONS["formal"])
    sections_list = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(tmpl["sections"]))
    citations_block = f"\nIncorporate these case citations naturally:\n{req.case_citations}" if req.case_citations else ""
    acts_block = f"\nReference these acts/sections: {req.act_sections}" if req.act_sections else ""
    court_block = f"\nCourt: {req.court}" if req.court else ""
    
    # Add language-specific instruction
    language_instr = get_language_instruction(req.language)
    
    # Add jurisdiction-specific rules
    jurisdiction_rules = get_jurisdiction_rules(req.state)
    jurisdiction_block = f"\nJURISDICTION-SPECIFIC RULES:\n{jurisdiction_rules}" if jurisdiction_rules else ""

    return f"""You are an expert Indian legal drafting assistant. Draft a complete, professional {tmpl["title"]} for Indian courts.

TONE: {tone_instr}

PARTIES:
- Party / Petitioner: {req.party_name}
- Opposite Party / Respondent: {req.opposite_party}
- Jurisdiction: {req.jurisdiction or "India"}
{court_block}

FACTS PROVIDED:
{req.facts}

RELIEF SOUGHT:
{req.relief_sought}
{acts_block}
{citations_block}

STRUCTURE — Generate all these sections in order:
{sections_list}

RULES:
- Use proper Indian legal formatting (WHEREAS, NOW THEREFORE, WHEREFORE etc.)
- Every section must have a clear heading in CAPS
- Include [PLACEHOLDER] wherever the user needs to fill in specific dates, amounts, or details
- At the end, add a "CERTIFICATE / VERIFICATION" clause
- Do NOT add any commentary — output only the legal document
{jurisdiction_block}{language_instr}

BEGIN THE DOCUMENT:"""


def build_refine_prompt(req: RefineRequest) -> str:
    return f"""You are an expert Indian legal drafting assistant.

Refine the following legal draft based on this instruction: "{req.instruction}"

RULES:
- Keep the overall structure intact unless told to change it
- Only modify what the instruction asks
- Output the complete revised document, not just the changed parts
- Do NOT add commentary

ORIGINAL DRAFT:
{req.draft}

REFINED DRAFT:"""


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@router.post("/draft")
async def generate_draft(req: DraftRequest):
    """Generate a legal document draft via Ollama (streaming)."""
    prompt = build_draft_prompt(req)

    async def stream_ollama():
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": 0.3,   # low = more consistent legal text
                "num_predict": 2048,
            }
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as response:
                if response.status_code != 200:
                    yield f"data: {json.dumps({'error': 'Ollama error'})}\n\n"
                    return
                async for line in response.aiter_lines():
                    if line:
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("response", "")
                            done = chunk.get("done", False)
                            yield f"data: {json.dumps({'token': token, 'done': done})}\n\n"
                            if done:
                                break
                        except json.JSONDecodeError:
                            continue

    return StreamingResponse(stream_ollama(), media_type="text/event-stream")


@router.post("/draft/refine")
async def refine_draft(req: RefineRequest):
    """Refine an existing draft with a specific instruction (streaming)."""
    prompt = build_refine_prompt(req)

    async def stream_ollama():
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": 0.3, "num_predict": 2048}
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as response:
                async for line in response.aiter_lines():
                    if line:
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("response", "")
                            done = chunk.get("done", False)
                            yield f"data: {json.dumps({'token': token, 'done': done})}\n\n"
                            if done:
                                break
                        except json.JSONDecodeError:
                            continue

    return StreamingResponse(stream_ollama(), media_type="text/event-stream")


@router.post("/draft/export-pdf")
async def export_pdf(req: dict):
    """
    Generate PDF from draft text using WeasyPrint (backend) or fallback to frontend jsPDF.
    Request body: { "draft": "...", "title": "..." }
    """
    draft_text = req.get("draft", "")
    title = req.get("title", "Legal Draft")
    
    if not draft_text:
        raise HTTPException(status_code=400, detail="Draft text required")
    
    # If WeasyPrint not available, return error (client will use jsPDF)
    if not WEASYPRINT_AVAILABLE:
        return {
            "status": "fallback",
            "message": "WeasyPrint not available. Use frontend jsPDF export.",
            "hint": "Install: pip install weasyprint"
        }
    
    try:
        # Create professional HTML for PDF
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        @page {{ 
            size: A4; 
            margin: 2.5cm;
            @bottom-center {{ content: "Page " counter(page); font-size: 10pt; }}
        }}
        body {{ 
            font-family: 'Cambria', 'Times New Roman', serif; 
            font-size: 12pt; 
            line-height: 1.8; 
            color: #222;
            text-align: justify;
        }}
        h1 {{ 
            font-size: 16pt; 
            text-align: center; 
            margin-bottom: 2rem;
            text-transform: uppercase;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        .section-heading {{ 
            font-weight: bold; 
            text-transform: uppercase; 
            margin-top: 1.5rem;
            margin-bottom: 0.5rem;
            font-size: 11pt;
        }}
        p {{ margin: 0.5rem 0; }}
        pre {{ 
            white-space: pre-wrap; 
            font-family: 'Cambria', 'Times New Roman', serif; 
            font-size: 12pt;
            overflow-wrap: break-word;
        }}
        .footer {{ 
            margin-top: 2rem; 
            border-top: 1px solid #ccc; 
            padding-top: 1rem;
            font-size: 10pt;
            color: #666;
        }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <pre>{draft_text}</pre>
    <div class="footer">
        <p>Generated by Madhav.ai — Legal Intelligence Platform</p>
        <p>This document is for reference purposes. Consult a lawyer before filing.</p>
    </div>
</body>
</html>"""
        
        # Generate PDF to temporary file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            pdf_path = tmp_file.name
        
        # Render HTML to PDF
        HTML(string=html_content).write_pdf(pdf_path)
        
        # Return file as download
        filename = re.sub(r'[^a-zA-Z0-9\s]', '', title).replace(' ', '_') + ".pdf"
        return FileResponse(
            path=pdf_path,
            filename=filename,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"PDF generation failed: {str(e)}",
            "fallback": "Use frontend jsPDF export"
        }


@router.get("/draft/prefill/{case_id}")
async def get_draft_prefill(case_id: str):
    """
    Fetch case metadata from database and format it for the drafting form.
    Queries: petitioner, respondent, court, acts_referred, outcome_summary
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Query the legal_cases table for relevant fields
        cursor.execute("""
            SELECT 
                case_name,
                petitioner,
                respondent,
                court,
                acts_referred,
                outcome_summary,
                judgment
            FROM legal_cases
            WHERE case_id = %s
        """, (case_id,))
        
        case = cursor.fetchone()
        cursor.close()
        
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found in database")
        
        # Extract jurisdiction from court name (e.g. "Supreme Court of India" → "India")
        court_name = case.get("court") or ""
        jurisdiction = "India"
        if "High Court" in court_name:
            # Extract state from High Court name
            parts = court_name.split()
            if len(parts) > 2:
                jurisdiction = parts[2]  # e.g. "High Court of Maharashtra" → "Maharashtra"
        
        # Format acts as comma-separated string
        acts_list = case.get("acts_referred") or []
        acts_str = ", ".join(acts_list) if acts_list else ""
        
        # Use outcome_summary as facts hint, fallback to case_name
        facts_hint = case.get("outcome_summary") or case.get("case_name") or ""
        
        return {
            "party_name":     case.get("petitioner") or "Unknown Petitioner",
            "opposite_party": case.get("respondent") or "Unknown Respondent",
            "court":          court_name,
            "jurisdiction":   jurisdiction,
            "act_sections":   acts_str,
            "case_citations": case_id,  # Use the case_id itself as the citation
            "facts_hint":     facts_hint[:500],  # Limit to 500 chars for form readability
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/draft/templates")
async def get_templates():
    """Return available templates and their sections."""
    return {
        name: {
            "title": tmpl["title"],
            "sections": tmpl["sections"],
        }
        for name, tmpl in TEMPLATES.items()
    }
