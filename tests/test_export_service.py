"""Tests for export_service field correctness, direction enforcement, and manifest schema.

Covers:
  - assumptions.csv uses evidence_grade/criticality, not confidence/statement/category
  - risk_register.csv uses probability/impact, not likelihood
  - evidence_matrix direction enforcement for SUPPORTED_BY
  - invalid SUPPORTED_BY direction produces warning, not fabricated row
  - JSON-LD @context declares evidence/reviewer fields, not stale confidence
  - manifest includes all required count fields
  - empty-graph export warns loudly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.export_service import (
    assumptions_rows,
    evidence_matrix_rows,
    export_all,
    graph_to_jsonld,
    risk_register_rows,
)


def test_assumptions_csv_includes_criticality_and_evidence_grade():
    graph = {
        "nodes": [{
            "id": "a1",
            "type": "Assumption",
            "name": "Users will pay for this",
            "criticality": "high",
            "evidence_grade": "paraphrase",
            "reviewer_confidence": "moderate",
            "status": "validated",
        }],
        "edges": [],
    }
    rows = assumptions_rows(graph)

    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "Users will pay for this"
    assert row["criticality"] == "high"
    assert row["evidence_grade"] == "paraphrase"
    assert row["reviewer_confidence"] == "moderate"
    assert "confidence" not in row, "stale confidence field must not appear"
    assert "statement" not in row, "non-ontology statement field must not appear"
    assert "category" not in row, "non-ontology category field must not appear"


def test_risk_register_uses_probability_and_impact():
    graph = {
        "nodes": [{
            "id": "r1",
            "type": "Risk",
            "name": "Key dependency fails",
            "severity": "high",
            "probability": "medium",
            "impact": "critical service outage",
            "mitigation": "add redundancy",
        }],
        "edges": [],
    }
    rows = risk_register_rows(graph)

    assert len(rows) == 1
    row = rows[0]
    assert row["probability"] == "medium"
    assert row["impact"] == "critical service outage"
    assert "likelihood" not in row, "stale likelihood field must not appear in risk register"


def test_evidence_matrix_direction_for_supported_by():
    """Standard direction: claim (Assumption) is source, Evidence is target."""
    graph = {
        "nodes": [
            {"id": "a1", "type": "Assumption", "name": "Claim A"},
            {"id": "ev1", "type": "Evidence", "name": "Study 1", "source_file": "study.pdf"},
        ],
        "edges": [{
            "source": "a1",
            "target": "ev1",
            "relationship": "SUPPORTED_BY",
            "evidence_grade": "paraphrase",
        }],
    }
    rows, warnings = evidence_matrix_rows(graph)

    assert not warnings
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_id"] == "a1"
    assert row["claim"] == "Claim A"
    assert row["evidence_id"] == "ev1"
    assert row["evidence"] == "Study 1"
    assert row["evidence_grade"] == "paraphrase"


def test_evidence_matrix_warns_on_invalid_supported_by_direction():
    """Inverted SUPPORTED_BY (evidence→claim) must warn and produce no row."""
    graph = {
        "nodes": [
            {"id": "a1", "type": "Assumption", "name": "Claim A"},
            {"id": "ev1", "type": "Evidence", "name": "Study 1"},
        ],
        "edges": [{
            "source": "ev1",   # inverted: evidence as source
            "target": "a1",    # inverted: claim as target
            "relationship": "SUPPORTED_BY",
        }],
    }
    rows, warnings = evidence_matrix_rows(graph)

    assert rows == [], "Inverted direction must produce no rows"
    assert len(warnings) == 1
    assert "inverted" in warnings[0].lower() or "direction" in warnings[0].lower()


def test_evidence_matrix_excludes_evidenced_by():
    """EVIDENCED_BY is not in the ontology and must be silently ignored."""
    graph = {
        "nodes": [
            {"id": "a1", "type": "Assumption", "name": "Claim A"},
            {"id": "ev1", "type": "Evidence", "name": "Study 1"},
        ],
        "edges": [{"source": "a1", "target": "ev1", "relationship": "EVIDENCED_BY"}],
    }
    rows, warnings = evidence_matrix_rows(graph)
    assert rows == []
    assert warnings == []


def test_jsonld_context_includes_evidence_and_reviewer_fields():
    """@context must declare evidence_grade, reviewer_confidence, reviewer_comment,
    source_document_id, and ontology_version; must not declare stale confidence."""
    jsonld = graph_to_jsonld({"nodes": [], "edges": []})
    context = jsonld["@context"]

    assert "evidence_grade" in context
    assert "reviewer_confidence" in context
    assert "reviewer_comment" in context
    assert "source_document_id" in context
    assert "ontology_version" in context
    assert "confidence" not in context, "stale confidence must not appear in @context"


def test_manifest_counts_match_exported_graph(tmp_path):
    """Manifest must include all required fields with correct counts."""
    graph = {
        "nodes": [
            {
                "id": "e1",
                "type": "Assumption",
                "name": "Test assumption",
                "source_document_id": "doc-1",
                "evidence_grade": "inference",   # weak grade
            },
            {
                "id": "e2",
                "type": "Evidence",
                "name": "Study",
                "source_document_id": "doc-1",
                "evidence_grade": "direct_quote",  # strong grade
            },
        ],
        "edges": [{"source": "e1", "target": "e2", "relationship": "SUPPORTED_BY"}],
    }
    result = export_all(graph=graph, export_dir=tmp_path)
    manifest = json.loads(Path(result["manifest"]).read_text())

    assert manifest["entity_count"] == 2
    assert manifest["relation_count"] == 1
    assert manifest["source_document_count"] == 1   # both nodes share "doc-1"
    assert manifest["weak_evidence_count"] == 1     # only e1 has "inference"
    assert manifest["warning_count"] == len(manifest["warnings"])
    assert "graph_snapshot_id" in manifest
    assert "export_timestamp" in manifest
    assert manifest["ontology_version"] != "unknown"


def test_export_fails_when_no_validated_knowledge(tmp_path):
    """export_all must raise ValueError immediately when the graph is empty.
    No files should be written to disk — the caller surfaces st.error() to the user."""
    with pytest.raises(ValueError, match="No validated knowledge"):
        export_all(graph={"nodes": [], "edges": []}, export_dir=tmp_path)
    # Confirm no export directory or files were created before the raise.
    assert not any(tmp_path.iterdir()), "No files must be written when export_all raises"
