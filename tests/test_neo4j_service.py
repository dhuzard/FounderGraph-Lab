"""Tests for Neo4jService: parameterized writes, whitelist enforcement,
MENTIONS link provenance, and relation endpoint existence checks."""

from __future__ import annotations

import pytest

from app.services.neo4j_service import Neo4jService, Neo4jServiceError


# ---------------------------------------------------------------------------
# Fake Neo4j driver infrastructure
# ---------------------------------------------------------------------------

class FakeResult(list):
    pass


class FakeSession:
    """FakeSession that responds to entity-existence count queries.

    Queries containing 'count(e) AS n' return {"n": 1} for IDs in
    known_entity_ids and {"n": 0} for all others, mirroring what a real
    Neo4j session would return for MATCH (e:Entity {id: $id}).
    """

    def __init__(self, calls: list, known_entity_ids: set | None = None):
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
    """FakeDriver whose sessions are aware of which entity IDs exist."""

    def __init__(self, known_entity_ids: set | None = None):
        self.calls: list = []
        self._known: set[str] = set(known_entity_ids or [])

    def session(self, **kwargs):
        return FakeSession(self.calls, self._known)

    def close(self):
        self.calls.append(("close", {}))


def service(known_entity_ids: set | None = None):
    return Neo4jService(
        driver=FakeDriver(known_entity_ids=known_entity_ids),
        allowed_labels={"Entity", "Company"},
        allowed_relationships={"FOUNDED"},
    )


# ---------------------------------------------------------------------------
# Whitelist and validation enforcement (existing tests)
# ---------------------------------------------------------------------------

def test_refuses_non_validated_entity_write():
    graph = service()
    with pytest.raises(Neo4jServiceError, match="non-validated"):
        graph.upsert_entity({"id": "e1", "name": "Draft Co", "label": "Company", "status": "pending"})


def test_refuses_non_whitelisted_label():
    graph = service()
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        graph.upsert_entity({"id": "e1", "name": "Bad", "label": "BadLabel", "status": "validated"})


def test_upsert_entity_uses_parameterized_query_and_preserves_provenance():
    graph = service()
    graph.upsert_entity(
        {
            "id": "e1",
            "name": "Acme",
            "label": "Company",
            "status": "validated",
            "source_snippet": "Acme raised a seed round.",
            "provenance": {"document_id": "doc1", "offset": 42},
        }
    )

    query, params = graph.driver.calls[-1]
    assert "SET e:Company" in query
    assert "$name" in query
    assert "Acme" not in query
    assert params["source_snippet"] == "Acme raised a seed round."
    assert params["provenance_json"] == '{"document_id": "doc1", "offset": 42}'


def test_refuses_non_whitelisted_relationship_type():
    graph = service()
    with pytest.raises(Neo4jServiceError, match="Relationship type is not whitelisted"):
        graph.upsert_relation(
            {
                "id": "r1",
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "type": "OWNS; MATCH (n) DETACH DELETE n",
                "status": "validated",
            }
        )


def test_upsert_relation_uses_whitelisted_type_and_parameters():
    # Both endpoints must exist in the graph for the pre-check to pass.
    graph = service(known_entity_ids={"founder1", "company1"})
    graph.upsert_relation(
        {
            "id": "r1",
            "source_entity_id": "founder1",
            "target_entity_id": "company1",
            "type": "FOUNDED",
            "status": "validated",
            "source_snippet": "Founder started the company.",
            "provenance": {"document_id": "doc1"},
        }
    )

    query, params = graph.driver.calls[-1]
    assert "[r:FOUNDED" in query
    assert "$source_id" in query
    assert "founder1" not in query
    assert params["source_id"] == "founder1"
    assert params["target_id"] == "company1"
    assert params["source_snippet"] == "Founder started the company."


# ---------------------------------------------------------------------------
# MENTIONS link provenance (Patch B)
# ---------------------------------------------------------------------------

