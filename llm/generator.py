"""
llm/generator.py
================
Ollama LLM integration for Madhav.AI.
Handles Research mode (legal answer) + Study mode (simplified + notes).

Ollama runs locally — free, no API key needed.
Default model: llama3 (or mistral, phi3 — configurable)

Switch to Claude API / OpenAI in production with one config change.
"""

import os
import json
import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.1:8b")   # Change to "mistral" if preferred
LLM_TIMEOUT     = int(os.getenv("LLM_TIMEOUT", 180))    # Seconds - standard timeout (increased for briefing)
BRIEF_TIMEOUT   = int(os.getenv("BRIEF_TIMEOUT", 240))  # Seconds - extended timeout for comprehensive briefs


# ─────────────────────────────────────────────────────────────────────────────
# CORE OLLAMA CALL
# ─────────────────────────────────────────────────────────────────────────────

def _call_ollama(prompt: str, system_prompt: str = "", max_tokens: int = 1024, timeout: int = None) -> str:
    """
    Call local Ollama LLM with a prompt.
    Returns the generated text, or an error message if Ollama is down.
    
    Args:
        prompt: The prompt text to send to Ollama
        system_prompt: Optional system prompt for context
        max_tokens: Maximum tokens to generate (affects timeout needed)
        timeout: Custom timeout in seconds (defaults to LLM_TIMEOUT)
    """
    if timeout is None:
        timeout = LLM_TIMEOUT
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.2,   # Low temp for factual legal answers
            "top_p": 0.9
        }
    }

    if system_prompt:
        payload["system"] = system_prompt

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()

    except requests.exceptions.ConnectionError:
        log.error("❌ Ollama not running. Start with: ollama serve")
        return (
            "⚠️ AI answer generation temporarily unavailable. "
            "Search results are shown above. "
            "(Start Ollama with: ollama serve)"
        )
    except requests.exceptions.Timeout:
        log.error(f"❌ Ollama timeout after {timeout}s")
        return "⚠️ AI answer timed out. Showing search results only."
    except Exception as e:
        log.error(f"❌ Ollama error: {e}")
        return f"⚠️ AI generation error: {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_RESEARCH = """You are Madhav, an expert Indian legal research assistant.
You answer legal questions based ONLY on the case law excerpts provided.
Always cite the specific case names in your answer.
Be precise, use legal terminology appropriately, and be concise.
If the provided excerpts don't cover the question fully, say so clearly.
Do NOT make up cases or legal principles not present in the context."""

SYSTEM_CASE_ANSWER = """You are Madhav, an expert Indian legal case explainer.
The user is asking a question about a SPECIFIC case.
Your answer must be grounded ONLY in that case's facts, judgment, and order.
Explain what the case says, why the court decided that way, and what it means.
Always cite specific case outcomes and reasoning.
Do NOT generalize to other cases or legal principles beyond what this case establishes.
Be clear and thorough — the user wants to understand THIS case completely."""

SYSTEM_STUDY = """You are Madhav, a friendly Indian legal tutor helping law students understand case law.
You explain legal concepts clearly using the case excerpts provided.
Use simple language where possible, but maintain legal accuracy.
Explain WHY courts decided the way they did, not just WHAT they decided.
Always cite specific case names."""

SYSTEM_SIMPLIFY = """You are Madhav, explaining Indian law to someone with no legal background.
Use very simple language — explain it like you would to a smart friend who is not a lawyer.
Avoid jargon. If you must use a legal term, explain it immediately.
Keep the explanation short and practical."""

SYSTEM_NOTES = """You are Madhav, creating structured study notes for a law student.
Create well-organized notes from the legal excerpts provided.
Format your notes with ## headings for each key concept.
Include: Key principle, Which case established it, Why it matters.
Be concise. Each note should be 2-4 sentences."""

SYSTEM_SUMMARY = """You are Madhav, writing a 2-sentence case summary for a law student.
Write exactly 2 sentences: 
Sentence 1: What was the dispute about?
Sentence 2: What did the court decide and why?
Be factual and concise."""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def generate_research_answer(query: str, context: str, mode: str = "research") -> str:
    """
    Generate a RAG answer for Research, Study, or Case Answer mode.

    Args:
        query:   User's legal question
        context: Formatted paragraph excerpts from DB
        mode:    'research' | 'study' | 'case_answer' | 'simplify'

    Returns:
        LLM-generated answer string
    """

    if mode == "case_answer":
        system = SYSTEM_CASE_ANSWER
        prompt = f"""The user is asking about a specific case. Answer ONLY based on the case excerpts provided:

