-- boolean/index_setup.sql
-- ======================
-- Verify and create all indexes required for Boolean search performance.
--
-- Run this once after deploying the boolean module.
-- It is fully idempotent — IF NOT EXISTS on every statement.
--
-- Execution time on a populated DB:
--   GIN tsvector indexes  → ~30s per million paragraphs (runs concurrently)
--   BTREE indexes         → < 1s
--   GIN array indexes     → ~5s
--
-- Run with:
--   psql -U postgres -d legal_knowledge_graph -f boolean/index_setup.sql
-- Or from Python:
--   with open("boolean/index_setup.sql") as f: cursor.execute(f.read())
-- ─────────────────────────────────────────────────────────────────────────────


-- ═══════════════════════════════════════════════════════════════════════
-- 1. legal_paragraphs — PRIMARY BOOLEAN SEARCH TABLE
-- ═══════════════════════════════════════════════════════════════════════

-- Full-text search on paragraph text (ALREADY EXISTS — verified from schema)
-- idx_legal_paragraphs_text   gin (to_tsvector('english', text))
-- idx_paragraphs_text_search  gin (to_tsvector('english', text))
-- Both exist — no action needed.

-- acts_mentioned array (for field: act searches at paragraph level)
CREATE INDEX IF NOT EXISTS idx_paragraphs_acts_mentioned
    ON legal_paragraphs USING GIN (acts_mentioned);

-- sections_mentioned array
CREATE INDEX IF NOT EXISTS idx_paragraphs_sections_mentioned
    ON legal_paragraphs USING GIN (sections_mentioned);

-- judges_mentioned array (for judge: field searches)
CREATE INDEX IF NOT EXISTS idx_paragraphs_judges_mentioned
    ON legal_paragraphs USING GIN (judges_mentioned);

-- citations array in paragraphs
CREATE INDEX IF NOT EXISTS idx_paragraphs_citations
    ON legal_paragraphs USING GIN (citations);

-- para_type for filtering by paragraph type (reasoning, headnote, etc.)
CREATE INDEX IF NOT EXISTS idx_paragraphs_para_type_quality
    ON legal_paragraphs (para_type, quality_score DESC NULLS LAST);

-- Composite: case_id + para_no for ordered paragraph retrieval
CREATE INDEX IF NOT EXISTS idx_paragraphs_case_para_order
    ON legal_paragraphs (case_id, para_no ASC);


-- ═══════════════════════════════════════════════════════════════════════
-- 2. legal_cases — METADATA & FILTER TABLE
-- ═══════════════════════════════════════════════════════════════════════

-- case_name full-text (ALREADY EXISTS: idx_cases_case_name gin tsvector)

-- constitutional_articles array (for article: field searches)
-- ALREADY EXISTS: idx_cases_acts_referred — but NOT for constitutional_articles
CREATE INDEX IF NOT EXISTS idx_cases_constitutional_articles
    ON legal_cases USING GIN (constitutional_articles);

-- subject_tags array (for keyword: field searches)
-- ALREADY EXISTS: idx_cases_subject_tags
-- No action needed.

-- Composite: court + year (most common filter combination)
CREATE INDEX IF NOT EXISTS idx_cases_court_year
    ON legal_cases (court, year DESC NULLS LAST);

-- authority_score for relevance ordering
CREATE INDEX IF NOT EXISTS idx_cases_authority_score
    ON legal_cases (authority_score DESC NULLS LAST);

-- citation_count for citation-based sorting
CREATE INDEX IF NOT EXISTS idx_cases_citation_count
    ON legal_cases (citation_count DESC NULLS LAST);

-- date_of_order for date range queries and date sorting
CREATE INDEX IF NOT EXISTS idx_cases_date_of_order
    ON legal_cases (date_of_order DESC NULLS LAST);

-- court_type for tribunal/HC/SC filtering
CREATE INDEX IF NOT EXISTS idx_cases_court_type_year
    ON legal_cases (court_type, year DESC NULLS LAST);

-- outcome for doc_type filtering
CREATE INDEX IF NOT EXISTS idx_cases_outcome
    ON legal_cases (outcome);


-- ═══════════════════════════════════════════════════════════════════════
-- 3. case_acts — ACT & SECTION FILTER TABLE
-- ═══════════════════════════════════════════════════════════════════════

-- act_name full-text for flexible act matching
CREATE INDEX IF NOT EXISTS idx_case_acts_act_name_text
    ON case_acts USING GIN (to_tsvector('english', act_name));

-- Composite: case_id + act_name (most common join pattern)
CREATE INDEX IF NOT EXISTS idx_case_acts_case_act
    ON case_acts (case_id, act_name);

-- section for section: field searches
CREATE INDEX IF NOT EXISTS idx_case_acts_section
    ON case_acts (section);

-- Composite: act_name + section
CREATE INDEX IF NOT EXISTS idx_case_acts_act_section
    ON case_acts (act_name, section);


-- ═══════════════════════════════════════════════════════════════════════
-- 4. case_citations — CITATION COUNT & RELATIONSHIP
-- ═══════════════════════════════════════════════════════════════════════

-- ALREADY EXISTS:
--   idx_case_citations_cited    btree (cited_case_id)
--   idx_case_citations_source   btree (source_case_id)
--   idx_citations_relationship  btree (relationship)
--   idx_citations_confidence    btree (confidence DESC)

-- Composite: cited_case_id + relationship (for "cited_by with specific relationship")
CREATE INDEX IF NOT EXISTS idx_citations_cited_relationship
    ON case_citations (cited_case_id, relationship);


-- ═══════════════════════════════════════════════════════════════════════
-- 5. TSVECTOR STORED COLUMNS (Optional — improves GIN index update speed)
-- ═══════════════════════════════════════════════════════════════════════
-- If your PostgreSQL version is 12+ and you have high write volume,
-- consider adding a GENERATED ALWAYS stored tsvector column instead of
-- computing it on every query. Uncomment if needed:

-- ALTER TABLE legal_paragraphs
--     ADD COLUMN IF NOT EXISTS text_tsv tsvector
--     GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
--
-- CREATE INDEX IF NOT EXISTS idx_paragraphs_text_tsv
--     ON legal_paragraphs USING GIN (text_tsv);
--
-- ALTER TABLE legal_cases
--     ADD COLUMN IF NOT EXISTS case_name_tsv tsvector
--     GENERATED ALWAYS AS (to_tsvector('english', COALESCE(case_name, ''))) STORED;
--
-- CREATE INDEX IF NOT EXISTS idx_cases_case_name_tsv
--     ON legal_cases USING GIN (case_name_tsv);


-- ═══════════════════════════════════════════════════════════════════════
-- 6. VERIFICATION QUERY
-- ═══════════════════════════════════════════════════════════════════════
-- Run after setup to verify all critical indexes are present:

SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename IN (
    'legal_paragraphs',
    'legal_cases',
    'case_acts',
    'case_citations'
)
  AND indexname LIKE ANY(ARRAY[
    'idx_paragraphs_%',
    'idx_cases_%',
    'idx_case_acts_%',
    'idx_citations_%',
    'idx_legal_%'
  ])
ORDER BY tablename, indexname;
