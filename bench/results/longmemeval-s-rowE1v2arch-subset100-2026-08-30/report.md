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
| extract_workers | `8` |
| consolidate_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=300s {'reasoning_effort': 'none'}` |
| consolidate_allowed_ops | `config default (all)` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.805**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.600 |  |
| knowledge-update | 1.000 | 1.000 |
| multi-session | 0.760 | 1.000 |
| single-session-assistant | 1.000 | 1.000 |
| single-session-preference | 0.667 | 1.000 |
| single-session-user | 0.923 | 1.000 |
| temporal-reasoning | 0.760 | 0.960 |

mean prompt tokens: 3820.4 · mean context chars: 11469.0 · ingest 79.35s · retrieval 0.532s (per question)

answer string in context (non-abstention): 0.549

extraction: 4118 calls over 87 questions (47.3/question), 10973530 prompt + 1876902 completion tokens (126132.5 prompt/question), 483.3 facts/question, 2 parse failures, 1 refused by the model · profile in top-k: 0.989 · duplicate hits dropped/question: 5.06

| op | count |
|---|---|
| kept | 33636 |
| updated | 87 |
| merged | 203 |
| superseded | 25 |
| archived | 831 |
| retracted | 15 |
| unmatched | 2 |
| protected_rejected | 0 |

consolidation: 84/87 profiles compacted, 903 notes archived, status ['success'], 33.91s/question
