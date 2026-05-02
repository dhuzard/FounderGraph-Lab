# FounderGraph Lab — Fix Tracker

Items from assessment, code review, and architectural analysis.
Legend: ✅ done · 🔴 high · 🟡 medium · 🟢 low

---

## Completed

- ✅ Pin all 18 packages in `requirements.txt` with `>=` lower bound and `<` major upper bound
- ✅ Fix CWD-relative `PROMPT_DIR` and `AUDIT_DIR` in `agents.py`
- ✅ Fix staging overwrite — `_write_candidates` now merges by `id` instead of replacing
- ✅ Remove `_json_request` cross-module import from `agents.py`; use inline `urllib.request`
- ✅ Replace Cypher prefix-string guard with `READ_ACCESS` session in `agents.py`
- ✅ Add 50-record pagination to `03_validate_knowledge.py`
- ✅ Add `truncated` warning to `graph_snapshot` in `neo4j_service.py` + surface in UI
- ✅ Add `LLMService` protocol to `llm_service.py` (pluggable provider abstraction)
- ✅ Create `tests/conftest.py` — extract `FakeLLM`, `FakeDriver`, `fake_neo4j_service` fixture
- ✅ Create `app/services/ontology_service.py` — YAML as single source of truth for allowlists
- ✅ Wire `Neo4jService.__init__` to derive allowlists from ontology YAML by default
- ✅ Build `scripts/init_ontology.py` HITL CLI (`make init`)
- ✅ Add `make init` to `Makefile`
- ✅ Add `tests/test_ontology_service.py` (9 tests)

---

## Bugs

- 🔴 **Prompt placeholders never substituted** — `_build_prompt` in `entity_extractor.py:257`
  appends context as trailing `INPUT_JSON:` JSON but never replaces `{{document_text}}`,
  `{{document_metadata}}`, `{{entities_json}}` in the prompt templates. The LLM sees
  unfilled `{{...}}` markers on every call. Either do the substitution or remove the
  dead placeholders from the prompt files.

- 🔴 **`save_json` not atomic** — `validation_store.py:41` writes directly to
  `validated_entities.json` / `validated_relations.json` without `.tmp` → rename.
  A crash mid-write corrupts the only gate between staging and Neo4j. Apply the
  same atomic-write pattern used in `entity_extractor.py`.

- 🔴 **CWD-relative paths in `file_store.py` and `export_service.py`** — `BASE_DATA_DIR`,
  `VAULT_DOCUMENTS_DIR`, `EXPORT_DIR`, `AUDIT_DIR`, `VALIDATED_ENTITIES_PATH` all use
  relative string defaults. Use `Path(__file__).resolve().parents[2]` as the anchor,
  same fix applied to `agents.py`.

- 🔴 **Export silently falls back to sample data** — `export_service.py:62`: when no
  validated knowledge exists, `export_all` silently substitutes the fictional Metadatapp
  sample graph with no warning. A user gets a ZIP of fake data. Drop the fallback or
  raise a clear error.

- 🔴 **Extraction prompt not regenerated from ontology YAML** — `extract_entities.md`
  has a hardcoded list of 18 entity types. Custom types added via `make init` are written
  to the YAML and enforced in Neo4j, but the LLM will never extract them because the
  prompt still lists only the originals. Generate the allowed-types section of the
  extraction prompt dynamically from `OntologyConfig` at extraction time.

- 🟡 **`append_document_record` not atomic** — `file_store.py:82` reads, de-dupes, and
  writes `documents.json` as three separate operations. Two concurrent uploads can
  overwrite each other's entry. Use `.tmp` → rename.

- 🟡 **Null node ID in graph visualizer** — `graph_visualizer.py:22`: `str(node.get("id"))`
  returns `"None"` when `id` is absent. All such nodes collide at one key in PyVis,
  silently dropping them. Guard with `if not node.get("id"): continue`.

---

## Reliability

- 🟡 **No Docker healthchecks** — `depends_on` in `docker-compose.yml` only waits for
  container start, not Neo4j readiness (takes 15–30 s). Add a `healthcheck` with
  `neo4j-admin server status` or `cypher-shell` probe so the app waits for a live DB.

