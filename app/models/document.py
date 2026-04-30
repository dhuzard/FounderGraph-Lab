from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


DocumentType = Literal[
    "PitchDeck",
    "BusinessPlan",
    "CustomerInterview",
    "GrantApplication",
    "MarketResearch",
    "TechnicalRoadmap",
    "FinancialPlan",
    "ScientificNote",
    "MeetingNote",
    "LegalDocument",
    "Unknown",
]


class SourceDocument(BaseModel):
    id: str
    title: str
    original_filename: str
    file_type: str
    original_path: str
    extracted_text_path: str | None = None
    markdown_path: str | None = None
    document_type: DocumentType = "Unknown"
    date_uploaded: datetime
    extraction_status: Literal[
        "uploaded",
        "extracted",
        "converted_to_markdown",
        "classified",
        "entities_extracted",
        "validated",
        "failed",
    ] = "uploaded"
    confidentiality: Literal["public", "internal", "confidential", "sensitive"] = "internal"
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
