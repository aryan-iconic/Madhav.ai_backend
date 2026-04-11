-- madhav.ai — Day 2 Migration
-- Adds precedent_status cache table
-- Run once: psql -d your_db -f migration_precedent_status.sql

-- ─────────────────────────────────────────────
-- Table: precedent_status
-- Caches computed precedent status for every case.
-- Populated by precedent_processor.py
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS precedent_status (
    case_id          TEXT        PRIMARY KEY,
    status           TEXT        NOT NULL DEFAULT 'unknown',
                                 -- good_law | overruled | distinguished | doubted | unknown
    strength         INTEGER     NOT NULL DEFAULT 50,
                                 -- 0-100 score
    label            TEXT        NOT NULL DEFAULT 'Status not yet computed',
    treatment_counts JSONB       NOT NULL DEFAULT '{}',
                                 -- { "followed": 5, "distinguished": 2, ... }
    citing_count     INTEGER     NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for bulk lookups (used in /bulk-precedent-status)
CREATE INDEX IF NOT EXISTS idx_precedent_status_case_id ON precedent_status(case_id);
-- Index for filtering by status (e.g. "show only good law cases")
CREATE INDEX IF NOT EXISTS idx_precedent_status_status  ON precedent_status(status);
-- Index for strength-based sorting
CREATE INDEX IF NOT EXISTS idx_precedent_status_strength ON precedent_status(strength DESC);

-- ─────────────────────────────────────────────
-- Optional: Add status + strength columns to cases table
-- for faster joins without a separate lookup
-- ─────────────────────────────────────────────

ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS precedent_status   TEXT    DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS precedent_strength INTEGER DEFAULT 50;

-- Keeps cases table in sync when precedent_status is updated.
-- The processor upserts into precedent_status — add a trigger or
-- just do a JOIN in your case queries.

-- ─────────────────────────────────────────────
-- View: cases with precedent status (convenience)
-- Use this in your case queries instead of raw JOIN
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW cases_with_status AS
    SELECT
        c.*,
        COALESCE(ps.status,   'unknown') AS prec_status,
        COALESCE(ps.strength, 50)        AS prec_strength,
        COALESCE(ps.label,    'Status not yet computed') AS prec_label,
        COALESCE(ps.treatment_counts, '{}') AS prec_treatment_counts
    FROM cases c
    LEFT JOIN precedent_status ps ON ps.case_id = c.id;

-- ─────────────────────────────────────────────
-- Verification
-- ─────────────────────────────────────────────

-- After running precedent_processor.py --all, verify with:
-- SELECT status, COUNT(*) FROM precedent_status GROUP BY status ORDER BY count DESC;