- 🟡 **Unpinned `neo4j:community` image** — will pull whatever is current at build time,
  including future breaking major versions. Pin to `neo4j:5.20-community` or the
  tested minor version.

- 🟢 **Demo seed timestamps frozen at import time** — `DEMO_ENTITIES` is a module-level
  constant; `_now()` is called once at import, giving all demo records the same
  timestamp. Move `_now()` calls inside `seed_demo_candidates()`.

- 🟢 **`ValidationStore.load_entities()` re-normalizes on every render** — the
  validation page calls `store.load_entities()` twice per Streamlit cycle (metrics +
  editor). Each call re-reads and re-merges all staging JSON. Cache the result in
  `st.session_state` keyed by file mtime.

---

## Architecture / Design

- 🔴 **Synchronous extraction blocks the upload page** — three sequential LLM calls
  (classify → entities → relations) run inside the Streamlit upload handler. A large
  PDF freezes the tab for 3–5 minutes and can hit Streamlit's request timeout. Decouple
  upload from extraction: upload stores the file, extraction is triggered by an explicit
  button on the Extracted Documents page or via a background thread.

- 🟡 **Dual `status` + `validation_status` fields** — every entity and relation carries
  both fields with identical values, synchronized by scattered
  `item.get("status", item.get("validation_status"))` guards across four modules.
  Pick one field name (`validation_status`), normalise old data on load, remove the
  duplicate.

- 🟡 **Stale Qdrant chunks not deleted on document re-index** — `qdrant_service.py`
  upserts chunks with stable IDs (idempotent) but never deletes chunks from a previous
  version of the same document when it is re-uploaded or modified. Issue a
  `DELETE /collections/{col}/points?filter={"must":[{"key":"document_id","match":{"value":...}}]}`
  before re-upserting.

- 🟡 **No entity deduplication in staging** — the same concept extracted from two
  documents creates two unlinked staging entities with no merge signal. Add a
  deduplication pass: embed candidate labels with `QdrantService.embed()`, find
  cosine-similar pairs above a threshold, confirm with a short LLM call, then merge
  before the human review step. (Pattern observed in CocoIndex `resolve_entities`.)

- 🟢 **Dead fallback `SourceDocument` in `file_store.py`** — the `try/except ImportError`
  block defines an inline dataclass that is never reachable because `app.models.document`
  is always importable. Remove it and the `_compatible_payload` hack it enables.

- 🟢 **No per-document staging index** — all entities from all documents merge into one
  flat `candidate_entities.json`. There is no way to filter or re-extract a single
  document's candidates without scanning the entire file. Consider a
  `data/staging/{doc_id}/` subdirectory layout.

---

## Neurosymbolic / Standards Upgrades
*(from architectural review — prioritised by implementation effort vs. value)*

- 🔴 **Replace `OntologyService` with LinkML schema** — the current custom
  `ontology_service.py` hand-rolls what LinkML provides: one YAML source that
  compiles to Pydantic v2 models (`gen-pydantic`), JSON-Schema, and SHACL shapes
  (`gen-shacl`). This eliminates the three-way drift between `entity.py` (Literal),
  `startup_ontology.yaml`, and `neo4j_service.py` allowlists. Add `linkml` +
  `linkml-runtime` to requirements. Replace `app/ontology/startup_ontology.yaml`
  with a LinkML `.yaml` schema. Run `gen-pydantic` + `gen-shacl` as a `make generate`
  step and commit the outputs. W3C / FAIR compliance is a bonus; the main gain is
  eliminating schema drift.

- 🔴 **Add pySHACL deterministic pre-validation gate** — insert a validation step
  between LLM extraction and the human review queue. Use `pySHACL` with the
  LinkML-generated SHACL shapes to auto-reject candidates that violate ontological
  constraints (wrong relation endpoint types, missing required fields, nonsensical
  predicate–entity-type combinations). Humans should only review data already proven
  structurally and ontologically sound. Add `pyshacl` to requirements. Run as a
  post-extraction step in `entity_extractor.py::extract_to_staging`; write rejected
  candidates with violation details to `data/staging/shacl_violations.json`.

