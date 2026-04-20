# Privacy Policy

**Palinode** — Persistent memory for AI agents
**Effective date:** April 12, 2026
**Last updated:** April 12, 2026

---

## 1. Open Source (Self-Hosted)

When you run Palinode yourself using the open source package (`pip install palinode`), **no data leaves your machine** unless you explicitly configure it to.

- **No telemetry.** Palinode does not phone home, track usage, or collect analytics.
- **No accounts required.** There is no sign-up, no API key, no registration.
- **Your data stays on your filesystem.** Memory files are markdown files in a directory you control. The index is a local SQLite database.
- **Embeddings are computed locally.** By default, Palinode uses Ollama running on your machine. No data is sent to external embedding services unless you configure a remote endpoint.
- **Git operations are your choice.** If you configure `palinode push` to sync with a remote git repository, that's your repository under your control. Palinode does not operate or have access to any remote git service.

**We have no access to your data.** Phase Space (the company behind Palinode) does not receive, store, process, or have visibility into any data you create, index, or search with the self-hosted version.

## 2. Optional Cloud Services (Future)

Phase Space may offer optional hosted services in the future, such as managed API hosting, hosted embeddings, or team synchronization. If and when these services become available:

- **Opt-in only.** No data will be sent to Phase Space services unless you explicitly enable them.
- **Data processing limited to the service.** Your data will only be processed as needed to provide the service you opted into (e.g., computing embeddings, syncing memory across team members).
- **No selling or sharing.** Your data will never be sold to or shared with third parties for advertising, training, or any other purpose.
- **Export and deletion.** You can export all your data or request deletion at any time. Your memory files remain markdown on your filesystem regardless of cloud features.
- **Transparency.** If cloud services store or process your data, we will document exactly what is stored, where, and for how long.

This section will be updated with specific terms before any cloud service launches.

## 3. Enterprise

Enterprise customers who require formal data processing agreements, on-premises deployment, or compliance certifications (SOC 2, GDPR DPA, etc.) can contact us at paul@phasespace.co. Enterprise deployments are available fully on-premises with no external dependencies.

## 4. Third-Party Services

Palinode integrates with services you configure:

- **Ollama** (or any OpenAI-compatible endpoint) for embeddings and LLM consolidation. Data sent to these services is governed by their respective privacy policies.
- **Git hosting** (GitHub, GitLab, etc.) if you configure remote push. Data sent to these services is governed by their respective privacy policies.

Palinode does not require or default to any third-party service.

## 5. Changes to This Policy

We will update this policy as Palinode evolves. Material changes will be noted in the changelog and release notes. The effective date at the top of this document reflects the most recent revision.

## Contact

For privacy questions: paul@phasespace.co

---

*Phase Space*
