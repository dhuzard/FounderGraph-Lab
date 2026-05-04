"""LLM-driven staging extraction for candidate graph objects.

This module intentionally writes only to JSON staging files. It never opens a
Neo4j connection or mutates the production graph.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.llm_service import LLMInvalidJSONError, LLMService, OllamaLLMService


try:  # Integrator-owned shared models may be added later.
    from app.models.knowledge import KnowledgeEntity as SharedKnowledgeEntity
    from app.models.knowledge import KnowledgeRelation as SharedKnowledgeRelation
except ImportError:  # pragma: no cover - exercised implicitly when shared models are absent.
    SharedKnowledgeEntity = None
    SharedKnowledgeRelation = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = PROJECT_ROOT / "app" / "prompts"
DEFAULT_STAGING_DIR = PROJECT_ROOT / "data" / "staging"

# Fixed namespace UUID for deterministic entity ID generation.
# Never change this value — changing it would invalidate all existing entity IDs.
_ENTITY_ID_NAMESPACE = uuid.UUID("a7f3c2e1-9b4d-4f8a-b6e5-1d2c3e4f5a6b")


def stable_entity_id(doc_id: str, label: str, entity_type: str) -> str:
    """Generate a stable, collision-safe UUID for an entity.

    The ID is deterministic given the same (doc_id, label, entity_type) triple,
    so re-extracting the same document produces the same IDs and enables safe
    MERGE operations in Neo4j.
    """
    key = f"{doc_id}::{label.strip().lower()}::{entity_type}"
    return str(uuid.uuid5(_ENTITY_ID_NAMESPACE, key))


_DEFAULT_ENTITY_TYPES = (
    "- CustomerSegment\n- Problem\n- ValueProposition\n- ProductFeature\n"
    "- Assumption\n- Evidence\n- Risk\n- Experiment\n- Decision\n- Milestone\n"
    "- GrantCall\n- Investor\n- Partner\n- Competitor\n- IPAsset\n"
    "- RegulatoryConstraint\n- TechnicalDependency\n- FinancialHypothesis"
)
DEFAULT_CHUNK_SIZE = 12_000
DEFAULT_CHUNK_OVERLAP = 1_000


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


EVIDENCE_GRADES = ("direct_quote", "paraphrase", "inference", "speculation")
REVIEWER_CONFIDENCES = ("strong", "moderate", "weak", "ungraded")


class CandidateKnowledgeEntity(BaseModel):
    """Fallback KnowledgeEntity staging schema.

    Required fields: id, name, type. Optional fields carry evidence and metadata
    for downstream review before any graph write happens.

    confidence (float|str) is kept for backward-compatibility with existing
    staging files but should not be treated as a calibrated probability.
    Use evidence_grade (set by the LLM) and reviewer_confidence (set by the
    human reviewer) as the canonical grounding signals.
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
    source_document_id: str | None = None
    # Deprecated: LLM-emitted float/string confidence — not a calibrated probability.
    confidence: float | str | None = None
    # How directly the document text supports this entity.
    evidence_grade: str | None = None
    # Set by the human reviewer during validation.
    reviewer_confidence: str | None = None
    reviewer_comment: str | None = None
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_evidence_grade(cls, value: Any) -> Any:
        """Accept legacy numeric confidence and convert to evidence_grade."""
        if not isinstance(value, dict):
            return value
        item = dict(value)
        raw_conf = item.get("confidence")
        if isinstance(raw_conf, (int, float)):
            # Numeric LLM confidence → categorical evidence grade.
            if raw_conf >= 0.8:
                item.setdefault("evidence_grade", "paraphrase")
            elif raw_conf >= 0.5:
                item.setdefault("evidence_grade", "inference")
            else:
                item.setdefault("evidence_grade", "speculation")
            item["confidence"] = None  # discard the numeric value
        return item

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
        if self.evidence_grade and self.evidence_grade not in EVIDENCE_GRADES:
            self.evidence_grade = "inference"
        if self.reviewer_confidence and self.reviewer_confidence not in REVIEWER_CONFIDENCES:
            self.reviewer_confidence = "ungraded"
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
    source_document_id: str | None = None
    # Deprecated: LLM-emitted float/string confidence — not a calibrated probability.
    confidence: float | str | None = None
    # How directly the document text supports this relation.
    evidence_grade: str | None = None
    # Set by the human reviewer during validation.
    reviewer_comment: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_ontology_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        item = dict(value)
        if "subject_temporary_id" in item:
            item.setdefault("source_entity_id", item.get("subject_temporary_id"))
            item.setdefault("target_entity_id", item.get("object_temporary_id"))
            item.setdefault("type", item.get("predicate"))
        # Coerce numeric LLM confidence to evidence_grade (same logic as entity).
        raw_conf = item.get("confidence")
        if isinstance(raw_conf, (int, float)):
            if raw_conf >= 0.8:
                item.setdefault("evidence_grade", "paraphrase")
            elif raw_conf >= 0.5:
                item.setdefault("evidence_grade", "inference")
            else:
                item.setdefault("evidence_grade", "speculation")
            item["confidence"] = None
        return item

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
            # key matches {{entities_json}} placeholder in the template
            extra_context={"entities_json": entity_payload},
        )
        payload = self.llm_service.generate_json(prompt)
        items = _coerce_items(payload, "relations")
        return self._validate_relations(items)

    def extract_to_staging(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        """Run extraction, normalize IDs, validate against ontology, then stage.

        After LLM extraction, each entity receives a stable UUIDv5 derived from
        (source_document_id, normalised_label, type) so that re-extracting the
        same document yields the same entity IDs.  Relation source/target IDs
        are updated to match.  Ontology violations are logged separately and
        only valid candidates are staged unless the validator itself fails.
        """
        meta = metadata or {}
        classification = self.classify_document(text, meta)
        doc_id = str(
            meta.get("source_document_id")
            or meta.get("document_id")
            or meta.get("source_document")
            or ""
        )
        stable_doc_id = doc_id or None
        chunks = self._chunk_text(text)

        all_entities: list[CandidateKnowledgeEntity] = []
        all_relations: list[CandidateKnowledgeRelation] = []
        multi_chunk = len(chunks) > 1

        for chunk_index, chunk_text in enumerate(chunks, start=1):
            chunk_meta = {
                **meta,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
            }
            entities = self.extract_entities(chunk_text, chunk_meta)

            # Build temporary_id → stable UUIDv5 mapping so relations can be updated.
            tmp_to_stable: dict[str, str] = {}
            for entity in entities:
                sid = stable_entity_id(doc_id, entity.label or "", entity.type)
                old_tmp = entity.temporary_id or entity.id or ""
                tmp_to_stable[old_tmp] = sid
                entity.id = sid
                entity.temporary_id = sid
                entity.source_document_id = stable_doc_id
                if multi_chunk:
                    entity.properties.setdefault("extraction_chunks", [])
                    entity.properties["extraction_chunks"].append(chunk_index)

            relations = self.extract_relations(chunk_text, entities, chunk_meta)
            for relation in relations:
                if relation.source_entity_id in tmp_to_stable:
                    relation.source_entity_id = tmp_to_stable[relation.source_entity_id]
                    relation.subject_temporary_id = relation.source_entity_id
                if relation.target_entity_id in tmp_to_stable:
                    relation.target_entity_id = tmp_to_stable[relation.target_entity_id]
                    relation.object_temporary_id = relation.target_entity_id
                relation.source_document_id = stable_doc_id
                if multi_chunk:
                    relation.properties.setdefault("extraction_chunks", [])
                    relation.properties["extraction_chunks"].append(chunk_index)

            all_entities.extend(entities)
            all_relations.extend(relations)

        entities = self._merge_entities(all_entities)
        relations = self._merge_relations(all_relations)

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

    @staticmethod
    def _chunk_text(
        text: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> list[str]:
        """Split long documents so rich PDFs are not under-extracted by context truncation."""
        clean = text.strip()
        if not clean:
            return [""]
        if len(clean) <= chunk_size:
            return [clean]

        chunks: list[str] = []
        start = 0
        while start < len(clean):
            hard_end = min(start + chunk_size, len(clean))
            end = hard_end
            if hard_end < len(clean):
                paragraph_break = clean.rfind("\n\n", start + chunk_size // 2, hard_end)
                sentence_break = clean.rfind(". ", start + chunk_size // 2, hard_end)
                end = max(paragraph_break, sentence_break)
                if end <= start:
                    end = hard_end
                elif end == sentence_break:
                    end += 1
            chunk = clean[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(clean):
                break
            start = max(end - overlap, start + 1)
        return chunks or [clean]

    @staticmethod
    def _merge_entities(entities: list[CandidateKnowledgeEntity]) -> list[CandidateKnowledgeEntity]:
        merged: dict[str, CandidateKnowledgeEntity] = {}
        for entity in entities:
            key = str(entity.id or entity.temporary_id)
            if key not in merged:
                merged[key] = entity
                continue
            existing = merged[key]
            if entity.description and entity.description not in (existing.description or ""):
                existing.description = "\n\n".join(part for part in (existing.description, entity.description) if part)
            if entity.source_snippet and entity.source_snippet not in (existing.source_snippet or ""):
                existing.source_snippet = "\n---\n".join(part for part in (existing.source_snippet, entity.source_snippet) if part)
            existing.tags = sorted(set(existing.tags) | set(entity.tags))
            chunks = set(existing.properties.get("extraction_chunks", [])) | set(entity.properties.get("extraction_chunks", []))
            if chunks:
                existing.properties["extraction_chunks"] = sorted(chunks)
        return list(merged.values())

    @staticmethod
    def _merge_relations(relations: list[CandidateKnowledgeRelation]) -> list[CandidateKnowledgeRelation]:
        grade_rank = {"direct_quote": 0, "paraphrase": 1, "inference": 2, "speculation": 3, None: 4}
        merged: dict[tuple[str, str, str], CandidateKnowledgeRelation] = {}
        for relation in relations:
            key = (
                str(relation.source_entity_id),
                str(relation.predicate or relation.type),
                str(relation.target_entity_id),
            )
            if key not in merged:
                merged[key] = relation
                continue
            existing = merged[key]
            if grade_rank.get(relation.evidence_grade, 4) < grade_rank.get(existing.evidence_grade, 4):
                existing.evidence_grade = relation.evidence_grade
            if relation.source_snippet and relation.source_snippet not in (existing.source_snippet or ""):
                existing.source_snippet = "\n---\n".join(part for part in (existing.source_snippet, relation.source_snippet) if part)
            chunks = set(existing.properties.get("extraction_chunks", [])) | set(relation.properties.get("extraction_chunks", []))
            if chunks:
                existing.properties["extraction_chunks"] = sorted(chunks)
        return list(merged.values())

    def _build_prompt(
        self,
        prompt_name: str,
        text: str,
        metadata: dict[str, Any],
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        """Load a prompt template and substitute all {{key}} placeholders.

        Built-in substitutions:
          {{document_text}}     — the raw document text
          {{document_metadata}} — JSON-serialised metadata dict
          {{entity_types}}      — allowed entity type block from the ontology

        Additional substitutions come from extra_context, where each key maps
        to a value that is JSON-serialised if it is not already a string.
        Unrecognised placeholders are left as-is so the model receives the
        literal text rather than an empty string.
        """
        template = _load_prompt(prompt_name).strip()
        substitutions: dict[str, str] = {
            "document_text": text,
            "document_metadata": _json_for_prompt(metadata),
            "entity_types": self._entity_types_block(),
        }
        if extra_context:
            for key, value in extra_context.items():
                substitutions[key] = value if isinstance(value, str) else _json_for_prompt(value)
        for placeholder, replacement in substitutions.items():
            template = template.replace("{{" + placeholder + "}}", replacement)
        return template

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
        self._accumulate_json(
            self.staging_dir / "candidate_entities.json",
            [_dump_model(entity) for entity in entities],
        )
        self._accumulate_json(
            self.staging_dir / "candidate_relations.json",
            [_ensure_relation_id(r) for r in (_dump_model(rel) for rel in relations)],
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
