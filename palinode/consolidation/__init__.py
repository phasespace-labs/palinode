"""
Palinode Consolidation — Weekly memory distillation.

Reads raw daily notes from the past week, uses an LLM to distill them
into curated project summaries, detect superseded decisions, extract
cross-project insights, and archive processed daily notes.

Runs as a cron job (default: Sunday 3am UTC).
"""
