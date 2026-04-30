# FounderGraph Lab

FounderGraph Lab is a local-first Streamlit application that converts startup documents into validated, reusable startup knowledge.

Core loop:

```text
startup files -> extraction -> Markdown vault -> LLM staging -> human validation -> Neo4j -> Qdrant -> audit agents
```

Neo4j stores validated structured entities and relationships only. Original files and Markdown remain on the filesystem. Qdrant stores semantic indexes. Ollama provides local LLM and embedding APIs.

## First Run

```bash
cp .env.example .env
make up
make pull-models
```

Open:

- Streamlit: http://localhost:8501
- Neo4j Browser: http://localhost:7474
- Qdrant: http://localhost:6333

Neo4j credentials:

```text
neo4j / foundergraph_password
```

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app/main.py
```

Run checks:

```bash
make test
make lint
```

## Demo Script

1. Open Streamlit and upload `sample_data/pitch_deck_text.md` plus one DOCX interview note.
2. Use **Extracted Documents** to confirm Markdown vault output in `vault/documents/`.
3. Use LLM extraction or seed `data/staging/candidate_entities.json` from the sample themes.
4. Validate assumptions, evidence, risks, customer segments, and product features.
5. Write validated knowledge to Neo4j from the graph page.
6. Run **Unsupported Assumption Agent** or **Pitch Audit**.
7. Create an export bundle from **Exports**.

Sample data lives in `sample_data/` and uses a fictional metadata platform for preclinical research.

## Security Rules

- Original files are never overwritten.
- LLM output goes to staging, never directly to Neo4j.
- Only validated knowledge enters Neo4j.
- Every entity and relation preserves source provenance.
- Labels and relation types are whitelisted.
- No arbitrary LLM-generated Cypher is executed.
