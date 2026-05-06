-- 0006_epics.sql
--
-- Forward-only, additive. Adds an `epics` table for multi-cycle KPI-driven
-- projects.
--
-- An epic is a 3-8 step plan tied to a KPI gap (motto-kpis.md). The planner
-- lens (orchestrator.py) emits epics; epic_executor picks the next step from
-- each active epic on every cycle and queues it as a normal pending_move
-- with `epic_id` + `step_order` set in move_payload.
--
-- Status lifecycle:
--   proposed  -> awaiting human approval in cockpit /epics
--   active    -> executor pulls one move per cycle from `plan` until done
--   paused    -> human paused; executor skips it
--   closed    -> all moves applied OR human marked done (terminal)
--   abandoned -> human killed it (KPI changed, approach wrong, etc.) (terminal)
--
-- Safe to run repeatedly (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
-- EXISTS). Does NOT touch any existing director tables.

CREATE TABLE IF NOT EXISTS epics (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             TEXT,                            -- run that proposed it
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    title              TEXT        NOT NULL,
    kpi_ref            TEXT        NOT NULL,            -- e.g. "AMC panel registrations"
    rationale          TEXT        NOT NULL DEFAULT '',
    estimated_cycles   INTEGER     NOT NULL DEFAULT 0,
    success_criteria   TEXT        NOT NULL DEFAULT '',

    -- Full plan as the planner emitted it. Preserved as audit log; never
    -- mutated. Step status is derived by joining to pending_moves on
    -- (epic_id, step_order) inside move_payload.
    plan               JSONB       NOT NULL,

    status             TEXT        NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed','active','paused','closed','abandoned')),

    approved_by        TEXT,
    approved_at        TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,
    closed_reason      TEXT
);

CREATE INDEX IF NOT EXISTS epics_status_idx
    ON epics (status, created_at DESC);

CREATE INDEX IF NOT EXISTS epics_kpi_ref_idx
    ON epics (kpi_ref);

-- Avoid two open epics for the same KPI.
CREATE UNIQUE INDEX IF NOT EXISTS epics_open_per_kpi_idx
    ON epics (kpi_ref)
    WHERE status IN ('proposed','active','paused');
