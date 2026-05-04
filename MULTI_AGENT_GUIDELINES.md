# FAIR-VCG-mentor — Multi-Agent Guidelines

> Version: 1.0 | Date: 2026-05-01

These guidelines cover two complementary topics:

1. **Extending the built-in agent workflows** in `app/services/agents.py`
2. **Using a Claude Code multi-agent setup** to develop, audit, and operate this repository

---

## Part 1: Extending Built-In Agent Workflows

### 1.1 Architecture of a Workflow

Every workflow is a single Python function that:

1. Reads a Markdown prompt file from `app/prompts/`
2. Runs a read-only Neo4j Cypher query via `_neo4j_read()`
3. Runs a Qdrant semantic search via `QdrantService().semantic_search()`
4. Synthesizes both into an Ollama prompt
5. Writes a timestamped Markdown audit to `vault/audits/`
6. Returns a result dict with `path`, `graph`, `snippets`, and `ollama` keys

All workflows are registered in `WORKFLOWS` dict and automatically appear in the Streamlit agents page.

```python
# Pattern for adding a new workflow
def my_new_workflow() -> dict[str, Any]:
    return run_agent_workflow(
        slug="my-workflow",
        title="My Workflow Title",
        prompt_file="my_prompt.md",
        query="semantic search terms that match relevant document chunks",
        cypher=(
            "MATCH (n:Entity) "
            "WHERE n:SomeLabel "
            "RETURN n.id AS id, n.label AS label LIMIT 50"
        ),
    )

WORKFLOWS["My New Workflow"] = my_new_workflow
```

### 1.2 Allowed Cypher Patterns

Agents must only issue read-only Cypher. Use these safe patterns:

```cypher
-- Fetch entities by label
MATCH (n:Entity:Assumption) RETURN n.id, n.label, n.description LIMIT 75

-- Traverse relationships
MATCH (a:Entity:Assumption)-[:SUPPORTED_BY]->(e:Entity:Evidence)
RETURN a.label AS assumption, collect(e.label) AS evidence

-- Optional matches for sparse graphs
MATCH (a:Entity:Assumption)
OPTIONAL MATCH (ex:Entity:Experiment)-[:TESTS]->(a)
RETURN a.label, collect(ex.label) AS experiments LIMIT 75

-- Count patterns
MATCH (n:Entity) RETURN labels(n) AS labels, count(*) AS total
```

Never use `CREATE`, `MERGE`, `SET`, `DELETE`, `DETACH DELETE`, or `CALL` in agent queries. Use `graph_snapshot()` from `Neo4jService` for general graph reads.

### 1.3 Writing Effective Prompts

Prompt files live in `app/prompts/`. Each prompt is injected as the system instruction; graph context and vector snippets are appended below it at runtime.

Guidelines for prompt files:
- State exactly what the agent should analyze and what sections to include in output.
- Specify the output format (the app expects Markdown with headings).
- Include explicit grounding instructions: `"Use only the graph context and evidence snippets below. Do not invent facts."`
- Keep prompts under 600 tokens so the combined prompt + context fits local model context windows (typically 4096–8192 tokens for `llama3.1:8b`).

### 1.4 Graceful Degradation Contract

Every workflow must produce output even when backends are unavailable. The `run_agent_workflow` helper handles this automatically via `_fallback_markdown`. Do not bypass it by calling `_ollama_generate` directly in new workflows.

### 1.5 Adding a New Ontology Type

To support a new entity type across the full stack:

1. **`app/ontology/startup_ontology.yaml`** — add the class definition with properties.
2. **`app/models/entity.py`** — add to the `EntityType` enum.
3. **`app/services/neo4j_service.py`** — add to `DEFAULT_ALLOWED_LABELS`.
4. **`app/prompts/extract_entities.md`** — add the type to the allowed types list with a description.
5. **`app/services/neo4j_service.py:ensure_schema()`** — add any new uniqueness constraints or indexes.
6. Update tests in `tests/test_entity_schema.py` to cover the new type.

---

## Part 2: Claude Code Multi-Agent Development Guidelines

