# FounderGraph-Lab — GraphRAG Upgrade Plan

Tracking implementation of the ontology-driven GraphRAG upgrades.
Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

**Confirmed decisions**
- Keep Qdrant for chunk-level full-text + vector search.
- Add Neo4j native vector indexes for entity and community summaries (not for chunks).
- Adopt LinkML as the single source of truth now (Phase 1 runs before Phase 2).
- Entity resolution writes `SAME_AS` edges first (reversible); hard merges only behind explicit human approval.

**Execution waves** (each wave only contains tasks that touch disjoint files)

| Wave | Phases | Notes |
|---|---|---|
| 1 | Phase 0 (foundation) + Phase 2 (discovery queries) | Phase 0 lives in `neo4j_service.py` / `ontology_validator.py`; Phase 2 adds new files only. Safe in parallel. |
| 2 | Phase 1 (LinkML) | Big refactor touching ontology + models + generators. Run alone. |
| 3 | Phase 3 (text2Cypher) + Phase 5 (citations) + Phase 6 (entity resolution) | All add new services. `agents.py` edits are sequenced inside the wave. |
| 4 | Phase 4 (Neo4j vectors + hybrid retrieval) | Touches `neo4j_service.py`, `agents.py` heavily. Run alone. |
| 5 | Phase 7 (community summarization) | Builds on Phase 4. |
| 6 | Phase 8 (polish, docs, sample data) | Storytelling layer. |

---

## Phase 0 — Foundation hardening

- [x] 0.1 Replace `_safe_label` / `_safe_relationship` with backtick-quoting helpers `_quote_label` / `_quote_rel` in `app/services/neo4j_service.py`; reject anything outside `^[A-Za-z_][A-Za-z0-9_]*$`; use everywhere a label/rel-type is interpolated (lines 279, 353-356).
- [x] 0.2 Change relationship MERGE to identify on `(source, type, target)` triple; demote `id` to property; set `created_at` / `updated_at` via `ON CREATE` / `ON MATCH`.
- [x] 0.3 Add relationship indexes in `ensure_schema` for ontology-declared predicates (auto-grown from YAML, not hard-coded).
- [x] 0.4 Remove permissive fallback in `OntologyLoader.validate_relation` (`ontology_validator.py:63-80`); untyped endpoints now fail validation.
- [x] 0.5 Add bi-temporal properties (`valid_from`, `valid_to`, `superseded_by`) on entities and relations; add `Neo4jService.supersede(old_id, new_id)`; add `as_of(timestamp)` query helper.
- [x] 0.6 Tests:
  - [x] `test_safe_quoting` — reject labels with spaces, semicolons, unicode, leading digits.
  - [x] `test_relation_idempotent_on_triple` — double-upsert different ids on same triple → 1 edge.
  - [x] `test_ensure_schema_relationship_indexes` — assert created.
  - [x] `test_untyped_relation_rejected` — validator returns False, violation recorded.
  - [x] `test_bi_temporal_supersede` — entity superseded, `as_of(past)` returns old, `as_of(now)` returns new.

## Phase 1 — LinkML as single source of truth

- [x] 1.1 Author `app/ontology/startup_ontology.linkml.yaml` mirroring `startup_ontology.yaml`.
- [x] 1.2 Add `scripts/generate_ontology_artifacts.py` that runs `gen-pydantic`, `gen-json-schema`, `gen-shacl`, plus a custom Cypher DDL generator.
- [x] 1.3 Add `make generate` target; wire pre-commit / CI check to fail on stale artifacts.
- [x] 1.4 Replace `app/models/entity.py`, `relation.py`, `document.py` with the generated Pydantic v2 models (or re-export).
- [x] 1.5 Rewrite `ensure_schema` to load DDL from `app/ontology/generated/cypher_constraints.cypher`.
- [x] 1.6 Rewrite `OntologyLoader` / `OntologyValidator` to consume generated JSON-Schema + SHACL.
- [x] 1.7 Integrate pySHACL deterministic gate; serialize staging graph to RDF, run shapes, write violations to `data/staging/shacl_violations.json`.
- [x] 1.8 Tests:
  - [x] `test_linkml_artifacts_in_sync` — regenerate and diff against committed; CI fails on drift.
  - [x] `test_pyshacl_violation` — Assumption missing `criticality` shape fails.
  - [x] All existing ontology tests still green.

## Phase 2 — Ontology-driven discovery queries

- [x] 2.1 Create `app/services/discovery_queries.py` with `@register` decorator and these queries:
  - [x] `unsupported_assumptions`
  - [x] `contradicted_assumptions`
  - [x] `orphan_segments`
  - [x] `orphan_problems`
  - [x] `risked_milestones`
  - [x] `untested_critical_assumptions`
  - [x] `weak_evidence_chains`
