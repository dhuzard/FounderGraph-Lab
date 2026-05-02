"""Tests for OntologyValidator pre-validation gate."""

from __future__ import annotations

import json

import pytest

from app.services.ontology_validator import OntologyValidator, ValidationReport


def _make_entity(id="e1", name="Acme", type="Startup"):
    return {"id": id, "temporary_id": id, "name": name, "label": name, "type": type}


def _make_relation(src="e1", pred="RELATED_TO", tgt="e2"):
    return {"source_entity_id": src, "predicate": pred, "type": pred, "target_entity_id": tgt}


def test_valid_entity_passes():
    v = OntologyValidator()
    report = v.validate([_make_entity()], [])
    assert report.is_clean
    assert len(report.valid_entities) == 1


def test_unknown_entity_type_is_rejected():
    v = OntologyValidator()
    report = v.validate([_make_entity(type="WeirdType")], [])
    assert not report.is_clean
    assert any(vv.rule == "unknown-type" for vv in report.violations)
    assert report.valid_entities == []


def test_entity_missing_label_is_rejected():
    v = OntologyValidator()
    bad = {"id": "e1", "temporary_id": "e1", "type": "Startup"}
    report = v.validate([bad], [])
    assert not report.is_clean
    assert any(vv.rule == "missing-label" for vv in report.violations)


def test_entity_missing_id_is_rejected():
    v = OntologyValidator()
    bad = {"name": "Acme", "label": "Acme", "type": "Startup"}
    report = v.validate([bad], [])
    assert not report.is_clean
    assert any(vv.rule == "missing-id" for vv in report.violations)


def test_valid_relation_passes():
    v = OntologyValidator()
    report = v.validate([], [_make_relation()])
    assert report.is_clean
    assert len(report.valid_relations) == 1


def test_unknown_predicate_is_rejected():
    v = OntologyValidator()
    report = v.validate([], [_make_relation(pred="INVENTED_RELATION")])
    assert not report.is_clean
    assert any(vv.rule == "unknown-predicate" for vv in report.violations)


def test_relation_missing_source_is_rejected():
    v = OntologyValidator()
    bad = {"predicate": "RELATED_TO", "type": "RELATED_TO", "target_entity_id": "e2"}
    report = v.validate([], [bad])
    assert not report.is_clean
    assert any(vv.rule == "missing-source" for vv in report.violations)


def test_cross_batch_relation_not_rejected_as_dangling():
    """A relation pointing to an entity not in the current batch must be allowed."""
    v = OntologyValidator()
    report = v.validate(
        [_make_entity(id="e1")],
        [_make_relation(src="e1", tgt="e99-from-previous-batch")],
    )
    assert report.is_clean


def test_violations_written_atomically(tmp_path):
    v = OntologyValidator(violations_path=tmp_path / "violations.json")
    v.validate([_make_entity(type="BadType")], [])
    assert (tmp_path / "violations.json").exists()
    assert not (tmp_path / "violations.json.tmp").exists()
    records = json.loads((tmp_path / "violations.json").read_text())
    assert len(records) == 1
    assert records[0]["rule"] == "unknown-type"


def test_violations_are_appended_not_overwritten(tmp_path):
    v = OntologyValidator(violations_path=tmp_path / "violations.json")
    v.validate([_make_entity(type="Bad1")], [])
    v.validate([_make_entity(type="Bad2")], [])
    records = json.loads((tmp_path / "violations.json").read_text())
    assert len(records) == 2


def test_report_summary_string():
    report = ValidationReport(
        valid_entities=[_make_entity()],
        valid_relations=[_make_relation()],
    )
    assert "1 valid entities" in report.summary()
    assert "1 valid relations" in report.summary()
    assert "0 violation" in report.summary()