This section defines how to orchestrate multiple Claude agents (via Claude Code's `Agent` tool / SDK) when working on FAIR-VCG-mentor tasks. These patterns apply both to human developers using Claude Code and to automated pipelines.

### 2.1 Core Principles

**Isolation by concern.** Each agent should own a single layer of the pipeline. Never let a single agent span extraction, validation, and graph writes in one session — that defeats the human-in-the-loop architecture.

**Read before write.** Every agent that proposes a graph change must first read the current graph state via `graph_snapshot()` or a targeted Cypher query. Blind writes create duplicate entities and orphaned relations.

**Staging is the handoff point.** Agents may write to `data/staging/` freely. Nothing in staging can reach Neo4j without passing through the human validation UI. Treat staging files as the safe inter-agent message bus.

**Audit trail first.** Agents should prefer writing Markdown outputs to `vault/audits/` over mutating application state. Analysis results, recommendations, and summaries belong in audits, not in staging or the graph.

---

### 2.2 Recommended Agent Roles

The following roles map naturally to the pipeline stages:

#### Role: Document Ingest Agent
- **Scope**: `app/services/file_store.py`, `app/services/extractors.py`, `app/services/markdown_converter.py`
- **Inputs**: Raw files from `data/original_files/`
- **Outputs**: Markdown files in `vault/documents/`, extracted text in `data/extracted_text/`
- **Must not**: Touch staging, Neo4j, or Qdrant
- **Typical tasks**: Batch-convert uploaded documents, validate extraction quality, detect unsupported file formats

#### Role: Extraction Agent
- **Scope**: `app/services/entity_extractor.py`, `app/services/llm_service.py`, `app/prompts/`
- **Inputs**: Markdown from `vault/documents/`
- **Outputs**: `data/staging/candidate_entities.json`, `data/staging/candidate_relations.json`
- **Must not**: Write to Neo4j or Qdrant; must not overwrite previously staged candidates without merging
- **Typical tasks**: Re-extract after prompt changes, extract a batch of documents with merge semantics, tune extraction prompts

#### Role: Validation Review Agent
- **Scope**: `app/services/validation_store.py`, `data/staging/`, `data/knowledge/`
- **Inputs**: Candidate entities/relations from staging
- **Outputs**: Updated validation status fields in `data/knowledge/`
- **Must not**: Write to Neo4j directly — only update JSON validation status
- **Typical tasks**: Bulk pre-approve high-confidence candidates, flag entities with missing source snippets, deduplicate candidates before human review

#### Role: Graph Persistence Agent
- **Scope**: `app/services/neo4j_service.py`, `app/services/qdrant_service.py`
- **Inputs**: Validated entities/relations from `data/knowledge/`
- **Outputs**: Neo4j nodes/relations, Qdrant embeddings
- **Must not**: Process non-validated records (`validation_status != "validated"`)
- **Typical tasks**: Bulk-write a new batch of validated knowledge, run `ensure_schema()` after ontology changes, re-index Qdrant after graph writes

#### Role: Audit Agent
- **Scope**: `app/services/agents.py`, `vault/audits/`
- **Inputs**: Neo4j read queries, Qdrant search, Ollama synthesis
- **Outputs**: Timestamped Markdown audits in `vault/audits/`
- **Must not**: Mutate Neo4j, staging, or knowledge files
- **Typical tasks**: Run assumption audits on demand, generate due diligence checklists, compare audit outputs across time

#### Role: Code Review / QA Agent
- **Scope**: Entire repository (read-only)
- **Inputs**: Source files, test results, `ruff` output
- **Outputs**: Assessment documents, suggested patches
- **Must not**: Commit directly to `main`; must open PRs or write findings to assessment files
- **Typical tasks**: Security review, test coverage gap analysis, dependency audit, ontology consistency checks

---

### 2.3 Agent Task Decomposition

When a task spans multiple pipeline stages, decompose it into sequential sub-tasks, each assigned to the appropriate role agent. The output of one agent becomes the explicit input to the next.

**Example: "Add a new document type and extract entities from a new file"**

```
1. [Code Review Agent]      Read current ontology and entity enum — report gaps
2. [Code Review Agent]      Propose changes to ontology.yaml, entity.py, neo4j_service.py
3. [Document Ingest Agent]  Convert new file to Markdown in vault/documents/
4. [Extraction Agent]       Run extract_to_staging() on the new Markdown
5. [Validation Review Agent] Pre-screen high-confidence candidates (confidence >= 0.8)
6. [Human]                  Review and approve in Streamlit validation page
7. [Graph Persistence Agent] Write validated knowledge to Neo4j + Qdrant
8. [Audit Agent]            Run assumption audit to verify new knowledge integrates correctly
```

---

### 2.4 Boundary Rules

These rules prevent agents from violating the pipeline's safety invariants:

| Rule | Rationale |
|------|-----------|
| Staging files must use **merge semantics** when appending — never replace the entire file | Prevents loss of previously staged candidates from other documents |
| Any agent writing Cypher must use the `Neo4jService` API — never raw driver sessions | Ensures label/relationship whitelisting is always applied |
| Agents must not modify `data/knowledge/validated_*.json` to change `status` to `validated` in bulk without logging rationale | Bypasses human review intent |
| Agents running Cypher reads must open sessions with `access_mode="READ"` | Prevents accidental write escalation |
| Audit outputs are append-only; agents must not delete or overwrite existing audit files | Preserves audit history |
| New prompt files must be reviewed by a human before being used in production extraction | LLM prompt changes can silently alter extraction quality |

---

### 2.5 Context Handoff Protocol

When one agent completes a task and hands off to the next:

1. **Write a brief summary to `vault/audits/`** using the `_save_audit` helper, stating: what was processed, what was produced, and any anomalies found.
2. **Return a structured result dict** with at minimum: `{"status": "ok"|"error", "outputs": [...paths...], "counts": {...}, "notes": "..."}`.
3. **Do not pass large payloads in memory between agents.** Use files as the handoff medium — staging JSON, Markdown vault files, and audit Markdown.

---

### 2.6 Model Selection

| Role | Recommended Model | Rationale |
|------|-------------------|-----------|
| Document Ingest | `claude-haiku-4-5` | File I/O, format conversion — speed matters |
| Extraction Agent | `claude-sonnet-4-6` | Structured JSON extraction requires strong instruction-following |
| Validation Review | `claude-haiku-4-5` | Pattern matching and filtering — simple reasoning |
| Code Review / QA | `claude-opus-4-7` | Deep codebase analysis, security review |
| Audit / Synthesis | `claude-sonnet-4-6` | Balanced capability for long-context synthesis |

Use `claude-sonnet-4-6` as the default when role is unclear.

---

### 2.7 Parallel Execution Guidelines

These tasks are safe to run in parallel (no shared write targets):

- Multiple **Document Ingest** agents processing different source files simultaneously
- **Audit Agent** running while **Extraction Agent** is working on a new document (audits read only)
- **Code Review Agent** analyzing any service module while other agents operate

These tasks must run sequentially (shared write targets or dependency ordering):

- Multiple **Extraction Agents** writing to the same staging files — must merge serially
- **Graph Persistence Agent** and any agent that reads graph state for decision-making — persist first, then read
- **Validation Review Agent** and **Graph Persistence Agent** — validate first, then persist

---

### 2.8 Error Handling and Rollback

**Staging layer**: if an agent fails mid-extraction, the `.tmp` → rename pattern means the staging file is either fully written or unchanged. No manual rollback needed.

**Neo4j layer**: all writes use `MERGE` — re-running a persistence agent is idempotent. On error, log the failure and retry the specific entity/relation that failed rather than the entire batch.

**Recovery runbook for common failures**:

| Failure | Recovery |
|---------|----------|
| LLM returns invalid JSON | Retry with increased temperature or simplified prompt; log attempt count |
| Neo4j connection refused | Check `NEO4J_URI` env var; run `make up`; retry with exponential backoff (2s, 4s, 8s, 16s) |
| Staging file corrupted | Restore from `data/staging/*.tmp` if present; otherwise re-run extraction |
| Qdrant unavailable | Proceed with Neo4j-only workflows; Qdrant indexing can be deferred |
| Validation status not `"validated"` on persist attempt | Do not promote; surface to human review queue |

---

### 2.9 Quick Reference: File Paths by Role

```
app/prompts/              → Extraction Agent: prompt templates
app/services/extractors.py         → Document Ingest Agent
app/services/file_store.py         → Document Ingest Agent
app/services/entity_extractor.py   → Extraction Agent
app/services/validation_store.py   → Validation Review Agent
app/services/neo4j_service.py      → Graph Persistence Agent
app/services/qdrant_service.py     → Graph Persistence Agent
app/services/agents.py             → Audit Agent
data/original_files/               → Document Ingest: read-only source
data/extracted_text/               → Document Ingest: output
vault/documents/                   → Extraction Agent: input
data/staging/                      → Extraction Agent: output; Validation Agent: input
data/knowledge/                    → Validation Agent: output; Persistence Agent: input
vault/audits/                      → Audit Agent: output (append-only)
data/exports/                      → Export Agent: output
```