QUESTION: {query}

CASE DETAILS:
{context}

Explain this case thoroughly. What happened? What was decided? Why did the court decide that way? What does it mean for the law?"""

    elif mode == "study":
        system = SYSTEM_STUDY
        prompt = f"""Based on the following Indian case law excerpts, answer this legal question for a law student:

QUESTION: {query}

CASE LAW EXCERPTS:
{context}

Provide a clear, educational answer. Explain the legal principles and cite specific cases."""

    elif mode == "simplify":
        system = SYSTEM_SIMPLIFY
        prompt = f"""Explain the following legal question in very simple terms for a non-lawyer:

QUESTION: {query}

BASED ON:
{context}

Give a simple, plain-English explanation."""

    else:  # research (default)
        system = SYSTEM_RESEARCH
        prompt = f"""Answer the following legal research question based ONLY on the provided case excerpts:

QUESTION: {query}

RELEVANT CASE LAW:
{context}

Provide a precise legal answer with specific case citations."""


def generate_study_notes(query: str, context: str) -> str:
    """
    Generate structured study notes from the retrieved legal context.
    Output is formatted with ## headings for easy parsing.

    Returns:
        Notes text with ## Heading\\n content structure
    """
    prompt = f"""Create structured study notes about this legal topic:

TOPIC: {query}

CASE LAW EXCERPTS:
{context}

Create 3-5 study notes using this format:
## [Note Title]
[2-4 sentences explaining the key legal principle, which case established it, and why it matters]

## [Next Note Title]
[Content]

Focus on principles a law student must remember."""

    log.info("[LLM] Generating study notes")
    return _call_ollama(prompt, system_prompt=SYSTEM_NOTES, max_tokens=600)


def generate_case_summary(case_name: str, context: str) -> str:
    """
    Generate a 2-sentence case summary.
    Used in Study mode to give quick overviews of top cases.
    """
    if not context or not case_name:
        return f"{case_name} — Summary not available."

    prompt = f"""Write a 2-sentence summary of this case:

CASE: {case_name}

EXCERPT:
{context[:500]}

Write exactly 2 sentences:
1. What was the dispute about?
2. What did the court decide and why?"""

    result = _call_ollama(prompt, system_prompt=SYSTEM_SUMMARY, max_tokens=150)
    return result or f"{case_name} — Summary unavailable."


def check_ollama_status() -> dict:
    """Health check for Ollama — call this from /health endpoint if needed"""
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        models = [m['name'] for m in resp.json().get('models', [])]
        return {
            "status": "running",
            "available_models": models,
            "current_model": OLLAMA_MODEL,
            "model_ready": any(OLLAMA_MODEL in m for m in models)
        }
    except Exception as e:
        return {
            "status": "offline",
            "error": str(e),
            "fix": "Run: ollama serve && ollama pull llama3"
        }


# ─────────────────────────────────────────────────────────────────────────────
# JUDGMENT EXTRACTION & SUMMARIZATION (for Case Viewer)
# ─────────────────────────────────────────────────────────────────────────────

def extract_judgment_paragraph(paragraphs: list) -> dict:
    """
    Use LLM to identify and extract the most important judgment paragraph.
    
    Args:
        paragraphs: List of dicts with 'text', 'para_type', 'para_no'
    
    Returns:
        {
            'judgment_text': selected paragraph text,
            'para_no': paragraph number,
            'para_type': paragraph type,
            'confidence': LLM confidence (0.0-1.0)
        }
    """
    if not paragraphs:
        return {
            'judgment_text': 'No judgment text available.',
            'para_no': None,
            'para_type': None,
            'confidence': 0.0
        }
    
    # First, prefer paragraphs with judgment/order/finding/relief types
    judgment_types = ['judgment', 'order', 'finding', 'relief', 'decision']
    judgment_para = next(
        (p for p in paragraphs if p.get('para_type', '').lower() in judgment_types),
        None
    )
    
    if judgment_para:
        log.info(f"Found judgment paragraph of type: {judgment_para.get('para_type')}")
        return {
            'judgment_text': judgment_para.get('text', ''),
            'para_no': judgment_para.get('para_no'),
            'para_type': judgment_para.get('para_type'),
            'confidence': 0.9  # High confidence for explicitly-typed judgment
        }
    
    # If no explicit judgment type, use LLM to find the most important paragraph
    # Prepare paragraph candidates (longer ones more likely to be judgment)
    candidates = sorted(
        [p for p in paragraphs if p.get('text') and len(str(p.get('text', ''))) > 200],
        key=lambda p: len(str(p.get('text', ''))),
        reverse=True
    )[:5]  # Consider top 5 longest paragraphs
    
    if not candidates:
        # Fallback: just use the longest paragraph
        longest = max(paragraphs, key=lambda p: len(str(p.get('text', ''))))
        return {
            'judgment_text': longest.get('text', ''),
            'para_no': longest.get('para_no'),
            'para_type': longest.get('para_type'),
            'confidence': 0.5
        }
    
    # Format candidates for LLM analysis
    candidate_text = "\n\n".join([
        f"[Para {p.get('para_no')}] ({p.get('para_type')})\n{p.get('text', '')[:400]}"
        for p in candidates
    ])
    
    prompt = f"""You are analyzing court judgment paragraphs.
