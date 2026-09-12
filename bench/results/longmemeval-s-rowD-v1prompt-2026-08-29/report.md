# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=180s` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 500**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.578**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 1.000 |  |
| knowledge-update | 0.625 | 1.000 |
| multi-session | 0.430 | 0.992 |
| single-session-assistant | 0.946 | 1.000 |
| single-session-preference | 0.033 | 0.933 |
| single-session-user | 0.609 | 0.938 |
| temporal-reasoning | 0.543 | 0.984 |

mean prompt tokens: 21950.7 · mean context chars: 102442.6 · ingest 12.05s · retrieval 0.447s (per question)
