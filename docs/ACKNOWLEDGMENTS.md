# Acknowledgments

Palinode builds on ideas from across the agent memory landscape:

- [Karpathy's LLM Knowledge Bases](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the original "agents need memory" gist that kicked off much of this work
- [Letta](https://github.com/letta-ai/letta) — tiered memory (main context + archival), self-editing memory blocks
- [LangMem](https://github.com/langchain-ai/langmem) — typed schemas, background consolidation
- [memsearch](https://zilliztech.github.io/memsearch/) — hybrid BM25 + vector search, content-hash dedup
- [Hermes](https://github.com/NousResearch/hermes-agent) — FTS5 sanitization patterns, security scanning
- [OB1](https://github.com/NateBJones-Projects/OB1) — two-door capture pattern (structured vs unstructured intake)

If you know of prior art we missed, please [open an issue](https://github.com/phasespace-labs/palinode/issues).
