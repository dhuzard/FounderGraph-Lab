"""Runtime ontology loader and validator for FounderGraph Lab.

Reads app/ontology/startup_ontology.yaml and exposes the allowed labels,
allowed relationships, domain/range constraints, and required fields so that
every other layer (extraction prompts, Neo4j writes, exports) can derive its
rules from a single source of truth instead of hard-coded Python sets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ONTOLOGY_PATH = PROJECT_ROOT / "app" / "ontology" / "startup_ontology.yaml"

# Always-present base labels that are not in the ontology classes section.
_BASE_LABELS = {"Entity", "Document"}
# Always-present fallback relationship that is never domain/range constrained.
_FALLBACK_RELATIONS = {"RELATED_TO", "MENTIONS", "SOURCE_OF"}


class OntologyLoader:
    """Load and query the startup ontology YAML.

    Provides:
      - allowed_labels          — set of valid Neo4j node labels
      - allowed_relationships   — set of valid Neo4j relationship types
      - domain_range_map        — {predicate: [(subject_type, object_type), ...]}
      - required_fields(type)   — list of required property names for a class
      - validate_relation(...)  — bool domain/range check

    Falls back to empty collections rather than raising if the YAML is missing
    or malformed, so the application degrades gracefully.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path or DEFAULT_ONTOLOGY_PATH)
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

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
        """Map predicate → [(subject_type, object_type), ...].

        Predicates with domain/range "Entity" or "Document" are treated as
        permissive (any subject/object type is accepted).
        """
        result: dict[str, list[tuple[str, str]]] = {}
        for rel in self._data.get("relations", []):
            pred = rel.get("predicate")
            subj = rel.get("subject")
            obj = rel.get("object")
            if pred and subj and obj:
                result.setdefault(pred, []).append((subj, obj))
        return result

    def required_fields(self, entity_type: str) -> list[str]:
        """Return the list of required property names for an entity class."""
        classes = self._data.get("classes", {})
        return list(classes.get(entity_type, {}).get("required_fields", []))

    def validate_relation(
        self,
        rel_type: str,
        source_type: str | None,
        target_type: str | None,
    ) -> bool:
        """Return True if (source_type, rel_type, target_type) is a valid triple.

        Returns True (permissive) when:
          - the relation has no domain/range restrictions in the ontology
          - either source_type or target_type is unknown/None
          - either side is the permissive "Entity" or "Document" base class
        """
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001 — degrade gracefully on any parse error
            return {}


# Module-level singleton so the YAML is parsed once per process.
_loader: OntologyLoader | None = None


def get_ontology() -> OntologyLoader:
    """Return the module-level OntologyLoader singleton."""
    global _loader
    if _loader is None:
        _loader = OntologyLoader()
    return _loader
