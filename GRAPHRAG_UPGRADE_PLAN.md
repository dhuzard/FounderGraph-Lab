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

- [ ] 1.1 Author `app/ontology/startup_ontology.linkml.yaml` mirroring `startup_ontology.yaml`.
- [ ] 1.2 Add `scripts/generate_ontology_artifacts.py` that runs `gen-pydantic`, `gen-json-schema`, `gen-shacl`, plus a custom Cypher DDL generator.
- [ ] 1.3 Add `make generate` target; wire pre-commit / CI check to fail on stale artifacts.
- [ ] 1.4 Replace `app/models/entity.py`, `relation.py`, `document.py` with the generated Pydantic v2 models (or re-export).
- [ ] 1.5 Rewrite `ensure_schema` to load DDL from `app/ontology/generated/cypher_constraints.cypher`.
- [ ] 1.6 Rewrite `OntologyLoader` / `OntologyValidator` to consume generated JSON-Schema + SHACL.
- [ ] 1.7 Integrate pySHACL deterministic gate; serialize staging graph to RDF, run shapes, write violations to `data/staging/shacl_violations.json`.
- [ ] 1.8 Tests:
  - [ ] `test_linkml_artifacts_in_sync` — regenerate and diff against committed; CI fails on drift.
  - [ ] `test_pyshacl_violation` — Assumption missing `criticality` shape fails.
  - [ ] All existing ontology tests still green.

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

- [ ] 3.1 Create `app/services/cypher_planner.py` with `plan(question) -> CypherPlan` and validation gate (label/rel/domain-range whitelist, read-only enforcement, query timeout, one repair attempt).
- [ ] 3.2 Create `app/prompts/cypher_plan.md` — ontology view + NL→Cypher examples + forbidden tokens.
- [ ] 3.3 Wire into `app/pages/05_agents.py` — "Ask the graph" box, expander shows generated Cypher + rationale.
- [ ] 3.4 Tests `tests/test_cypher_planner.py`:
  - [ ] Golden — natural language → expected Cypher shape.
  - [ ] Adversarial — DELETE / off-ontology label / injection rejected.

## Phase 4 — Native Neo4j vectors + hybrid retrieval

- [ ] 4.1 Extend `ensure_schema` to create `VECTOR INDEX entity_embedding` (and a community-summary index later in Phase 7). Chunks stay in Qdrant.
- [ ] 4.2 Materialize entity-summary embeddings on `(:Entity)` nodes; backfill script in `scripts/`.
- [ ] 4.3 Create `app/services/hybrid_retriever.py` with three-stage retrieval (vector seed → typed expansion → graph-aware re-rank). Surface weights in `app/config.py`.
- [ ] 4.4 Swap each agent in `app/services/agents.py` to use `hybrid_retriever.retrieve(...)`; keep static discovery queries from Phase 2 unchanged.
- [ ] 4.5 Tests:
  - [ ] `test_hybrid_retriever_ordering` — supported assumptions outrank unsupported ones for an investor-style question.
  - [ ] `test_entity_vector_index_created`.

## Phase 5 — Grounded citations & hallucination filter

- [ ] 5.1 Rewrite `app/prompts/*_audit.md` to require JSON output (`{summary, findings:[{claim, evidence_entity_ids, source_chunk_ids, confidence}]}`).
- [ ] 5.2 Create `app/services/citation_verifier.py` that drops/flags findings whose cited ids aren't in the retrieved context.
- [ ] 5.3 Update `app/pages/05_agents.py` rendering — show verified vs. ungrounded findings with badges.
- [ ] 5.4 Tests `tests/test_citation_verifier.py` — fake LLM hallucinates an entity id → filtered.

## Phase 6 — Entity resolution (SAME_AS first)

- [ ] 6.1 Create `app/services/entity_resolver.py`:
  - [ ] Cluster by `(type, label_embedding)` cosine ≥ 0.92 + name-token Jaccard.
  - [ ] LLM same-as confirmation prompt.
  - [ ] Write `(:Entity)-[:SAME_AS]->(:Entity)` edges (reversible) on approval.
  - [ ] Provide `consolidate(canonical_id)` helper using `apoc.refactor.mergeNodes` behind explicit UI button.
- [ ] 6.2 Extend `app/pages/03_validate_knowledge.py` with a "Resolve duplicates" tab presenting merge proposals + diff.
- [ ] 6.3 Tests `tests/test_entity_resolver.py` — seeded duplicates → resolver proposes merge → approval writes SAME_AS edge.

## Phase 7 — Community summarization (Microsoft GraphRAG-style)

- [ ] 7.1 Add `app/services/community_service.py` — GDS Louvain projection, writes `community_id` onto nodes, builds `(:Community {id, summary, embedding})`.
- [ ] 7.2 Create `VECTOR INDEX community_embedding` (Phase 4 leaves a stub).
- [ ] 7.3 Add `app/pages/08_communities.py` — community list ranked by size / risk exposure.
- [ ] 7.4 Hybrid retriever routes global questions to community summaries, local questions to nodes.
- [ ] 7.5 Tests — community detection deterministic on a fixed seed graph.

## Phase 8 — Polish, MCP, docs

- [ ] 8.1 Temporal filters in discovery + Graph Explorer (`as_of(date)` slider, deadline windows).
- [ ] 8.2 `PROFILE` telemetry expander on agent results.
- [ ] 8.3 MCP servers in `app/mcp/` (`neo4j_server.py`, `qdrant_server.py`) exposing `query_graph`, `semantic_search`, `discovery_query`.
- [ ] 8.4 README rewrite framed around five pillars: constrained extraction, deterministic discovery, ontology-guarded text2Cypher, grounded citations, bi-temporal audit trail.
- [ ] 8.5 Sample dataset upgrade — deliberately contradictory pitch deck under `sample_data/` so discovery agents surface drama on first run.

---

## Open follow-ups (parking lot)

- Decide eventual retirement of Qdrant once Neo4j vector index proves out for entity + community summaries.
- Decide whether to enforce Ollama JSON-mode with `outlines` / `instructor` if local models drift.
- Decide whether `SAME_AS` should ever auto-promote to hard merge after N independent confirmations.
