"""Export FounderGraph Lab data into portable files."""

from __future__ import annotations

import csv
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPORT_DIR = Path(os.getenv("FOUNDERGRAPH_EXPORT_DIR", "data/exports"))
AUDIT_DIR = Path(os.getenv("FOUNDERGRAPH_AUDIT_DIR", "vault/audits"))
VALIDATED_ENTITIES_PATH = Path(os.getenv("FOUNDERGRAPH_VALIDATED_ENTITIES", "data/knowledge/validated_entities.json"))
VALIDATED_RELATIONS_PATH = Path(os.getenv("FOUNDERGRAPH_VALIDATED_RELATIONS", "data/knowledge/validated_relations.json"))


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def load_validated_graph() -> dict[str, Any]:
    entities = [
        item
        for item in _load_json_list(VALIDATED_ENTITIES_PATH)
        if item.get("validation_status", item.get("status")) == "validated"
    ]
    relations = [
        item
        for item in _load_json_list(VALIDATED_RELATIONS_PATH)
        if item.get("validation_status", item.get("status")) == "validated"
    ]
    nodes = []
    for entity in entities:
        nodes.append(
            {
                "id": entity.get("id", ""),
                "type": entity.get("type", "Entity"),
                "name": entity.get("label") or entity.get("name", ""),
                "description": entity.get("description", ""),
                # Canonical evidence fields — confidence is not passed through.
                "evidence_grade": entity.get("evidence_grade", ""),
                "reviewer_confidence": entity.get("reviewer_confidence", ""),
                "reviewer_comment": entity.get("reviewer_comment", ""),
                "status": entity.get("validation_status", entity.get("status", "")),
                "source": entity.get("source_file", ""),
                "source_document_id": entity.get("source_document_id", ""),
                "source_snippet": entity.get("source_snippet", ""),
                "tags": entity.get("tags", []),
                # Ontology-defined per-type fields needed by CSV exports.
                "criticality": entity.get("criticality", ""),   # Assumption
                "probability": entity.get("probability", ""),    # Risk
                "impact": entity.get("impact", ""),              # Risk
                "mitigation": entity.get("mitigation", ""),      # Risk, Experiment
                "owner": entity.get("owner", ""),
            }
        )
    edges = []
    for relation in relations:
        edges.append(
            {
                "id": relation.get("id", ""),
                "source": relation.get("subject_id") or relation.get("source_entity_id") or relation.get("source", ""),
                "target": relation.get("object_id") or relation.get("target_entity_id") or relation.get("target", ""),
                "relationship": relation.get("predicate") or relation.get("type", ""),
                # Canonical evidence fields.
                "evidence_grade": relation.get("evidence_grade", ""),
                "reviewer_confidence": relation.get("reviewer_confidence", ""),
                "status": relation.get("validation_status", relation.get("status", "")),
                "source_document_id": relation.get("source_document_id", ""),
                "source_file": relation.get("source_file", ""),
                "source_snippet": relation.get("source_snippet", ""),
            }
        )
    return {"nodes": nodes, "edges": edges}


