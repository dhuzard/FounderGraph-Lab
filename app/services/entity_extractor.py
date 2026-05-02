"""LLM-driven staging extraction for candidate graph objects.

This module intentionally writes only to JSON staging files. It never opens a
Neo4j connection or mutates the production graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.llm_service import LLMInvalidJSONError, LLMService, LLMServiceError, OllamaLLMService


try:  # Integrator-owned shared models may be added later.
    from app.models.knowledge import KnowledgeEntity as SharedKnowledgeEntity
    from app.models.knowledge import KnowledgeRelation as SharedKnowledgeRelation
except ImportError:  # pragma: no cover - exercised implicitly when shared models are absent.
    SharedKnowledgeEntity = None
    SharedKnowledgeRelation = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = PROJECT_ROOT / "app" / "prompts"
DEFAULT_STAGING_DIR = PROJECT_ROOT / "data" / "staging"

_DEFAULT_ENTITY_TYPES = (
    "- CustomerSegment\n- Problem\n- ValueProposition\n- ProductFeature\n"
    "- Assumption\n- Evidence\n- Risk\n- Experiment\n- Decision\n- Milestone\n"
    "- GrantCall\n- Investor\n- Partner\n- Competitor\n- IPAsset\n"
    "- RegulatoryConstraint\n- TechnicalDependency\n- FinancialHypothesis"
)


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


def _ensure_relation_id(r: dict[str, Any]) -> dict[str, Any]:
    if r.get("id"):
        return r
    src = r.get("source_entity_id") or r.get("subject_temporary_id") or ""
    pred = r.get("predicate") or r.get("type") or "RELATED_TO"
    tgt = r.get("target_entity_id") or r.get("object_temporary_id") or ""
    return {**r, "id": f"{src}:{pred}:{tgt}"}


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
        llm_service: LLMService | None = None,
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
        """Run classification + extraction, validate against ontology, then stage."""
        classification = self.classify_document(text, metadata)
        entities = self.extract_entities(text, metadata)
        relations = self.extract_relations(text, entities, metadata)

        try:
            from app.services.ontology_validator import OntologyValidator
            report = OntologyValidator().validate(entities, relations)
            # Stage only ontologically valid candidates; violations are logged separately.
            valid_eids = {
                str(e.get("id") or e.get("temporary_id"))
                for e in report.valid_entities
            }
            valid_rkeys = {
                (
                    str(r.get("source_entity_id") or r.get("subject_temporary_id") or ""),
                    str(r.get("predicate") or r.get("type") or ""),
                    str(r.get("target_entity_id") or r.get("object_temporary_id") or ""),
                )
                for r in report.valid_relations
            }
            staged_entities = [e for e in entities if str(e.id or e.temporary_id) in valid_eids]
            staged_relations = [
                r for r in relations
                if (str(r.source_entity_id), str(r.predicate or r.type), str(r.target_entity_id)) in valid_rkeys
            ]
            self._write_candidates(staged_entities, staged_relations)
        except Exception:
            # Validator failure must never block staging; fall back to writing all candidates.
            staged_entities = entities
            staged_relations = relations
            self._write_candidates(entities, relations)

        return ExtractionResult(
            classification=classification,
            entities=staged_entities,
            relations=staged_relations,
            wrote_files=True,
        )

    def _build_prompt(
        self,
        prompt_name: str,
        text: str,
        metadata: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        template = _load_prompt(prompt_name).strip()
        substitutions = {
            "{{document_text}}": text,
            "{{document_metadata}}": _json_for_prompt(metadata),
            "{{entities_json}}": _json_for_prompt((extra_context or {}).get("candidate_entities", [])),
            "{{entity_types}}": self._entity_types_block(),
        }
        result = template
        for placeholder, value in substitutions.items():
            result = result.replace(placeholder, value)
        return result

    def _entity_types_block(self) -> str:
        try:
            from app.services.ontology_service import load_ontology
            config = load_ontology()
            if config.entity_classes:
                lines = []
                for name, cls in config.entity_classes.items():
                    suffix = f": {cls.description}" if cls.description else ""
                    lines.append(f"- {name}{suffix}")
                return "\n".join(lines)
        except Exception:
            pass
        return _DEFAULT_ENTITY_TYPES

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
        self._merge_and_write(
            self.staging_dir / "candidate_entities.json",
            [_dump_model(entity) for entity in entities],
            merge_key="id",
        )
        self._merge_and_write(
            self.staging_dir / "candidate_relations.json",
            [_ensure_relation_id(r) for r in (_dump_model(rel) for rel in relations)],
            merge_key="id",
        )

    def _merge_and_write(self, path: Path, new_items: list[dict[str, Any]], merge_key: str = "id") -> None:
        """Merge new_items into the existing staging file by merge_key, then write atomically."""
        existing: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                for item in json.loads(path.read_text(encoding="utf-8")):
                    if isinstance(item, dict) and item.get(merge_key):
                        existing[str(item[merge_key])] = item
            except (json.JSONDecodeError, OSError):
                pass
        for item in new_items:
            if isinstance(item, dict) and item.get(merge_key):
                existing[str(item[merge_key])] = item
        self._atomic_write_json(path, list(existing.values()))

    @staticmethod
    def _atomic_write_json(path: Path, payload: list[dict[str, Any]]) -> None:
        text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(f"{text}\n", encoding="utf-8")
        tmp_path.replace(path)
