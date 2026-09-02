# LongMemEval × Palinode

| setting | value |
|---|---|
| dataset | `longmemeval_s_cleaned.json` |
| top_k | `10` |
| threshold | `0.4` |
| consolidate | `False` |
| embedder | `reachable` |
| answer_model | `qwen3-coder-30b-a3b-instruct @ http://LMSTUDIO_HOST:1234/v1 timeout=300s` |
| judge_model | `gemini-2.5-flash @ https://generativelanguage.googleapis.com/v1beta/openai timeout=120s {'reasoning_effort': 'none'}` |

**n = 500**, retrieval: hybrid, hybrid-reworded, keyword, ingest chat-LLM calls: 0

**Accuracy: 0.448**

| type | accuracy | evidence recall@k |
|---|---|---|
| abstention | 0.633 |  |
| knowledge-update | 0.486 | 1.000 |
| multi-session | 0.289 | 0.992 |
| single-session-assistant | 0.929 | 1.000 |
| single-session-preference | 0.000 | 0.933 |
| single-session-user | 0.781 | 0.938 |
| temporal-reasoning | 0.260 | 0.874 |

mean prompt tokens: 22031.9 · mean context chars: 99951.9 · ingest 10.58s · retrieval 0.425s (per question)
