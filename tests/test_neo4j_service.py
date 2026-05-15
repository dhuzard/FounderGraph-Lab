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

    Queries containing 'e.type AS type' return ``[{"type": <known>}]`` when
    the id has been pre-registered via ``known_entity_types`` (the type
    inference path used by ``_relation_ops``).
    """

    def __init__(
        self,
        calls: list,
        known_entity_ids: set | None = None,
        known_entity_types: dict | None = None,
    ):
        self.calls = calls
        self._known: set[str] = set(known_entity_ids or [])
        self._types: dict[str, str] = dict(known_entity_types or {})

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
        if "e.type AS type" in query and "id" in p:
            t = self._types.get(p["id"])
            return [{"type": t}] if t else []
        return FakeResult()

    def execute_write(self, fn):
        fn(self)


class FakeDriver:
    """FakeDriver whose sessions are aware of which entity IDs exist."""

    def __init__(
        self,
        known_entity_ids: set | None = None,
        known_entity_types: dict | None = None,
    ):
        self.calls: list = []
        self._known: set[str] = set(known_entity_ids or [])
        self._types: dict[str, str] = dict(known_entity_types or {})

    def session(self, **kwargs):
        return FakeSession(self.calls, self._known, self._types)

    def close(self):
        self.calls.append(("close", {}))


def service(known_entity_ids: set | None = None, known_entity_types: dict | None = None):
    return Neo4jService(
        driver=FakeDriver(
            known_entity_ids=known_entity_ids,
            known_entity_types=known_entity_types,
        ),
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
    # Phase 0.1: labels are now backtick-quoted for safe Cypher interpolation.
    assert "SET e:`Company`" in query
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
    # Phase 0.4: endpoint types are required, so the FakeDriver advertises them.
    graph = service(
        known_entity_ids={"founder1", "company1"},
        known_entity_types={"founder1": "Founder", "company1": "Startup"},
    )
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
    # Phase 0.1: relationship types are now backtick-quoted for safe interpolation.
    assert "[r:`FOUNDED`" in query
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
    # Only target exists; source is absent.  Endpoint types are still supplied
    # explicitly so the (Phase 0.4) domain/range gate succeeds and the missing
    # endpoint is the failing condition under test.
    graph = service(known_entity_ids={"company1"})
    with pytest.raises(Neo4jServiceError, match="missing source entity"):
        graph.upsert_relation({
            "id": "r1",
            "source_entity_id": "founder1",
            "target_entity_id": "company1",
            "type": "FOUNDED",
            "subject_type": "Founder",
            "object_type": "Startup",
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
            "subject_type": "Founder",
            "object_type": "Startup",
            "status": "validated",
        })


def test_valid_relation_write_succeeds_when_both_endpoints_exist():
    """upsert_relation must not raise when both endpoint entities are present."""
    graph = service(
        known_entity_ids={"founder1", "company1"},
        known_entity_types={"founder1": "Founder", "company1": "Startup"},
    )
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
        # Phase 0.4: types are required by domain/range validation; provide
        # them explicitly so the failing condition under test is the missing
        # endpoint, not the (separate) domain/range gate.
        "subject_type": "Company",
        "object_type": "Company",
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
        # Phase 0.4: endpoint types are required by domain/range validation.
        "subject_type": "Company",
        "object_type": "Company",
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


# ---------------------------------------------------------------------------
# Phase 0.1 — safe Cypher identifier quoting
# ---------------------------------------------------------------------------

def test_safe_quoting():
    """_quote_label / _quote_rel must reject malformed or non-whitelisted names
    and return backtick-quoted forms for valid ones."""
    svc = Neo4jService(
        driver=FakeDriver(),
        allowed_labels={"Entity", "Assumption"},
        allowed_relationships={"SUPPORTED_BY"},
    )

    # --- labels ---
    # Spaces, semicolons, leading digits, and unicode lookalikes are rejected.
    # ``normalize_label`` strips spaces/semicolons, which then leaves a valid
    # token shape but a value NOT in the allow-list, so it still raises.
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        svc._quote_label("foo bar")
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        svc._quote_label("foo; DROP")
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        svc._quote_label("1foo")
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        svc._quote_label("NotAllowedLabel")
    # Valid whitelisted label is quoted with backticks.
    assert svc._quote_label("Assumption") == "`Assumption`"

    # --- relationships ---
    with pytest.raises(Neo4jServiceError, match="not whitelisted"):
        svc._quote_rel("foo bar")
    with pytest.raises(Neo4jServiceError, match="not whitelisted"):
        svc._quote_rel("foo; DROP")
    with pytest.raises(Neo4jServiceError, match="not whitelisted"):
        svc._quote_rel("1foo")
    with pytest.raises(Neo4jServiceError, match="not whitelisted"):
        svc._quote_rel("NOT_ALLOWED")
    assert svc._quote_rel("SUPPORTED_BY") == "`SUPPORTED_BY`"


# ---------------------------------------------------------------------------
# Phase 0.2 — relationship MERGE on (source, type, target) triple
# ---------------------------------------------------------------------------

def test_relation_idempotent_on_triple():
    """Upserting the same (source, type, target) triple with different surrogate
    ids must produce identical MERGE shapes — the id is a property, not part
    of the MERGE key, so the second upsert collapses onto the same edge."""
    svc = service(
        known_entity_ids={"e1", "e2"},
        known_entity_types={"e1": "Assumption", "e2": "Evidence"},
    )
    # Allow SUPPORTED_BY for this test.
    svc.allowed_relationships = svc.allowed_relationships | {"SUPPORTED_BY"}

    base = {
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "SUPPORTED_BY",
        "predicate": "SUPPORTED_BY",
        "subject_type": "Assumption",
        "object_type": "Evidence",
        "status": "validated",
        "validation_status": "validated",
    }
    svc.upsert_relation({**base, "id": "rel-A"})
    svc.upsert_relation({**base, "id": "rel-B"})

    merge_queries = [
        q for q, _ in svc.driver.calls
        if isinstance(q, str) and "MERGE (source)-[r:" in q
    ]
    assert len(merge_queries) == 2, "Both upserts must emit a MERGE-on-triple"
    for q in merge_queries:
        # Identity is the triple — no ``{id: $id}`` inline filter on the edge.
        assert "[r:`SUPPORTED_BY`]" in q
        assert "MERGE (source)-[r:`SUPPORTED_BY`]->(target)" in q
        assert "MERGE (source)-[r:`SUPPORTED_BY` {id:" not in q
        # id is set as a property ON CREATE, not as a MERGE key.
        assert "ON CREATE SET r.id = $id" in q


# ---------------------------------------------------------------------------
# Phase 0.3 — relationship indexes emitted by ensure_schema
# ---------------------------------------------------------------------------

def test_ensure_schema_relationship_indexes():
    """ensure_schema must issue one ``CREATE INDEX rel_<NAME>_id ...`` per
    allowed relationship, in addition to the node constraints/indexes."""
    allowed_rels = {"SUPPORTED_BY", "CONTRADICTED_BY", "THREATENS"}
    svc = Neo4jService(
        driver=FakeDriver(),
        allowed_labels={"Entity", "Assumption"},
        allowed_relationships=allowed_rels,
    )
    svc.ensure_schema()

    rel_index_queries = [
        q for q, _ in svc.driver.calls
        if isinstance(q, str) and q.startswith("CREATE INDEX rel_") and "_id IF NOT EXISTS" in q
    ]
    assert len(rel_index_queries) == len(allowed_rels), (
        f"Expected one rel index per allowed relationship, got {rel_index_queries}"
    )
    rel_index_names = {q.split()[2] for q in rel_index_queries}
    expected_names = {f"rel_{name}_id" for name in allowed_rels}
    assert rel_index_names == expected_names
    # Backtick-quoted rel name inside ``FOR ()-[r:`NAME`]-()``.
    for q in rel_index_queries:
        assert "FOR ()-[r:`" in q
        assert "]-() ON (r.id)" in q
