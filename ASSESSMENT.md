# FounderGraph-Lab — Functionality & Usability Assessment

> Assessed: 2026-05-01 | Branch: `claude/assess-repo-multi-agent-E1XWI`

---

## 1. Project Overview

FounderGraph-Lab is a **local-first Streamlit application** for converting raw startup documents (pitch decks, business plans, interview notes, grant applications) into a validated, traversable knowledge graph. It combines LLM-powered extraction, human-in-the-loop validation, and graph persistence across three complementary stores: Neo4j (structured graph), Qdrant (semantic vectors), and a Markdown vault (human-readable archive).

Core pipeline:
```
startup files → extraction → Markdown vault → LLM staging → human validation → Neo4j → Qdrant → audit agents
```

---

## 2. Functionality Assessment

### 2.1 What Works Well

#### Extraction pipeline
- `EntityExtractor` classifies documents, extracts entities, then extracts relations in three sequential LLM calls, writing atomically to JSON staging files.
- Staging files are written with a `.tmp` → rename pattern (`_atomic_write_json`), preventing partial writes.
- The extractor is fully decoupled from Neo4j — it never opens a graph connection, satisfying the "staging never touches production" invariant.
- Flexible schema: accepts both legacy `is_startup_document` shapes and the current ontology shape via `@model_validator`.

#### Knowledge graph (Neo4j)
- `Neo4jService` uses parameterized MERGE-based Cypher — no string interpolation of user data into queries.
- Labels and relationship types are validated against a strict allowlist before any write.
- Validation gate: `_require_validated` rejects any entity or relation not explicitly marked `validated`, preventing accidental staging → graph promotion.
- Schema constraints and indexes are enforced via `ensure_schema()` (unique IDs, type/status indexes).
- Comprehensive read helpers: `get_unsupported_assumptions`, `get_risks_by_milestone`, `get_features_by_problem`, `audit_recent_writes`.

#### Ontology
- 19 entity types and 16 relation types cover the full startup lifecycle: assumptions, evidence, experiments, investors, IP, regulatory constraints, financial hypotheses, and more.
- Defined in `startup_ontology.yaml` and mirrored in both Python enums and Pydantic models — single source of truth.

#### Agent workflows
- 7 read-only workflows in `agents.py`: Pitch Audit, Assumption Audit, Unsupported Assumption Agent, Customer Discovery, Due Diligence Checklist, Next Experiment Suggestions, Grant Strategy.
- Each workflow combines a Neo4j Cypher read, a Qdrant semantic search, and Ollama synthesis.
- All workflows gracefully degrade: if Neo4j, Qdrant, or Ollama is unavailable, a structured fallback Markdown report is written instead of raising an exception.
- Audit outputs are timestamped Markdown files in `vault/audits/` — persisted and human-readable.

#### UI (Streamlit multi-page app)
- 6 clearly scoped pages: Upload → Extracted Documents → Validate Knowledge → Graph Explorer → Agent Audits → Exports.
- Validation page uses `st.data_editor` with dropdown status selectors and large-text columns — usable for reviewing dozens of candidates without custom frontend code.
- Sidebar demo-seed button lowers onboarding friction significantly.
- Metrics bar (candidates / validated counts) gives at-a-glance pipeline status.

#### Testing
- 5 test files covering: schema validation, file extraction, Markdown conversion, Neo4j safety, and Qdrant service.
- `FakeLLM` stub in tests allows full extraction pipeline tests without a live LLM.
- Tests verify the atomic write/rollback invariant (no staging files written on LLM error).

#### Developer experience
- `Makefile` wraps `up`, `down`, `logs`, `test`, `lint`, `format`, `pull-models` — minimal ramp-up.
- Docker Compose orchestrates all four services (app, Neo4j, Qdrant, Ollama) with persistent volumes.
- `.env.example` documents every required environment variable.
- `ruff` enforced for linting and formatting.

---

