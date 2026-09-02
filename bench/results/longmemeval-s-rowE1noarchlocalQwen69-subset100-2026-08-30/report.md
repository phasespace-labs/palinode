# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| pipeline | `session-end+consolidate` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| extract_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=300s {'chat_template_kwargs': {'enable_thinking': False}}` |
| extract_prompt | `v1` |
| extract_workers | `8` |
| consolidate_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=600s {'chat_template_kwargs': {'enable_thinking': False}}` |
| consolidate_allowed_ops | `KEEP,UPDATE,MERGE,SUPERSEDE` |
| answer_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=300s {'chat_template_kwargs': {'enable_thinking': False}}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.750**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.714 | 1.000 |
| multi-session | 0.680 | 1.000 |
| single-session-assistant | 0.545 | 1.000 |
| single-session-preference | 0.667 | 1.000 |
| single-session-user | 1.000 | 1.000 |
| temporal-reasoning | 0.800 | 0.960 |

mean prompt tokens: 2694.3 · mean context chars: 8286.6 · ingest 160.88s · retrieval 0.528s (per question)

answer string in context (non-abstention): 0.543

extraction: 4748 calls over 100 questions (47.5/question), 12177274 prompt + 1393286 completion tokens (121772.7 prompt/question), 281.4 facts/question, 1 parse failures, 0 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.55

| op | count |
|---|---|
| kept | 25367 |
| updated | 0 |
| merged | 24 |
| superseded | 5 |
| archived | 0 |
| retracted | 0 |
| unmatched | 1 |
| protected_rejected | 0 |

consolidation: 91/100 profiles compacted, 1166 notes archived, status ['success'], 76.84s/question