def test_mentions_link_ops_present_when_source_document_id_set():
    """_entity_ops must return two ops when source_document_id is set:
    the entity MERGE and the Document MERGE + MENTIONS MERGE."""
    graph = service()
    entity = {
        "id": "e1",
        "name": "Test Entity",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
        "source_document_id": "doc-1",
        "source_snippet": "some text",
    }
    ops = graph._entity_ops(entity)

    assert len(ops) == 2, "Expected entity MERGE op + MENTIONS link op"
    entity_query, _ = ops[0]
    link_query, link_params = ops[1]

    assert "MERGE (e:Entity {id: $id})" in entity_query
    assert "MENTIONS" in link_query
    assert link_params["document_id"] == "doc-1"
    assert link_params["entity_id"] == "e1"


def test_mentions_link_absent_when_no_source_document_id():
    """_entity_ops must return only one op when source_document_id is absent."""
    graph = service()
    entity = {
        "id": "e1",
        "name": "No Provenance",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
    }
    ops = graph._entity_ops(entity)
    assert len(ops) == 1


def test_mentions_link_uses_merge_not_match_for_document():
    """The MENTIONS link query must use MERGE for the Document node so it is
    created on demand even when the document was not explicitly upserted."""
    graph = service()
    entity = {
        "id": "e1",
        "name": "X",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
        "source_document_id": "doc-99",
    }
    ops = graph._entity_ops(entity)
    link_query, _ = ops[1]

    assert link_query.strip().startswith("MERGE (d:Document"), (
        "Document node must be created via MERGE, not silently skipped by MATCH"
    )
    assert "MATCH (d:Document" not in link_query


# ---------------------------------------------------------------------------
# Relation endpoint existence checks (Patch C)
# ---------------------------------------------------------------------------

def test_relation_write_raises_when_source_missing():
    """upsert_relation must raise Neo4jServiceError when the source entity
    does not exist in the graph."""
    # Only target exists; source is absent.
    graph = service(known_entity_ids={"company1"})
    with pytest.raises(Neo4jServiceError, match="missing source entity"):
        graph.upsert_relation({
            "id": "r1",
            "source_entity_id": "founder1",
            "target_entity_id": "company1",
            "type": "FOUNDED",
            "status": "validated",
        })


def test_relation_write_raises_when_target_missing():
    """upsert_relation must raise Neo4jServiceError when the target entity
    does not exist in the graph."""
    # Only source exists; target is absent.
    graph = service(known_entity_ids={"founder1"})
    with pytest.raises(Neo4jServiceError, match="missing target entity"):
        graph.upsert_relation({
            "id": "r1",
            "source_entity_id": "founder1",
            "target_entity_id": "company1",
            "type": "FOUNDED",
            "status": "validated",
        })


def test_valid_relation_write_succeeds_when_both_endpoints_exist():
    """upsert_relation must not raise when both endpoint entities are present."""
    graph = service(known_entity_ids={"founder1", "company1"})
    graph.upsert_relation({
        "id": "r1",
        "source_entity_id": "founder1",
        "target_entity_id": "company1",
        "type": "FOUNDED",
        "status": "validated",
    })  # must not raise


def test_upsert_validated_knowledge_raises_before_write_when_endpoint_missing():
    """upsert_validated_knowledge must raise before execute_write if a relation
    references an entity not in the batch and not in the graph."""
    driver = FakeDriver(known_entity_ids={"e1"})
    graph = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Company"},
        allowed_relationships={"RELATED_TO"},
    )
    entities = [{
        "id": "e1",
        "name": "A",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
    }]
    relations = [{
        "source_entity_id": "e1",
        "target_entity_id": "orphan",   # not in batch, not in DB
        "type": "RELATED_TO",
        "status": "validated",
        "validation_status": "validated",
    }]

    with pytest.raises(Neo4jServiceError, match="missing target entity"):
        graph.upsert_validated_knowledge(entities, relations)

    # No MERGE writes should have executed — execute_write was never reached.
    merge_calls = [q for q, _ in driver.calls if isinstance(q, str) and "MERGE" in q]
    assert merge_calls == [], "No MERGE queries should run when pre-check fails"


