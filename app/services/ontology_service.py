"""Static ontology configuration helpers: load, modify, and save the YAML.

Boundary note — two modules read startup_ontology.yaml with different APIs:

  ontology_validator.OntologyLoader / get_ontology()
      Runtime validator used by Neo4jService and EntityExtractor.
      Parses the YAML directly; provides allowed_labels, allowed_relationships,
      domain_range_map, required_fields(), and validate_relation().
      This is the authoritative source for graph-write decisions.

  ontology_service.OntologyConfig / load_ontology() / save_ontology()  ← THIS MODULE
      Static configuration helper used by scripts/init_ontology.py (the CLI
      wizard) and the ontology-editing UI.  Provides mutable dataclass objects
      so the wizard can add/remove/rename entity classes and save changes back
      to the YAML.  Not used at runtime by Neo4jService or EntityExtractor.

Keep these two roles separate: use get_ontology() for validation at runtime;
use load_ontology()/save_ontology() for interactive YAML editing only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_YAML = Path(__file__).resolve().parents[2] / "app" / "ontology" / "startup_ontology.yaml"

# Generic utility predicates that are valid across any entity pair and are not
# captured as explicit subject→object triplets in the YAML.
_UTILITY_RELATIONS: frozenset[str] = frozenset({
    "RELATED_TO",
    "PROVIDES",
    "COMPETES_ON",
    "PROTECTS",
    "MENTIONS",
    "SOURCE_OF",
    "DEPENDS_ON",
})


@dataclass
class EntityClassDef:
    description: str = ""
    fields: list[str] = field(default_factory=list)


@dataclass
class RelationDef:
    subject: str
    predicate: str
    object: str
    description: str | None = None


@dataclass
class OntologyConfig:
    entity_classes: dict[str, EntityClassDef] = field(default_factory=dict)
    relations: list[RelationDef] = field(default_factory=list)
    domain: str = ""
    goals: list[str] = field(default_factory=list)

    def allowed_labels(self) -> set[str]:
        """Node labels that Neo4jService is permitted to write."""
        return {"Entity", "Document"} | set(self.entity_classes.keys())

    def allowed_relationships(self) -> set[str]:
        """Relationship types that Neo4jService is permitted to write."""
        return {r.predicate for r in self.relations} | _UTILITY_RELATIONS

    def add_entity_class(self, name: str, description: str, fields: list[str] | None = None) -> None:
        self.entity_classes[name] = EntityClassDef(
            description=description,
            fields=list(fields or []),
        )

    def remove_entity_class(self, name: str) -> bool:
        if name in self.entity_classes:
            del self.entity_classes[name]
            return True
        return False

    def rename_entity_class(self, old: str, new: str) -> bool:
        if old not in self.entity_classes:
            return False
        self.entity_classes[new] = self.entity_classes.pop(old)
        return True

    def add_relation(self, subject: str, predicate: str, obj: str, description: str | None = None) -> None:
        self.relations.append(RelationDef(subject=subject, predicate=predicate, object=obj, description=description))

    def entity_type_names(self) -> list[str]:
        return sorted(self.entity_classes.keys())

    def relation_predicates(self) -> list[str]:
        return sorted({r.predicate for r in self.relations} | _UTILITY_RELATIONS)


def load_ontology(path: Path | str | None = None) -> OntologyConfig:
    """Load ontology YAML; return an empty OntologyConfig if the file does not exist."""
    p = Path(path or _DEFAULT_YAML)
    if not p.exists():
        return OntologyConfig()

    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    classes: dict[str, EntityClassDef] = {}
    for name, spec in (raw.get("classes") or {}).items():
        if isinstance(spec, dict):
            classes[name] = EntityClassDef(
                description=spec.get("description", ""),
                fields=list(spec.get("fields") or []),
            )
        else:
            classes[name] = EntityClassDef()

    relations: list[RelationDef] = []
    for item in raw.get("relations") or []:
        if isinstance(item, dict) and item.get("predicate"):
            relations.append(RelationDef(
                subject=str(item.get("subject", "*")),
                predicate=str(item["predicate"]),
                object=str(item.get("object", "*")),
                description=item.get("description") or None,
            ))

    return OntologyConfig(
        entity_classes=classes,
        relations=relations,
        domain=str(raw.get("domain", "")),
        goals=list(raw.get("goals") or []),
    )


def save_ontology(config: OntologyConfig, path: Path | str | None = None) -> Path:
    """Atomically write the ontology to YAML and return the path."""
    p = Path(path or _DEFAULT_YAML)
    p.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if config.domain:
        data["domain"] = config.domain
    if config.goals:
        data["goals"] = list(config.goals)

    data["classes"] = {
        name: {"description": cls.description, "fields": cls.fields}
        for name, cls in config.entity_classes.items()
    }
    data["relations"] = [
        {k: v for k, v in {
            "subject": r.subject,
            "predicate": r.predicate,
            "object": r.object,
            "description": r.description,
        }.items() if v is not None}
        for r in config.relations
    ]

    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(p)
    return p
