"""LongMemEval adapter for Palinode.

Runs the LongMemEval benchmark (Wu et al., ICLR 2025) against a real Palinode
store: each question's haystack sessions are written as dated daily notes into
a scratch ``PALINODE_DIR``, indexed through the canonical ``index_file``
pipeline, then answered by hybrid recall feeding an external answering model.
Judging reuses the upstream per-type prompts verbatim.
"""
