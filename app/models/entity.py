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
    # Deprecated: LLM-emitted confidence — keep for backward compat with staging files.
    confidence: Literal["low", "medium", "high"] | None = None
    # How directly the document text supports this entity.
    evidence_grade: Literal["direct_quote", "paraphrase", "inference", "speculation"] | None = None
    # Set by the human reviewer during the validation step.
    reviewer_confidence: Literal["strong", "moderate", "weak", "ungraded"] | None = None
    reviewer_comment: str | None = None
    validation_status: Literal[
        "pending",
        "validated",
        "rejected",
        "needs_more_evidence",
        "needs_review",
    ] = "pending"
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
