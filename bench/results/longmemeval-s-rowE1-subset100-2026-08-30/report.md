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
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.750**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.833 |  |
| knowledge-update | 0.857 | 1.000 |
| multi-session | 0.640 | 1.000 |
| single-session-assistant | 0.818 | 1.000 |
| single-session-preference | 0.500 | 1.000 |
| single-session-user | 0.923 | 1.000 |
| temporal-reasoning | 0.720 | 0.960 |

mean prompt tokens: 2708.3 · mean context chars: 8238.3 · ingest 66.76s · retrieval 0.557s (per question)

answer string in context (non-abstention): 0.521

extraction: 4748 calls over 100 questions (47.5/question), 12127657 prompt + 1482831 completion tokens (121276.6 prompt/question), 300.2 facts/question, 0 parse failures, 2 refused by the model · profile in top-k: 0.990 · duplicate hits dropped/question: 4.7

| op | count |
|---|---|
| kept | 25650 |
| updated | 109 |
| merged | 154 |
| superseded | 39 |
| archived | 1058 |
| retracted | 9 |
| unmatched | 5 |
| protected_rejected | 0 |

consolidation: 98/100 profiles compacted, 1262 notes archived, status ['success'], 22.44s/question
