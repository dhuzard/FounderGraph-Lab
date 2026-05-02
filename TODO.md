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
