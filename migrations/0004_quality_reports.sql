-- 0004_quality_reports.sql
--
-- Forward-only, additive. Stores the weekly QualityReport produced by the
-- director quality flywheel so we can trend confidence_score and the
-- recurrence of top problems across runs.
--
-- Safe to run repeatedly (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
-- EXISTS). Does NOT touch any existing director tables.

CREATE TABLE IF NOT EXISTS quality_reports (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signals           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    top_problems      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    suggested_fixes   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    confidence_score  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    pr_urls           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    issue_urls        JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS quality_reports_created_at_idx
    ON quality_reports (created_at DESC);

CREATE INDEX IF NOT EXISTS quality_reports_confidence_idx
    ON quality_reports (confidence_score DESC);
