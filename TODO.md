# FounderGraph-Lab — Fix Tracker

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
- ✅ Fix prompt placeholders — `_build_prompt` substitutes `{{document_text}}`, `{{document_metadata}}`, `{{entities_json}}`, `{{entity_types}}`; `extract_entities.md` uses `{{entity_types}}` generated live from `OntologyConfig`
- ✅ Fix `save_json` atomic — `validation_store.py` now uses `.tmp` → rename
- ✅ Fix CWD-relative paths in `file_store.py` — imports anchored paths from `app.config`; `append_document_record` atomic; dead fallback `SourceDocument` dataclass and `_compatible_payload` removed
- ✅ Fix CWD-relative paths in `export_service.py` — imports from `app.config`; `export_all` raises `ValueError` instead of silently serving fake sample data
- ✅ Fix null node ID in graph visualizer — skips nodes without `id`
- ✅ Fix relation staging silent drop — `_write_candidates` generates compound IDs (`src:pred:tgt`) for id-less relations
- ✅ Create `app/services/ontology_validator.py` — deterministic pre-validation gate; violations written atomically to `data/staging/shacl_violations.json`; integrated into `extract_to_staging`
- ✅ Add `tests/test_ontology_validator.py` (11 tests); 41 tests total

---

## Reliability

- ✅ Add Docker healthchecks — `cypher-shell` probe on Neo4j; app waits for live DB
- ✅ Pin `neo4j:community` image to `neo4j:5.20-community`
- ✅ Fix demo seed timestamps — move `_now()` calls inside `seed_demo_candidates()` not module-level
- ✅ Cache `ValidationStore.load_entities()` in `st.session_state` keyed by file mtime

---

## Architecture / Design

- ✅ Decouple extraction from upload — three LLM calls moved off the upload handler; triggered explicitly on the Extracted Documents page
- ✅ Add extraction step progress feedback — `st.status()` shows classify / extract entities / extract relations steps
- 🟡 Fix dual `status` + `validation_status` fields — pick `validation_status`, normalise on load, remove duplicate
- 🟡 Fix stale Qdrant chunks on document re-upload — DELETE by `document_id` filter before re-upserting
- 🟡 Add per-document staging index — `data/staging/{doc_id}/` subdirectory layout instead of flat files
- 🟡 Add entity deduplication pass — embed candidate labels, cosine-similarity clustering, LLM confirm, merge before human review

---

## UX

- ✅ Add service health panel to Home page — green/red row for Neo4j / Qdrant / Ollama
- 🟡 Add confidence-stratified review UI — fast-approve lane / standard editor / detailed diff view with source snippet
- 🟢 Replace PyVis graph explorer with interactive component (`streamlit-agraph`) — click-to-inspect, node expansion, type filtering

---

## Neurosymbolic / Standards

- 🔴 Replace `OntologyService` with LinkML schema — one YAML compiles to Pydantic v2, JSON-Schema, SHACL; eliminates three-way drift; add `make generate` step
- 🔴 Add pySHACL deterministic pre-validation gate — structural constraint checks (endpoint types, required fields, predicate–type combinations) before human review queue
- 🟡 Add MCP servers for Neo4j and Qdrant — `app/mcp/neo4j_server.py` + `app/mcp/qdrant_server.py`; expose `query_graph`, `get_unsupported_assumptions`, `semantic_search` as MCP tools
- 🟡 Add bi-temporal provenance — `valid_from`, `valid_to`, `superseded_by` on entity/relation schema; evaluate Graphiti (Zep AI) before rolling manually

---

## Developer / Operational Experience

- 🟢 Expand audit prompts — `assumption_audit.md` and `pitch_audit.md` to ~1 page each with section headers, output format spec, word-count target, and "cite source snippet" instruction
- 🟢 Add `app/services/__init__.py` — re-export main entry points of each service module
