from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AuditResult(BaseModel):
    id: str
    title: str
    agent_name: str
    markdown_path: str
    source_entity_ids: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)
    created_at: datetime
