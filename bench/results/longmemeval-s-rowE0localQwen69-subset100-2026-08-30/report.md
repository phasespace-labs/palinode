# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| pipeline | `session-end` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| extract_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=300s {'chat_template_kwargs': {'enable_thinking': False}}` |
| extract_prompt | `v1` |
| extract_workers | `8` |
| answer_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=300s {'chat_template_kwargs': {'enable_thinking': False}}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.740**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.714 | 1.000 |
| multi-session | 0.720 | 1.000 |
| single-session-assistant | 0.455 | 1.000 |
| single-session-preference | 0.333 | 1.000 |
| single-session-user | 1.000 | 1.000 |
| temporal-reasoning | 0.840 | 0.960 |

mean prompt tokens: 2692.3 · mean context chars: 8262.6 · ingest 78.48s · retrieval 0.532s (per question)

answer string in context (non-abstention): 0.532

extraction: 4748 calls over 100 questions (47.5/question), 12177274 prompt + 1390181 completion tokens (121772.7 prompt/question), 280.7 facts/question, 0 parse failures, 0 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.45
