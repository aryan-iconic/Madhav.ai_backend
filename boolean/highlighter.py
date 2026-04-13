"""
boolean/highlighter.py
======================
Post-processing module: takes raw paragraph rows from the DB and produces
clean, highlighted KWIC (KeyWord In Context) snippets for result cards.

Responsibilities:
  1. Parse ts_headline output (<<<term>>> markers) into structured spans
  2. Fallback: plain-text windowed extraction when ts_headline is unavailable
  3. Deduplicate and rank snippet fragments
  4. Produce a clean snippet dict ready for JSON serialisation
  5. Extract all highlighted terms for frontend rendering

No DB calls here — pure text processing.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# ts_headline delimiters we set in executor.py
_HL_START = "<<<"
_HL_STOP  = ">>>"

# Max characters per snippet fragment shown in result card
SNIPPET_MAX_CHARS   = 300
SNIPPET_CONTEXT_WIN = 80    # chars of context around a match in fallback mode
MAX_FRAGMENTS       = 3     # max highlight fragments per case


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HighlightSpan:
    text:        str
    highlighted: bool


@dataclass
class SnippetFragment:
    para_no:   int
    para_type: str | None
    spans:     list[HighlightSpan]
    raw_text:  str            # clean text without markers, for display fallback

    @property
    def plain_text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class CaseSnippet:
    case_id:         str
    fragments:       list[SnippetFragment]
    matched_terms:   list[str]            # for frontend highlighting
    total_matches:   int                  # total paragraphs matched


# ─────────────────────────────────────────────────────────────────────────────
# Core: parse ts_headline output
# ─────────────────────────────────────────────────────────────────────────────

def parse_ts_headline(raw_headline: str) -> list[HighlightSpan]:
    """
    Parse PostgreSQL ts_headline output with <<<...>>> delimiters.

    Input:  "...principles of <<<natural justice>>> require that..."
    Output: [
        HighlightSpan("...principles of ", False),
        HighlightSpan("natural justice",   True),
        HighlightSpan(" require that...",  False),
    ]
    """
    spans: list[HighlightSpan] = []
    pattern = re.compile(
        re.escape(_HL_START) + r"(.*?)" + re.escape(_HL_STOP),
        re.DOTALL
    )
    last_end = 0

    for m in pattern.finditer(raw_headline):
        # Text before the highlight
        before = raw_headline[last_end : m.start()]
        if before:
            spans.append(HighlightSpan(text=before, highlighted=False))

        # Highlighted term
        spans.append(HighlightSpan(text=m.group(1), highlighted=True))
        last_end = m.end()

    # Remaining text after last highlight
    tail = raw_headline[last_end:]
    if tail:
        spans.append(HighlightSpan(text=tail, highlighted=False))

    return spans


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: extract snippet from raw paragraph text
# ─────────────────────────────────────────────────────────────────────────────

def extract_fallback_snippet(text: str, terms: list[str]) -> list[HighlightSpan]:
    """
    When ts_headline is not available (paragraph had no DB snippet),
    find the first occurrence of any search term in the raw text
    and return a windowed context around it with manual highlighting.

    Falls back to the first SNIPPET_MAX_CHARS characters if no term found.
    """
    if not terms or not text:
        truncated = text[:SNIPPET_MAX_CHARS] + ("..." if len(text) > SNIPPET_MAX_CHARS else "")
        return [HighlightSpan(text=truncated, highlighted=False)]

    # Build a combined regex for all terms
    patterns = []
    for t in terms:
        escaped = re.escape(t)
        patterns.append(escaped)
    combined = re.compile(
        r"(" + "|".join(patterns) + r")",
        re.IGNORECASE
    )

    match = combined.search(text)
    if not match:
        truncated = text[:SNIPPET_MAX_CHARS] + ("..." if len(text) > SNIPPET_MAX_CHARS else "")
        return [HighlightSpan(text=truncated, highlighted=False)]

    # Window around match
    start = max(0, match.start() - SNIPPET_CONTEXT_WIN)
    end   = min(len(text), match.end() + SNIPPET_CONTEXT_WIN)

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    window = text[start:end]

    # Now highlight all terms within the window
    return _highlight_terms_in_text(prefix + window + suffix, terms)


def _highlight_terms_in_text(text: str, terms: list[str]) -> list[HighlightSpan]:
    """
    Split text into HighlightSpan list, marking all term occurrences.
    Case-insensitive. Handles overlapping terms by processing left-to-right.
    """
    if not terms:
        return [HighlightSpan(text=text, highlighted=False)]

    patterns = [re.escape(t) for t in terms if t]
    if not patterns:
        return [HighlightSpan(text=text, highlighted=False)]

    combined = re.compile(r"(" + "|".join(patterns) + r")", re.IGNORECASE)

    spans: list[HighlightSpan] = []
    last_end = 0

    for m in combined.finditer(text):
        before = text[last_end : m.start()]
        if before:
            spans.append(HighlightSpan(text=before, highlighted=False))
        spans.append(HighlightSpan(text=m.group(0), highlighted=True))
        last_end = m.end()

    tail = text[last_end:]
    if tail:
        spans.append(HighlightSpan(text=tail, highlighted=False))

    return spans


# ─────────────────────────────────────────────────────────────────────────────
# Main: process DB paragraph rows into CaseSnippet
# ─────────────────────────────────────────────────────────────────────────────

def build_case_snippet(
    case_id:       str,
    para_rows:     list[dict],     # rows from build_snippet_query
    search_terms:  list[str],
) -> CaseSnippet:
    """
    Convert raw paragraph DB rows into a CaseSnippet.

    para_rows keys expected:
      paragraph_id, para_no, text, para_type, snippet (ts_headline output or None)

    Returns a CaseSnippet with up to MAX_FRAGMENTS SnippetFragments.
    """
    fragments: list[SnippetFragment] = []
    seen_texts: set[str] = set()

    for row in para_rows[:MAX_FRAGMENTS]:
        raw_snippet  = row.get("snippet")       # ts_headline output
        raw_text     = row.get("text", "")
        para_no      = row.get("para_no") or 0
        para_type    = row.get("para_type")

        # Prefer ts_headline if available and non-empty
        if raw_snippet and _HL_START in raw_snippet:
            spans    = parse_ts_headline(raw_snippet)
            clean    = re.sub(
                re.escape(_HL_START) + r".*?" + re.escape(_HL_STOP),
                lambda m: m.group(0)[len(_HL_START):-len(_HL_STOP)],
                raw_snippet,
                flags=re.DOTALL
            )
        else:
            spans = extract_fallback_snippet(raw_text, search_terms)
            clean = raw_text[:SNIPPET_MAX_CHARS]

        # Deduplicate fragments by their cleaned text (first 60 chars)
        dedup_key = clean[:60].strip()
        if dedup_key in seen_texts:
            continue
        seen_texts.add(dedup_key)

        fragments.append(SnippetFragment(
            para_no   = para_no,
            para_type = para_type,
            spans     = spans,
            raw_text  = clean,
        ))

    return CaseSnippet(
        case_id       = case_id,
        fragments     = fragments,
        matched_terms = search_terms,
        total_matches = len(para_rows),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────────────

def snippet_to_dict(snippet: CaseSnippet) -> dict:
    """
    Convert a CaseSnippet to a JSON-serialisable dict.
    The frontend receives this structure per result card.

    Output shape:
    {
        "case_id": "...",
        "matched_terms": ["natural justice", "audi alteram partem"],
        "total_paragraph_matches": 4,
        "fragments": [
            {
                "para_no": 12,
                "para_type": "reasoning",
                "spans": [
                    {"text": "The principle of ", "highlighted": false},
                    {"text": "natural justice",   "highlighted": true},
                    {"text": " demands that...",  "highlighted": false}
                ],
                "plain_text": "The principle of natural justice demands that..."
            }
        ]
    }
    """
    return {
        "case_id":                  snippet.case_id,
        "matched_terms":            snippet.matched_terms,
        "total_paragraph_matches":  snippet.total_matches,
        "fragments": [
            {
                "para_no":   f.para_no,
                "para_type": f.para_type,
                "spans": [
                    {"text": s.text, "highlighted": s.highlighted}
                    for s in f.spans
                ],
                "plain_text": f.plain_text,
            }
            for f in snippet.fragments
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Utility: extract all highlighted terms from a list of snippets
# ─────────────────────────────────────────────────────────────────────────────

def collect_all_highlighted_terms(snippets: list[CaseSnippet]) -> list[str]:
    """
    Aggregate all unique highlighted terms across all case snippets.
    Useful for the frontend to know which terms to colour-code globally.
    """
    seen: set[str] = set()
    result: list[str] = []
    for s in snippets:
        for t in s.matched_terms:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl)
                result.append(t)
    return result


def clean_snippet_text(text: str) -> str:
    """
    Strip ts_headline markers from text, leaving clean readable text.
    Useful when storing snippet text or logging.
    """
    return re.sub(
        re.escape(_HL_START) + r"(.*?)" + re.escape(_HL_STOP),
        r"\1",
        text,
        flags=re.DOTALL
    )
