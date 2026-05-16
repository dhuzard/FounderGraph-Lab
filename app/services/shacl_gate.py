"""Deterministic SHACL gate over staged FounderGraph extractions.

This module is the ``OntologyValidator``'s deterministic counterpart: where
the validator runs Python-level structural checks (id present, type
whitelisted, domain/range allowed), the SHACL gate runs the LinkML-generated
``app/ontology/generated/shapes.ttl`` over an in-memory RDF graph built from
the staged candidates.  This catches *content* errors (e.g. an Assumption
missing its ``criticality`` value) that the structural checks alone don't
see.

Design notes
------------
*   The RDF serialization is intentionally minimal: each entity becomes a
    typed resource (``fg:<id> a fg:<Type>``) with its dict properties as
    literal triples.  We don't attempt to reify Neo4j relationships --
    SHACL shapes are class-scoped and care about per-node properties.
*   Violations are persisted to ``data/staging/shacl_violations.json``
    atomically (``.tmp`` rename, same pattern as ``ValidationStore``).
*   The whole gate can be turned off with ``FG_SHACL_ENABLED=0`` for
    operators that want the legacy behavior while we evaluate false-positive
    rates -- the gate appends to violations, it never blocks writes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# rdflib + pyshacl are heavy imports; defer until ``validate()`` actually
# runs so importing this module is cheap even on machines without the deps.


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHAPES_PATH = (
    PROJECT_ROOT / "app" / "ontology" / "generated" / "shapes.ttl"
)
DEFAULT_VIOLATIONS_PATH = (
    PROJECT_ROOT / "data" / "staging" / "shacl_violations.json"
)

# Namespace must match the LinkML schema's ``default_prefix: fg`` declaration
# so the URIs we mint line up with the SHACL ``sh:targetClass`` IRIs.
FG_NAMESPACE = "https://foundergraph.dev/ontology/"


def is_enabled() -> bool:
    """Return False when the operator has flipped FG_SHACL_ENABLED off.

    Default is on -- the kill-switch is for emergency rollback only.
    """
    raw = os.environ.get("FG_SHACL_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Violation:
    """A single SHACL constraint failure on a staged candidate."""

    focus_node: str
    constraint: str
    severity: str
    message: str
    source_shape: str | None = None
    path: str | None = None
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def serialize_staging_to_rdf(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
):
    """Build an rdflib Graph from staged entities + relations.

    Each entity becomes ``fg:<id> a fg:<Type>`` plus one literal triple per
    populated property.  ``relations`` is currently accepted for forward
    compatibility but intentionally ignored by the serializer because the
    generated NodeShapes are closed-world and relation triples would trigger
    ``sh:ClosedConstraintComponent`` noise.

    Returns an ``rdflib.Graph``; raises ``ImportError`` if rdflib isn't
    available (we surface this at call-time so the module import remains
    side-effect-free).
    """
    from rdflib import Graph, Literal, Namespace, URIRef
    from rdflib.namespace import RDF

    graph = Graph()
    fg = Namespace(FG_NAMESPACE)
    graph.bind("fg", fg)

    for entity in entities or []:
        entity_id = str(entity.get("id") or entity.get("temporary_id") or "").strip()
        entity_type = str(entity.get("type") or "").strip()
        if not entity_id or not entity_type:
            # Structural errors are the OntologyValidator's job; SHACL only
            # reasons about well-formed nodes.
            continue
        subject = URIRef(f"{FG_NAMESPACE}{entity_id}")
        graph.add((subject, RDF.type, URIRef(f"{FG_NAMESPACE}{entity_type}")))

        for key, value in entity.items():
            if key in {"id", "type", "temporary_id"}:
                continue
            if value is None or value == "":
                continue
            # Skip nested structures -- those map onto separate property
            # shapes that our current SHACL output doesn't constrain.
            if isinstance(value, (dict, list)):
                continue
            predicate = URIRef(f"{FG_NAMESPACE}{key}")
            graph.add((subject, predicate, Literal(value)))

    # NOTE: We deliberately do NOT serialize the ``relations`` payload to
    # RDF here.  The LinkML-generated NodeShapes are closed-world
    # (``sh:closed true``) so any extra property -- including edges to other
    # entities -- would trip a ``sh:ClosedConstraintComponent`` violation
    # that the OntologyValidator's domain/range whitelisting already covers.
    # We keep the ``relations`` parameter on this function so callers can
    # extend the gate without breaking the signature (e.g. add a Relation
    # NodeShape later that constrains evidence_grade on every edge).
    _ = relations  # explicitly ignored for SHACL purposes today.

    return graph


def validate(
    graph,
    shapes_path: Path | str | None = None,
) -> list[Violation]:
    """Run pySHACL over ``graph`` and return any constraint violations.

    The shapes file is the LinkML-generated ``shapes.ttl`` by default; tests
    may inject a different path.  When the file is missing we return an
    empty list so a fresh checkout without ``make generate`` doesn't crash
    -- the OntologyValidator already enforces structural checks.
    """
    from pyshacl import validate as pyshacl_validate
    from rdflib import Graph

    shapes = Path(shapes_path or DEFAULT_SHAPES_PATH)
    if not shapes.exists():
        return []

    # ``shapes.ttl`` is serialized as canonical N-Triples by the generator;
    # ``rdflib`` auto-detects the format from the file content.
    shapes_graph = Graph()
    shapes_graph.parse(str(shapes))

    conforms, results_graph, _ = pyshacl_validate(
        data_graph=graph,
        shacl_graph=shapes_graph,
        inference="none",
        meta_shacl=False,
        debug=False,
        advanced=False,
    )
    if conforms:
        return []

    return _extract_violations(results_graph)


def _extract_violations(results_graph) -> list[Violation]:
    """Translate a pySHACL results graph into structured Violation records."""
    from rdflib.namespace import RDF, Namespace

    SH = Namespace("http://www.w3.org/ns/shacl#")
    violations: list[Violation] = []
    for result in results_graph.subjects(RDF.type, SH.ValidationResult):
        focus = results_graph.value(result, SH.focusNode)
        constraint = results_graph.value(result, SH.sourceConstraintComponent)
        message = results_graph.value(result, SH.resultMessage)
        severity = results_graph.value(result, SH.resultSeverity)
        path = results_graph.value(result, SH.resultPath)
        value = results_graph.value(result, SH.value)
        source_shape = results_graph.value(result, SH.sourceShape)
        violations.append(
            Violation(
                focus_node=_short_iri(focus),
                constraint=_short_iri(constraint),
                severity=_short_iri(severity) or "Violation",
                message=str(message) if message else "",
                source_shape=_short_iri(source_shape),
                path=_short_iri(path),
                value=str(value) if value is not None else None,
            )
        )
    return violations


def _short_iri(node) -> str | None:
    """Render an rdflib IRI/BNode/Literal as a short string for logging."""
    if node is None:
        return None
    text = str(node)
    if text.startswith(FG_NAMESPACE):
        return text[len(FG_NAMESPACE):]
    # Trim the SHACL namespace down to ``sh:Foo`` for readability.
    if text.startswith("http://www.w3.org/ns/shacl#"):
        return "sh:" + text[len("http://www.w3.org/ns/shacl#"):]
    return text


def write_violations(
    violations: list[Violation],
    path: Path | str | None = None,
) -> Path:
    """Atomically persist ``violations`` to JSON.

    Uses the same write-tmp-then-rename pattern as ``ValidationStore`` so a
    crash mid-write never leaves a partially-serialized JSON file on disk.
    Existing violations in the target file are preserved (we append).
    """
    target = Path(path or DEFAULT_VIOLATIONS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []
    payload = existing + [v.to_dict() for v in violations]
    text = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target


def run_gate(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
    *,
    shapes_path: Path | str | None = None,
    violations_path: Path | str | None = None,
) -> list[Violation]:
    """End-to-end helper used by ``OntologyValidator``.

    Builds the RDF graph, runs SHACL, and persists any violations atomically
    via ``write_violations``.  Returns the violation list so callers can
    surface them in the same report as the structural checks.

    Honors the ``FG_SHACL_ENABLED`` kill-switch -- when off, we return an
    empty list without touching disk.
    """
    if not is_enabled():
        return []
    try:
        graph = serialize_staging_to_rdf(entities, relations)
        violations = validate(graph, shapes_path=shapes_path)
    except ImportError:
        # pySHACL or rdflib missing -- treat as opt-out, never crash the
        # validator pipeline because of a missing optional dep.
        return []
    if violations:
        write_violations(violations, violations_path)
    return violations
