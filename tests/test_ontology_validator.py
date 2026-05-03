"""Regression tests for OntologyLoader and ontology-driven validation.

These tests protect against four categories of knowledge-graph corruption:
  1. Ontology drift   — Python sets diverging from the YAML source of truth.
  2. Staging accumulation — second extraction must not overwrite first.
  3. Idempotent Neo4j writes — upsert_entity called twice must not duplicate.
  4. Empty-export warning — export_all() must warn, not silently emit sample data.
  5. Domain/range enforcement — invalid triples must be rejected at write time.
  6. No-write-before-validation — pending entities must be refused by Neo4j.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ontology_validator import OntologyLoader, get_ontology
from app.services.neo4j_service import (
    DEFAULT_ALLOWED_LABELS,
    DEFAULT_ALLOWED_RELATIONSHIPS,
    Neo4jService,
    Neo4jServiceError,
)
from app.services.entity_extractor import EntityExtractor, stable_entity_id
from app.services.llm_service import LLMInvalidJSONError
from app.services.export_service import export_all, load_validated_graph, create_manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_json(self, prompt):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResult(list):
    pass


class FakeSession:
    """FakeSession that responds to entity-existence count queries.

    Queries containing 'count(e) AS n' return {"n": 1} for IDs in
    known_entity_ids and {"n": 0} for all others, so that the relation
    endpoint pre-check works correctly in tests.
    """

    def __init__(self, calls, known_entity_ids=None):
        self.calls = calls
        self._known: set[str] = set(known_entity_ids or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params=None):
        p = params or {}
        self.calls.append((query, p))
        if "count(e) AS n" in query and "id" in p:
            n = 1 if p["id"] in self._known else 0
            return [{"n": n}]
        return FakeResult()

    def execute_write(self, fn):
        fn(self)


class FakeDriver:
    def __init__(self, known_entity_ids=None):
        self.calls = []
        self._known: set[str] = set(known_entity_ids or [])

    def session(self, **kwargs):
        return FakeSession(self.calls, self._known)

    def close(self):
        self.calls.append(("close", {}))


def _service(extra_labels=None, extra_rels=None, known_entity_ids=None):
    labels = {"Entity", "Assumption", "Evidence", "Risk", "Startup"} | (extra_labels or set())
    rels = {"RELATED_TO", "SUPPORTED_BY", "CONTRADICTED_BY", "THREATENS", "FOUNDED"} | (extra_rels or set())
    return Neo4jService(driver=FakeDriver(known_entity_ids=known_entity_ids), allowed_labels=labels, allowed_relationships=rels)


# ---------------------------------------------------------------------------
# 1. Ontology drift detection
# ---------------------------------------------------------------------------

class TestOntologyDrift:
    def test_ontology_loads_version(self):
        loader = get_ontology()
        assert loader.version != "unknown", "startup_ontology.yaml must declare a version"

    def test_ontology_allowed_labels_covers_default_python_set(self):
        """Every label in DEFAULT_ALLOWED_LABELS must exist in the YAML."""
        ontology_labels = get_ontology().allowed_labels
        missing = DEFAULT_ALLOWED_LABELS - ontology_labels
        assert not missing, (
            f"These labels are in DEFAULT_ALLOWED_LABELS but not in the ontology YAML: {missing}. "
            "Either add them to startup_ontology.yaml or remove them from the Python set."
        )

    def test_ontology_allowed_relationships_covers_default_python_set(self):
        """Every relationship in DEFAULT_ALLOWED_RELATIONSHIPS must exist in the YAML."""
        ontology_rels = get_ontology().allowed_relationships
        missing = DEFAULT_ALLOWED_RELATIONSHIPS - ontology_rels
        assert not missing, (
            f"These relations are in DEFAULT_ALLOWED_RELATIONSHIPS but not in the ontology YAML: {missing}. "
            "Either add them to startup_ontology.yaml or remove them from the Python set."
        )

    def test_ontology_required_fields_for_assumption(self):
        loader = get_ontology()
        fields = loader.required_fields("Assumption")
        assert "label" in fields
        assert "description" in fields

    def test_ontology_domain_range_map_not_empty(self):
        dr = get_ontology().domain_range_map
        assert len(dr) > 5, "domain_range_map should have entries for core predicates"

    def test_validate_relation_known_valid(self):
        loader = get_ontology()
        assert loader.validate_relation("SUPPORTED_BY", "Assumption", "Evidence")

    def test_validate_relation_known_invalid(self):
        loader = get_ontology()
        # Founder -THREATENS-> ProductFeature is not in the ontology
        assert not loader.validate_relation("THREATENS", "Founder", "ProductFeature"), (
            "THREATENS should only be valid from Risk to Milestone"
        )

    def test_validate_relation_permissive_when_type_unknown(self):
        loader = get_ontology()
        # Unknown types → permissive (we don't have type info for old records)
        assert loader.validate_relation("SUPPORTED_BY", None, None)

    def test_missing_yaml_degrades_gracefully(self, tmp_path):
        loader = OntologyLoader(tmp_path / "nonexistent.yaml")
        assert loader.allowed_labels == {"Entity", "Document"}
        assert "RELATED_TO" in loader.allowed_relationships


# ---------------------------------------------------------------------------
# 2. Staging accumulation (second extraction must not overwrite first)
# ---------------------------------------------------------------------------

class TestStagingAccumulation:
    def _make_llm(self, entity_id, entity_name, entity_type="Assumption"):
        return FakeLLM([
            {"document_type": "PitchDeck", "secondary_types": [], "summary": "", "tags": [], "confidence": "high"},
            {"entities": [{"temporary_id": entity_id, "type": entity_type, "label": entity_name,
                           "description": "test", "source_snippet": "test snippet", "evidence_grade": "paraphrase"}]},
            {"relations": []},
        ])

    def test_second_extraction_adds_to_staging_not_replaces(self, tmp_path):
        extractor1 = EntityExtractor(
            llm_service=self._make_llm("TMP-A", "First assumption"),
            staging_dir=tmp_path,
        )
        extractor1.extract_to_staging("First doc text.", {"source_document_id": "doc-1"})

        extractor2 = EntityExtractor(
            llm_service=self._make_llm("TMP-B", "Second assumption"),
            staging_dir=tmp_path,
        )
        extractor2.extract_to_staging("Second doc text.", {"source_document_id": "doc-2"})

        entities = json.loads((tmp_path / "candidate_entities.json").read_text())
        labels = {e["label"] for e in entities}
        assert "First assumption" in labels, "First extraction must survive the second run"
        assert "Second assumption" in labels

    def test_same_entity_extracted_twice_is_deduplicated(self, tmp_path):
        extractor = EntityExtractor(
            llm_service=self._make_llm("TMP-A", "Shared assumption"),
            staging_dir=tmp_path,
        )
        extractor.extract_to_staging("Doc text.", {"source_document_id": "doc-1"})

        extractor2 = EntityExtractor(
            llm_service=self._make_llm("TMP-A", "Shared assumption"),
            staging_dir=tmp_path,
        )
        extractor2.extract_to_staging("Doc text.", {"source_document_id": "doc-1"})

        entities = json.loads((tmp_path / "candidate_entities.json").read_text())
        # Same (doc_id, label, type) → same stable ID → deduplicated
        assert len(entities) == 1


# ---------------------------------------------------------------------------
# 3. Idempotent Neo4j writes
# ---------------------------------------------------------------------------

class TestIdempotentNeo4jWrites:
    def test_upsert_entity_twice_produces_single_merge(self):
        svc = _service()
        entity = {
            "id": "e-stable-1",
            "name": "Test Assumption",
            "label": "Assumption",
            "type": "Assumption",
            "status": "validated",
            "validation_status": "validated",
            "source_snippet": "test",
        }
        svc.upsert_entity(entity)
        svc.upsert_entity(entity)

        # Both calls should use MERGE (not CREATE), so the query contains MERGE
        merge_calls = [
            q for q, _ in svc.driver.calls
            if isinstance(q, str) and "MERGE" in q and "Entity" in q
        ]
        assert len(merge_calls) == 2, "Should have two MERGE calls (one per upsert)"
        # Both use the same id parameter → single logical node in a real DB
        ids = [p.get("id") for _, p in svc.driver.calls if isinstance(p, dict) and "id" in p]
        assert ids.count("e-stable-1") >= 2

    def test_upsert_relation_twice_uses_merge_with_stable_id(self):
        # Endpoints e1 and e2 must exist in the graph for the pre-check to pass.
        svc = _service(known_entity_ids={"e1", "e2"})
        relation = {
            "id": "r-1",
            "source_entity_id": "e1",
            "target_entity_id": "e2",
            "type": "SUPPORTED_BY",
            "predicate": "SUPPORTED_BY",
            "status": "validated",
            "validation_status": "validated",
        }
        svc.upsert_relation(relation)
        svc.upsert_relation(relation)

        merge_calls = [
            q for q, _ in svc.driver.calls
            if isinstance(q, str) and "MERGE" in q and "SUPPORTED_BY" in q
        ]
        assert len(merge_calls) == 2
        ids = [p.get("id") for _, p in svc.driver.calls if isinstance(p, dict) and p.get("id") == "r-1"]
        assert len(ids) >= 2


# ---------------------------------------------------------------------------
# 4. Empty-export warning — no sample data leakage
# ---------------------------------------------------------------------------

class TestEmptyExportWarning:
    def test_export_raises_when_no_validated_knowledge(self, tmp_path):
        # export_all must raise immediately — no files are written to disk.
        with pytest.raises(ValueError, match="No validated knowledge"):
            export_all(graph={"nodes": [], "edges": []}, export_dir=tmp_path)

    def test_export_no_warning_when_knowledge_exists(self, tmp_path):
        graph = {
            "nodes": [{"id": "e1", "type": "Assumption", "name": "Test"}],
            "edges": [],
        }
        result = export_all(graph=graph, export_dir=tmp_path)
        assert not result["warnings"], "Should not warn when knowledge is present"

    def test_manifest_included_in_export(self, tmp_path):
        graph = {"nodes": [{"id": "e1", "type": "Risk", "name": "Test risk"}], "edges": []}
        result = export_all(graph=graph, export_dir=tmp_path)
        assert "manifest" in result
        manifest_data = json.loads(Path(result["manifest"]).read_text())
        assert manifest_data["entity_count"] == 1
        assert manifest_data["relation_count"] == 0
        assert "Risk" in manifest_data["entity_types"]

    def test_manifest_records_ontology_version(self, tmp_path):
        graph = {"nodes": [], "edges": []}
        manifest = create_manifest(graph, "test-export-1", [])
        assert manifest["ontology_version"] != "unknown"

    def test_export_does_not_contain_sample_data(self, tmp_path):
        """export_all must raise on an empty graph — no files written, no fake data."""
        with pytest.raises(ValueError, match="No validated knowledge"):
            export_all(graph={"nodes": [], "edges": []}, export_dir=tmp_path)
        assert not any(tmp_path.rglob("graph.json")), "No files must be written when export raises"


# ---------------------------------------------------------------------------
# 5. Domain/range enforcement
# ---------------------------------------------------------------------------

class TestDomainRangeEnforcement:
    def test_ontology_rejects_invalid_triple(self):
        # Both endpoints must exist so the pre-check passes and domain/range
        # enforcement is the failing gate, not the existence check.
        svc = Neo4jService(
            driver=FakeDriver(known_entity_ids={"f1", "f2"}),
            allowed_labels={"Entity", "Risk", "Founder"},
            allowed_relationships={"THREATENS"},
        )
        relation = {
            "id": "r-bad",
            "source_entity_id": "f1",
            "target_entity_id": "f2",
            "type": "THREATENS",
            "predicate": "THREATENS",
            "subject_type": "Founder",
            "object_type": "Founder",
            "status": "validated",
            "validation_status": "validated",
        }
        with pytest.raises(Neo4jServiceError, match="domain/range"):
            svc.upsert_relation(relation)

    def test_ontology_accepts_valid_triple(self):
        # Both endpoints must exist so the pre-check and domain/range both pass.
        svc = Neo4jService(
            driver=FakeDriver(known_entity_ids={"r1", "m1"}),
            allowed_labels={"Entity", "Risk", "Milestone"},
            allowed_relationships={"THREATENS"},
        )
        relation = {
            "id": "r-ok",
            "source_entity_id": "r1",
            "target_entity_id": "m1",
            "type": "THREATENS",
            "predicate": "THREATENS",
            "subject_type": "Risk",
            "object_type": "Milestone",
            "status": "validated",
            "validation_status": "validated",
        }
        svc.upsert_relation(relation)  # must not raise


# ---------------------------------------------------------------------------
# 6. No-write-before-validation
# ---------------------------------------------------------------------------

class TestNoWriteBeforeValidation:
    def test_pending_entity_is_refused(self):
        svc = _service()
        with pytest.raises(Neo4jServiceError, match="non-validated"):
            svc.upsert_entity({
                "id": "e-pending",
                "name": "Draft",
                "type": "Assumption",
                "label": "Assumption",
                "status": "pending",
            })

    def test_rejected_entity_is_refused(self):
        svc = _service()
        with pytest.raises(Neo4jServiceError, match="non-validated"):
            svc.upsert_entity({
                "id": "e-rejected",
                "name": "Bad",
                "type": "Risk",
                "label": "Risk",
                "status": "rejected",
            })

    def test_needs_more_evidence_entity_is_refused(self):
        svc = _service()
        with pytest.raises(Neo4jServiceError, match="non-validated"):
            svc.upsert_entity({
                "id": "e-nme",
                "name": "Uncertain",
                "type": "Assumption",
                "label": "Assumption",
                "status": "needs_more_evidence",
            })

    def test_validated_entity_is_accepted(self):
        svc = _service()
        svc.upsert_entity({
            "id": "e-ok",
            "name": "Proven",
            "type": "Assumption",
            "label": "Assumption",
            "status": "validated",
            "validation_status": "validated",
        })  # must not raise


# ---------------------------------------------------------------------------
# 7. Stable entity ID determinism
# ---------------------------------------------------------------------------

class TestStableEntityId:
    def test_same_inputs_produce_same_id(self):
        id1 = stable_entity_id("doc-1", "CROs need metadata", "Assumption")
        id2 = stable_entity_id("doc-1", "CROs need metadata", "Assumption")
        assert id1 == id2

    def test_different_label_produces_different_id(self):
        id1 = stable_entity_id("doc-1", "Label A", "Assumption")
        id2 = stable_entity_id("doc-1", "Label B", "Assumption")
        assert id1 != id2

    def test_different_doc_produces_different_id(self):
        id1 = stable_entity_id("doc-1", "Same label", "Assumption")
        id2 = stable_entity_id("doc-2", "Same label", "Assumption")
        assert id1 != id2

    def test_label_normalisation_is_case_insensitive(self):
        id1 = stable_entity_id("doc-1", "CROs Need Metadata", "Assumption")
        id2 = stable_entity_id("doc-1", "cros need metadata", "Assumption")
        assert id1 == id2, "stable_entity_id should normalise label to lowercase"

    def test_output_is_valid_uuid(self):
        import uuid
        result = stable_entity_id("doc-1", "Test", "Risk")
        uuid.UUID(result)  # raises ValueError if not a valid UUID
