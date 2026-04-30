"""Storage helpers for human-reviewed knowledge candidates.

The store is intentionally JSON-file based so extracted knowledge can be
reviewed before any graph writes happen. Only records with ``status=validated``
should be passed to Neo4j.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


try:
    from app.config import (
        CANDIDATE_ENTITIES_JSON,
        CANDIDATE_RELATIONS_JSON,
        KNOWLEDGE_DIR,
    )
except ImportError:
    KNOWLEDGE_DIR = Path("data/knowledge")
    CANDIDATE_ENTITIES_JSON = Path("data/staging/candidate_entities.json")
    CANDIDATE_RELATIONS_JSON = Path("data/staging/candidate_relations.json")


DEFAULT_KNOWLEDGE_DIR = KNOWLEDGE_DIR
ENTITY_CANDIDATE_NAMES = ("candidate_entities.json", "entities_candidates.json", "entities.json")
RELATION_CANDIDATE_NAMES = (
    "candidate_relations.json",
    "relations_candidates.json",
    "relations.json",
)
VALIDATED_ENTITIES_FILE = "validated_entities.json"
VALIDATED_RELATIONS_FILE = "validated_relations.json"

VALIDATION_STATUSES = ("pending", "validated", "rejected", "needs_review", "needs_more_evidence")


class ValidationStoreError(ValueError):
    """Raised when validation data cannot be read or written safely."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValidationStoreError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def first_existing(base_dir: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        path = base_dir / name
        if path.exists():
            return path
    return None


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Accept either a list or common wrapped payloads from extraction agents."""
    if payload is None:
        return []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("items", "records", "entities", "relations", "data", "candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        else:
            records = [payload]
    else:
        raise ValidationStoreError(f"Expected JSON object or list, got {type(payload).__name__}")

    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationStoreError(f"Record {index} is not an object")
        normalized.append(deepcopy(record))
    return normalized


def ensure_record_id(record: dict[str, Any], prefix: str, index: int) -> dict[str, Any]:
    candidate = deepcopy(record)
    existing = candidate.get("id") or candidate.get("uid") or candidate.get("key")
    candidate["id"] = str(existing or f"{prefix}_{index + 1:04d}")
    return candidate


def normalize_entity(record: dict[str, Any], index: int) -> dict[str, Any]:
    entity = ensure_record_id(record, "entity", index)
    entity.setdefault("type", entity.get("category") or "Entity")
    entity.setdefault("label", entity.get("name") or entity.get("text") or "")
    entity.setdefault("name", entity.get("label") or entity.get("text") or "")
    entity.setdefault("description", "")
    status = entity.get("status") or entity.get("validation_status") or "pending"
    entity["status"] = status
    entity["validation_status"] = status
    entity.setdefault(
        "provenance",
        {
            "source_document_id": entity.get("source_document_id"),
            "source_file": entity.get("source_file"),
            "source_location": entity.get("source_location"),
        },
    )
    entity.setdefault("source_snippet", entity.get("snippet") or entity.get("source_snippet") or "")
    entity.setdefault("metadata", {})
    return entity


def normalize_relation(record: dict[str, Any], index: int) -> dict[str, Any]:
    relation = ensure_record_id(record, "relation", index)
    relation.setdefault("source_entity_id", relation.get("subject_id") or relation.get("source") or relation.get("from") or "")
    relation.setdefault("target_entity_id", relation.get("object_id") or relation.get("target") or relation.get("to") or "")
    relation.setdefault("subject_id", relation.get("source_entity_id", ""))
    relation.setdefault("object_id", relation.get("target_entity_id", ""))
    relation.setdefault("type", relation.get("predicate") or relation.get("relation") or "RELATED_TO")
    relation.setdefault("predicate", relation.get("type", "RELATED_TO"))
    status = relation.get("status") or relation.get("validation_status") or "pending"
    relation["status"] = status
    relation["validation_status"] = status
    relation.setdefault(
        "provenance",
        {
            "source_document_id": relation.get("source_document_id"),
            "source_file": relation.get("source_file"),
        },
    )
    relation.setdefault("source_snippet", relation.get("snippet") or relation.get("source_snippet") or "")
    relation.setdefault("metadata", {})
    return relation


def _merge_by_id(candidates: list[dict[str, Any]], reviewed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(item.get("id")): deepcopy(item) for item in candidates if item.get("id")}
    for item in reviewed:
        item_id = str(item.get("id", ""))
        if item_id:
            merged[item_id] = deepcopy(item)
    return list(merged.values())


class ValidationStore:
    """Read candidate JSON, merge edits, and persist validated JSON files."""

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_KNOWLEDGE_DIR,
        entity_candidate_path: Path | str | None = None,
        relation_candidate_path: Path | str | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.entity_candidate_path = Path(entity_candidate_path or CANDIDATE_ENTITIES_JSON)
        self.relation_candidate_path = Path(relation_candidate_path or CANDIDATE_RELATIONS_JSON)

    @property
    def validated_entities_path(self) -> Path:
        return self.base_dir / VALIDATED_ENTITIES_FILE

    @property
    def validated_relations_path(self) -> Path:
        return self.base_dir / VALIDATED_RELATIONS_FILE

    def load_entities(self) -> list[dict[str, Any]]:
        candidate_path = self.entity_candidate_path if self.entity_candidate_path.exists() else first_existing(self.base_dir, ENTITY_CANDIDATE_NAMES)
        candidates = extract_records(load_json(candidate_path)) if candidate_path else []
        normalized = [normalize_entity(item, index) for index, item in enumerate(candidates)]
        reviewed = extract_records(load_json(self.validated_entities_path))
        return _merge_by_id(normalized, reviewed)

    def load_relations(self) -> list[dict[str, Any]]:
        candidate_path = self.relation_candidate_path if self.relation_candidate_path.exists() else first_existing(self.base_dir, RELATION_CANDIDATE_NAMES)
        candidates = extract_records(load_json(candidate_path)) if candidate_path else []
        normalized = [normalize_relation(item, index) for index, item in enumerate(candidates)]
        reviewed = extract_records(load_json(self.validated_relations_path))
        return _merge_by_id(normalized, reviewed)

    def save_entities(self, records: list[dict[str, Any]]) -> Path:
        payload = self._prepare_records(records, normalize_entity)
        save_json(self.validated_entities_path, payload)
        return self.validated_entities_path

    def save_relations(self, records: list[dict[str, Any]]) -> Path:
        payload = self._prepare_records(records, normalize_relation)
        save_json(self.validated_relations_path, payload)
        return self.validated_relations_path

    def validated_entities(self) -> list[dict[str, Any]]:
        return [item for item in self.load_entities() if item.get("validation_status", item.get("status")) == "validated"]

    def validated_relations(self) -> list[dict[str, Any]]:
        return [item for item in self.load_relations() if item.get("validation_status", item.get("status")) == "validated"]

    @staticmethod
    def _prepare_records(records: list[dict[str, Any]], normalizer: Any) -> list[dict[str, Any]]:
        prepared = []
        for index, record in enumerate(records):
            item = normalizer(record, index)
            status = item.get("status", "pending")
            if status not in VALIDATION_STATUSES:
                raise ValidationStoreError(f"Unsupported validation status: {status}")
            item["validation_status"] = status
            item["updated_at"] = now_iso()
            prepared.append(item)
        return prepared
