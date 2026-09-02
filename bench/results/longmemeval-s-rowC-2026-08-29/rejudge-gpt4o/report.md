# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `gpt-5.5 @ codex://local timeout=300s` |
| rejudged_from | `<path> |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |

**n = 500**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

**Accuracy: 0.882**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.933 |  |
| knowledge-update | 0.958 | 1.000 |
| multi-session | 0.826 | 0.992 |
| single-session-assistant | 0.982 | 1.000 |
| single-session-preference | 0.733 | 0.933 |
| single-session-user | 0.891 | 0.938 |
| temporal-reasoning | 0.866 | 0.984 |

mean prompt tokens: 24206.2 · mean context chars: 102442.6 · ingest 11.91s · retrieval 0.469s (per question)