def create_manifest(
    graph: dict[str, Any],
    export_id: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Build a manifest dict describing the export snapshot."""
    try:
        from app.services.ontology_validator import get_ontology
        ontology_version = get_ontology().version
    except Exception:  # noqa: BLE001
        ontology_version = "unknown"

    node_types: dict[str, int] = {}
    for node in graph.get("nodes", []):
        t = str(node.get("type", "Entity"))
        node_types[t] = node_types.get(t, 0) + 1

    rel_types: dict[str, int] = {}
    for edge in graph.get("edges", []):
        t = str(edge.get("relationship") or edge.get("type", "RELATED_TO"))
        rel_types[t] = rel_types.get(t, 0) + 1

    source_doc_ids = {
        node.get("source_document_id")
        for node in graph.get("nodes", [])
        if node.get("source_document_id")
    }

    weak_grades = {"inference", "speculation"}
    weak_evidence_count = sum(
        1 for node in graph.get("nodes", [])
        if node.get("evidence_grade") in weak_grades
    )

    return {
        "export_id": export_id,
        "graph_snapshot_id": export_id,
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "ontology_version": ontology_version,
        "entity_count": len(graph.get("nodes", [])),
        "relation_count": len(graph.get("edges", [])),
        "source_document_count": len(source_doc_ids),
        "weak_evidence_count": weak_evidence_count,
        "entity_types": node_types,
        "relation_types": rel_types,
        "warning_count": len(warnings),
        "warnings": warnings,
        "generator": "FounderGraph Lab",
    }


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def graph_to_jsonld(graph: dict[str, Any]) -> dict[str, Any]:
    """Serialise the graph as JSON-LD with a proper @context.

    Nodes are typed with fg:<EntityType>; edges use fg:<PREDICATE> as the
    predicate IRI.  source/target are @id references so triple-stores can
    load the file directly.
    """
    context = {
        "@vocab": "https://foundergraph.local/ontology#",
        "fg": "https://foundergraph.local/ontology#",
        "schema": "https://schema.org/",
        # Node properties
        "name": "fg:name",
        "description": "fg:description",
        "source_snippet": "fg:sourceSnippet",
        "source_document_id": {"@id": "fg:sourceDocument", "@type": "@id"},
        "evidence_grade": "fg:evidenceGrade",
        "reviewer_confidence": "fg:reviewerConfidence",
        "reviewer_comment": "fg:reviewerComment",
        "ontology_version": "fg:ontologyVersion",
        # Edge properties
        "fg:sourceEntity": {"@type": "@id"},
        "fg:targetEntity": {"@type": "@id"},
        "relationship": "fg:relationshipType",
    }
    nodes = []
    for node in graph.get("nodes", []):
        item: dict[str, Any] = {
            "@id": f"fg:{node.get('id')}",
            "@type": f"fg:{node.get('type', 'Entity')}",
        }
        for key, value in node.items():
            if key not in {"id", "type"} and value not in (None, "", []):
                item[key] = value
        nodes.append(item)
    edges = []
    for edge in graph.get("edges", []):
        rel = edge.get("relationship") or edge.get("type", "RELATED_TO")
        edge_id = edge.get("id") or f"{edge.get('source')}:{rel}:{edge.get('target')}"
        item = {
            "@id": f"fg:rel:{edge_id}",
            "@type": f"fg:{rel}",
            "fg:sourceEntity": {"@id": f"fg:{edge.get('source')}"},
            "fg:targetEntity": {"@id": f"fg:{edge.get('target')}"},
        }
        if edge.get("source_snippet"):
            item["source_snippet"] = edge["source_snippet"]
        if edge.get("evidence_grade"):
            item["evidence_grade"] = edge["evidence_grade"]
        edges.append(item)
    return {"@context": context, "@graph": nodes + edges}


def _nodes_by_type(graph: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [node for node in graph.get("nodes", []) if node.get("type") == node_type]


def assumptions_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for node in _nodes_by_type(graph, "Assumption"):
        rows.append(
            {
                "id": node.get("id", ""),
                "label": node.get("name") or node.get("label", ""),
                "criticality": node.get("criticality", ""),
                "evidence_grade": node.get("evidence_grade", ""),
                "reviewer_confidence": node.get("reviewer_confidence", ""),
                "status": node.get("status", ""),
                "owner": node.get("owner", ""),
            }
        )
    return rows


# Node types whose instances may be "supported by" or "contradicted by" evidence.
_CLAIM_TYPES = frozenset({
    "Assumption", "Risk", "FinancialHypothesis", "ValueProposition", "Decision",
})


def evidence_matrix_rows(
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (rows, warnings) for SUPPORTED_BY and CONTRADICTED_BY edges.

    Direction is enforced: source must be a claim-type node and target must be
    an Evidence node.  Edges with inverted direction produce a warning and are
    skipped rather than silently fabricating a row with swapped endpoints.
    """
    evidence_by_id = {node.get("id"): node for node in _nodes_by_type(graph, "Evidence")}
    claim_by_id = {
        node.get("id"): node
        for node in graph.get("nodes", [])
        if node.get("type") in _CLAIM_TYPES
    }
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for edge in graph.get("edges", []):
        relationship = edge.get("relationship") or edge.get("type", "")
        if relationship not in {"SUPPORTED_BY", "CONTRADICTED_BY"}:
            continue
        src = edge.get("source")
        tgt = edge.get("target")
        claim = claim_by_id.get(src)
        evidence = evidence_by_id.get(tgt)
        if claim is None or evidence is None:
            # Detect and warn about inverted direction (evidence→claim).
            if evidence_by_id.get(src) and claim_by_id.get(tgt):
                warnings.append(
                    f"Skipped {relationship} edge {src!r}→{tgt!r}: direction is inverted "
                    f"(evidence node is source, claim node is target). "
                    f"Expected: claim → evidence."
                )
            elif claim is None and evidence is None:
                warnings.append(
                    f"Skipped {relationship} edge {src!r}→{tgt!r}: neither endpoint "
                    f"is a recognised claim or evidence node."
                )
            continue
        rows.append(
            {
                "claim_id": claim.get("id", ""),
                "claim": claim.get("name") or claim.get("label", ""),
                "evidence_id": evidence.get("id", ""),
                "evidence": evidence.get("name") or evidence.get("label", ""),
                "relationship": relationship,
                "evidence_grade": edge.get("evidence_grade") or evidence.get("evidence_grade", ""),
                "reviewer_confidence": edge.get("reviewer_confidence", ""),
                "source": evidence.get("source") or evidence.get("source_file", ""),
            }
        )
    return rows, warnings


def risk_register_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for node in _nodes_by_type(graph, "Risk"):
        rows.append(
            {
                "id": node.get("id", ""),
                "risk": node.get("name") or node.get("label", ""),
                "severity": node.get("severity", ""),
                "probability": node.get("probability", ""),
                "impact": node.get("impact", ""),
                "mitigation": node.get("mitigation", ""),
                "owner": node.get("owner", ""),
            }
        )
    return rows


def export_all(graph: dict[str, Any] | None = None, export_dir: str | Path = EXPORT_DIR) -> dict[str, Any]:
    graph = graph or load_validated_graph()
    if not graph.get("nodes") and not graph.get("edges"):
        raise ValueError(
            "No validated knowledge found. "
            "Validate entities and relations on the Validate Knowledge page before exporting."
        )
    warnings: list[str] = []
    export_id = _timestamp()
    base = Path(export_dir) / export_id
    base.mkdir(parents=True, exist_ok=True)

    evidence_rows, evidence_warnings = evidence_matrix_rows(graph)
    warnings.extend(evidence_warnings)

    manifest = create_manifest(graph, export_id, warnings)

    paths: dict[str, Any] = {
        "manifest": _write_json(base / "manifest.json", manifest),
        "graph_json": _write_json(base / "graph.json", graph),
        "graph_jsonld": _write_json(base / "graph.jsonld", graph_to_jsonld(graph)),
        "assumptions_csv": _write_csv(
            base / "assumptions.csv",
            assumptions_rows(graph),
            ["id", "label", "criticality", "evidence_grade", "reviewer_confidence", "status", "owner"],
        ),
        "evidence_matrix_csv": _write_csv(
            base / "evidence_matrix.csv",
            evidence_rows,
            ["claim_id", "claim", "evidence_id", "evidence", "relationship",
             "evidence_grade", "reviewer_confidence", "source"],
        ),
        "risk_register_csv": _write_csv(
            base / "risk_register.csv",
            risk_register_rows(graph),
            ["id", "risk", "severity", "probability", "impact", "mitigation", "owner"],
        ),
    }

    audits_dir = base / "audits"
    audits_dir.mkdir(exist_ok=True)
    for audit in sorted(AUDIT_DIR.glob("*.md")):
        (audits_dir / audit.name).write_text(audit.read_text(encoding="utf-8"), encoding="utf-8")
    paths["audits_dir"] = audits_dir

    zip_path = base.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in base.rglob("*"):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(base))
    paths["zip"] = zip_path
    paths["warnings"] = warnings
    return paths
