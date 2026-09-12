# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `gpt-5.5 @ codex://local timeout=300s` |

**n = 500**, retrieval: hybrid, hybrid-reworded, ingest chat-LLM calls: 0

(no judge run)

| type | evidence recall@k |
|---|---|
| knowledge-update | 1.000 |
| multi-session | 0.992 |
| single-session-assistant | 1.000 |
| single-session-preference | 0.933 |
| single-session-user | 0.938 |
| temporal-reasoning | 0.984 |

mean prompt tokens: 24206.2 · mean context chars: 102442.6 · ingest 11.91s · retrieval 0.469s (per question)
