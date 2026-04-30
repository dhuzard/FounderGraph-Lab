from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import CANDIDATE_ENTITIES_JSON, CANDIDATE_RELATIONS_JSON


def _now() -> str:
    return datetime.now(UTC).isoformat()


DEMO_ENTITIES: list[dict[str, Any]] = [
    {
        "id": "CUST-CRO-001",
        "type": "CustomerSegment",
        "label": "Preclinical CROs",
        "description": "Contract research organizations running repeated preclinical study workflows.",
        "source_document_id": "sample-pitch",
        "source_file": "sample_data/pitch_deck_text.md",
        "source_snippet": "Preclinical CROs urgently need metadata interoperability to reduce duplicated data entry.",
        "confidence": "high",
        "validation_status": "pending",
        "tags": ["customer"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "PROB-META-001",
        "type": "Problem",
        "label": "Fragmented preclinical metadata",
        "description": "Metadata are lost across LIMS, spreadsheets, ELNs, imaging tools, and reporting templates.",
        "source_document_id": "sample-interview-cro",
        "source_file": "sample_data/customer_interview_cro_01.docx",
        "source_snippet": "The team loses metadata between the LIMS, spreadsheets, and study reports.",
        "confidence": "high",
        "validation_status": "pending",
        "tags": ["metadata", "interoperability"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "FEAT-MAP-001",
        "type": "ProductFeature",
        "label": "Metadata mapping layer",
        "description": "A lightweight metadata layer that maps experimental context across tools.",
        "source_document_id": "sample-business-plan",
        "source_file": "sample_data/business_plan.md",
        "source_snippet": "The first wedge is a lightweight metadata layer that maps experimental context.",
        "confidence": "medium",
        "validation_status": "pending",
        "tags": ["product"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "ASSUMP-WTP-001",
        "type": "Assumption",
        "label": "CROs will pay for metadata interoperability",
        "description": "CROs will pay for integration if it reduces duplicated data entry.",
        "source_document_id": "sample-business-plan",
        "source_file": "sample_data/business_plan.md",
        "source_snippet": "CROs will pay for integration if it reduces duplicated data entry.",
        "confidence": "medium",
        "validation_status": "pending",
        "tags": ["pricing", "gtm"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "ASSUMP-AI-001",
        "type": "Assumption",
        "label": "Pharma needs metadata for AI-readiness",
        "description": "Pharma translational teams will sponsor pilots when metadata quality affects AI readiness.",
        "source_document_id": "sample-pitch",
        "source_file": "sample_data/pitch_deck_text.md",
        "source_snippet": "Pharma translational teams need traceable metadata for AI-ready research datasets.",
        "confidence": "medium",
        "validation_status": "pending",
        "tags": ["pharma", "ai-readiness"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "EVID-CRO-001",
        "type": "Evidence",
        "label": "CRO interview reports metadata loss",
        "description": "A CRO operations lead described metadata loss across tools.",
        "source_document_id": "sample-interview-cro",
        "source_file": "sample_data/customer_interview_cro_01.docx",
        "source_snippet": "The team loses metadata between the LIMS, spreadsheets, and study reports.",
        "confidence": "high",
        "validation_status": "pending",
        "tags": ["interview"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "EVID-PHARMA-001",
        "type": "Evidence",
        "label": "Pharma stakeholder asks for provenance",
        "description": "A pharma stakeholder asked for better provenance before AI-readiness pilots.",
        "source_document_id": "sample-pitch",
        "source_file": "sample_data/pitch_deck_text.md",
        "source_snippet": "A pharma stakeholder asked for better provenance before approving AI-readiness pilots.",
        "confidence": "medium",
        "validation_status": "pending",
        "tags": ["stakeholder"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "RISK-SALES-001",
        "type": "Risk",
        "label": "Long enterprise sales cycle",
        "description": "Enterprise sales cycles may delay revenue and pilots.",
        "source_document_id": "sample-pitch",
        "source_file": "sample_data/pitch_deck_text.md",
        "source_snippet": "Enterprise sales cycles may be longer than expected.",
        "confidence": "medium",
        "validation_status": "pending",
        "tags": ["sales"],
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "RISK-INT-001",
        "type": "Risk",
        "label": "Legacy integration complexity",
        "description": "Integrations with legacy research tools may be harder than planned.",
        "source_document_id": "sample-roadmap",
        "source_file": "sample_data/technical_roadmap.md",
        "source_snippet": "Legacy system integration complexity.",
        "confidence": "high",
        "validation_status": "pending",
        "tags": ["technical"],
        "created_at": _now(),
        "updated_at": _now(),
    },
]

DEMO_RELATIONS: list[dict[str, Any]] = [
    {
        "id": "REL-CRO-PROB-001",
        "subject_id": "CUST-CRO-001",
        "subject_type": "CustomerSegment",
        "predicate": "HAS_PROBLEM",
        "object_id": "PROB-META-001",
        "object_type": "Problem",
        "source_document_id": "sample-interview-cro",
        "source_file": "sample_data/customer_interview_cro_01.docx",
        "source_snippet": "The team loses metadata between the LIMS, spreadsheets, and study reports.",
        "confidence": "high",
        "validation_status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "REL-FEAT-PROB-001",
        "subject_id": "FEAT-MAP-001",
        "subject_type": "ProductFeature",
        "predicate": "ADDRESSES",
        "object_id": "PROB-META-001",
        "object_type": "Problem",
        "source_document_id": "sample-business-plan",
        "source_file": "sample_data/business_plan.md",
        "source_snippet": "A lightweight metadata layer that maps experimental context across study planning.",
        "confidence": "medium",
        "validation_status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
    },
    {
        "id": "REL-ASSUMP-EVID-001",
        "subject_id": "ASSUMP-AI-001",
        "subject_type": "Assumption",
        "predicate": "SUPPORTED_BY",
        "object_id": "EVID-PHARMA-001",
        "object_type": "Evidence",
        "source_document_id": "sample-pitch",
        "source_file": "sample_data/pitch_deck_text.md",
        "source_snippet": "A pharma stakeholder asked for better provenance before approving AI-readiness pilots.",
        "confidence": "medium",
        "validation_status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
    },
]


def seed_demo_candidates(overwrite: bool = False) -> tuple[Path, Path]:
    CANDIDATE_ENTITIES_JSON.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_RELATIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not CANDIDATE_ENTITIES_JSON.exists():
        CANDIDATE_ENTITIES_JSON.write_text(json.dumps(DEMO_ENTITIES, indent=2) + "\n", encoding="utf-8")
    if overwrite or not CANDIDATE_RELATIONS_JSON.exists():
        CANDIDATE_RELATIONS_JSON.write_text(json.dumps(DEMO_RELATIONS, indent=2) + "\n", encoding="utf-8")
    return CANDIDATE_ENTITIES_JSON, CANDIDATE_RELATIONS_JSON