- [x] 2.2 Create `app/pages/07_discovery.py` — tile per query, click → table with deep links.
- [x] 2.3 Tests `tests/test_discovery_queries.py` — seed a tiny graph via `FakeDriver` and assert each query finds the planted gap.

## Phase 3 — Schema-aware text2Cypher

- [x] 3.1 Create `app/services/cypher_planner.py` with `plan(question) -> CypherPlan` and validation gate (label/rel/domain-range whitelist, read-only enforcement, query timeout, one repair attempt).
- [x] 3.2 Create `app/prompts/cypher_plan.md` — ontology view + NL→Cypher examples + forbidden tokens.
- [x] 3.3 Wire into `app/pages/05_agents.py` — "Ask the graph" box, expander shows generated Cypher + rationale.
- [x] 3.4 Tests `tests/test_cypher_planner.py`:
  - [x] Golden — natural language → expected Cypher shape.
  - [x] Adversarial — DELETE / off-ontology label / injection rejected.

## Phase 4 — Native Neo4j vectors + hybrid retrieval

- [x] 4.1 Extend `ensure_schema` to create `VECTOR INDEX entity_embedding` (and a community-summary index later in Phase 7). Chunks stay in Qdrant.
- [x] 4.2 Materialize entity-summary embeddings on `(:Entity)` nodes; backfill script in `scripts/`.
- [x] 4.3 Create `app/services/hybrid_retriever.py` with three-stage retrieval (vector seed → typed expansion → graph-aware re-rank). Surface weights in `app/config.py`.
- [x] 4.4 Swap each agent in `app/services/agents.py` to use `hybrid_retriever.retrieve(...)`; keep static discovery queries from Phase 2 unchanged.
- [x] 4.5 Tests:
  - [x] `test_hybrid_retriever_ordering` — supported assumptions outrank unsupported ones for an investor-style question.
  - [x] `test_entity_vector_index_created`.

## Phase 5 — Grounded citations & hallucination filter

- [x] 5.1 Rewrite `app/prompts/*_audit.md` to require JSON output (`{summary, findings:[{claim, evidence_entity_ids, source_chunk_ids, confidence}]}`).
- [x] 5.2 Create `app/services/citation_verifier.py` that drops/flags findings whose cited ids aren't in the retrieved context.
- [x] 5.3 Update `app/pages/05_agents.py` rendering — show verified vs. ungrounded findings with badges.
- [x] 5.4 Tests `tests/test_citation_verifier.py` — fake LLM hallucinates an entity id → filtered.

## Phase 6 — Entity resolution (SAME_AS first)

- [x] 6.1 Create `app/services/entity_resolver.py`:
  - [x] Cluster by `(type, label_embedding)` cosine ≥ 0.92 + name-token Jaccard.
  - [x] LLM same-as confirmation prompt.
  - [x] Write `(:Entity)-[:SAME_AS]->(:Entity)` edges (reversible) on approval.
  - [x] Provide `consolidate(canonical_id)` helper using `apoc.refactor.mergeNodes` behind explicit UI button.
- [x] 6.2 Extend `app/pages/03_validate_knowledge.py` with a "Resolve duplicates" tab presenting merge proposals + diff.
- [x] 6.3 Tests `tests/test_entity_resolver.py` — seeded duplicates → resolver proposes merge → approval writes SAME_AS edge.

## Phase 7 — Community summarization (Microsoft GraphRAG-style)

- [x] 7.1 Add `app/services/community_service.py` — GDS Louvain projection, writes `community_id` onto nodes, builds `(:Community {id, summary, embedding})`.
- [x] 7.2 Create `VECTOR INDEX community_embedding` (Phase 4 leaves a stub).
- [x] 7.3 Add `app/pages/08_communities.py` — community list ranked by size / risk exposure.
- [x] 7.4 Hybrid retriever routes global questions to community summaries, local questions to nodes.
- [x] 7.5 Tests — community detection deterministic on a fixed seed graph.

## Phase 8 — Polish, MCP, docs

- [x] 8.1 Temporal filters in discovery + Graph Explorer (`as_of(date)` slider, deadline windows).
- [x] 8.2 `PROFILE` telemetry expander on agent results.
- [x] 8.3 MCP servers in `app/mcp/` (`neo4j_server.py`, `qdrant_server.py`) exposing `query_graph`, `semantic_search`, `discovery_query`.
- [x] 8.4 README rewrite framed around five pillars: constrained extraction, deterministic discovery, ontology-guarded text2Cypher, grounded citations, bi-temporal audit trail.
- [x] 8.5 Sample dataset upgrade — deliberately contradictory pitch deck under `sample_data/` so discovery agents surface drama on first run.

---

## Quality check (post-Phase-8)

Run on commit `ca7861d`:

