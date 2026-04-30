from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


EntityType = Literal[
    "Startup",
    "Founder",
    "CustomerSegment",
    "Problem",
    "ValueProposition",
    "ProductFeature",
    "Assumption",
    "Evidence",
    "Risk",
    "Experiment",
    "Decision",
    "Milestone",
    "GrantCall",
    "Investor",
    "Partner",
    "Competitor",
    "IPAsset",
    "RegulatoryConstraint",
    "TechnicalDependency",
    "FinancialHypothesis",
]


class KnowledgeEntity(BaseModel):
    id: str
    type: EntityType
    label: str
    description: str | None = None
    source_document_id: str
    source_file: str
    source_snippet: str | None = None
    source_location: str | None = None
    confidence: Literal["low", "medium", "high"] = "medium"
    validation_status: Literal[
        "pending",
        "validated",
        "rejected",
        "needs_more_evidence",
    ] = "pending"
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
