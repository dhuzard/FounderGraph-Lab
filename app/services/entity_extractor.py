"""LLM-driven staging extraction for candidate graph objects.

This module intentionally writes only to JSON staging files. It never opens a
Neo4j connection or mutates the production graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.llm_service import LLMInvalidJSONError, LLMServiceError, OllamaLLMService


try:  # Integrator-owned shared models may be added later.
    from app.models.knowledge import KnowledgeEntity as SharedKnowledgeEntity
    from app.models.knowledge import KnowledgeRelation as SharedKnowledgeRelation
except ImportError:  # pragma: no cover - exercised implicitly when shared models are absent.
    SharedKnowledgeEntity = None
    SharedKnowledgeRelation = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = PROJECT_ROOT / "app" / "prompts"
DEFAULT_STAGING_DIR = PROJECT_ROOT / "data" / "staging"


class DocumentClassification(BaseModel):
    """Classification gate for startup document processing."""

    model_config = ConfigDict(extra="forbid")

    document_type: str = Field(default="Unknown", min_length=1)
    secondary_types: list[str] = Field(default_factory=list)
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    confidence: str = "medium"

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_shape(cls, value: Any) -> Any:
        if isinstance(value, dict) and "is_startup_document" in value:
            confidence = value.get("confidence", "medium")
            if isinstance(confidence, int | float):
                confidence = "high" if confidence >= 0.75 else "medium" if confidence >= 0.4 else "low"
            return {
                "document_type": value.get("document_type", "Unknown"),
                "secondary_types": [],
                "summary": value.get("rationale", ""),
                "tags": [],
                "confidence": confidence,
            }
        return value


class CandidateKnowledgeEntity(BaseModel):
    """Fallback KnowledgeEntity staging schema.

    Required fields: id, name, type. Optional fields carry evidence and metadata
    for downstream review before any graph write happens.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    temporary_id: str | None = None
    name: str | None = None
    label: str | None = None
    type: str = Field(min_length=1)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    evidence: str | None = None
    source_snippet: str | None = None
    source_document: str | None = None
    confidence: float | str = "medium"
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_identity(self) -> "CandidateKnowledgeEntity":
        if not self.id:
            self.id = self.temporary_id
        if not self.temporary_id:
            self.temporary_id = self.id
        if not self.label:
            self.label = self.name
        if not self.name:
            self.name = self.label
        if not self.id or not self.label:
            raise ValueError("Candidate entity requires id/temporary_id and label/name")
        return self


