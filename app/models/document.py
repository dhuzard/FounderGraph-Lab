"""Re-export of the LinkML-generated Document class.

The legacy ``SourceDocument`` model accepted upload-time metadata (filename,
upload date, extraction status) that's bigger than the LinkML ``Document``
class.  We keep that legacy Pydantic model here so ``file_store.py`` and the
Streamlit upload flow keep working untouched, while also re-exporting the
LinkML-generated ``Document`` under the new name ``OntologyDocument`` for
callers that want to round-trip a document through the ontology graph.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

try:
    from app.ontology.generated.models import (  # type: ignore[import-not-found]
        Document as OntologyDocument,
    )
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    class OntologyDocument(BaseModel):  # type: ignore[no-redef]
        id: str


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
    """Upload-time metadata for a document.

    Hand-written because the LinkML ``Document`` class only models the
    in-graph identity; the upload pipeline needs richer fields (original
    filename, extraction status, confidentiality) that don't belong in the
    knowledge graph itself.
    """

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


__all__ = ["SourceDocument", "OntologyDocument", "DocumentType"]
