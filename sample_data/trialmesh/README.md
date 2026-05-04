# TrialMesh Demo Dataset

TrialMesh is a fictional startup built for live demos of FounderGraph-Lab.

TrialMesh sells a local-first evidence orchestration platform for clinical trial startup teams. The product turns protocol packets, vendor trackers, monitoring notes, regulatory requirements, and operational evidence into a validated knowledge graph that helps sponsors and CROs reduce trial startup delays.

## Why this dataset is useful

This dataset is intentionally broader and messier than a tidy pitch deck sample. It is organized the way a real early-stage team might keep files:

- strategy and founder notes
- customer discovery interviews
- product specs and roadmap artifacts
- evidence and KPI snapshots
- finance and investor updates
- regulatory and compliance documents
- grants and partnership notes
- operating plans and risk registers

## Recommended live demo flow

1. Ingest the whole folder recursively.
2. Extract candidates from a small batch first.
3. Validate obvious entities like TrialMesh, founders, customer segments, problems, risks, milestones, and investors.
4. Write validated records to Neo4j.
5. Run Assumption Audit, Due Diligence Checklist Agent, and Grant Strategy.
6. Export the bundle and show evidence matrix and risk register outputs.

## Suggested story arc for a demo

- The startup claims it can reduce site startup cycle time by 21 percent.
- Customer interviews show high urgency around protocol amendments, document version confusion, and vendor handoffs.
- Product docs reveal dependencies on Microsoft 365, SSO, audit logs, and redaction workflows.
- Regulatory notes surface HIPAA, 21 CFR Part 11, and ICH E6 concerns.
- Finance assumptions reveal dependency on 3 lighthouse pilots and one enterprise conversion.
- Risk and support files introduce realistic contradictions, missing evidence, and issues requiring human review.
