"""Tests for Phase 4 native Neo4j vector additions to Neo4jService.

These exercise the *query shapes* emitted by the new methods, not a live
Neo4j server: a captured-query ``FakeDriver`` lets us assert that
``ensure_schema`` emits ``CREATE VECTOR INDEX entity_embedding``, that
``upsert_entity_embedding`` is parameterised, that ``vector_search_entities``
goes through ``db.index.vector.queryNodes``, and that ``get_neighborhood``
delegates to ``apoc.path.subgraphAll``.
"""

from __future__ import annotations

import pytest

from app.services.neo4j_service import Neo4jService, Neo4jServiceError


# ---------------------------------------------------------------------------
# Fake driver that records every (query, params) pair.
# ---------------------------------------------------------------------------


class _CapturedSession:
    def __init__(self, calls: list, records: list | None = None):
        self.calls = calls
        self._records = records or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params=None):
        self.calls.append((query, params or {}))
        return list(self._records)


class _CapturedDriver:
    def __init__(self, records: list | None = None):
        self.calls: list = []
        self._records = records or []

    def session(self, **kwargs):
        return _CapturedSession(self.calls, self._records)

    def close(self):
        self.calls.append(("close", {}))


def _make_service(records=None) -> tuple[Neo4jService, _CapturedDriver]:
    driver = _CapturedDriver(records=records)
    svc = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Document", "Assumption", "Evidence", "Startup"},
        allowed_relationships={
            "RELATED_TO",
            "SUPPORTED_BY",
            "MENTIONS",
            "SOURCE_OF",
            "SUPERSEDED_BY",
            "SAME_AS",
        },
    )
    return svc, driver


# ---------------------------------------------------------------------------
# ensure_schema
# ---------------------------------------------------------------------------


def test_ensure_schema_emits_entity_vector_index():
    """``ensure_schema`` should run the generated VECTOR INDEX DDL once."""
    svc, driver = _make_service()
    svc.ensure_schema()
    statements = [q for q, _ in driver.calls if isinstance(q, str)]
    vector_statements = [s for s in statements if "CREATE VECTOR INDEX entity_embedding" in s]
    assert len(vector_statements) == 1
    # The dimension is hardcoded in the DDL and must travel through.
    assert "vector.dimensions" in vector_statements[0]
    assert "768" in vector_statements[0]
    assert "cosine" in vector_statements[0].lower()


def test_ensure_schema_tolerates_vector_index_failure(monkeypatch):
    """Older Neo4j servers without vector index syntax must not abort schema."""

    class _ExplodingVectorSession(_CapturedSession):
        def run(self, query, params=None):
            self.calls.append((query, params or {}))
            if "VECTOR INDEX" in query.upper():
                raise RuntimeError("simulated old-Neo4j syntax error")
            return []

    class _ExplodingDriver(_CapturedDriver):
        def session(self, **kwargs):
            return _ExplodingVectorSession(self.calls)

    driver = _ExplodingDriver()
    svc = Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Document", "Startup"},
        allowed_relationships={"RELATED_TO", "MENTIONS", "SOURCE_OF", "SUPERSEDED_BY", "SAME_AS"},
    )
    # Should not raise -- the failure is logged and swallowed.
    svc.ensure_schema()
    # And every non-vector statement should still have been attempted.
    non_vector = [q for q, _ in driver.calls if isinstance(q, str) and "VECTOR INDEX" not in q.upper()]
    assert non_vector, "non-vector DDL statements should still have been run"


# ---------------------------------------------------------------------------
# upsert_entity_embedding
# ---------------------------------------------------------------------------


def test_upsert_entity_embedding_uses_parameterized_query():
    svc, driver = _make_service()
    svc.upsert_entity_embedding("entity-1", [0.1, 0.2, 0.3])
    query, params = driver.calls[-1]
    assert "SET e.embedding = $embedding" in query
    assert params["id"] == "entity-1"
    assert params["embedding"] == [0.1, 0.2, 0.3]
    assert "model" in params


def test_upsert_entity_embedding_rejects_empty_vector():
    svc, _ = _make_service()
    with pytest.raises(Neo4jServiceError):
        svc.upsert_entity_embedding("entity-1", [])


def test_upsert_entity_embedding_requires_id():
    svc, _ = _make_service()
    with pytest.raises(Neo4jServiceError):
        svc.upsert_entity_embedding("", [0.1])


# ---------------------------------------------------------------------------
# vector_search_entities
# ---------------------------------------------------------------------------


def test_vector_search_entities_uses_index_call():
    svc, driver = _make_service(
        records=[{"id": "e1", "name": "n", "type": "Assumption", "score": 0.9}]
    )
    out = svc.vector_search_entities([0.1, 0.2], k=5)
    query, params = driver.calls[-1]
    assert "db.index.vector.queryNodes('entity_embedding'" in query
    assert params["k"] == 5
    assert params["vec"] == [0.1, 0.2]
    assert out and out[0]["id"] == "e1"


def test_vector_search_entities_label_filter_quoted():
    svc, driver = _make_service()
    svc.vector_search_entities([0.1], k=3, label_filter="Assumption")
    query, _ = driver.calls[-1]
    # Backtick-quoted label confirms _quote_label was used.
    assert "node:`Assumption`" in query


def test_vector_search_entities_rejects_off_whitelist_label():
    svc, _ = _make_service()
    with pytest.raises(Neo4jServiceError):
        svc.vector_search_entities([0.1], k=3, label_filter="DROP TABLE")


# ---------------------------------------------------------------------------
# get_neighborhood
# ---------------------------------------------------------------------------


def test_get_neighborhood_uses_apoc_subgraph():
    svc, driver = _make_service(
        records=[
            {
                "source_id": "a",
                "source_name": "A",
                "type": "SUPPORTED_BY",
                "target_id": "b",
                "target_name": "B",
            }
        ]
    )
    rows = svc.get_neighborhood(["a"], hops=1)
    query, params = driver.calls[-1]
    assert "apoc.path.subgraphAll" in query
    assert params["ids"] == ["a"]
    assert params["hops"] == 1
    assert rows and rows[0]["source_id"] == "a"


def test_get_neighborhood_respects_relationship_whitelist():
    svc, driver = _make_service()
    svc.get_neighborhood(["a"], hops=2, allowed_relationships=["SUPPORTED_BY", "MENTIONS"])
    _, params = driver.calls[-1]
    # The filter is joined with '|' per APOC convention.
    assert params["rel_filter"] in {"SUPPORTED_BY|MENTIONS", "MENTIONS|SUPPORTED_BY"}


def test_get_neighborhood_rejects_off_whitelist_relationship():
    svc, _ = _make_service()
    with pytest.raises(Neo4jServiceError):
        svc.get_neighborhood(["a"], hops=1, allowed_relationships=["EVIL_RELATION"])


def test_get_neighborhood_with_empty_ids_short_circuits():
    svc, driver = _make_service()
    assert svc.get_neighborhood([], hops=1) == []
    # No driver calls should have been made.
    assert driver.calls == []
