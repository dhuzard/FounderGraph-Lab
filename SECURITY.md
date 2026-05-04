# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✓ Current |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Email security reports to: **dhuzard@gmail.com**

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix (optional)

You can expect an acknowledgement within 48 hours and a status update within 7 days.

## Security model

FounderGraph-Lab is a **local-first** application designed to run on your own machine or private infrastructure. Key design decisions that affect its security posture:

| Guarantee | Implementation |
|-----------|---------------|
| LLM output never reaches Neo4j directly | `_require_validated()` rejects any non-validated record |
| No dynamic Cypher construction | All queries use `$parameter` placeholders |
| Label and relationship allowlists | Checked via regex and against the ontology YAML before any write |
| Read-only agent access | Audit agents use a `READ_ACCESS` Neo4j session |
| No external API calls during ingestion | All LLM calls go to a local Ollama instance |
| Atomic file writes | `.tmp` → rename prevents partial-write corruption |

## Credentials

Default passwords in `.env.example` and `docker-compose.yml` (`foundergraph_password`) are **development defaults** for local use only. Before exposing any service to a network:

1. Change `NEO4J_PASSWORD` in your `.env` file.
2. Do not commit `.env` — it is listed in `.gitignore`.
3. Never commit Google service account JSON files — keep them outside the project root or in a gitignored `secrets/` directory.