- [x] `python -m pytest tests/ -q` — **240 passed**, 0 failed, 0 skipped.
- [x] `python -m compileall app/ scripts/ tests/` — clean, no syntax errors.
- [x] Import smoke test for every Phase 0–8 service module — clean.
- [x] LinkML drift check — `python scripts/generate_ontology_artifacts.py` regenerates identical artifacts (no `git diff` on `app/ontology/generated/`).
- [x] `git status` clean, branch up to date with `origin/claude/streamlit-neo4j-knowledge-graph-vpPyO`.
- [x] In-code TODOs in services — only 1 contextual note in `neo4j_service.py:457` (Phase 0.4 inference path), no orphan FIXMEs / HACKs.
- [x] LoC: services 8 077 lines, tests 5 379 lines (~0.67 test-to-code ratio).

---

## Phase 9 — Logical next steps

Ordered by impact-to-effort. None of these are blockers; the system is shippable as-is for an illustration repo.

### Ontology completeness

- [x] 9.1 Add `MITIGATES` predicate (`Experiment → Risk`) to `startup_ontology.linkml.yaml`; regenerate artifacts; update `discovery_queries.risked_milestones` to use `MITIGATES` instead of the current `TESTS` substitution (see comment in `app/services/discovery_queries.py:153`). Drop the substitution note.
- [ ] 9.2 Audit `app/services/ontology_service.py` — it predates LinkML. Either retire it (move `init_ontology.py` wizard onto the LinkML loader) or document its scope so future contributors don't add drift.

### Demo readiness

- [ ] 9.3 End-to-end smoke run on `sample_data/contradictory_*.md`: upload → extract → validate → discover. Capture screenshots of the Discovery page showing the planted contradictions, and the Agents page showing grounded citations. Embed in `README.md` or `STEP_BY_STEP_USER_GUIDE.md`.
- [x] 9.4 Add a `make demo` target that wipes `data/` then loads the contradictory sample, so a fresh clone shows drama in <2 min.
- [ ] 9.5 30-second screencast per pillar (5 total) for the README.

### CI / DevEx

- [x] 9.6 Wire `make generate-check` into `.github/workflows/` so a PR that edits the LinkML YAML without regenerating artifacts fails CI.
- [ ] 9.7 Add a GitHub Actions job that runs `pytest` + the LinkML drift check on every push.
- [ ] 9.8 Pre-commit hook for `make generate-check` so the failure surfaces locally before CI.

### Carry-overs from the original `TODO.md`

These were `🟡` / `🟢` before the GraphRAG upgrade and remain open:

- [ ] 9.9 Dedupe `status` vs `validation_status` fields on `KnowledgeEntity`; pick one, normalize on load.
- [ ] 9.10 Delete stale Qdrant chunks by `document_id` filter before re-upserting on document re-upload.
- [ ] 9.11 Per-document staging index — `data/staging/{doc_id}/` subdirectory layout instead of flat files.
- [ ] 9.12 Confidence-stratified review UI — fast-approve lane / standard editor / detailed diff view.
- [ ] 9.13 Replace PyVis graph explorer with `streamlit-agraph` for click-to-inspect + type filtering.
- [ ] 9.14 Expand `assumption_audit.md` and `pitch_audit.md` prompt texts (still short).

### Hardening

- [ ] 9.15 Live-Neo4j integration test (currently all driver use is `FakeDriver`). A small `docker-compose -f docker-compose.test.yml` running real Neo4j 5.20 + APOC, with one end-to-end happy-path test gated behind `pytest -m integration`.
- [ ] 9.16 Benchmark the hybrid retriever on a 10k-node graph — confirm the α/β/γ defaults in `app/config.py` still produce sensible rankings. Tune if the supported-vs-unsupported separation drops below 0.1.
- [ ] 9.17 Strict JSON-mode for Ollama via `outlines` or `instructor` — current `parse_audit` has a balanced-brace fallback; making it strict would simplify the path and surface model drift faster.
- [ ] 9.18 Add `MCP` server runtime test using a real `mcp` SDK install (gated behind an extras dep).

### Parking lot decisions still open

- [ ] 9.19 Decide eventual retirement of Qdrant once Neo4j vector index proves out for entity + community summaries (chunks would migrate to `(:Chunk {embedding})` nodes).
- [ ] 9.20 Decide whether `SAME_AS` should ever auto-promote to hard merge after N independent confirmations.
- [ ] 9.21 Decide whether to add provenance edges from `Community` to the underlying audit run (so global summaries can be re-derived).

### Stretch — beyond the illustration scope

- [ ] 9.22 `Graphiti` (Zep AI) evaluation as a managed alternative to the hand-rolled bi-temporal layer.
- [ ] 9.23 Multi-tenant data isolation (per-startup namespace) — currently single-tenant.
- [ ] 9.24 Replace Ollama as the default LLM with `claude-haiku-4-5-20251001` for the planner / verifier hot paths (cheaper, faster, JSON-mode native). Keep Ollama for embedding so local-first remains intact.
