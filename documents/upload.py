"""
documents/upload.py
===================
PHASE 2 — Document upload pipeline.
Extracts text from PDF/DOCX/TXT → chunks it → embeds → stores in legal_paragraphs.
Uses the SAME table as case data so Research mode searches both automatically.
"""

import os
import uuid
import logging
from typing import List, Dict, Any

log = logging.getLogger(__name__)


def process_document_upload(
    conn,
    filename: str,
    content: bytes,
    session_id: str = None
) -> Dict[str, Any]:
    """
    Full upload pipeline:
    1. Detect file type and extract text
    2. Chunk text into paragraph-sized pieces
    3. Generate embeddings for each chunk
    4. Store in legal_paragraphs with source_type='document'

    Returns: UploadResponse dict
    """
    document_id = str(uuid.uuid4())
    log.info(f"[UPLOAD] Processing {filename} → doc_id={document_id}")

    # ── Step 1: Extract text ──────────────────────────────────────────────────
    text = extract_text(filename, content)
    if not text or len(text.strip()) < 50:
        raise ValueError(f"Could not extract readable text from {filename}")

    log.info(f"[UPLOAD] Extracted {len(text)} characters from {filename}")

    # ── Step 2: Chunk ─────────────────────────────────────────────────────────
    from documents.chunking import chunk_legal_text
    chunks = chunk_legal_text(text, document_id=document_id)
    log.info(f"[UPLOAD] Created {len(chunks)} chunks")

    # ── Step 3: Generate embeddings ───────────────────────────────────────────
    from retrieval.embedder import embed_texts_batch
    chunk_texts = [c['text'] for c in chunks]
    embeddings = embed_texts_batch(chunk_texts)

    embeddings_generated = 0
    if embeddings:
        for i, chunk in enumerate(chunks):
            if i < len(embeddings):
                chunk['embedding'] = embeddings[i]
                embeddings_generated += 1

    # ── Step 4: Store in DB ───────────────────────────────────────────────────
    _store_chunks(conn, chunks, document_id, filename, session_id)

    return {
        "success": True,
        "document_id": document_id,
        "filename": filename,
        "chunks_created": len(chunks),
        "embeddings_generated": embeddings_generated,
        "message": f"Document uploaded and indexed. Now searchable in Research mode."
    }


def extract_text(filename: str, content: bytes) -> str:
    """Extract plain text from PDF, DOCX, or TXT"""
    ext = filename.lower().split('.')[-1]

    if ext == 'pdf':
        return _extract_pdf(content)
    elif ext == 'docx':
        return _extract_docx(content)
    elif ext == 'txt':
        return content.decode('utf-8', errors='ignore')
    else:
        raise ValueError(f"Unsupported file type: .{ext}")


def _extract_pdf(content: bytes) -> str:
    """Extract text from PDF bytes using PyMuPDF (fitz) or pdfplumber"""
    try:
        import fitz  # PyMuPDF — fastest
        doc = fitz.open(stream=content, filetype="pdf")
        pages = [page.get_text() for page in doc]
        doc.close()
        return '\n\n'.join(pages)
    except ImportError:
        pass

    try:
        import pdfplumber
        import io
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [p.extract_text() or '' for p in pdf.pages]
        return '\n\n'.join(pages)
    except ImportError:
        raise ImportError(
            "PDF extraction requires PyMuPDF or pdfplumber. "
            "Install: pip install pymupdf  OR  pip install pdfplumber"
        )


def _extract_docx(content: bytes) -> str:
    """Extract text from DOCX bytes"""
    try:
        import docx
        import io
        doc = docx.Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return '\n\n'.join(paragraphs)
    except ImportError:
        raise ImportError(
            "DOCX extraction requires python-docx. "
            "Install: pip install python-docx"
        )


def _store_chunks(conn, chunks: List[Dict], document_id: str, filename: str, session_id: str = None):
    """
    Store document chunks in legal_paragraphs table.
    Uses same table as case data — so hybrid search covers docs automatically.
    Requires the Phase 2 schema migration (source_type, document_id columns).
    """
    cursor = conn.cursor()

    for chunk in chunks:
        embedding = chunk.get('embedding')
        embedding_str = None
        if embedding:
            embedding_str = '[' + ','.join(map(str, embedding)) + ']'

        try:
            cursor.execute("""
                INSERT INTO legal_paragraphs (
                    paragraph_id, case_id, text, word_count,
                    quality_score, embedding,
                    source_type, document_id
                ) VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
                ON CONFLICT (paragraph_id) DO NOTHING
            """, (
                chunk['paragraph_id'],
                f"DOC_{document_id}",          # Use doc ID as fake case_id
                chunk['text'],
                len(chunk['text'].split()),
                0.7,                            # Default quality for user docs
                embedding_str,
                'document',
                document_id
            ))
        except Exception as e:
            # source_type column might not exist yet — run Phase 2 migration first
            log.error(f"[UPLOAD] Insert failed: {e}")
            log.error("Run: ALTER TABLE legal_paragraphs ADD COLUMN source_type VARCHAR(20) DEFAULT 'case', ADD COLUMN document_id TEXT;")
            raise

    conn.commit()
    cursor.close()
    log.info(f"[UPLOAD] Stored {len(chunks)} chunks for doc {document_id}")
