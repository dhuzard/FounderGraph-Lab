# Compliance Gap Assessment

## In scope

- HIPAA handling for startup documents containing investigator or patient-adjacent context
- 21 CFR Part 11 expectations for auditability and records integrity
- ICH E6 R3 principles around quality by design and traceability

## Current strengths

- append-only style provenance retained for extracted knowledge
- explicit human validation gate before graph writes
- source snippet retention for auditability
- local-first deployment option reduces some data-transfer concerns

## Current gaps

- no formal validation package for regulated software claims
- redaction workflow incomplete for mixed-sensitivity documents
- role-based access controls are basic in prototype state
- retention and deletion policy not fully specified

## Recommended actions

- complete threat model and access-control matrix
- define audit event taxonomy
- document validation boundary for AI-assisted extraction
- separate regulated claims from productivity claims in sales materials
