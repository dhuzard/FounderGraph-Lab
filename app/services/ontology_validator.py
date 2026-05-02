"""Deterministic pre-validation gate for LLM-extracted staging candidates.

Validates entity and relation candidates against the OntologyConfig *before*
they enter the human review queue.  Candidates that fail are written to
``data/staging/shacl_violations.json`` with details so operators can diagnose
prompt drift or schema violations without touching Neo4j.

No external dependencies — this is an in-process rule engine, not pySHACL.
Switch to pyshacl + LinkML-generated shapes once the ontology is migrated to
LinkML (see TODO.md — Neurosymbolic / Standards Upgrades).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VIOLATIONS_PATH = PROJECT_ROOT / "data" / "staging" / "shacl_violations.json"


@dataclass
class Violation:
    candidate_id: str
    kind: str  # "entity" | "relation"
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
        v = len(self.violations)
        e = len(self.valid_entities)
        r = len(self.valid_relations)
        return f"{e} valid entities, {r} valid relations, {v} violation(s)"


class OntologyValidator:
    """Validate LLM extractions against the current OntologyConfig.

    Usage::

        validator = OntologyValidator()
        report = validator.validate(entities, relations)
        # report.valid_entities / report.valid_relations are safe to stage
        # report.violations are written to shacl_violations.json
    """

    def __init__(self, violations_path: Path | str = DEFAULT_VIOLATIONS_PATH) -> None:
        self.violations_path = Path(violations_path)
        self._config = self._load_config()

    def _load_config(self):  # type: ignore[return]
        try:
            from app.services.ontology_service import load_ontology
            return load_ontology()
        except Exception:
            return None

    @property
    def allowed_entity_types(self) -> set[str]:
        if self._config is not None:
            return self._config.allowed_labels() - {"Entity", "Document"}
        return set()

    @property
    def allowed_predicates(self) -> set[str]:
        if self._config is not None:
            return self._config.allowed_relationships()
        return set()

    def validate(
        self,
        entities: list[Any],
        relations: list[Any],
    ) -> ValidationReport:
        """Return a ValidationReport partitioning candidates into valid and violated sets."""
        from app.services.entity_extractor import _dump_model

        entity_dicts = [item if isinstance(item, dict) else _dump_model(item) for item in entities]
        relation_dicts = [item if isinstance(item, dict) else _dump_model(item) for item in relations]

        report = ValidationReport()

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

        for relation in relation_dicts:
            violations = self._check_relation(relation)
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

    def _check_relation(self, relation: dict[str, Any]) -> list[tuple[str, str]]:
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
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        tmp = self.violations_path.with_suffix(self.violations_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.violations_path)
