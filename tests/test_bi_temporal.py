"""Phase 0.5 — bi-temporal hardening of Neo4jService.

These tests use the in-memory FakeDriver to inspect the Cypher emitted by
``Neo4jService.supersede`` and ``Neo4jService.as_of`` without requiring a live
Neo4j instance.  We check three things:

  1. ``supersede`` sets ``valid_to`` and ``superseded_by`` on the old entity.
  2. ``supersede`` creates a ``SUPERSEDED_BY`` edge (backtick-quoted for safety).
  3. ``as_of`` emits a temporal filter using the bi-temporal columns.

In addition we cover the safety contract: self-supersession and missing ids
must raise ``Neo4jServiceError`` before any query is issued.
"""

from __future__ import annotations

import pytest

from app.services.neo4j_service import Neo4jService, Neo4jServiceError


# ---------------------------------------------------------------------------
# Local FakeDriver — kept tiny so this file is self-contained.
# ---------------------------------------------------------------------------

class FakeResult(list):
    pass


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params=None):
        self.calls.append((query, params or {}))
        return FakeResult()


class FakeDriver:
    def __init__(self):
        self.calls: list = []

    def session(self, **kwargs):
        return FakeSession(self.calls)

    def close(self):
        self.calls.append(("close", {}))


def _service():
    return Neo4jService(
        driver=FakeDriver(),
        allowed_labels={"Entity", "Assumption"},
        # SUPERSEDED_BY is a default-allowed relationship per Phase 0.5.
        allowed_relationships={"SUPPORTED_BY", "SUPERSEDED_BY"},
    )


# ---------------------------------------------------------------------------
# supersede()
# ---------------------------------------------------------------------------

def test_bi_temporal_supersede():
    """supersede() must (a) mark valid_to + superseded_by on the old entity and
    (b) MERGE a SUPERSEDED_BY edge from old → new."""
    svc = _service()
    svc.supersede("old-1", "new-1")

    queries = [q for q, _ in svc.driver.calls if isinstance(q, str)]
    params = [p for _, p in svc.driver.calls if isinstance(p, dict) and p]

    # --- (a) valid_to / superseded_by on the old entity ---
    mark_queries = [q for q in queries if "old.valid_to = datetime()" in q]
    assert mark_queries, (
        f"Expected a query setting old.valid_to, got: {queries}"
    )
    assert any("old.superseded_by = $new_id" in q for q in mark_queries), (
        "Expected old.superseded_by to be set to the new entity's id"
    )

    # --- (b) SUPERSEDED_BY edge created from old → new ---
    edge_queries = [
        q for q in queries
        if "MERGE (old)-[r:`SUPERSEDED_BY`]->(new)" in q
    ]
    assert edge_queries, (
        f"Expected a SUPERSEDED_BY MERGE edge, got queries: {queries}"
    )
    # The edge query stamps r.at on creation so the supersession is queryable.
    assert any("ON CREATE SET r.at = datetime()" in q for q in edge_queries)

    # Parameters carry both ids.
    assert any(p.get("old_id") == "old-1" and p.get("new_id") == "new-1" for p in params)


def test_supersede_rejects_missing_ids():
    svc = _service()
    with pytest.raises(Neo4jServiceError):
        svc.supersede("", "new-1")
    with pytest.raises(Neo4jServiceError):
        svc.supersede("old-1", "")


def test_supersede_rejects_self_supersession():
    svc = _service()
    with pytest.raises(Neo4jServiceError, match="self-supersession"):
        svc.supersede("same-id", "same-id")


# ---------------------------------------------------------------------------
# as_of()
# ---------------------------------------------------------------------------

def test_as_of_emits_bi_temporal_filter():
    """as_of() must filter on valid_from <= ts AND (valid_to IS NULL OR ts < valid_to)."""
    svc = _service()
    svc.as_of("2024-01-15T00:00:00Z")

    queries = [q for q, _ in svc.driver.calls if isinstance(q, str) and "MATCH (e:Entity)" in q]
    assert queries, "as_of must emit a MATCH (e:Entity) query"
    q = queries[-1]
    assert "e.valid_from <= datetime($ts)" in q
    assert "e.valid_to IS NULL OR datetime($ts) < e.valid_to" in q

    params = [p for _, p in svc.driver.calls if isinstance(p, dict) and "ts" in p]
    assert any(p["ts"] == "2024-01-15T00:00:00Z" for p in params)


def test_as_of_with_label_filter_uses_quoted_label():
    """When a label is supplied, the Cypher must include a backtick-quoted
    subtype filter (e.g. ``(e:Entity:`Assumption`)``)."""
    svc = _service()
    svc.as_of("2024-01-15T00:00:00Z", label="Assumption")

    queries = [q for q, _ in svc.driver.calls if isinstance(q, str)]
    assert any("(e:Entity:`Assumption`)" in q for q in queries), (
        f"Expected a backtick-quoted label filter, got: {queries}"
    )


def test_as_of_rejects_non_whitelisted_label():
    svc = _service()
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        svc.as_of("2024-01-15T00:00:00Z", label="NotAllowed")


# ---------------------------------------------------------------------------
# Entity create / update timestamps include valid_from
# ---------------------------------------------------------------------------

def test_entity_create_sets_valid_from():
    """ON CREATE on entity MERGE must stamp valid_from in addition to created_at."""
    svc = _service()
    entity_query, _ = svc._entity_ops({
        "id": "e1",
        "type": "Assumption",
        "label": "X",
        "status": "validated",
        "validation_status": "validated",
    })[0]

    assert "ON CREATE SET e.created_at = datetime()" in entity_query
    assert "e.valid_from = datetime()" in entity_query


def test_relation_create_sets_valid_from():
    """ON CREATE on relation MERGE must stamp valid_from in addition to created_at."""
    svc = _service()
    # Supply types so domain/range succeeds; SUPPORTED_BY needs Assumption→Evidence.
    relation = {
        "id": "r1",
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "predicate": "SUPPORTED_BY",
        "subject_type": "Assumption",
        "object_type": "Evidence",
        "status": "validated",
        "validation_status": "validated",
    }
    rel_query, _ = svc._relation_ops(relation)[0]
    assert "ON CREATE SET r.id = $id" in rel_query
    assert "r.created_at = datetime()" in rel_query
    assert "r.valid_from = datetime()" in rel_query
    assert "ON MATCH SET r.updated_at = datetime()" in rel_query
