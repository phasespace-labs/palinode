# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Palinode, please report it responsibly.

**Email:** paul@phasespace.co

**What to include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- Acknowledgment within 48 hours
- Assessment and plan within 7 days
- Fix released as soon as practical, with credit to the reporter (unless you prefer anonymity)

**Please do not:**
- Open a public GitHub issue for security vulnerabilities
- Exploit the vulnerability beyond what's needed to demonstrate it

## Scope

Palinode runs locally on your machine. The primary attack surface is:
- Path traversal in file operations (mitigated: all paths validated against PALINODE_DIR)
- API endpoint abuse (mitigated: rate limiting, request size limits)
- LLM prompt injection via memory content (mitigated: deterministic executor, LLM never writes files directly)

## Supported Versions

Security fixes are applied to the latest release only.
