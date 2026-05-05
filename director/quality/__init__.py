"""Quality flywheel: meta-director's signal -> synthesis -> PR pipeline.

Three signal collectors (langfuse_signals, postgres_signals, github_signals)
feed into synthesizer.py to produce a QualityReport. pr_generator.py turns
high-confidence suggested fixes into PRs, distinguishing cheap (config /
prompt) edits from expensive (logic) edits and gating protected files.

Each module degrades gracefully when its data source is unavailable, so the
weekly cron always produces *some* output even if e.g. Langfuse credentials
aren't set.
"""

from director.quality.synthesizer import QualityReport, SuggestedFix, synthesize

__all__ = ["QualityReport", "SuggestedFix", "synthesize"]