- 🟡 **Redesign validation UI with confidence-stratified review** — the flat
  `st.data_editor` table produces rubber-stamp approvals at scale (VeriLA research
  finding: humans approve ~70% of AI outputs when the process "looks reasonable").
  Stratify the review queue: SHACL-valid + high-confidence candidates auto-advance
  to a fast-approval lane; medium-confidence go to the standard editor; low-confidence
  and SHACL-flagged go to a detailed diff view showing source snippet alongside each
  extracted field. Add an "LLM inference" badge on fields where no direct source quote
  exists. Build in Streamlit with `st.tabs` for each confidence tier.

- 🟡 **Add MCP servers for Neo4j and Qdrant** — expose the graph and vector store
  as MCP tools so agents (including Claude Code sessions and external LLM clients)
  can query the knowledge graph without bespoke integration code. Create
  `app/mcp/neo4j_server.py` and `app/mcp/qdrant_server.py` using the MCP Python SDK.
  Expose `query_graph`, `get_entity`, `get_unsupported_assumptions`,
  `get_risks_by_milestone` as tools for Neo4j; `semantic_search` for Qdrant.
  Add `make mcp` target to `Makefile`.

- 🟡 **Add bi-temporal provenance to entity and relation schema** — startup facts
  change rapidly (valuations, board members, assumptions validated/invalidated). The
  current schema tracks only `updated_at` (transaction time). Add `valid_from` and
  `valid_to` (valid time — when the fact was true in the world) and `superseded_by`
  (relation to a newer version of the same entity). Evaluate **Graphiti** (Zep AI,
  open-source, Neo4j-native bi-temporal memory) as a drop-in before rolling manually.
  Add to ontology YAML and `ensure_schema()` constraints. Surface in the audit agents
  as "stale facts" alerts.

- 🟢 **Dynamic extraction prompt from YAML schema** — (overlaps with bug #5 above,
  listed here for architectural context) OntoGPT/SPIRES is not a fit for general
  startup documents (biomedical-first tooling), but its core principle — grounding
  extraction in the schema — applies. When `entity_extractor.py` builds the entity
  extraction prompt, inject the full entity type list, field definitions, and
  descriptions from `OntologyConfig` rather than using a hardcoded prompt template.
  Custom types added via `make init` then automatically become extractable without
  any prompt edits.

- 🟢 **Evaluate CocoIndex as ingestion layer** — CocoIndex (`Target = F(Source)`)
  is compatible with FounderGraph Lab's HITL model if scoped to the extraction-to-
  staging half of the pipeline only. The boundary: CocoIndex owns
  `files → LLM extraction → candidate_*.json`; FounderGraph Lab's `ValidationStore`
  and `Neo4jService` own `candidate_*.json → human review → validated → Neo4j`.
  Key prerequisite: write a custom CocoIndex `TargetConnector` that serialises
  extracted records to FounderGraph Lab's staging JSON format. Main gains: incremental
  memoization (unchanged docs never re-extracted), parallel async extraction,
  `instructor`-backed structured output, entity resolution across documents.
  Main risk: CocoIndex re-runs on prompt changes overwrite in-progress human reviews
  in staging — mitigate with `extraction_run_id` tagging.

---

## Developer / Operational Experience

- 🟢 **No `.dockerignore`** — `COPY . .` in the Dockerfile includes `tests/`, `sample_data/`,
  `vault/`, `data/`, assessment `.md` files, and any local `.env`. Add `.dockerignore`
  to exclude these from the image.

- 🟢 **No service health panel on home page** — `app/main.py` is static text. Add a
  connection health row (Neo4j / Qdrant / Ollama — green/red) so users know immediately
  whether services are reachable before navigating deeper.

- 🟢 **Audit prompts too thin** — `assumption_audit.md` and `pitch_audit.md` are ~5
  bullet points each with no output format spec, no depth guidance, and no explicit
  grounding rules beyond what `run_agent_workflow` appends. Expand each to ~1 page:
  section headers, example output structure, word-count target, and explicit
  "cite source snippet" instruction.

- 🟢 **No `app/services/__init__.py`** — every import uses the full
  `app.services.X` path with no declared public API. Add a minimal `__init__.py`
  that re-exports the main entry points of each service module.
