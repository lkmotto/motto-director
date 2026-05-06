-- 0005_pending_moves.sql
--
-- Forward-only, additive. Adds a pending_moves table for human-in-the-loop
-- approval of director moves. When DIRECTOR_APPROVAL_MODE=manual, act.py
-- writes rows here instead of executing. The cockpit /director UI and the
-- Telegram /director command read from this table and approve/reject rows.
-- A separate apply_approved_moves job pulls approved rows and executes them
-- through the normal act() helpers.
--
-- Safe to run repeatedly (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
-- EXISTS). Does NOT touch any existing director tables.

CREATE TABLE IF NOT EXISTS pending_moves (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The serialized NextMove (asdict)
    repo            TEXT        NOT NULL,
    kind            TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    rationale       TEXT        NOT NULL DEFAULT '',
    intent          TEXT        NOT NULL DEFAULT '',
    priority        INTEGER     NOT NULL DEFAULT 0,
    move_payload    JSONB       NOT NULL,

    -- Approval workflow
    -- pending  -> awaiting human review
    -- approved -> human approved, awaiting execution
    -- rejected -> human rejected, will not execute
    -- applied  -> approved + executed (terminal)
    -- failed   -> approved but execution failed (terminal; details in apply_detail)
    -- expired  -> sat in pending too long, garbage-collected
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','applied','failed','expired')),
    approved_by     TEXT,        -- 'cockpit:<token>' | 'telegram:<chat_id>' | 'auto:<reason>'
    approved_at     TIMESTAMPTZ,
    applied_at      TIMESTAMPTZ,
    apply_detail    TEXT
);

CREATE INDEX IF NOT EXISTS pending_moves_status_idx
    ON pending_moves (status, created_at DESC);

CREATE INDEX IF NOT EXISTS pending_moves_run_id_idx
    ON pending_moves (run_id);

-- Dedup index: if the same (repo, kind, title) is already pending, we don't
-- want to re-queue it on the next tick. Partial index on pending only.
CREATE UNIQUE INDEX IF NOT EXISTS pending_moves_dedup_pending_idx
    ON pending_moves (repo, kind, lower(title))
    WHERE status = 'pending';
