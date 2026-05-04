# Step-by-Step User Guide

This guide explains how to run the frontend end-to-end, including what each page, button, and option does.

## 0) Prerequisites

1. Start services:

```bash
docker compose up -d --build
```

2. Pull Ollama models once:

```bash
docker exec fair_vcg_mentor_ollama ollama pull llama3.1:8b
docker exec fair_vcg_mentor_ollama ollama pull nomic-embed-text
```

3. Open frontend:

- http://localhost:8501

## 1) Home page

Purpose:

- Understand the pipeline before interacting with data.

What to do:

1. Read the core loop and MVP workflow summary.
2. Use sidebar navigation from top to bottom.

## 2) Document Upload page

You have two tabs.

### Tab A: Upload files

Use when you want to upload one or a few documents manually.

Controls:

- Upload source documents:
  - Accepts multiple files.
  - Supported types include PDF, DOCX, CSV, TXT, MD, HTML, PPTX, XLSX.

What happens after upload:

1. Original file is stored (deduplicated by SHA-256).
2. Text is extracted.
3. Markdown document is created in vault.
4. Staging document metadata is recorded.

Messages:

- Ingested: file processed successfully.
- Failed: file could not be ingested.

### Tab B: Ingest folder

Use when you have many files in a local or mounted directory.

Controls:

- How to make your folder available:
  - Shows mount instructions for Docker users.
- Folder path:
  - Server-side path (inside container context for Docker workflows).
- Scan folder:
  - Recursively scans and ingests supported files.

System behavior:

- Skips Google Workspace shortcut files (.gdoc/.gsheet/.gslides).
- Skips unsupported file types.
- Shows summary and a detailed ingest log.

Drive handoff behavior:

- If you just ran ontology Init, folder path can be prefilled automatically.
- You may see a button: Export from Drive and ingest now.
- That button opens the Drive Sync page.

## 2.5) Drive Sync page

Purpose:

- Export Google Drive folders to local files before ingestion.

Controls:

- Drive folder ID
- Service account JSON path
- Local export output folder
- Target export formats
- Include non-Google files checkbox
- Export Drive folder button

What happens:

1. Google-native files are detected by MIME type.
2. Files are exported in selected formats.
3. A manifest is written with exported/skipped/failed items.
4. The exported output folder is saved as the next suggested Upload folder.

Next step:

- Click Open Upload page and run Ingest folder on the exported directory.

## 3) Extracted Documents page

Purpose:

- Review markdown outputs and run LLM candidate extraction.

Controls:

- Document dropdown:
  - Select markdown document to inspect.
- Download Markdown:
  - Download selected markdown file.
- Extracted plain text expander:
  - View source text used for extraction.

LLM extraction controls:

- Extract selected document:
  - Runs extraction only for selected document.
- Batch latest documents:
  - Set number of recent documents to process.
- Extract batch:
  - Runs extraction over selected batch size.

Result:

- Candidate entities and relations are written to staging JSON for human review.

## 4) Validate Knowledge page

Purpose:

- Human review gate before Neo4j writes.

Key rule:

- Only records with status validated can be written to Neo4j.

Sidebar controls:

- Knowledge JSON directory:
  - Choose where validation JSON files are loaded/saved.
- Seed demo candidates:
  - Inserts demo entities and relations.
- Filters:
  - Status, Evidence grade, Entity type, Source document.

Statuses:

- pending
- validated
- rejected
- needs_review
- needs_more_evidence

### Entities tab

Controls:

- Focused candidate:
  - Pick one candidate for focused review.
- Decision:
  - Set validation status.
- Reviewer confidence:
  - strong, moderate, weak, ungraded.
- Reviewer note:
  - Add rationale/comment.
- Save focused review:
  - Saves selected candidate.
- Approve all pending:
  - Bulk validates pending entities.
- Reject all low-grade:
  - Rejects pending speculation-grade entities.
- Save entity validations:
  - Saves edits from the table editor.

### Relations tab

Controls:

- Same focused review flow as entities (without reviewer confidence).
- Approve all pending for relations.
- Save relation validations for table edits.

Best practice:

- Use needs_more_evidence when source support is weak.
- Reserve validated for evidence-backed records.

## 5) Graph Explorer page

Purpose:

- Write validated data to Neo4j and inspect the graph.

Controls:

- Write validated JSON to Neo4j:
  - Writes validated entities/relations only.
- Entity labels filter:
  - Restrict node types shown.
- Relations filter:
  - Restrict edge types shown.
- Max relations slider:
  - Limit graph size rendered.
- Load graph:
  - Fetch and render graph snapshot.
- Graph data expander:
  - View node and edge tables.
- Audit recent writes:
  - Inspect latest write activity.

Typical sequence:

1. Click Write validated JSON to Neo4j.
2. Set filters (optional).
3. Click Load graph.

## 6) Agent Audits page

Purpose:

- Run read-only workflows combining Neo4j context and Qdrant snippets.

Controls:

- Workflow selector:
  - Unsupported Assumption Agent
  - Pitch Audit
  - Assumption Audit
  - Customer Discovery Agent
  - Due Diligence Checklist Agent
  - Next Experiment Suggestion Agent
  - Grant Strategy
- Index vault in Qdrant:
  - Embeds/indexes vault content for retrieval.
- Run workflow:
  - Runs selected workflow and saves markdown audit output.

Results:

- Audit saved to vault audits directory.
- Availability warnings shown if Neo4j/Qdrant/Ollama are unavailable.

## 7) Exports page

Purpose:

- Generate portable output bundle from validated knowledge.

Controls:

- Create export bundle:
  - Builds all export artifacts.

Artifacts:

- manifest.json
- graph.json
- graph.jsonld
- assumptions.csv
- evidence_matrix.csv
- risk_register.csv
- audits folder
- export zip bundle

Guardrail:

- Export fails with a clear message if no validated knowledge exists.

## 8) Recommended quick run for new users

1. Upload 1-3 documents.
2. Extract selected document first (quality check).
3. Validate a small set of entities/relations.
4. Write validated JSON to Neo4j.
5. Load graph.
6. Run one audit workflow.
7. Create export bundle.
