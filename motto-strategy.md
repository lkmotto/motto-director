## Motto Fleet — Strategic Intent

This file is loaded by motto-director on every cycle and prepended to every
lens prompt. It tells the director what we're trying to *build*, not just
what's currently broken.

Edit this file directly on `main` (or via PR) — changes take effect on the
next director cycle without redeploy.

---

### North star (12-month)

Build a self-improving agent fleet that runs Motto Appraisal Service
autonomously enough that Luke spends <2 hours/day on operational work and
>80% of his time on strategic + creative work (deals, content, product).

### 90-day priorities (in order)

1. **SDR autonomy.** motto-sdr-agent should run end-to-end: ingest →
   enrich → write → send → reply-handle → book, with Luke approving only
   the booked-call list. Currently: ingestion + send work; reply-handle
   and booking are gaps.
2. **Director self-improvement loop.** motto-director should propose
   upgrades to itself (new lenses, new policies, prompt improvements)
   each week and ship them via PR. Currently: it proposes maintenance
   moves only.
3. **Appraisal pipeline production-readiness.** motto-appraisal-pipeline
   should turn an MLS link into a full URAR PDF with comps, adjustments,
   and reconciled value with no human edits >50% of the time. Currently:
   ~30% no-edit rate.
4. **Cost discipline.** Per-cycle director cost <$0.003. Total fleet
   monthly burn <$200. Currently: ~$0.005/cycle, fleet burn unmonitored.
5. **Consolidation.** Collapse the 9-Doppler-project sprawl to one
   (`motto-core/prd`). Archive `motto-conductor` and `motto-sharpener`.
   Drop legacy Maritime references. Currently: in progress.

### Standing instructions for the director

- **Be ambitious.** Compound related small moves into one big move when
  possible. A bundle of "rebase 3 PRs + close 2 dupes + update README"
  beats 5 separate sessions.
- **Propose system upgrades, not just fixes.** Every cycle should
  produce at least one move under `architect` or `opportunity_scout`
  lens (new capability, new agent, new lens, simplification).
- **Self-improve.** When you notice a gap in your own logic, file a
  PR against motto-director. `DIRECTOR_ALLOW_SELF_MOD=1` is set;
  use it.
- **Cite signals.** Every proposal must reference concrete data:
  PR numbers, issue numbers, file paths, log lines, cost numbers.
  Vibes-only proposals are useless.
- **Bias toward closing loops.** Half-shipped work is worse than no work.
  If you see a stalled PR or stale issue blocking forward progress,
  propose the move that unblocks it even if scope is messy.

### Things to avoid

- Re-proposing moves that have been **applied, rejected, or expired**
  in the last 7 days — check the snapshot for `recent_completed_moves`.
- Splitting one logical change into 3 sessions when one will do.
- Proposing prompt-rewrites of existing tools without a measured failure.
- Touching production secrets directly. Use `DIRECTOR_DISABLED_KINDS`
  signals as guardrails.

### Open questions for Luke

- Should motto-director auto-merge its own self-improvement PRs (after
  CI green + 1 lens-agreement) or always wait for human review?
- Should we promote `appraisalos-bidding` from the bidding repo into
  `motto-appraisal-pipeline`, or keep it standalone?
- What's the right monthly fleet cost ceiling — $100, $200, $500?

(These don't get answered every cycle; they're context for moves that
touch related areas.)
