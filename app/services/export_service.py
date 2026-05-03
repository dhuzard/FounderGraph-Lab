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
                "confidence": entity.get("confidence", ""),
                "status": entity.get("validation_status", entity.get("status", "")),
                "source": entity.get("source_file", ""),
                "source_document_id": entity.get("source_document_id", ""),
                "source_snippet": entity.get("source_snippet", ""),
                "tags": entity.get("tags", []),
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
                "confidence": relation.get("confidence", ""),
                "status": relation.get("validation_status", relation.get("status", "")),
                "source_document_id": relation.get("source_document_id", ""),
                "source_file": relation.get("source_file", ""),
                "source_snippet": relation.get("source_snippet", ""),
            }
        )
    return {"nodes": nodes, "edges": edges}


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
    context = {
        "fg": "https://foundergraph.local/ontology#",
        "name": "fg:name",
        "type": "@type",
        "source": "fg:source",
        "confidence": "fg:confidence",
    }
    nodes = []
    for node in graph.get("nodes", []):
        item = {"@id": node.get("id"), "type": node.get("type", "Entity")}
        item.update({key: value for key, value in node.items() if key not in {"id", "type"}})
        nodes.append(item)
    edges = []
    for edge in graph.get("edges", []):
        edges.append(
            {
                "@id": edge.get("id") or f"{edge.get('source')}:{edge.get('relationship')}:{edge.get('target')}",
                "type": edge.get("relationship", "RELATED_TO"),
                "source": {"@id": edge.get("source")},
                "target": {"@id": edge.get("target")},
            }
        )
    return {"@context": context, "@graph": nodes + edges}


def _nodes_by_type(graph: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [node for node in graph.get("nodes", []) if node.get("type") == node_type]


def assumptions_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for node in _nodes_by_type(graph, "Assumption"):
        rows.append(
            {
                "id": node.get("id", ""),
                "statement": node.get("statement") or node.get("name") or node.get("label", ""),
                "category": node.get("category", ""),
                "confidence": node.get("confidence", ""),
                "status": node.get("status", ""),
                "owner": node.get("owner", ""),
            }
        )
    return rows


def evidence_matrix_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_by_id = {node.get("id"): node for node in _nodes_by_type(graph, "Evidence")}
    assumptions_by_id = {node.get("id"): node for node in _nodes_by_type(graph, "Assumption")}
    rows = []
    for edge in graph.get("edges", []):
        relationship = edge.get("relationship") or edge.get("type", "")
        if relationship not in {"SUPPORTED_BY", "CONTRADICTED_BY", "EVIDENCED_BY"}:
            continue
        assumption = assumptions_by_id.get(edge.get("source")) or assumptions_by_id.get(edge.get("target"), {})
        evidence = evidence_by_id.get(edge.get("target")) or evidence_by_id.get(edge.get("source"), {})
        rows.append(
            {
                "assumption_id": assumption.get("id", ""),
                "assumption": assumption.get("statement") or assumption.get("name") or assumption.get("label", ""),
                "evidence_id": evidence.get("id", ""),
                "evidence": evidence.get("summary") or evidence.get("name") or evidence.get("label", ""),
                "relationship": relationship,
                "strength": edge.get("strength", evidence.get("strength", "")),
                "source": evidence.get("source") or evidence.get("source_file", ""),
            }
        )
    return rows


def risk_register_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for node in _nodes_by_type(graph, "Risk"):
        rows.append(
            {
                "id": node.get("id", ""),
                "risk": node.get("name") or node.get("label", ""),
                "severity": node.get("severity", ""),
                "likelihood": node.get("likelihood", ""),
                "mitigation": node.get("mitigation", ""),
                "owner": node.get("owner", ""),
            }
        )
    return rows


def export_all(graph: dict[str, Any] | None = None, export_dir: str | Path = EXPORT_DIR) -> dict[str, Any]:
    graph = graph or load_validated_graph()
    warnings: list[str] = []
    if not graph.get("nodes") and not graph.get("edges"):
        warnings.append(
            "No validated knowledge found. "
            "Validate entities and relations on the Validate Knowledge page before exporting."
        )
    base = Path(export_dir) / _timestamp()
    base.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Any] = {
        "graph_json": _write_json(base / "graph.json", graph),
        "graph_jsonld": _write_json(base / "graph.jsonld", graph_to_jsonld(graph)),
        "assumptions_csv": _write_csv(
            base / "assumptions.csv",
            assumptions_rows(graph),
            ["id", "statement", "category", "confidence", "status", "owner"],
        ),
        "evidence_matrix_csv": _write_csv(
            base / "evidence_matrix.csv",
            evidence_matrix_rows(graph),
            ["assumption_id", "assumption", "evidence_id", "evidence", "relationship", "strength", "source"],
        ),
        "risk_register_csv": _write_csv(
            base / "risk_register.csv",
            risk_register_rows(graph),
            ["id", "risk", "severity", "likelihood", "mitigation", "owner"],
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
