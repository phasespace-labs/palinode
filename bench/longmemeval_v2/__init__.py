"""LongMemEval-V2 adapter for Palinode.

Plugs a real Palinode store into the LongMemEval-V2 evaluation harness
(Wu et al., 2026 — web-agent trajectory memory, not V1's chat sessions) as a
``memory_modules`` backend: ``insert(trajectory)`` writes the trajectory as a
markdown file into a scratch ``PALINODE_DIR`` and indexes it through the
canonical ``index_file`` pipeline; ``query(question)`` is hybrid recall over
that store, returned as the harness's text context items. The reader, judge,
haystacks and scoring are the upstream harness's own — nothing here answers or
judges.
"""
