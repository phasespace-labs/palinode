# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| answer_prompt | `v2` |
| embedder | `reachable` |
| answer_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=180s` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 500**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.758**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.700 |  |
| knowledge-update | 0.847 | 1.000 |
| multi-session | 0.620 | 0.992 |
| single-session-assistant | 1.000 | 1.000 |
| single-session-preference | 0.433 | 0.933 |
| single-session-user | 0.938 | 0.938 |
| temporal-reasoning | 0.732 | 0.984 |

mean prompt tokens: 21956.7 · mean context chars: 102442.6 · ingest 12.07s · retrieval 0.457s (per question)
