# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 100**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.750**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.667 |  |
| knowledge-update | 0.857 | 1.000 |
| multi-session | 0.640 | 1.000 |
| single-session-assistant | 1.000 | 1.000 |
| single-session-preference | 0.500 | 0.833 |
| single-session-user | 0.923 | 0.923 |
| temporal-reasoning | 0.680 | 0.960 |

mean prompt tokens: 21850.4 · mean context chars: 96492.8 · ingest 15.56s · retrieval 0.658s (per question)
