# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — Phase 8 (Polish, MCP, docs)
- Bi-temporal "View graph as of" date slider in Graph Explorer; snapshot is filtered through `Neo4jService.as_of`.
- "Upcoming milestones" expander in Graph Explorer with a deadline-window day input.
- `valid_at` date filter on the Discovery page; `discovery_queries.run()` accepts an optional `valid_at` argument that injects a bi-temporal predicate into the first MATCH clause.
- "Show query plan" checkbox on the Agents "Ask the graph" section. When enabled, the page runs `PROFILE <cypher>` and renders operator-level dbHits in an expander.
- MCP stub servers under `app/mcp/`: `neo4j_server.py` (`query_graph`, `get_unsupported_assumptions`), `qdrant_server.py` (`semantic_search`), `discovery_server.py` (`list_discovery_queries`, `discovery_query`). All three import without the `mcp` SDK and raise a typed `MCPUnavailableError` with an install hint when `serve()` is called.
- README lede rewritten around the five pillars (constrained extraction, deterministic discovery, ontology-guarded text-to-Cypher, grounded citations, bi-temporal audit trail).
- Deliberately contradictory sample dataset: `sample_data/contradictory_pitch.md` (BioVerify pitch with conflicting TAM and pricing claims) and `sample_data/contradictory_interview.md` (Geisinger interview supplying contradicting evidence) so contradiction-detection discovery surfaces drama on first run.
- `tests/test_mcp_servers.py` — verifies the degraded-mode contract for each MCP server.

## [0.1.0] — 2026-05-04

### Added
- Local-first Streamlit application for startup knowledge graph construction
- Multi-format document ingestion: PDF, DOCX, TXT, Markdown
- LLM-driven entity and relation extraction via Ollama (`llama3.1:8b`)
- Ontology pre-validation gate — blocks unknown entity types and predicates before human review
- Human-in-the-loop review screen with four-state status workflow
- Neo4j persistence with parameterised Cypher, label allowlists, and relation allowlists
- Qdrant vector index with `nomic-embed-text` embeddings
- LangGraph-backed audit agents: Assumption Audit, Pitch Audit, Grant Strategy
- ZIP export bundle: graph JSON, JSON-LD, assumptions CSV, evidence matrix CSV, risk register CSV
- Interactive ontology CLI (`make init`) for domain-specific entity and relation configuration
- Google Drive export pipeline for Drive-native document sources
- Demo dataset reset command (`make reset-demo`)
- Docker Compose stack: Streamlit app, Neo4j, Qdrant, Ollama
- 88-test suite covering extraction, validation, Neo4j writes, export, and end-to-end pipeline
- Sample datasets: Metadatapp (preclinical metadata) and TrialMesh (clinical trial workflow)

[Unreleased]: https://github.com/dhuzard/FounderGraph-Lab/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dhuzard/FounderGraph-Lab/releases/tag/v0.1.0
