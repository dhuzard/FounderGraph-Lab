from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


RelationType = Literal[
    "TARGETS",
    "HAS_PROBLEM",
    "ADDRESSES",
    "BASED_ON",
    "SUPPORTED_BY",
    "CONTRADICTED_BY",
    "TESTS",
    "GENERATES",
    "THREATENS",
    "FUNDS",
    "PROVIDES",
    "COMPETES_ON",
    "PROTECTS",
    "MENTIONS",
    "SOURCE_OF",
    "DEPENDS_ON",
]


class KnowledgeRelation(BaseModel):
    id: str
    subject_id: str
    subject_type: str
    predicate: RelationType
    object_id: str
    object_type: str
    source_document_id: str
    source_file: str
    source_snippet: str | None = None
    confidence: Literal["low", "medium", "high"] = "medium"
    validation_status: Literal["pending", "validated", "rejected"] = "pending"
    created_at: datetime
    updated_at: datetime
