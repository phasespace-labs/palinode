# Sample Memory

A pre-populated memory directory for trying Palinode. Copy it to your memory directory:

```bash
cp -r examples/sample-memory/* ~/.palinode/
```

Contains:
- 3 people (Alice, Bob, Carol)
- 1 project (Mobile Checkout Redesign)
- 2 decisions (REST API choice, single-page checkout)
- 1 insight (integration testing strategy)

All files reference each other via entities, so you can test entity graph traversal, search, blame, and other tools against realistic data.
