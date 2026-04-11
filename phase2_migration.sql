-- ════════════════════════════════════════════════════════
-- PHASE 2 MIGRATION — Run ONCE before using document upload
-- ════════════════════════════════════════════════════════
-- This adds source_type and document_id columns to legal_paragraphs
-- so documents and cases live in the same table and search together.

ALTER TABLE legal_paragraphs
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) DEFAULT 'case',
    ADD COLUMN IF NOT EXISTS document_id TEXT;

-- Index for filtering by source type
CREATE INDEX IF NOT EXISTS idx_paragraphs_source_type
    ON legal_paragraphs(source_type);

-- Index for fetching all chunks of a document
CREATE INDEX IF NOT EXISTS idx_paragraphs_document_id
    ON legal_paragraphs(document_id);

-- Confirm
SELECT source_type, COUNT(*) FROM legal_paragraphs GROUP BY source_type;