Which paragraph best represents the COURT'S FINAL JUDGMENT or DECISION?
Focus on paragraphs that contain the ruling, outcome, or final order.

CANDIDATES:
{candidate_text}

Respond with ONLY the paragraph number ([Para X]) that contains the actual judgment."""
    
    try:
        response = _call_ollama(prompt, system_prompt="You are a legal document analyzer.", max_tokens=50)
        # Extract paragraph number from response
        import re
        match = re.search(r'\[Para (\d+)\]', response)
        if match:
            para_no = int(match.group(1))
            selected = next((p for p in candidates if p.get('para_no') == para_no), candidates[0])
            return {
                'judgment_text': selected.get('text', ''),
                'para_no': selected.get('para_no'),
                'para_type': selected.get('para_type'),
                'confidence': 0.7
            }
    except Exception as e:
        log.warning(f"LLM judgment selection failed: {e}, using longest paragraph")
    
    # Fallback: use longest paragraph
    selected = candidates[0]
    return {
        'judgment_text': selected.get('text', ''),
        'para_no': selected.get('para_no'),
        'para_type': selected.get('para_type'),
        'confidence': 0.6
    }


def generate_case_summary(case_name: str, judgment_text: str, acts: list = None, all_paragraphs: list = None) -> str:
    """
    Use LLM to generate a COMPREHENSIVE case overview (not just judgment).
    
    Covers:
    1. Case background/facts
    2. Legal issues at stake
    3. Court's decision
    4. Significance or legal principle established
    
    Args:
        case_name: Name of the case
        judgment_text: The judgment paragraph text
        acts: List of acts/statutes mentioned (optional)
        all_paragraphs: All case paragraphs for context (optional)
    
    Returns:
        LLM-generated comprehensive case overview (3-4 sentences, ~300-400 words)
    """
    if not judgment_text or len(judgment_text) < 50:
        return f"Case: {case_name}. Full case details not available in the system."
    
    # Extract background/facts/issues from paragraphs if available
    facts_context = ""
    if all_paragraphs:
        # Try to find facts and issues sections
        facts_para = next(
            (p for p in all_paragraphs if p.get('para_type', '').lower() in ['fact', 'facts', 'background']),
            None
        )
        issue_para = next(
            (p for p in all_paragraphs if p.get('para_type', '').lower() in ['issue', 'issues']),
            None
        )
        
        facts_text = ""
        if facts_para and facts_para.get('text'):
            facts_text = facts_para.get('text', '')[:500]
        if issue_para and issue_para.get('text'):
            if facts_text:
                facts_text += "\n\n" + issue_para.get('text', '')[:500]
            else:
                facts_text = issue_para.get('text', '')[:500]
        
        if facts_text:
            facts_context = f"\n\nCASE BACKGROUND & ISSUES:\n{facts_text}"
    
    acts_context = ""
    if acts:
        acts_context = f"\n\nGoverning laws/statutes: {', '.join(str(a) for a in acts[:8])}"
    
    prompt = f"""Generate a comprehensive 3-4 sentence overview of this legal case.
Your summary should cover the ENTIRE case, not just the judgment.

Write exactly 3-4 sentences covering:
1. What was the case about? (background/context)
2. What were the key legal issues?
3. What did the court decide?
4. Why is this decision significant or what legal principle does it establish?

Keep it clear enough for a law student or legal professional to understand.
Be concise but comprehensive. Focus on substance over procedure.

