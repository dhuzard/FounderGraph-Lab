# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
