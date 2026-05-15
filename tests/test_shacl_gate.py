"""Tests for the pySHACL deterministic gate (Phase 1.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytest.importorskip("pyshacl")
pytest.importorskip("rdflib")


from app.services import shacl_gate
from app.services.shacl_gate import (
    DEFAULT_SHAPES_PATH,
    Violation,
    run_gate,
    serialize_staging_to_rdf,
    validate,
    write_violations,
)


SHAPES_AVAILABLE = DEFAULT_SHAPES_PATH.exists()


@pytest.fixture
def shapes_path() -> Path:
    if not SHAPES_AVAILABLE:
        pytest.skip("Generated shapes.ttl missing -- run `make generate`")
    return DEFAULT_SHAPES_PATH


def test_assumption_without_criticality_fails_shape(shapes_path):
    """An Assumption missing its required ``criticality`` violates the shape."""
    entities = [
        {
            "id": "a-no-crit",
            "type": "Assumption",
            "name": "Risky claim",
            "description": "Does not declare its criticality",
        }
    ]
    graph = serialize_staging_to_rdf(entities, [])
    violations = validate(graph, shapes_path=shapes_path)
    assert violations, "Expected at least one SHACL violation"
    # The missing-criticality rule fires as a MinCount constraint.
    constraints = {v.constraint for v in violations}
    assert any(
        "MinCount" in c or "criticality" in (v.path or "")
        for c in constraints
        for v in violations
    )


def test_valid_graph_passes(shapes_path):
    """A minimally complete Assumption + Evidence should produce zero violations."""
    entities = [
        {
            "id": "a-good",
            "type": "Assumption",
            "name": "Customers want FAIR data",
            "description": "Validated via interviews",
            "criticality": "high",
            "validation_status": "validated",
        },
        {
            "id": "e-good",
            "type": "Evidence",
            "name": "CRO interview transcript",
            "description": "Direct quote: 'we need this'",
            "validation_status": "validated",
        },
    ]
    graph = serialize_staging_to_rdf(
        entities,
        [
            {
                "source_entity_id": "a-good",
                "target_entity_id": "e-good",
                "predicate": "SUPPORTED_BY",
            }
        ],
    )
    violations = validate(graph, shapes_path=shapes_path)
    assert violations == [], (
        f"Expected zero violations for a clean graph, got: {violations}"
    )


def test_violations_persisted_atomically(tmp_path):
    """``write_violations`` must use the ``.tmp`` -> rename pattern."""
    target = tmp_path / "shacl_violations.json"
    violations = [
        Violation(
            focus_node="a-1",
            constraint="sh:MinCountConstraintComponent",
            severity="Violation",
            message="Less than 1 value on fg:criticality",
            source_shape=None,
            path="criticality",
            value=None,
        )
    ]
    # Track whether a ``.tmp`` file ever exists during write -- the atomic
    # write must rename it into place, never overwrite the target in-place.
    original_replace = Path.replace
    saw_tmp = {"value": False}

    def _spy_replace(self, target_path):
        if self.suffix == ".tmp":
            saw_tmp["value"] = True
        return original_replace(self, target_path)

    # Patch Path.replace to observe the rename without breaking the contract.
    monkeypatched = False
    try:
        Path.replace = _spy_replace  # type: ignore[method-assign]
        monkeypatched = True
        out = write_violations(violations, target)
    finally:
        if monkeypatched:
            Path.replace = original_replace  # type: ignore[method-assign]

    assert out == target
    assert target.exists()
    assert saw_tmp["value"], "write_violations must write via a .tmp file first"
    payload = json.loads(target.read_text())
    assert payload[0]["focus_node"] == "a-1"
    assert payload[0]["path"] == "criticality"


def test_kill_switch_disables_gate(monkeypatch, shapes_path):
    """Setting ``FG_SHACL_ENABLED=0`` must short-circuit ``run_gate``."""
    monkeypatch.setenv("FG_SHACL_ENABLED", "0")
    entities = [{"id": "a-skip", "type": "Assumption", "name": "Without criticality"}]
    out = run_gate(entities, [])
    assert out == [], "Kill-switch must prevent any SHACL run"


def test_run_gate_writes_violations(tmp_path, monkeypatch, shapes_path):
    """End-to-end: ``run_gate`` should both return violations and persist them."""
    monkeypatch.setenv("FG_SHACL_ENABLED", "1")
    target = tmp_path / "violations.json"
    entities = [{"id": "a-no-crit-2", "type": "Assumption", "name": "Missing crit"}]
    violations = run_gate(entities, [], violations_path=target)
    assert violations, "Expected SHACL to find at least one violation"
    assert target.exists(), "Violations must be persisted to disk"
    payload = json.loads(target.read_text())
    assert len(payload) == len(violations)
