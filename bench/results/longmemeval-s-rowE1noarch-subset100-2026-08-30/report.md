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
| extract_prompt | `v1` |
| extract_workers | `8` |
| consolidate_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=300s {'reasoning_effort': 'none'}` |
| consolidate_allowed_ops | `KEEP,UPDATE,MERGE,SUPERSEDE` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.820**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.929 | 1.000 |
| multi-session | 0.800 | 1.000 |
| single-session-assistant | 0.727 | 1.000 |
| single-session-preference | 1.000 | 1.000 |
| single-session-user | 1.000 | 1.000 |
| temporal-reasoning | 0.680 | 0.960 |

mean prompt tokens: 2730.6 · mean context chars: 8256.4 · ingest 61.24s · retrieval 0.491s (per question)

answer string in context (non-abstention): 0.543

extraction: 4748 calls over 100 questions (47.5/question), 12127657 prompt + 1481594 completion tokens (121276.6 prompt/question), 300.4 facts/question, 0 parse failures, 2 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.74

| op | count |
|---|---|
| kept | 25565 |
| updated | 109 |
| merged | 143 |
| superseded | 38 |
| archived | 0 |
| retracted | 0 |
| unmatched | 2 |
| protected_rejected | 0 |

consolidation: 100/100 profiles compacted, 1274 notes archived, status ['success'], 22.09s/question