### 2.2 Functional Gaps and Issues

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | **High** | `requirements.txt` | No version pins — all 19 packages are unpinned. Any upstream breaking release will silently break the install. |
| 2 | **High** | `app/services/agents.py:13` | `PROMPT_DIR = Path("app/prompts")` is a relative path resolved against the process CWD. If the app is launched from any directory other than the project root, prompt loading silently returns empty strings, causing the LLM to operate without instructions. |
| 3 | **High** | `app/services/entity_extractor.py:284–292` | `_write_candidates` **overwrites** the entire staging file on every extraction run. A user who uploads multiple documents successively loses all previous staging results — only the last document's candidates persist. |
| 4 | **Medium** | `app/services/agents.py:12` | `AUDIT_DIR` defaults to a relative path (`vault/audits`) with no fallback to an absolute project-rooted path, matching the `PROMPT_DIR` CWD issue above. |
| 5 | **Medium** | `app/services/agents.py:48–64` | `_ollama_generate` imports `_json_request` from `qdrant_service` — a private function from an unrelated service. This creates a hidden coupling that will break if the Qdrant service is refactored. |
| 6 | **Medium** | `app/pages/03_validate_knowledge.py` | No pagination on the `st.data_editor` tables. With >200 staging candidates the table becomes unusable in the browser without horizontal scrolling. |
| 7 | **Medium** | `app/services/neo4j_service.py:262–293` | `graph_snapshot` returns up to LIMIT 100 results with no warning when the limit is reached, making partial graph views look complete. |
| 8 | **Low** | `app/services/agents.py:30` | The read-only Cypher guard checks only that the string starts with `MATCH`, `CALL DB.`, `RETURN`, or `WITH`. A string like `MATCH … DELETE …` would pass the prefix check. Use a proper read-only Neo4j session (`access_mode="READ"`) instead. |
| 9 | **Low** | `tests/` | No `conftest.py` — common fixtures (`tmp_path` patterns, fake service setup) are repeated across test files. |
| 10 | **Low** | `app/services/llm_service.py` | Single Ollama provider with no abstraction for alternative LLM backends (OpenAI-compatible APIs, Anthropic, etc.). |

---

## 3. Usability Assessment

### 3.1 Strengths

- **Zero-friction onboarding**: `cp .env.example .env && make up && make pull-models` is the full setup sequence.
- **Demo data included**: `sample_data/` ships with a realistic fictional startup (Metadatapp) across pitch deck, business plan, roadmap, financial CSV, and two customer interviews.
- **Human-in-the-loop is the default, not an afterthought**: staging → validation → graph is a hard gate, not an opt-in flag.
- **Auditability built in**: every entity and relation carries `source_snippet`, `source_document_id`, and `provenance_json`. The `audit_recent_writes` query surfaces the most recent graph mutations for spot-checking.
- **Graceful degradation**: the app starts and operates with partially available backends (e.g., Qdrant or Neo4j down). Each service failure surfaces a UI warning rather than a crash.

### 3.2 Usability Gaps

| # | Area | Gap |
|---|------|-----|
| 1 | **Multi-document workflow** | No queue or batch upload; documents must be extracted one at a time and staging results are overwritten on each run (see gap #3 above). |
| 2 | **Multi-user / multi-project** | All data lives in flat per-project JSON files and a single Neo4j database. No namespace isolation, no project switching from the UI. |
| 3 | **Validation workflow** | No keyboard shortcuts or quick-approve actions on the data editor. Validating 50+ candidates requires many individual dropdown interactions. |
| 4 | **Extraction feedback** | No progress indicator during the three-step LLM extraction chain. The UI shows a spinner but not which step (classify / extract entities / extract relations) is running. |
| 5 | **Error surfacing** | `LLMInvalidJSONError` and `Neo4jServiceError` propagate as bare Python exceptions that Streamlit renders as red tracebacks. No friendly error messages with suggested remediation. |
| 6 | **Graph explorer** | The PyVis visualization is static HTML rendered in an iframe — no click-to-inspect, no node expansion, and no filtering by entity type from the UI. |
| 7 | **Onboarding documentation** | The README demo script (7 steps) assumes familiarity with the data model. New users benefit from a glossary of the 19 entity types and when to use each. |

---

## 4. Security Assessment

The project applies several strong security controls:

- **No LLM-generated Cypher**: all queries are hardcoded in Python with parameterized values.
- **Label/relationship whitelisting**: `_safe_label` and `_safe_relationship` raise `Neo4jServiceError` for any value not in the allowlist, preventing graph injection.
- **Validation gate**: `_require_validated` is a hard check; non-validated records cannot reach Neo4j.
- **Atomic staging writes**: `.tmp` → rename prevents partial/corrupt staging files.
- **Original file archiving**: uploaded files are never overwritten.

One remaining gap: the read-only Cypher guard in `agents.py` (string prefix check) should be replaced with a proper Neo4j read-mode session.

---

## 5. Summary Ratings

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Core pipeline correctness | ★★★★☆ | Solid extraction → validation → graph; staging overwrite is a significant gap |
| Security | ★★★★☆ | Strong; Cypher prefix guard is the weak link |
| Onboarding friction | ★★★★☆ | Docker + Makefile + demo data is well done; unpinned deps risk breakage |
| UI usability | ★★★☆☆ | Functional for small datasets; no batch ops, no pagination, limited graph interactivity |
| Test coverage | ★★★☆☆ | Core services covered; no integration tests, no UI tests, no conftest |
| Extensibility | ★★★☆☆ | Agent workflows easy to add; LLM provider is hardwired to Ollama |
| Documentation | ★★★☆☆ | README covers the happy path; no entity type glossary, no architecture diagram |
