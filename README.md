# FounderGraph-Lab

A local-first, human-in-the-loop Streamlit application that converts raw startup documents into a validated, queryable Neo4j knowledge graph. Every piece of LLM-extracted knowledge passes through an explicit human review gate before entering the graph.

```
startup files
    └─▶ text extraction & Markdown vault
            └─▶ LLM classification + entity/relation extraction
                    └─▶ ontology pre-validation (type + predicate gate)
                            └─▶ human review (approve / reject / flag)
                                    └─▶ Neo4j graph  ←  Qdrant vector index
                                                └─▶ audit agents + export
```

**Key guarantee:** LLM output never reaches Neo4j without explicit human approval.

## Start here (new users)

Use the step-by-step guide before your first run:

- [Step-by-Step User Guide](STEP_BY_STEP_USER_GUIDE.md)

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Ontology](#ontology)
4. [Services reference](#services-reference)
5. [Pages reference](#pages-reference)
6. [Step-by-step user guide](#step-by-step-user-guide)
7. [Quick start (Docker)](#quick-start-docker)
8. [Local development](#local-development)
9. [Google Drive export pipeline](#google-drive-export-pipeline)
10. [Initializing the ontology](#initializing-the-ontology)
11. [Environment variables](#environment-variables)
12. [Data layout](#data-layout)
13. [Running tests](#running-tests)
14. [Security model](#security-model)
15. [Validated knowledge pipeline](#validated-knowledge-pipeline)
16. [Future UX TODO (new user comprehension)](#future-ux-todo-new-user-comprehension)
17. [Sample data](#sample-data)

---

## What it does

FounderGraph-Lab ingests any combination of pitch decks, business plans, customer interview notes, grant applications, technical roadmaps, and meeting notes, and turns them into a structured graph of startup knowledge:

- **Entities** — typed graph nodes (Startup, Founder, Assumption, Evidence, Risk, Milestone, etc.)
- **Relations** — typed graph edges (TARGETS, SUPPORTED_BY, CONTRADICTED_BY, THREATENS, FUNDS, etc.)
- **Provenance** — every node and edge stores the source document ID, source file path, and a source snippet from the original text
- **Audit trails** — LLM agents query the live graph to surface unsupported assumptions, investor-readiness gaps, and grant alignment

### Core capabilities

| Capability | Description |
|---|---|
| Multi-format ingestion | PDF (PyMuPDF), DOCX (python-docx), TXT, Markdown |
| LLM extraction | Classify → extract entities → extract relations via Ollama |
| Ontology pre-validation | Auto-reject candidates with unknown types or predicates before human review |
| Human-in-the-loop review | Paginated Streamlit editor; four-state status workflow |
| Graph persistence | Neo4j with parameterized Cypher, label allowlists, and relation allowlists |
| Semantic search | Qdrant vector store with `nomic-embed-text` embeddings |
| Audit agents | LangGraph-backed structured agents with read-only graph access |
| Export | ZIP bundle: graph JSON, JSON-LD, assumptions CSV, evidence matrix CSV, risk register CSV, audit reports |
| Ontology CLI | Interactive HITL CLI (`make init`) to configure entity types and relations from real documents |

---

## Architecture

### Tech stack

| Layer | Technology |
|---|---|
| UI | Streamlit 1.32+ |
| LLM inference | Ollama (`llama3.1:8b` by default) |
| Embeddings | Ollama (`nomic-embed-text`) |
| Graph database | Neo4j Community 5.x |
| Vector store | Qdrant |
| Agent framework | LangGraph + LangChain Core |
| Data validation | Pydantic v2 |
| Schema | YAML ontology (`app/ontology/startup_ontology.yaml`) |
| Containerisation | Docker Compose |

### Data flow

```
Upload (01_upload.py)
  │
  ├─ store_original()          ← SHA-256 dedup, atomic temp-file write
  ├─ extract_text_from_path()  ← PDF/DOCX/TXT → plain text
  ├─ document_to_markdown()    ← structured Markdown → vault/documents/
  └─ append_document_record()  ← atomic JSON append to data/staging/documents.json

EntityExtractor.extract_to_staging()
  │
  ├─ classify_document()       ← LLM call 1: document type + tags
  ├─ extract_entities()        ← LLM call 2: typed entity list with source snippets
  ├─ extract_relations()       ← LLM call 3: typed relation list
  ├─ OntologyValidator.validate()
  │     ├─ check entity types against OntologyConfig.allowed_labels()
  │     ├─ check predicates against OntologyConfig.allowed_relationships()
  │     └─ write violations → data/staging/shacl_violations.json (atomic)
  └─ _write_candidates()       ← atomic merge-by-id into candidate_*.json

ValidationStore (03_validate_knowledge.py)
  │
  ├─ load_entities() / load_relations()   ← merge candidates + reviewed records
  ├─ human edits status field (pending / validated / rejected / needs_review / needs_more_evidence)
  └─ save_entities() / save_relations()   ← atomic write to validated_*.json

Neo4jService.upsert_validated_knowledge()
  │
  ├─ _require_validated()      ← refuses non-validated records
  ├─ _safe_label()             ← allowlist check + SAFE_LABEL regex
  ├─ _safe_relationship()      ← allowlist check + SAFE_TOKEN regex
  └─ parameterized MERGE queries (no dynamic Cypher)
```

### Atomicity

All JSON persistence uses `.tmp` → `rename` (atomic on POSIX):

- `EntityExtractor._atomic_write_json()` — candidate staging files
- `ValidationStore.save_json()` — validated entities/relations
- `FileStore.append_document_record()` — documents index
- `OntologyValidator._write_violations()` — violation log
- `OntologyService.save_ontology()` — ontology YAML

---

## Ontology

The ontology is defined in `app/ontology/startup_ontology.yaml` and is the **single source of truth** for:

- Which entity types the LLM is prompted to extract (`{{entity_types}}` injected at runtime)
- Which labels Neo4j is allowed to write (enforced in `Neo4jService._safe_label()`)
- Which relationship types Neo4j is allowed to write (enforced in `Neo4jService._safe_relationship()`)
- Which candidates the `OntologyValidator` admits to staging

### Default entity types (20)

| Type | Description |
|---|---|
| `Startup` | The startup or project being analyzed |
| `Founder` | A founder or key team member |
| `CustomerSegment` | A group of potential users, buyers, or beneficiaries |
| `Problem` | A pain point or unmet need |
| `ValueProposition` | The promised value delivered by the startup |
| `ProductFeature` | A technical or functional product component |
| `Assumption` | A claim not yet fully proven |
| `Evidence` | A source-backed observation (interview, metric, quote, result) |
| `Risk` | A business, technical, regulatory, market, or execution risk |
| `Experiment` | A test designed to validate or invalidate an assumption |
| `Decision` | A strategic or operational decision |
| `Milestone` | A time-bound objective |
| `GrantCall` | A funding opportunity |
| `Investor` | An investor or investment organization |
| `Partner` | A partner organization |
| `Competitor` | A competing product, company, or workaround |
| `IPAsset` | An intellectual property asset |
| `RegulatoryConstraint` | A regulatory or compliance constraint |
| `TechnicalDependency` | A technical dependency or integration requirement |
| `FinancialHypothesis` | A financial forecast or business model hypothesis |

### Default relation types (17)

`RELATED_TO` · `TARGETS` · `HAS_PROBLEM` · `ADDRESSES` · `BASED_ON` · `SUPPORTED_BY` · `CONTRADICTED_BY` · `TESTS` · `GENERATES` · `THREATENS` · `FUNDS` · `PROVIDES` · `COMPETES_ON` · `PROTECTS` · `MENTIONS` · `SOURCE_OF` · `DEPENDS_ON`

### Customising the ontology

Run `make init` for an interactive CLI that:
1. Asks about your startup's domain and goals
2. Discovers documents in your vault
3. Runs LLM analysis to propose domain-specific types
4. Lets you add, remove, or rename entity classes and relations interactively
5. Saves the result to `startup_ontology.yaml` and optionally initializes Neo4j schema constraints

Custom types added via `make init` are immediately reflected in extraction prompts — no code changes required.

---

## Services reference

### `app/services/file_store.py`

Handles file ingestion end-to-end.

| Function | Description |
|---|---|
| `ingest_document(uploaded_file, filename)` | Full pipeline: store → extract text → convert to Markdown → record |
| `store_original(uploaded_file, filename)` | SHA-256 streaming hash, atomic temp-file rename, dedup by digest |
| `build_source_document(...)` | Constructs a `SourceDocument` Pydantic model |
| `append_document_record(source_document)` | Atomic read-dedup-write to `data/staging/documents.json` |
| `ensure_storage_dirs()` | Creates `data/original_files/`, `data/extracted_text/`, `vault/documents/` |

Supported formats: PDF (`.pdf`), DOCX (`.docx`), plain text (`.txt`), Markdown (`.md`).

---

### `app/services/entity_extractor.py`

LLM-driven staging extraction. **Never opens a Neo4j connection.**

| Method | Description |
|---|---|
| `classify_document(text, metadata)` | Classifies document type (PitchDeck, CustomerInterview, etc.) |
| `extract_entities(text, metadata)` | Extracts typed entity candidates with source snippets |
| `extract_relations(text, entities, metadata)` | Extracts typed relation candidates referencing entity IDs |
| `extract_to_staging(text, metadata)` | Runs all three LLM calls, validates, and writes to staging |
| `_build_prompt(prompt_name, text, metadata, extra_context)` | Substitutes `{{document_text}}`, `{{document_metadata}}`, `{{entities_json}}`, `{{entity_types}}` into prompt templates |
| `_entity_types_block()` | Generates the entity type list from `OntologyConfig` at runtime |

Prompts live in `app/prompts/`. The `{{entity_types}}` placeholder is filled dynamically from the YAML ontology, so custom types are automatically extractable.

Staging files use atomic merge-by-id writes — re-uploading a document updates existing candidates without losing other documents' extractions.

---

### `app/services/ontology_validator.py`

Deterministic pre-validation gate between LLM extraction and human review.

Checks each candidate against the live `OntologyConfig`:

**Entity checks:**
- `missing-id` — no `id` or `temporary_id`
- `missing-label` — no `label` or `name`
- `missing-type` — no `type` field
- `unknown-type` — type not in `OntologyConfig.allowed_labels()`

**Relation checks:**
- `missing-source` / `missing-target` — no source or target entity ID
- `missing-predicate` — no `predicate` or `type`
- `unknown-predicate` — predicate not in `OntologyConfig.allowed_relationships()`

Invalid candidates are removed from the staging batch. All violations are appended atomically to `data/staging/shacl_violations.json` with the full candidate payload for diagnosis. If the validator itself errors, the pipeline falls back to staging all candidates (never blocks ingestion).

---

### `app/services/validation_store.py`

JSON-based staging store for the human review gate.

| Method | Description |
|---|---|
| `load_entities()` | Merges candidate JSON with reviewed JSON by entity `id` |
| `load_relations()` | Same for relations |
| `save_entities(records)` | Atomic write to `data/knowledge/validated_entities.json` |
| `save_relations(records)` | Atomic write to `data/knowledge/validated_relations.json` |
| `validated_entities()` | Returns only records with `validation_status == "validated"` |
| `validated_relations()` | Same for relations |

Validation statuses: `pending` · `validated` · `rejected` · `needs_review` · `needs_more_evidence`

---

### `app/services/neo4j_service.py`

Graph persistence with strict safety controls.

| Method | Description |
|---|---|
| `ensure_schema()` | Creates uniqueness constraints and indexes |
| `upsert_document(document)` | Writes a Document node |
| `upsert_entity(entity)` | Writes an Entity node (validated records only) |
| `upsert_relation(relation)` | Writes a typed relationship (validated records only) |
| `upsert_validated_knowledge(entities, relations, documents)` | Batch upsert |
| `graph_snapshot(labels, relationship_types, limit)` | Returns nodes + edges for visualisation |
| `get_unsupported_assumptions()` | Assumptions with no `SUPPORTED_BY` evidence link |
| `get_risks_by_milestone()` | Risks grouped by threatened milestone |
| `audit_recent_writes(limit)` | Most recently written nodes ordered by `updated_at` |

Safety guarantees:
- `_require_validated()` — refuses any record without `validation_status == "validated"`
- `_safe_label()` — label must match `^[A-Z][A-Za-z0-9_]*$` and be in `allowed_labels`
- `_safe_relationship()` — type must match `^[A-Z][A-Z0-9_]*$` and be in `allowed_relationships`
- All Cypher uses `$parameters` — no string interpolation of user data
- Neo4j driver read-only session used by audit agents (no write risk)

Allowlists are derived from `OntologyConfig` at startup; no hardcoded sets required.

---

### `app/services/ontology_service.py`

YAML-as-single-source-of-truth for the ontology.

| Function / Method | Description |
|---|---|
| `load_ontology(path)` | Parses YAML into `OntologyConfig`; returns empty config if file missing |
| `save_ontology(config, path)` | Atomic YAML write (`.tmp` → rename) |
| `OntologyConfig.allowed_labels()` | Returns `{"Entity", "Document"} ∪ entity_class_names` |
| `OntologyConfig.allowed_relationships()` | Returns `{relation_predicates} ∪ utility_relations` |
| `OntologyConfig.add_entity_class(name, description, fields)` | Adds a new class |
| `OntologyConfig.remove_entity_class(name)` | Removes a class |
| `OntologyConfig.rename_entity_class(old, new)` | Renames a class, preserving definition |

---

### `app/services/agents.py`

LangGraph-backed audit agents. Each agent:
1. Pulls a graph snapshot from Neo4j via `READ_ACCESS` session
2. Embeds it as JSON context in the audit prompt
3. Runs the LLM and returns structured Markdown output
4. Saves the report to `vault/audits/`

Available agents:
- **Assumption Audit** — identifies unsupported assumptions, assesses evidence quality
- **Pitch Audit** — evaluates investor-readiness, narrative clarity, traction gaps
- **Grant Strategy** — matches startup profile to grant themes and recommends positioning

---

### `app/services/export_service.py`

Generates portable export bundles from validated knowledge.

| Output | Description |
|---|---|
| `graph.json` | Full graph with nodes and edges |
| `graph.jsonld` | JSON-LD with `fg:` ontology context |
| `assumptions.csv` | Assumption nodes with criticality, evidence_grade, reviewer_confidence, status, owner |
| `evidence_matrix.csv` | Assumption ↔ Evidence links with relationship type, evidence_grade, and source |
| `risk_register.csv` | Risk nodes with severity, probability, impact, mitigation, owner |
| `audits/` | All Markdown audit reports |
| `*.zip` | All of the above in one archive |

Raises `ValueError` if no validated knowledge exists — never silently serves placeholder data.

---

### `app/services/llm_service.py`

Pluggable LLM backend via `LLMService` protocol.

```python
@runtime_checkable
class LLMService(Protocol):
    def generate_text(self, prompt: str) -> str: ...
    def generate_json(self, prompt: str) -> Any: ...
```

`OllamaLLMService` is the default implementation. Swap for any provider by implementing the protocol.

---

## Pages reference

| Page | File | Description |
|---|---|---|
| Home | `app/main.py` | Overview and service status |
| Upload | `app/pages/01_upload.py` | Upload files; triggers text extraction and Markdown conversion |
| Extracted Documents | `app/pages/02_extracted_documents.py` | Browse vault documents; trigger LLM extraction |
| Validate Knowledge | `app/pages/03_validate_knowledge.py` | Paginated HITL editor (50 records/page); write validated records to Neo4j |
| Graph Explorer | `app/pages/04_graph_explorer.py` | Interactive PyVis graph with label/relationship filters |
| Agents | `app/pages/05_agents.py` | Run audit agents; view and download reports |
| Exports | `app/pages/06_exports.py` | Generate and download ZIP export bundle |
| Drive Sync | `app/pages/00_drive_sync.py` | Export Google Drive folder content to local backup files for ingestion |

---

## Step-by-step user guide

For a detailed walkthrough of frontend pages, controls, statuses, and recommended workflow:

- [Step-by-Step User Guide](STEP_BY_STEP_USER_GUIDE.md)

---

## Quick start (Docker)

**Requirements:** Docker Desktop or Docker Engine with Compose V2; ~8 GB RAM recommended for Ollama.

```bash
# 1. Clone and configure
git clone <repo-url>
cd FounderGraph-Lab
cp .env.example .env          # edit if needed (default credentials work as-is)

# 2. Start all services
docker compose up -d --build   # builds app image, starts Neo4j, Qdrant, Ollama, Streamlit

# 3. Pull LLM and embedding models (one-time, ~5 GB)
docker exec FounderGraph-Lab_ollama ollama pull llama3.1:8b
docker exec FounderGraph-Lab_ollama ollama pull nomic-embed-text

# 4. (Optional) Customize the ontology for your startup (Bash)
PYTHONPATH=. python scripts/init_ontology.py
```

If you use Windows PowerShell and do not have GNU Make installed, use the commands above directly.

PowerShell equivalent for step 4:

```powershell
$env:PYTHONPATH='.'
python scripts/init_ontology.py
```

Services:

| Service | URL | Notes |
|---|---|---|
| Streamlit app | http://localhost:8501 | Main UI |
| Neo4j Browser | http://localhost:7474 | Credentials: `neo4j` / `foundergraph_password` |
| Neo4j Bolt | bolt://localhost:7687 | For direct Cypher queries |
| Qdrant dashboard | http://localhost:6333 | Vector store UI |
| Ollama API | http://localhost:11434 | Local LLM API |

Optional Make shortcuts (Linux/macOS/WSL or Windows with Make installed):

```bash
make up            # docker compose up -d --build
make pull-models   # docker exec ... ollama pull ...
make init          # python scripts/init_ontology.py
make reset-demo    # backup + clear previous staging/knowledge/vault docs for a clean demo run
make down          # stop all containers
make logs          # tail all container logs
```

When running `make init` with sample data, the initializer now offers a clean-start prompt that can automatically reset old staging and knowledge artifacts before continuing.

### Troubleshooting: "make up" does not work on Windows

If you see `make: The term 'make' is not recognized`, GNU Make is not installed in your shell.

Use this equivalent command instead:

```bash
docker compose up -d --build
```

You can continue using `docker compose ...` commands directly, or install Make (`choco install make` or `scoop install make`) if you prefer Make targets.

---

## Local development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Point to running services (Docker or local)
cp .env.example .env

# Run the app
streamlit run app/main.py

# Or run a single page directly
streamlit run app/pages/03_validate_knowledge.py
```

---

## Google Drive export pipeline

If your startup source of truth is in Google Drive, export to local files first, then ingest those exported files in the Upload page.

Why this exists:

- The ontology initializer uses documents only for type suggestion during setup and does not persist extracted files.
- The frontend Upload step is the canonical ingestion path that creates extracted text, Markdown vault files, provenance records, and staging JSON.
- Keeping these separate preserves a strict setup phase vs. ingestion phase, but this pipeline bridges the gap for Drive-native sources.

### What the export script does

- Recursively crawls a Google Drive folder by folder ID
- Detects Google-native files by MIME type
- Exports each file to one or more local formats (for example DOCX, PDF, TXT, XLSX, CSV, PPTX, PNG)
- Optionally downloads non-Google files in original format
- Writes `manifest.json` with all exported, skipped, and failed items

Script: `scripts/export_google_drive_folder.py`

### One-time setup

1. Create a Google Cloud project and enable the Google Drive API.
2. Create a service account key JSON file.
3. Share the target Drive folder with the service account email.
4. Install dependencies:

```bash
pip install -r requirements.txt
```

### Run export

```bash
python scripts/export_google_drive_folder.py \
  --folder-id <DRIVE_FOLDER_ID> \
  --output-dir data/drive_backup \
  --service-account-file /path/to/service-account.json \
  --formats docx,pdf,txt,xlsx,csv,pptx,png \
  --include-non-google
```

After export, use Upload -> Ingest folder and point to the exported folder (for example `data/drive_backup`).

---

## Initializing the ontology

Run once (or whenever the startup's domain evolves) to configure entity types and relation predicates for your specific context:

On Bash (Linux/macOS/WSL):

```bash
PYTHONPATH=. python scripts/init_ontology.py
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH='.'
python scripts/init_ontology.py
```

If you have GNU Make available, `make init` is an equivalent shortcut.

The CLI walks through six phases:

1. **Startup context** — domain, maturity stage, primary goals (assumption validation, investor readiness, grant alignment, milestone tracking)
2. **Document discovery** — scans `vault/documents/` and asks which files to analyse
3. **LLM document analysis** — extracts domain-specific concepts from up to 3 representative documents
4. **Entity class review** — proposes new types; you confirm, rename, or reject each
5. **Relation review** — proposes new predicates; you confirm or skip
6. **Save + schema init** — writes `startup_ontology.yaml` atomically; optionally initializes Neo4j constraints and indexes

After initialization, any new entity types are automatically included in the LLM extraction prompt and enforced in the Neo4j allowlist — no code changes needed.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `foundergraph_password` | Neo4j password |
| `NEO4J_DATABASE` | _(default database)_ | Optional database name |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant base URL |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `LLM_MODEL` | `llama3.1:8b` | Ollama model for extraction and agents |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for embeddings |
| `FOUNDERGRAPH_DATA_DIR` | `<project_root>/data` | Override data directory |
| `FOUNDERGRAPH_VAULT_DIR` | `<project_root>/vault` | Override vault directory |

All path defaults are anchored to the project root — the app works correctly regardless of the working directory at startup.

---

## Data layout

```
data/
├── original_files/          # uploaded originals (SHA-256 prefixed filenames)
├── extracted_text/          # plain text extracted from each document
├── staging/
│   ├── documents.json       # index of all ingested documents
│   ├── candidate_entities.json   # LLM-extracted entities (pre-review)
│   ├── candidate_relations.json  # LLM-extracted relations (pre-review)
│   └── shacl_violations.json     # ontology pre-validation rejects
└── knowledge/
    ├── validated_entities.json   # human-approved entities
    └── validated_relations.json  # human-approved relations

vault/
├── documents/               # Markdown version of every ingested document
├── entities/
└── audits/                  # Markdown audit reports from agents

app/
├── main.py
├── config.py                # anchored path constants + ensure_directories()
├── models/
│   └── document.py          # SourceDocument Pydantic model
├── ontology/
│   └── startup_ontology.yaml  ← single source of truth for schema
├── pages/                   # Streamlit pages (01–06)
├── prompts/                 # LLM prompt templates (Markdown with {{placeholders}})
└── services/                # all business logic
    ├── agents.py
    ├── entity_extractor.py
    ├── export_service.py
    ├── file_store.py
    ├── graph_visualizer.py
    ├── llm_service.py
    ├── markdown_converter.py
    ├── neo4j_service.py
    ├── ontology_service.py
    ├── ontology_validator.py
    ├── qdrant_service.py
    └── validation_store.py

scripts/
└── init_ontology.py         # backing script for `make init`
```

---

## Running tests

```bash
make test                    # runs pytest with PYTHONPATH=.
```

The test suite covers (88 tests):

| Module | Tests |
|---|---|
| `test_entity_schema.py` | Entity/relation Pydantic schemas, stable UUID IDs, staging pipeline, confidence quarantine |
| `test_neo4j_service.py` | Label/relationship allowlists, validated-only writes, parameterized Cypher, MENTIONS provenance, endpoint pre-check |
| `test_ontology_service.py` | YAML load/save, allowed_labels, allowed_relationships, add/rename/remove |
| `test_ontology_validator.py` | OntologyLoader drift, staging accumulation, idempotent writes, domain/range enforcement, no-write-before-validation, stable IDs |
| `test_export_service.py` | Field correctness (evidence_grade, probability/impact), direction enforcement, manifest schema |
| `test_end_to_end_pipeline.py` | Full mocked pipeline: extraction → provenance → Neo4j write → export |
| `test_extractors.py` | PDF/DOCX/TXT extraction |
| `test_markdown_converter.py` | Markdown conversion |
| `test_qdrant_service.py` | Vector store upsert and search |

`tests/conftest.py` provides a legacy `fake_neo4j_service` fixture. The high-risk test modules define their own inline `FakeDriver`/`FakeSession` with `known_entity_ids` support required by the endpoint pre-check tests.

Lint:

```bash
make lint                    # ruff check
make format                  # ruff format
```

---

## Security model

| Rule | Implementation |
|---|---|
| LLM output never reaches Neo4j directly | `_require_validated()` in `Neo4jService` rejects any non-validated record |
| No dynamic Cypher | All queries use `$parameter` placeholders; labels and types are validated before interpolation |
| Label allowlist | `_safe_label()` checks regex + `allowed_labels` set derived from ontology YAML |
| Relationship allowlist | `_safe_relationship()` checks regex + `allowed_relationships` set derived from ontology YAML |
| Read-only agent access | Audit agents use `READ_ACCESS` Neo4j session — cannot write even if prompt is hijacked |
| Ontology pre-validation | Unknown types and predicates are rejected before staging, logged to `shacl_violations.json` |
| Atomic writes | All JSON files use `.tmp` → rename; no partial write can corrupt the staging-to-graph gate |
| Source provenance | Every node and edge stores `source_document_id`, `source_file`, `source_snippet` |
| Original files immutable | Uploaded files are stored by digest and never overwritten |

---

## Validated knowledge pipeline

This section summarises the integrity guarantees that hold across the full pipeline. Each guarantee is enforced in code, not just by convention.

**Local-first ingestion.** All files are stored locally by SHA-256 digest before any LLM call. No document is sent to an external service during ingestion.

**LLM extraction produces candidates only.** `EntityExtractor.extract_to_staging()` writes to `data/staging/candidate_entities.json` and `candidate_relations.json`. It never opens a Neo4j connection. A candidate is just a proposal — it has no effect on the graph until a human approves it.

**Ontology pre-validation rejects unknown types and predicates.** Before candidates reach the review screen, `OntologyLoader.validate_relation()` checks every relation against the domain/range map in `startup_ontology.yaml`. Entities with unknown types and relations with unknown predicates are blocked at this gate, not silently admitted.

**Human approval is required before any Neo4j write.** `Neo4jService._require_validated()` raises `Neo4jServiceError` for any record whose `validation_status` (or `status`) is not `"validated"`. A pending, rejected, or needs-more-evidence record cannot reach the graph regardless of how it was constructed.

**`source_document_id` and MENTIONS preserve provenance.** Every entity receives a stable `source_document_id` at extraction time (keyed by `sha256` of the original file). When the entity is written to Neo4j, `_entity_ops()` automatically creates a `MERGE (d:Document)-[:MENTIONS]->(e:Entity)` link using `MERGE` (not `MATCH`), so the provenance edge is created on demand even when the document was not explicitly upserted first.

**Stable UUIDv5 entity IDs prevent phantom duplicates.** `stable_entity_id(doc_id, label, type)` generates a deterministic UUID from `(source_document_id, normalised_label, entity_type)`. Re-extracting the same document produces the same IDs, so Neo4j `MERGE` operations are safe across multiple extraction runs.

**Relation endpoint checks prevent silent graph corruption.** Before any relation write, `_pre_check_relation_endpoints()` verifies that both the source and target entity nodes exist — either in the current batch or already in the graph. A missing endpoint raises `Neo4jServiceError` before `execute_write` is called, so no partial relation can land in the graph.

**`export_all()` raises before any file I/O if no validated knowledge exists.** If both `nodes` and `edges` are empty, `export_all()` raises `ValueError("No validated knowledge found …")` without writing a single file. The caller (the Exports page) surfaces `st.error()` to the user. No placeholder ZIP is ever created.

---

## Future UX TODO (new user comprehension)

- [ ] Add a setup checklist on Home (Neo4j, Ollama, Qdrant, models pulled)
- [ ] Add guided mode banner with current step and next action
- [ ] Add contextual empty-state actions (go to Upload, extract latest doc)
- [ ] Add status legend in Validate Knowledge with decision examples
- [ ] Add evidence-grade rubric tooltip (direct quote/paraphrase/inference/speculation)
- [ ] Add post-action "next step" call-to-action buttons
- [ ] Add one-click quickstart sample flow
- [ ] Add confirmation modal before writing validated JSON to Neo4j
- [ ] Harmonize terminology across pages (candidate, validated, written)
- [ ] Add extraction progress ETA for batch operations

---

## Sample data

`sample_data/` contains documents for a fictional metadata interoperability startup (Metadatapp) for preclinical research:

It also contains a richer live-demo dataset for a fictional clinical trial startup workflow company:

- `trialmesh/` — a multi-folder dataset with strategy, customer interviews, product specs, evidence, finance, regulatory, partnerships, and operations artifacts

Recommended for demos that need more realistic folder structure, conflicting evidence, and broader entity/relation coverage.

| File | Type |
|---|---|
| `pitch_deck_text.md` | Pitch deck (Markdown) |
| `business_plan.md` | Business plan |
| `technical_roadmap.md` | Technical roadmap |
| `financial_assumptions.csv` | Financial model assumptions |
| `customer_interview_cro_01.docx` | Customer interview — CRO |
| `customer_interview_academic_lab_01.docx` | Customer interview — academic lab |
| `metadatapp/graph.json` | Pre-built graph snapshot for the sample startup |
| `trialmesh/README.md` | Rich live-demo dataset guide |

To run the demo:

1. Start services with `make up` and `make pull-models`
2. Upload the sample files via the Upload page
3. Trigger LLM extraction on the Extracted Documents page
4. Review and approve candidates in Validate Knowledge
5. Write to Neo4j and explore the graph
6. Run an Assumption Audit or Pitch Audit
7. Generate an export bundle
