# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `qwen3-coder-30b-a3b-instruct @ http://LMSTUDIO_HOST:1234/v1 timeout=300s` |
| judge_model | `gpt-4o-2024-08-06 @ https://api.openai.com/v1 timeout=120s` |
| rejudged_from | `<row-A run dir>` |

**n = 500**, retrieval: hybrid, hybrid-reworded, keyword, ingest chat-LLM calls: 0

**Accuracy: 0.482**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.700 |  |
| knowledge-update | 0.514 | 1.000 |
| multi-session | 0.306 | 0.992 |
| single-session-assistant | 0.929 | 1.000 |
| single-session-preference | 0.233 | 0.933 |
| single-session-user | 0.812 | 0.938 |
| temporal-reasoning | 0.276 | 0.874 |

mean prompt tokens: 22031.9 · mean context chars: 99951.9 · ingest 10.58s · retrieval 0.425s (per question)

**Judge agreement with original labels:** 0.950 (475/500); new-yes/orig-no 21, new-no/orig-yes 4; original accuracy 0.448
