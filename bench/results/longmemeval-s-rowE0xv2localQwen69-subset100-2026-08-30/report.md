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
| extract_prompt | `v2` |
| extract_workers | `8` |
| answer_model | `qwen3.8-27b @ http://INTERNAL_HOST:8000/v1 timeout=300s {'chat_template_kwargs': {'enable_thinking': False}}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.860**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.786 | 1.000 |
| multi-session | 0.880 | 1.000 |
| single-session-assistant | 0.909 | 1.000 |
| single-session-preference | 0.833 | 1.000 |
| single-session-user | 1.000 | 1.000 |
| temporal-reasoning | 0.800 | 0.960 |

mean prompt tokens: 3748.7 · mean context chars: 11404.7 · ingest 96.72s · retrieval 0.54s (per question)

answer string in context (non-abstention): 0.564

extraction: 4748 calls over 100 questions (47.5/question), 12675814 prompt + 1996234 completion tokens (126758.1 prompt/question), 474.7 facts/question, 5 parse failures, 0 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.74
