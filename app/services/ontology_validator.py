"""Runtime ontology loading and pre-staging validation for FounderGraph-Lab.

The runtime loader exposes labels, relationships, domain/range constraints,
and required fields.  Phase 1 wired this loader to the generated
``app/ontology/generated/schema.json`` so all downstream code consumes the
LinkML source of truth instead of a hand-edited YAML.

Backward-compat note: the legacy ``app/ontology/startup_ontology.yaml`` is
still read by ``ontology_service.py`` (HITL wizard) -- ``OntologyLoader``
falls back to that YAML when ``schema.json`` is missing so a fresh checkout
without ``make generate`` still boots; if both inputs disagree, the generated
file wins.  Run ``make generate`` to refresh the artifacts.

The candidate validator uses the same ontology data to partition LLM
extractions before they enter the human review queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ONTOLOGY_PATH = PROJECT_ROOT / "app" / "ontology" / "startup_ontology.yaml"
DEFAULT_SCHEMA_JSON_PATH = (
    PROJECT_ROOT / "app" / "ontology" / "generated" / "schema.json"
)
DEFAULT_LINKML_PATH = (
    PROJECT_ROOT / "app" / "ontology" / "startup_ontology.linkml.yaml"
)
DEFAULT_VIOLATIONS_PATH = PROJECT_ROOT / "data" / "staging" / "shacl_violations.json"

_BASE_LABELS = {"Entity", "Document"}
_FALLBACK_RELATIONS = {"RELATED_TO", "MENTIONS", "SOURCE_OF"}


class OntologyLoader:
    """Load and query the FounderGraph ontology.

    The loader prefers the generated ``schema.json`` (LinkML source of truth).
    If that file is missing -- typically when a developer has not run
    ``make generate`` yet -- it falls back to parsing the legacy
    ``startup_ontology.yaml`` so the runtime still boots.  ``path`` retains
    its legacy meaning (it's the YAML path that the HITL wizard edits) so
    existing tests like ``OntologyLoader(tmp_path / "nonexistent.yaml")``
    continue to work and degrade gracefully.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        schema_json_path: Path | str | None = None,
    ) -> None:
        self._path = Path(path or DEFAULT_ONTOLOGY_PATH)
        self._schema_json_path = Path(schema_json_path or DEFAULT_SCHEMA_JSON_PATH)
        # ``schema.json`` is preferred; falling back to the YAML keeps the
        # call sites that pass tmp_path-style legacy paths working in tests.
        if path is None and self._schema_json_path.exists():
            self._data = self._load_from_jsonschema()
            self._source = "schema.json"
        elif self._path.exists():
            self._data = self._load_legacy_yaml()
            self._source = "yaml"
        else:
            self._data = {}
            self._source = "empty"

    @property
    def source(self) -> str:
        """Indicates which artifact the loader is currently reading from.

        Useful in diagnostics / Streamlit footers so reviewers can tell at a
        glance whether the runtime is reading the generated schema or the
        legacy YAML fallback.
        """
        return self._source

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
        """Return the fields the LLM is told to extract for ``entity_type``.

        Historically this returned the full ``fields`` list from the legacy
        YAML (used to prime extraction prompts), not a strict JSON-Schema
        ``required`` list.  We preserve that behavior: callers wanting the
        truly required subset (e.g. ``id``, ``criticality``) should use
        ``strictly_required_fields`` instead.
        """
        classes = self._data.get("classes", {})
        cls = classes.get(entity_type, {})
        fields = list(cls.get("fields") or [])
        if fields:
            return fields
        return list(cls.get("required_fields") or [])

    def strictly_required_fields(self, entity_type: str) -> list[str]:
        """Return the LinkML-required subset of ``entity_type``'s fields."""
        classes = self._data.get("classes", {})
        cls = classes.get(entity_type, {})
        return list(cls.get("required_fields") or [])

    def validate_relation(
        self,
        rel_type: str,
        source_type: str | None,
        target_type: str | None,
    ) -> bool:
        """Strict domain/range validation.

        Untyped endpoints are no longer accepted — callers must supply both
        ``source_type`` and ``target_type``.  Use ``validate_relation_detail``
        to obtain a structured reason when validation fails.
        """
        ok, _ = self.validate_relation_detail(rel_type, source_type, target_type)
        return ok

    def validate_relation_detail(
        self,
        rel_type: str,
        source_type: str | None,
        target_type: str | None,
    ) -> tuple[bool, str | None]:
        """Return (is_valid, violation_reason).

        ``violation_reason`` is ``None`` on success; otherwise a short string
        the caller may surface to the user / staging report.
        """
        if not source_type or not target_type:
            return False, (
                f"Untyped endpoint(s) for predicate '{rel_type}': "
                f"source_type={source_type!r}, target_type={target_type!r}"
            )
        allowed_pairs = self.domain_range_map.get(rel_type)
        if not allowed_pairs:
            # Predicate is whitelisted but has no domain/range registered —
            # accept rather than block (e.g. legacy/test predicates).
            return True, None
        permissive = {"Entity", "Document"}
        for subj, obj in allowed_pairs:
            if subj in permissive or obj in permissive:
                return True, None
            if source_type == subj and target_type == obj:
                return True, None
        return False, (
            f"{source_type} -{rel_type}-> {target_type} is not an allowed domain/range pair"
        )

    def _load_from_jsonschema(self) -> dict[str, Any]:
        """Build the legacy-shaped data dict from the generated JSON-Schema."""
        try:
            payload = json.loads(self._schema_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        defs = payload.get("$defs", {}) or {}
        # Build the ``classes`` mapping so required_fields() and allowed_labels
        # behave the same as they did when reading the legacy YAML.
        classes: dict[str, dict[str, Any]] = {}
        entity_labels = payload.get("x-foundergraph-entity-labels") or []
        for name in entity_labels:
            spec = defs.get(name, {})
            if not isinstance(spec, dict):
                continue
            props = list(spec.get("properties", {}).keys()) if isinstance(spec.get("properties"), dict) else []
            required = list(spec.get("required") or [])
            classes[name] = {
                "description": spec.get("description", ""),
                "fields": props,
                "required_fields": required,
            }

        # Relation slots are stashed under ``x-foundergraph-relations`` by the
        # generator so we can recover (predicate, subject, object) triples.
        relations: list[dict[str, str]] = []
        for entry in payload.get("x-foundergraph-relations") or []:
            if not isinstance(entry, dict):
                continue
            pred = entry.get("predicate")
            subj = entry.get("domain") or "Entity"
            obj = entry.get("range") or "Entity"
            if pred:
                relations.append({"predicate": pred, "subject": subj, "object": obj})

        # Linkml schemas store the version at the root; the JSON Schema mirrors
        # it under metadata if available -- otherwise we fall back to "unknown".
        version = payload.get("metadata", {}).get("version") if isinstance(payload.get("metadata"), dict) else None
        if not version:
            # The version is preserved through the LinkML pydantic generator's
            # ``version = "X.Y.Z"`` module-level constant.  When that's not
            # parsable we re-read it from the linkml YAML.
            version = self._read_linkml_version() or "unknown"

        return {"classes": classes, "relations": relations, "version": version}

    def _read_linkml_version(self) -> str | None:
        try:
            text = DEFAULT_LINKML_PATH.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version:"):
                # ``version: "0.2.0"`` -- strip quotes if present.
                return stripped.split(":", 1)[1].strip().strip('"').strip("'")
        return None

    def _load_legacy_yaml(self) -> dict[str, Any]:
        """Fallback path: parse the editable legacy YAML directly."""
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

        # Phase 1.7: deterministic SHACL pass on top of the structural
        # checks above.  Failures are recorded in the same violations file
        # (one line per failure) and surfaced as ``shacl-violation`` rules so
        # the UI can render them alongside the legacy checks.  Gated by
        # FG_SHACL_ENABLED so operators can roll back if false positives
        # creep in.
        try:
            from app.services import shacl_gate

            shacl_violations = shacl_gate.run_gate(
                report.valid_entities + [v.candidate for v in report.violations if v.kind == "entity"],
                report.valid_relations,
                violations_path=self.violations_path,
            )
        except Exception:  # noqa: BLE001 — never let an optional dep crash the validator
            shacl_violations = []

        for sv in shacl_violations:
            report.violations.append(
                Violation(
                    candidate_id=sv.focus_node or "?",
                    kind="shacl",
                    rule=sv.constraint or "shacl-violation",
                    detail=sv.message or "",
                    candidate={
                        "focus_node": sv.focus_node,
                        "path": sv.path,
                        "value": sv.value,
                        "severity": sv.severity,
                        "source_shape": sv.source_shape,
                    },
                )
            )

        if report.violations:
            # Persist only the *non-SHACL* violations here; SHACL violations
            # are already on disk thanks to ``shacl_gate.run_gate``.
            structural = [v for v in report.violations if v.kind != "shacl"]
            if structural:
                self._write_violations(structural)

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
            ok, reason = self._ontology.validate_relation_detail(
                str(predicate), source_type, target_type
            )
            if not ok:
                issues.append((
                    "invalid-domain-range",
                    reason or f"Predicate '{predicate}' is not valid for {source_type} -> {target_type}",
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