def test_upsert_validated_knowledge_accepts_batch_relation_endpoints():
    """Relations referencing entities in the same batch must be accepted
    without a DB round-trip for those endpoints."""
    driver = FakeDriver(known_entity_ids=set())  # DB is empty
    graph = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Company"},
        allowed_relationships={"RELATED_TO"},
    )
    entities = [
        {
            "id": "e1",
            "name": "A",
            "type": "Company",
            "label": "Company",
            "status": "validated",
            "validation_status": "validated",
        },
        {
            "id": "e2",
            "name": "B",
            "type": "Company",
            "label": "Company",
            "status": "validated",
            "validation_status": "validated",
        },
    ]
    relations = [{
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "RELATED_TO",
        "status": "validated",
        "validation_status": "validated",
    }]

    # Must not raise: both endpoints are in the same batch even though the DB is empty.
    graph.upsert_validated_knowledge(entities, relations)


# ---------------------------------------------------------------------------
# Reviewer comment persistence (Patch Set 2)
# ---------------------------------------------------------------------------

def test_reviewer_comment_in_entity_cypher():
    """_entity_ops must include reviewer_comment in params and the Cypher SET clause."""
    graph = service()
    entity = {
        "id": "e1",
        "name": "Test Entity",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
        "reviewer_comment": "Confirmed via founder interview.",
    }
    ops = graph._entity_ops(entity)
    query, params = ops[0]

    assert "reviewer_comment" in params
    assert params["reviewer_comment"] == "Confirmed via founder interview."
    assert "e.reviewer_comment = $reviewer_comment" in query


def test_reviewer_comment_in_relation_cypher():
    """_relation_ops must include reviewer_comment in params and the Cypher SET clause."""
    graph = Neo4jService(
        driver=FakeDriver(),
        allowed_labels={"Entity", "Company"},
        allowed_relationships={"RELATED_TO"},
    )
    relation = {
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "RELATED_TO",
        "status": "validated",
        "validation_status": "validated",
        "reviewer_comment": "Directional correctness verified manually.",
    }
    ops = graph._relation_ops(relation)
    query, params = ops[0]

    assert "reviewer_comment" in params
    assert params["reviewer_comment"] == "Directional correctness verified manually."
    assert "r.reviewer_comment = $reviewer_comment" in query


# ---------------------------------------------------------------------------
# Canonical evidence fields (Patch Set 2)
# ---------------------------------------------------------------------------

def test_numeric_llm_confidence_not_persisted_as_confidence():
    """LLM-emitted confidence (numeric or string) must not be stored as e.confidence.
    evidence_grade is the canonical provenance field; confidence is quarantined."""
    graph = service()
    entity = {
        "id": "e1",
        "name": "Test",
        "type": "Company",
        "label": "Company",
        "status": "validated",
        "validation_status": "validated",
        "confidence": "medium",   # old LLM string confidence
        "evidence_grade": "paraphrase",
    }
    ops = graph._entity_ops(entity)
    _, params = ops[0]

    assert "confidence" not in params, (
        "LLM confidence must not be persisted to Neo4j; use evidence_grade instead"
    )
    assert params["evidence_grade"] == "paraphrase"


def test_get_all_entities_returns_evidence_fields_not_confidence():
    """get_all_entities must query evidence_grade and reviewer_confidence,
    not the stale confidence property."""
    graph = service()
    graph.get_all_entities()

    queries = [q for q, _ in graph.driver.calls if isinstance(q, str) and "MATCH (e:Entity)" in q]
    assert queries, "get_all_entities should execute a Cypher query"
    query = queries[0]

    assert "evidence_grade" in query
    assert "reviewer_confidence" in query
    assert "reviewer_comment" in query
    assert "e.confidence AS confidence" not in query


def test_get_unsupported_assumptions_returns_evidence_fields():
    """get_unsupported_assumptions must return evidence_grade and reviewer_confidence,
    not the stale confidence property."""
    graph = service()
    graph.get_unsupported_assumptions()

    queries = [q for q, _ in graph.driver.calls if isinstance(q, str) and "Assumption" in q]
    assert queries, "get_unsupported_assumptions should execute a Cypher query"
    query = queries[0]

    assert "evidence_grade" in query
    assert "reviewer_confidence" in query
    assert "a.confidence AS confidence" not in query
