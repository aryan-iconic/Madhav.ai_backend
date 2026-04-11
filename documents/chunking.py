"""
documents/chunking.py
=====================
Smart legal text chunker for document uploads.
Splits text into ~300-word chunks with overlap, respecting paragraph boundaries.
Generates paragraph IDs compatible with the existing legal_paragraphs table.
"""

import uuid
import re
import logging
from typing import List, Dict, Any

log = logging.getLogger(__name__)

# Chunk size tuning
CHUNK_SIZE_WORDS = 300       # Target words per chunk
CHUNK_OVERLAP_WORDS = 50    # Words overlapping between adjacent chunks
MIN_CHUNK_WORDS = 30        # Discard chunks smaller than this


def chunk_legal_text(text: str, document_id: str) -> List[Dict[str, Any]]:
    """
    Split a legal document into overlapping chunks.

    Strategy:
    1. Split on double newlines (paragraph boundaries)
    2. Group small paragraphs together until ~300 words
    3. Add 50-word overlap between chunks
    4. Assign unique paragraph IDs

    Returns:
        List of chunk dicts: {paragraph_id, text, word_count, para_no, chunk_start_char}
    """
    # ── Step 1: Split into natural paragraphs ─────────────────────────────────
    raw_paragraphs = _split_into_paragraphs(text)
    log.info(f"[CHUNK] {len(raw_paragraphs)} raw paragraphs from document")

    # ── Step 2: Group into ~300 word chunks ───────────────────────────────────
    chunks = _group_into_chunks(raw_paragraphs, document_id)
    log.info(f"[CHUNK] Created {len(chunks)} chunks (avg {CHUNK_SIZE_WORDS} words)")

    return chunks


def _split_into_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs on double newlines or section headers"""
    # Normalize whitespace
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Split on double newlines
    parts = text.split('\n\n')

    paragraphs = []
    for part in parts:
        part = part.strip()
        if len(part.split()) >= 5:  # Skip tiny fragments
            paragraphs.append(part)

    return paragraphs


def _group_into_chunks(paragraphs: List[str], document_id: str) -> List[Dict[str, Any]]:
    """
    Group paragraphs into target-size chunks with overlap.
    """
    chunks = []
    current_words = []
    current_texts = []
    para_no = 0

    for para in paragraphs:
        para_words = para.split()

        # If adding this paragraph exceeds chunk size, save current chunk
        if len(current_words) + len(para_words) > CHUNK_SIZE_WORDS and current_words:
            chunk_text = ' '.join(current_words)
            if len(current_words) >= MIN_CHUNK_WORDS:
                chunks.append(_make_chunk(chunk_text, document_id, para_no))
                para_no += 1

            # Start next chunk with overlap from end of current chunk
            overlap = current_words[-CHUNK_OVERLAP_WORDS:] if len(current_words) > CHUNK_OVERLAP_WORDS else current_words
            current_words = overlap + para_words
        else:
            current_words.extend(para_words)

    # Don't forget the last chunk
    if len(current_words) >= MIN_CHUNK_WORDS:
        chunks.append(_make_chunk(' '.join(current_words), document_id, para_no))

    return chunks


def _make_chunk(text: str, document_id: str, para_no: int) -> Dict[str, Any]:
    """Create a chunk dict with a unique paragraph_id"""
    return {
        "paragraph_id": f"DOC_{document_id}_P{para_no:04d}",
        "text": text.strip(),
        "word_count": len(text.split()),
        "para_no": para_no,
        "document_id": document_id,
        "embedding": None  # Filled in by upload.py after batch embedding
    }
