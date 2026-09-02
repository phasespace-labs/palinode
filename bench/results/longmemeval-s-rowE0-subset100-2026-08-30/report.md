# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| pipeline | `session-end` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| extract_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| extract_prompt | `v1` |
| extract_workers | `8` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.810**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.667 |  |
| knowledge-update | 0.929 | 1.000 |
| multi-session | 0.760 | 1.000 |
| single-session-assistant | 0.818 | 1.000 |
| single-session-preference | 0.667 | 1.000 |
| single-session-user | 1.000 | 1.000 |
| temporal-reasoning | 0.760 | 0.960 |

mean prompt tokens: 2782.0 · mean context chars: 8394.1 · ingest 39.69s · retrieval 0.532s (per question)

answer string in context (non-abstention): 0.553

extraction: 4748 calls over 100 questions (47.5/question), 12127657 prompt + 1482754 completion tokens (121276.6 prompt/question), 300.6 facts/question, 0 parse failures, 2 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.82
