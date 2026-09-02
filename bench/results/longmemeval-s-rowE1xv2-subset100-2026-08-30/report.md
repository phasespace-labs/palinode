# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| pipeline | `session-end+consolidate` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| extract_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| extract_prompt | `v2` |
| extract_workers | `4` |
| consolidate_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=300s {'reasoning_effort': 'none'}` |
| consolidate_allowed_ops | `KEEP,UPDATE,MERGE,SUPERSEDE` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.780**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.857 | 1.000 |
| multi-session | 0.720 | 1.000 |
| single-session-assistant | 0.818 | 1.000 |
| single-session-preference | 0.667 | 1.000 |
| single-session-user | 0.923 | 1.000 |
| temporal-reasoning | 0.720 | 0.960 |

mean prompt tokens: 3803.1 · mean context chars: 11423.3 · ingest 82.24s · retrieval 0.526s (per question)

answer string in context (non-abstention): 0.564

extraction: 4748 calls over 100 questions (47.5/question), 12616495 prompt + 2158843 completion tokens (126164.9 prompt/question), 484.7 facts/question, 3 parse failures, 2 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.78

| op | count |
|---|---|
| kept | 37098 |
| updated | 137 |
| merged | 267 |
| superseded | 42 |
| archived | 0 |
| retracted | 0 |
| unmatched | 3 |
| protected_rejected | 0 |

consolidation: 100/100 profiles compacted, 1274 notes archived, status ['success'], 31.23s/question
