"""Runtime ontology loading and pre-staging validation for FAIR-VCG-mentor.

The runtime loader exposes labels, relationships, domain/range constraints,
and required fields from app/ontology/startup_ontology.yaml.  The candidate
validator uses the same ontology data to partition LLM extractions before they
enter the human review queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ONTOLOGY_PATH = PROJECT_ROOT / "app" / "ontology" / "startup_ontology.yaml"
DEFAULT_VIOLATIONS_PATH = PROJECT_ROOT / "data" / "staging" / "shacl_violations.json"

_BASE_LABELS = {"Entity", "Document"}
_FALLBACK_RELATIONS = {"RELATED_TO", "MENTIONS", "SOURCE_OF"}


class OntologyLoader:
    """Load and query the startup ontology YAML."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path or DEFAULT_ONTOLOGY_PATH)
        self._data: dict[str, Any] = self._load()

    @property
    def version(self) -> str:
        return str(self._data.get("version", "unknown"))

    @property
    def allowed_labels(self) -> set[str]:
        return set(self._data.get("classes", {}).keys()) | _BASE_LABELS

    @property
    def allowed_relationships(self) -> set[str]:
        predicates = {r["predicate"] for r in self._data.get("relations", []) if "predicate" in r}
        return predicates | _FALLBACK_RELATIONS

    @property
    def domain_range_map(self) -> dict[str, list[tuple[str, str]]]:
        result: dict[str, list[tuple[str, str]]] = {}
        for rel in self._data.get("relations", []):
            pred = rel.get("predicate")
            subj = rel.get("subject")
            obj = rel.get("object")
            if pred and subj and obj:
                result.setdefault(pred, []).append((subj, obj))
        return result

    def required_fields(self, entity_type: str) -> list[str]:
        classes = self._data.get("classes", {})
        return list(classes.get(entity_type, {}).get("required_fields", []))

    def validate_relation(
        self,
        rel_type: str,
        source_type: str | None,
        target_type: str | None,
    ) -> bool:
        if not source_type or not target_type:
            return True
        allowed_pairs = self.domain_range_map.get(rel_type)
        if not allowed_pairs:
            return True
        permissive = {"Entity", "Document"}
        for subj, obj in allowed_pairs:
            if subj in permissive or obj in permissive:
                return True
            if source_type == subj and target_type == obj:
                return True
        return False

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


_loader: OntologyLoader | None = None


def get_ontology() -> OntologyLoader:
    """Return the module-level OntologyLoader singleton."""
    global _loader
    if _loader is None:
        _loader = OntologyLoader()
    return _loader


@dataclass
class Violation:
    candidate_id: str
    kind: str
    rule: str
    detail: str
    candidate: dict[str, Any]


@dataclass
class ValidationReport:
    valid_entities: list[dict[str, Any]] = field(default_factory=list)
    valid_relations: list[dict[str, Any]] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        return (
            f"{len(self.valid_entities)} valid entities, "
            f"{len(self.valid_relations)} valid relations, "
            f"{len(self.violations)} violation(s)"
        )


class OntologyValidator:
    """Validate LLM extractions against the runtime ontology before staging."""

    def __init__(self, violations_path: Path | str = DEFAULT_VIOLATIONS_PATH) -> None:
        self.violations_path = Path(violations_path)
        self._ontology = get_ontology()

    @property
    def allowed_entity_types(self) -> set[str]:
        return self._ontology.allowed_labels - _BASE_LABELS

    @property
    def allowed_predicates(self) -> set[str]:
        return self._ontology.allowed_relationships

    def validate(
        self,
        entities: list[Any],
        relations: list[Any],
    ) -> ValidationReport:
        """Return a report partitioning candidates into valid and violated sets."""
        from app.services.entity_extractor import _dump_model

        entity_dicts = [item if isinstance(item, dict) else _dump_model(item) for item in entities]
        relation_dicts = [item if isinstance(item, dict) else _dump_model(item) for item in relations]

        report = ValidationReport()
        entity_types_by_id: dict[str, str] = {}

        for entity in entity_dicts:
            violations = self._check_entity(entity)
            if violations:
                for rule, detail in violations:
                    report.violations.append(Violation(
                        candidate_id=str(entity.get("id") or entity.get("temporary_id") or "?"),
                        kind="entity",
                        rule=rule,
                        detail=detail,
                        candidate=entity,
                    ))
            else:
                report.valid_entities.append(entity)
                entity_id = str(entity.get("id") or entity.get("temporary_id") or "")
                if entity_id:
                    entity_types_by_id[entity_id] = str(entity.get("type") or "")

        for relation in relation_dicts:
            violations = self._check_relation(relation, entity_types_by_id)
            if violations:
                for rule, detail in violations:
                    report.violations.append(Violation(
                        candidate_id=str(relation.get("id") or "?"),
                        kind="relation",
                        rule=rule,
                        detail=detail,
                        candidate=relation,
                    ))
            else:
                report.valid_relations.append(relation)

        if report.violations:
            self._write_violations(report.violations)

        return report

    def _check_entity(self, entity: dict[str, Any]) -> list[tuple[str, str]]:
        issues: list[tuple[str, str]] = []
        entity_id = entity.get("id") or entity.get("temporary_id")
        if not entity_id:
            issues.append(("missing-id", "Entity has no id or temporary_id"))
        label = entity.get("label") or entity.get("name")
        if not label:
            issues.append(("missing-label", "Entity has no label or name"))
        entity_type = entity.get("type")
        if not entity_type:
            issues.append(("missing-type", "Entity has no type"))
        elif self.allowed_entity_types and entity_type not in self.allowed_entity_types:
            issues.append(("unknown-type", f"Entity type '{entity_type}' is not in the ontology"))
        return issues

    def _check_relation(
        self,
        relation: dict[str, Any],
        entity_types_by_id: dict[str, str],
    ) -> list[tuple[str, str]]:
        issues: list[tuple[str, str]] = []
        src = str(relation.get("source_entity_id") or relation.get("subject_temporary_id") or "")
        tgt = str(relation.get("target_entity_id") or relation.get("object_temporary_id") or "")
        predicate = relation.get("predicate") or relation.get("type")

        if not src:
            issues.append(("missing-source", "Relation has no source_entity_id or subject_temporary_id"))
        if not tgt:
            issues.append(("missing-target", "Relation has no target_entity_id or object_temporary_id"))
        if not predicate:
            issues.append(("missing-predicate", "Relation has no predicate or type"))
        elif self.allowed_predicates and predicate not in self.allowed_predicates:
            issues.append(("unknown-predicate", f"Predicate '{predicate}' is not in the ontology"))

        if predicate and src and tgt:
            source_type = entity_types_by_id.get(src)
            target_type = entity_types_by_id.get(tgt)
            if not self._ontology.validate_relation(str(predicate), source_type, target_type):
                issues.append((
                    "invalid-domain-range",
                    f"Predicate '{predicate}' is not valid for {source_type} -> {target_type}",
                ))

        return issues

    def _write_violations(self, violations: list[Violation]) -> None:
        self.violations_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, Any]] = []
        if self.violations_path.exists():
            try:
                existing = json.loads(self.violations_path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []

        new_records = [
            {
                "candidate_id": v.candidate_id,
                "kind": v.kind,
                "rule": v.rule,
                "detail": v.detail,
                "candidate": v.candidate,
            }
            for v in violations
        ]
        payload = existing + new_records
        text = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        tmp = self.violations_path.with_suffix(self.violations_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.violations_path)