class CandidateKnowledgeRelation(BaseModel):
    """Fallback KnowledgeRelation staging schema.

    Required fields: source_entity_id, target_entity_id, type. Entity ids should
    match candidate entity ids from the same extraction batch when possible.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    source_entity_id: str = Field(min_length=1)
    target_entity_id: str = Field(min_length=1)
    subject_temporary_id: str | None = None
    object_temporary_id: str | None = None
    predicate: str | None = None
    type: str = Field(min_length=1)
    description: str | None = None
    evidence: str | None = None
    source_snippet: str | None = None
    source_document: str | None = None
    confidence: float | str = "medium"
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_ontology_shape(cls, value: Any) -> Any:
        if isinstance(value, dict) and "subject_temporary_id" in value:
            item = dict(value)
            item.setdefault("source_entity_id", item.get("subject_temporary_id"))
            item.setdefault("target_entity_id", item.get("object_temporary_id"))
            item.setdefault("type", item.get("predicate"))
            return item
        return value

    @model_validator(mode="after")
    def _normalize_predicate(self) -> "CandidateKnowledgeRelation":
        if not self.subject_temporary_id:
            self.subject_temporary_id = self.source_entity_id
        if not self.object_temporary_id:
            self.object_temporary_id = self.target_entity_id
        if not self.predicate:
            self.predicate = self.type
        return self


KnowledgeEntityModel = SharedKnowledgeEntity or CandidateKnowledgeEntity
KnowledgeRelationModel = SharedKnowledgeRelation or CandidateKnowledgeRelation


class ExtractionResult(BaseModel):
    """Validated staged extraction result."""

    classification: DocumentClassification
    entities: list[CandidateKnowledgeEntity]
    relations: list[CandidateKnowledgeRelation]
    wrote_files: bool


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _json_for_prompt(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _coerce_items(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    raise LLMInvalidJSONError(f"Expected a JSON array or an object with '{key}' array")


def _validate_with_model(model: type[Any], item: Any) -> Any:
    if hasattr(model, "model_validate"):
        return model.model_validate(item)
    return model.parse_obj(item)


def _dump_model(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    return model.dict(exclude_none=True, exclude_defaults=True)


class EntityExtractor:
    """Classify documents and stage candidate graph objects as strict JSON."""

    def __init__(
        self,
        llm_service: OllamaLLMService | None = None,
        staging_dir: Path | str = DEFAULT_STAGING_DIR,
    ) -> None:
        self.llm_service = llm_service or OllamaLLMService()
        self.staging_dir = Path(staging_dir)

    def classify_document(self, text: str, metadata: dict[str, Any] | None = None) -> DocumentClassification:
        prompt = self._build_prompt("classify_document.md", text, metadata or {})
        payload = self.llm_service.generate_json(prompt)
        try:
            return DocumentClassification.model_validate(payload)
        except ValidationError as exc:
            raise LLMInvalidJSONError("Classification response failed schema validation") from exc

    def extract_entities(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[CandidateKnowledgeEntity]:
        prompt = self._build_prompt("extract_entities.md", text, metadata or {})
        payload = self.llm_service.generate_json(prompt)
        items = _coerce_items(payload, "entities")
        return self._validate_entities(items)

    def extract_relations(
        self,
        text: str,
        entities: Iterable[CandidateKnowledgeEntity],
        metadata: dict[str, Any] | None = None,
    ) -> list[CandidateKnowledgeRelation]:
        entity_payload = [_dump_model(entity) for entity in entities]
        prompt = self._build_prompt(
            "extract_relations.md",
            text,
            metadata or {},
            extra_context={"candidate_entities": entity_payload},
        )
        payload = self.llm_service.generate_json(prompt)
        items = _coerce_items(payload, "relations")
        return self._validate_relations(items)

    def extract_to_staging(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        """Run classification and extraction, then atomically stage JSON files."""

        classification = self.classify_document(text, metadata)
        entities = self.extract_entities(text, metadata)
        relations = self.extract_relations(text, entities, metadata)
        self._write_candidates(entities, relations)
        return ExtractionResult(
            classification=classification,
            entities=entities,
            relations=relations,
            wrote_files=True,
        )

    def _build_prompt(
        self,
        prompt_name: str,
        text: str,
        metadata: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        context = {"metadata": metadata, "document_text": text}
        if extra_context:
            context.update(extra_context)
        return f"{_load_prompt(prompt_name).strip()}\n\nINPUT_JSON:\n{_json_for_prompt(context)}"

    def _validate_entities(self, items: list[Any]) -> list[CandidateKnowledgeEntity]:
        staged: list[CandidateKnowledgeEntity] = []
        for item in items:
            try:
                validated = _validate_with_model(KnowledgeEntityModel, item)
                staged.append(CandidateKnowledgeEntity.model_validate(_dump_model(validated)))
            except (TypeError, ValueError, ValidationError) as exc:
                raise LLMInvalidJSONError("Entity response failed schema validation") from exc
        return staged

    def _validate_relations(self, items: list[Any]) -> list[CandidateKnowledgeRelation]:
        staged: list[CandidateKnowledgeRelation] = []
        for item in items:
            try:
                validated = _validate_with_model(KnowledgeRelationModel, item)
                staged.append(CandidateKnowledgeRelation.model_validate(_dump_model(validated)))
            except (TypeError, ValueError, ValidationError) as exc:
                raise LLMInvalidJSONError("Relation response failed schema validation") from exc
        return staged

    def _write_candidates(
        self,
        entities: list[CandidateKnowledgeEntity],
        relations: list[CandidateKnowledgeRelation],
    ) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._accumulate_json(
            self.staging_dir / "candidate_entities.json",
            [_dump_model(entity) for entity in entities],
        )
        self._accumulate_json(
            self.staging_dir / "candidate_relations.json",
            [_dump_model(relation) for relation in relations],
        )

    @staticmethod
    def _accumulate_json(path: Path, new_records: list[dict[str, Any]]) -> None:
        """Merge new_records into the existing staging file keyed by 'id'.

        New records overwrite existing ones with the same id; records from
        prior extraction runs are preserved.  Records without an explicit id
        are keyed by a stable content hash so they are still deduplicated.
        This prevents multi-document pipelines from silently discarding
        earlier extractions.
        """
        import hashlib

        def _record_key(item: dict[str, Any]) -> str:
            explicit = item.get("id") or item.get("temporary_id")
            if explicit:
                return str(explicit)
            return hashlib.sha256(
                json.dumps(item, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()[:16]

        existing: list[dict[str, Any]] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                existing = raw if isinstance(raw, list) else []
            except json.JSONDecodeError:
                existing = []

        merged: dict[str, dict[str, Any]] = {_record_key(item): item for item in existing}
        for item in new_records:
            merged[_record_key(item)] = item

        text = json.dumps(list(merged.values()), ensure_ascii=True, indent=2, sort_keys=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(f"{text}\n", encoding="utf-8")
        tmp_path.replace(path)

    @staticmethod
    def _atomic_write_json(path: Path, payload: list[dict[str, Any]]) -> None:
        """Fully replace a staging file (used for explicit resets)."""
        text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(f"{text}\n", encoding="utf-8")
        tmp_path.replace(path)