CASE NAME: {case_name}
{acts_context}
{facts_context}

COURT'S JUDGMENT/DECISION:
{judgment_text[:1500]}

Now write the comprehensive case overview (3-4 sentences):"""
    
    try:
        summary = _call_ollama(
            prompt, 
            system_prompt="""You are an expert legal case summarizer. 
Write comprehensive case overviews that give readers a complete understanding of the case - 
its background, the issues at stake, the court's ruling, and its significance.
Be accurate, concise, and professional.""",
            max_tokens=350
        )
        return summary or f"Case: {case_name} — Summary generation unavailable."
    except Exception as e:
        log.error(f"Summary generation error: {e}")
        # Fallback: return first part of judgment
        return f"{judgment_text[:300]}..."


def generate_full_case_brief(metadata: dict, para_context: str, citations: list) -> str:
    """
    Generate a STRUCTURED case intelligence brief with sections.
    
    This is the comprehensive version for Research mode full_case output.
    Covers: parties, date, facts, issues, held, sections used, cited cases, significance.
    
    Args:
        metadata: Case metadata dict with case_name, court, year, petitioner, respondent, acts_referred, etc.
        para_context: Grouped paragraph context (facts/issues/judgment/order sections with para refs)
        citations: List of cited case dicts with 'target_citation' or 'citation' field
    
    Returns:
        Structured case brief with clear sections (markdown-formatted)
    """
    case_name   = metadata.get("case_name", "Unknown Case")
    court       = metadata.get("court", "")
    year        = metadata.get("year", "")
    petitioner  = metadata.get("petitioner", "")
    respondent  = metadata.get("respondent", "")
    date_order  = metadata.get("date_of_order", "")
    acts        = metadata.get("acts_referred") or []
    
    # Format acts list for prompt
    acts_str = ""
    if acts:
        if isinstance(acts, list):
            acts_str = ", ".join(str(a) for a in acts[:10])
        else:
            acts_str = str(acts)
    
    # Format cited cases for prompt
    cited_str = ""
    if citations:
        cited_cases = []
        for c in citations[:8]:
            if isinstance(c, dict):
                cit = c.get("target_citation") or c.get("citation") or str(c)
            else:
                cit = str(c)
            if cit:
                cited_cases.append(cit)
        cited_str = "\n".join(f"- {c}" for c in cited_cases)
    
    prompt = f"""You are a senior legal analyst. Write a structured case intelligence brief for:

CASE: {case_name}
COURT: {court}
YEAR / DATE: {year} {date_order}
PETITIONER: {petitioner}
RESPONDENT: {respondent}
ACTS / SECTIONS REFERRED: {acts_str or "See judgment"}
CASES CITED: 
{cited_str or "See judgment"}

CASE PARAGRAPHS (grouped by type, with paragraph numbers):
{para_context}

---
Write a COMPLETE case brief with these EXACT sections. Be specific. Use paragraph references (e.g. "Para 5", "Para 12") where helpful.

**PARTIES**
Who is the petitioner, who is the respondent, and their relationship/dispute.

**BACKGROUND & FACTS** (refer to relevant para numbers)
What happened? Why did this come to court? Key facts chronologically.

**ISSUES / QUESTIONS OF LAW** (refer to relevant para numbers)
What legal questions did the court have to decide?

**SECTIONS & ACTS APPLIED**
List every section, article, or act the court relied upon with brief note on why.

**HELD / RATIO DECIDENDI** (refer to relevant para numbers)
What did the court hold? The core legal principle laid down.

**FINAL ORDER**
What was the actual order — dismissed/allowed/remanded? Any specific directions?

**CASES RELIED UPON**
Key precedents cited by the court.

**SIGNIFICANCE**
Why does this case matter? What principle does it establish?

Keep each section concise but complete. A law student should understand the entire case from this brief alone."""

    try:
        log.info(f"[BRIEF] Generating comprehensive case brief (max_tokens=1000, timeout={BRIEF_TIMEOUT}s)")
        response = _call_ollama(prompt, max_tokens=1000, timeout=BRIEF_TIMEOUT)
        if response and not response.startswith("⚠️"):
            log.info(f"[BRIEF] ✅ Generated brief ({len(response)} chars)")
            return response.strip()
        else:
            log.warning(f"[BRIEF] LLM generation returned error/warning: {response}")
            return None
    except Exception as e:
        log.warning(f"[BRIEF] LLM generation failed: {e}", exc_info=True)
        return None
