# Product Requirements - TrialMesh Core Workspace

## Goal

Enable startup teams to prove readiness with linked evidence rather than relying on static trackers.

## Core features

1. Document ingestion
- accept protocol drafts, trackers, notes, CSV exports, SOPs, and HTML exports
- preserve source provenance and snippets
- support batch folder ingestion

2. Evidence graph
- entities for sites, vendors, milestones, assumptions, risks, documents, and decisions
- relationships for supports, contradicts, depends on, threatens, source of, and mentions
- human validation before graph writes

3. Startup readiness workspace
- milestone view by country, site, and vendor
- blocker registry with owner and evidence
- dependency tracing when protocol amendments occur

4. Trust and control
- role-based access
- redaction before sharing external exports
- audit log for graph writes
- local-first deployment option for sensitive teams

## Non-functional requirements

- workspace should open under 2 seconds for 100k graph elements in filtered view
- search results should return in under 1.5 seconds for top 5 snippets
- exports should complete under 30 seconds for a 25k-node graph snapshot
- all write operations must refuse non-validated records

## Risks and dependencies

- dependency on Microsoft 365 connectors for many sponsor pilots
- dependency on Qdrant retrieval quality for agent usefulness
- dependency on Neo4j schema consistency for audit workflows
- risk that redaction controls lag enterprise buyer expectations
