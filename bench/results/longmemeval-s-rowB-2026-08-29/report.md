# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `gemini-3-flash-preview @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 500**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.812**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.900 |  |
| knowledge-update | 0.875 | 1.000 |
| multi-session | 0.736 | 0.992 |
| single-session-assistant | 0.982 | 1.000 |
| single-session-preference | 0.733 | 0.933 |
| single-session-user | 0.891 | 0.938 |
| temporal-reasoning | 0.732 | 0.984 |

mean prompt tokens: 23212.3 · mean context chars: 102442.6 · ingest 12.13s · retrieval 0.477s (per question)
