"""End-to-end smoke test: one document through the full mocked pipeline.

Covers the path from raw Markdown text → extraction → staging → validation →
Neo4j write → export without calling any real external service.

Invariants verified:
  - source_document_id is propagated to every staged entity and relation.
  - Entity IDs are stable UUIDv5 values (same input → same ID across runs).
  - Temporary IDs in relation endpoints are replaced by stable IDs.
  - reviewer_comment survives extraction and is written to Neo4j.
  - evidence_grade is written; confidence is never written to Neo4j.
  - Document provenance links use MERGE (not MATCH) for the Document node.
  - Pending entities are refused; only validated records reach Neo4j.
  - export_all() creates a ZIP and manifest only when validated knowledge exists.
  - Manifest reports correct entity_count, relation_count, source_document_count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.entity_extractor import EntityExtractor, stable_entity_id
from app.services.export_service import export_all
from app.services.neo4j_service import Neo4jService, Neo4jServiceError


# ---------------------------------------------------------------------------
# Minimal fake infrastructure — no external service calls
# ---------------------------------------------------------------------------

class FakeLLM:
    """Returns pre-canned JSON responses in order; raises if exhausted."""

    def __init__(self, responses: list):
        self._responses = list(responses)

    def generate_json(self, prompt: str):  # noqa: ARG002
        if not self._responses:
            raise AssertionError("FakeLLM exhausted — more LLM calls than expected")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResult(list):
    pass


class FakeSession:
    """Records every run() call; handles execute_write and count queries."""

    def __init__(self, calls: list, known_entity_ids: set | None = None):
        self.calls = calls
        self._known: set[str] = set(known_entity_ids or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query: str, params=None):
        p = params or {}
        self.calls.append((query, p))
        # Simulate MATCH (e:Entity {id: $id}) RETURN count(e) AS n
        if "count(e) AS n" in query and "id" in p:
            n = 1 if p["id"] in self._known else 0
            return [{"n": n}]
        return FakeResult()

    def execute_write(self, fn):
        fn(self)


class FakeDriver:
    """Accumulates all session calls in a single shared list."""

    def __init__(self, known_entity_ids: set | None = None):
        self.calls: list = []
        self._known: set[str] = set(known_entity_ids or [])

    def session(self, **kwargs):  # noqa: ARG002
        return FakeSession(self.calls, self._known)

    def close(self):
        self.calls.append(("close", {}))


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------

def test_end_to_end_mocked_pipeline_preserves_provenance_and_exports(tmp_path):
    DOC_ID = "doc-medex-v1"
    DOCUMENT_TEXT = (
        "Medex AI is a biotech startup. "
        "Their core assumption is that CROs will pay a subscription fee "
        "for automated metadata interoperability."
    )

    # Pre-compute the stable IDs so we can verify them without re-running the
    # hash — the UUIDv5 is deterministic for the same (doc_id, label, type).
    expected_startup_id = stable_entity_id(DOC_ID, "Medex AI", "Startup")
    expected_assumption_id = stable_entity_id(
        DOC_ID, "CROs will pay subscription fee", "Assumption"
    )

    # -----------------------------------------------------------------------
    # 1. Extraction to staging (three LLM calls: classify, entities, relations)
    # -----------------------------------------------------------------------
    llm = FakeLLM([
        # classify_document
        {
            "document_type": "PitchDeck",
            "secondary_types": [],
            "summary": "Biotech startup pitch.",
            "tags": ["biotech"],
            "confidence": "high",
        },
        # extract_entities
        {
            "entities": [
                {
                    "temporary_id": "TMP-STARTUP",
                    "type": "Startup",
                    "label": "Medex AI",
                    "description": "Biotech startup for CRO metadata interoperability.",
                    "source_snippet": "Medex AI is a biotech startup.",
                    "evidence_grade": "direct_quote",
                },
                {
                    "temporary_id": "TMP-ASSUMPTION",
                    "type": "Assumption",
                    "label": "CROs will pay subscription fee",
                    "description": "Core pricing assumption.",
                    "source_snippet": "CROs will pay a subscription fee.",
                    "evidence_grade": "paraphrase",
                },
            ]
        },
        # extract_relations
        {
            "relations": [
                {
                    "source_entity_id": "TMP-STARTUP",
                    "target_entity_id": "TMP-ASSUMPTION",
                    "type": "RELATED_TO",
                    "evidence_grade": "inference",
                }
            ]
        },
    ])

    staging_dir = tmp_path / "staging"
    extractor = EntityExtractor(llm_service=llm, staging_dir=staging_dir)
    result = extractor.extract_to_staging(DOCUMENT_TEXT, {"source_document_id": DOC_ID})

    # -----------------------------------------------------------------------
    # 2. Stable entity IDs — same input must produce the same UUID
    # -----------------------------------------------------------------------
    assert result.wrote_files is True
    extracted_ids = {e.id for e in result.entities}
    assert expected_startup_id in extracted_ids, "Startup must receive stable UUIDv5 ID"
    assert expected_assumption_id in extracted_ids, "Assumption must receive stable UUIDv5 ID"

    # -----------------------------------------------------------------------
    # 3. source_document_id propagation on every staged object
    # -----------------------------------------------------------------------
    for entity in result.entities:
        assert entity.source_document_id == DOC_ID, (
            f"source_document_id must be propagated to entity {entity.id!r}"
        )

    assert len(result.relations) == 1
    rel = result.relations[0]
    assert rel.source_document_id == DOC_ID

    # TMP-xxx IDs in relation endpoints must be replaced with stable IDs.
    assert rel.source_entity_id == expected_startup_id, (
        "Relation source must point to stable Startup ID, not TMP-STARTUP"
    )
    assert rel.target_entity_id == expected_assumption_id, (
        "Relation target must point to stable Assumption ID, not TMP-ASSUMPTION"
    )

    # -----------------------------------------------------------------------
    # 4. Staging files on disk contain source_document_id
    # -----------------------------------------------------------------------
    staged_entities = json.loads((staging_dir / "candidate_entities.json").read_text())
    assert len(staged_entities) == 2
    for e in staged_entities:
        assert e["source_document_id"] == DOC_ID

    # -----------------------------------------------------------------------
    # 5. Validation gate — pending entities must be refused by Neo4j
    # -----------------------------------------------------------------------
    driver = FakeDriver()
    neo4j = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Startup", "Assumption"},
        allowed_relationships={"RELATED_TO"},
    )

    with pytest.raises(Neo4jServiceError, match="non-validated"):
        neo4j.upsert_entity({
            "id": expected_startup_id,
            "name": "Medex AI",
            "type": "Startup",
            "label": "Startup",
            "status": "pending",  # not yet validated
        })

    # -----------------------------------------------------------------------
    # 6–8. Validated write: entities + relation succeed; reviewer_comment and
    #       evidence_grade are persisted; confidence is never written.
    # -----------------------------------------------------------------------
    validated_entities = [
        {
            "id": expected_startup_id,
            "name": "Medex AI",
            "label": "Startup",
            "type": "Startup",
            "description": "Biotech startup for CRO metadata interoperability.",
            "source_document_id": DOC_ID,
            "source_snippet": "Medex AI is a biotech startup.",
            "evidence_grade": "direct_quote",
            "reviewer_comment": "Confirmed from founder interview transcript.",
            "status": "validated",
            "validation_status": "validated",
        },
        {
            "id": expected_assumption_id,
            "name": "CROs will pay subscription fee",
            "label": "Assumption",
            "type": "Assumption",
            "description": "Core pricing assumption.",
            "source_document_id": DOC_ID,
            "source_snippet": "CROs will pay a subscription fee.",
            "evidence_grade": "paraphrase",
            "status": "validated",
            "validation_status": "validated",
        },
    ]
    validated_relations = [
        {
            "id": f"{expected_startup_id}:RELATED_TO:{expected_assumption_id}",
            "source_entity_id": expected_startup_id,
            "target_entity_id": expected_assumption_id,
            "type": "RELATED_TO",
            "predicate": "RELATED_TO",
            "source_document_id": DOC_ID,
            "evidence_grade": "inference",
            "reviewer_comment": "Directional correctness verified.",
            "status": "validated",
            "validation_status": "validated",
        },
    ]

    # Both endpoints are in the batch → no DB round-trip for endpoint pre-check.
    # FakeDriver starts empty; the batch_ids short-circuit is what makes this safe.
    driver = FakeDriver()
    neo4j = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Startup", "Assumption"},
        allowed_relationships={"RELATED_TO"},
    )
    neo4j.upsert_validated_knowledge(validated_entities, validated_relations)

    # Verify MERGE queries were executed.
    all_queries = [q for q, _ in driver.calls if isinstance(q, str)]
    merge_queries = [q for q in all_queries if "MERGE" in q]
    assert len(merge_queries) >= 3, (
        "Expected at least 3 MERGE calls: one per entity and one for the relation"
    )

    # Verify reviewer_comment and evidence_grade are in the Startup entity params;
    # confidence must never appear in Neo4j params.
    entity_params_list = [
        p for q, p in driver.calls
        if isinstance(q, str) and "MERGE (e:Entity" in q
    ]
    startup_params = next(
        (p for p in entity_params_list if p.get("id") == expected_startup_id), None
    )
    assert startup_params is not None, "Startup entity MERGE params not found"
    assert startup_params["reviewer_comment"] == "Confirmed from founder interview transcript."
    assert startup_params["evidence_grade"] == "direct_quote"
    assert startup_params["source_document_id"] == DOC_ID
    assert "confidence" not in startup_params, (
        "LLM confidence must never reach Neo4j params; use evidence_grade"
    )

    # Verify MENTIONS provenance link uses MERGE for Document node (not MATCH).
    mentions_queries = [q for q in all_queries if "MENTIONS" in q]
    assert len(mentions_queries) == 2, (
        "Each entity with source_document_id must generate a MENTIONS provenance link"
    )
    for q in mentions_queries:
        assert "MERGE (d:Document" in q, (
            "Document node in MENTIONS link must use MERGE so it is created on demand"
        )
        assert "MATCH (d:Document" not in q

    # -----------------------------------------------------------------------
    # 9–10. export_all creates ZIP; manifest has correct counts
    # -----------------------------------------------------------------------
    export_graph = {
        "nodes": [
            {
                "id": expected_startup_id,
                "type": "Startup",
                "name": "Medex AI",
                "source_document_id": DOC_ID,
                "evidence_grade": "direct_quote",
                "reviewer_comment": "Confirmed from founder interview transcript.",
                "status": "validated",
            },
            {
                "id": expected_assumption_id,
                "type": "Assumption",
                "name": "CROs will pay subscription fee",
                "source_document_id": DOC_ID,
                "evidence_grade": "paraphrase",
                "status": "validated",
            },
        ],
        "edges": [
            {
                "source": expected_startup_id,
                "target": expected_assumption_id,
                "relationship": "RELATED_TO",
                "evidence_grade": "inference",
                "source_document_id": DOC_ID,
            }
        ],
    }

    export_dir = tmp_path / "exports"
    paths = export_all(graph=export_graph, export_dir=export_dir)

    assert Path(paths["zip"]).exists(), "export ZIP must be written to disk"
    assert Path(paths["manifest"]).exists(), "manifest must be written to disk"

    manifest = json.loads(Path(paths["manifest"]).read_text())
    assert manifest["entity_count"] == 2
    assert manifest["relation_count"] == 1
    assert manifest["source_document_count"] == 1, (
        "Both nodes share doc-medex-v1 → one unique source document"
    )
    assert "graph_snapshot_id" in manifest
    assert "export_timestamp" in manifest
    assert manifest["ontology_version"] != "unknown"
