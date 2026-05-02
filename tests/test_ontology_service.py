"""Tests for OntologyService load/save/allowlist logic."""

from __future__ import annotations

import pytest
import yaml

from app.services.ontology_service import (
    EntityClassDef,
    OntologyConfig,
    RelationDef,
    load_ontology,
    save_ontology,
)


def _write_yaml(path, data):
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def test_load_ontology_returns_empty_config_when_file_missing(tmp_path):
    config = load_ontology(tmp_path / "nonexistent.yaml")
    assert config.entity_classes == {}
    assert config.relations == []


def test_load_ontology_parses_classes_and_relations(tmp_path):
    yaml_path = tmp_path / "ontology.yaml"
    _write_yaml(yaml_path, {
        "domain": "biotech",
        "goals": ["assumption_validation"],
        "classes": {
            "Startup": {"description": "The startup.", "fields": ["name"]},
            "Assumption": {"description": "An unproven claim.", "fields": ["label"]},
        },
        "relations": [
            {"subject": "Startup", "predicate": "HAS_ASSUMPTION", "object": "Assumption"},
        ],
    })

    config = load_ontology(yaml_path)

    assert config.domain == "biotech"
    assert config.goals == ["assumption_validation"]
    assert "Startup" in config.entity_classes
    assert config.entity_classes["Assumption"].description == "An unproven claim."
    assert len(config.relations) == 1
    assert config.relations[0].predicate == "HAS_ASSUMPTION"


def test_allowed_labels_includes_entity_and_document(tmp_path):
    config = OntologyConfig(
        entity_classes={"Startup": EntityClassDef(), "Founder": EntityClassDef()}
    )
    labels = config.allowed_labels()
    assert "Entity" in labels
    assert "Document" in labels
    assert "Startup" in labels
    assert "Founder" in labels


def test_allowed_relationships_includes_utility_relations():
    config = OntologyConfig(
        relations=[RelationDef(subject="Startup", predicate="TARGETS", object="CustomerSegment")]
    )
    rels = config.allowed_relationships()
    assert "TARGETS" in rels
    assert "RELATED_TO" in rels
    assert "MENTIONS" in rels


def test_save_and_reload_roundtrip(tmp_path):
    yaml_path = tmp_path / "saved.yaml"
    config = OntologyConfig(
        domain="saas",
        goals=["investor_readiness"],
        entity_classes={
            "Startup": EntityClassDef(description="The startup.", fields=["name"]),
            "ClinicalTrial": EntityClassDef(description="A clinical study.", fields=["phase"]),
        },
        relations=[
            RelationDef(subject="ClinicalTrial", predicate="VALIDATES", object="Assumption"),
        ],
    )
    save_ontology(config, yaml_path)
    reloaded = load_ontology(yaml_path)

    assert reloaded.domain == "saas"
    assert "ClinicalTrial" in reloaded.entity_classes
    assert reloaded.entity_classes["ClinicalTrial"].description == "A clinical study."
    assert reloaded.relations[0].predicate == "VALIDATES"


def test_save_is_atomic(tmp_path):
    yaml_path = tmp_path / "ontology.yaml"
    config = OntologyConfig(entity_classes={"Startup": EntityClassDef()})
    save_ontology(config, yaml_path)
    assert yaml_path.exists()
    assert not (tmp_path / "ontology.yaml.tmp").exists()


def test_add_and_remove_entity_class():
    config = OntologyConfig()
    config.add_entity_class("ClinicalTrial", "A preclinical study.", fields=["phase"])
    assert "ClinicalTrial" in config.entity_classes
    assert config.entity_classes["ClinicalTrial"].fields == ["phase"]

    removed = config.remove_entity_class("ClinicalTrial")
    assert removed is True
    assert "ClinicalTrial" not in config.entity_classes


def test_rename_entity_class():
    config = OntologyConfig(entity_classes={"OldName": EntityClassDef(description="desc")})
    renamed = config.rename_entity_class("OldName", "NewName")
    assert renamed is True
    assert "NewName" in config.entity_classes
    assert "OldName" not in config.entity_classes
    assert config.entity_classes["NewName"].description == "desc"


def test_default_ontology_yaml_loads_cleanly():
    config = load_ontology()
    assert len(config.entity_classes) >= 10
    assert len(config.allowed_labels()) >= 12
    assert len(config.allowed_relationships()) >= 5
