"""Tests for the Phase 7 :class:`CommunityService`.

The service is exercised against captured-query FakeDriver fakes (matching
the patterns used in ``tests/test_vector_index.py``) plus a tiny LLM stub.
We never touch a live Neo4j / Ollama instance here -- every assertion is on
the query text the service emits or on the Python-side dataclass output.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.services.community_service import Community, CommunityService
from app.services.hybrid_retriever import (
    HybridRetriever,
    RetrievalWeights,
    RetrievedItem,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _deterministic_embed(text: str) -> list[float]:
    """Hash-based 768-d vector so tests are byte-stable across runs."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(digest[i % 32] / 255.0) for i in range(768)]


class _StubLLM:
    """LLM stub: returns a fixed string (or raises a queued exception)."""

    def __init__(self, response: str | Exception = "summary text"):
        self.response = response
        self.calls: list[str] = []

    def generate_text(self, prompt: str) -> str:
        self.calls.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeNeo4j:
    """In-memory Neo4j-like peer.

    The clustering path calls ``_rows`` repeatedly; we route by recognising
    a substring of the Cypher.  Writes are pushed onto ``writes`` so tests
    can assert on the exact query / parameter pair.
    """

    def __init__(
        self,
        members: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        gds_rows: list[dict[str, Any]] | None = None,
        search_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.members = members or []
        self.edges = edges or []
        self.gds_rows = gds_rows  # None -> simulate "no GDS plugin"
        self.search_rows = search_rows or []
        self.writes: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _rows(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "gds.list()" in query:
            if self.gds_rows is None:
                raise RuntimeError("gds plugin not installed")
            return list(self.gds_rows)
        if "MATCH (e:Entity)" in query and "validation_status" in query and "labels(e)" in query:
            return list(self.members)
        if "MATCH (a:Entity)-[r]-(b:Entity)" in query:
            return list(self.edges)
        if "gds.louvain.stream" in query:
            # Each row is {entity_id, cluster} computed by the test harness.
            return list(getattr(self, "louvain_rows", []) or [])
        return []

    # ------------------------------------------------------------------
    # Writes (CommunityService.materialize)
    # ------------------------------------------------------------------

    def write_community_node(self, community: dict[str, Any]) -> None:
        # Capture the Cypher-shape we would otherwise emit so the test can
        # assert on it; the CommunityService never reaches into this private
        # detail.
        self.writes.append(
            (
                "MERGE (c:Community {id: $id}) "
                "SET c.summary = $summary, c.embedding = $embedding, "
                "c.size = $size, c.risk_exposure = $risk_exposure",
                dict(community),
            )
        )

    def set_node_community(
        self, entity_ids: list[str], community_id: str
    ) -> None:
        self.writes.append(
            (
                "UNWIND $ids AS id MATCH (e:Entity {id:id}) "
                "MERGE (e)-[r:IN_COMMUNITY]->(c:Community {id:$community_id})",
                {"ids": list(entity_ids), "community_id": community_id},
            )
        )

    def community_summary_search(
        self, query_embedding: list[float], k: int = 5
    ) -> list[dict[str, Any]]:
        self.writes.append(
            (
                "CALL db.index.vector.queryNodes('community_embedding', $k, $vec)",
                {"k": k, "vec": list(query_embedding)},
            )
        )
        return list(self.search_rows)[:k]


def _make_service(**fake_kwargs) -> tuple[CommunityService, FakeNeo4j, _StubLLM]:
    """Build a CommunityService wired against in-memory fakes."""
    neo = FakeNeo4j(**fake_kwargs)
    llm = _StubLLM("summary text")
    svc = CommunityService(neo4j_service=neo, llm_service=llm, embed_fn=_deterministic_embed)
    return svc, neo, llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_detect_falls_back_to_label_propagation_when_no_gds():
    """No GDS plugin -> pure-Python clustering still produces a community."""
    members = [
        {"id": "a", "name": "A", "type": "Assumption", "criticality": "", "labels": ["Entity", "Assumption"]},
        {"id": "b", "name": "B", "type": "Evidence", "criticality": "", "labels": ["Entity", "Evidence"]},
    ]
    edges = [{"source_id": "a", "target_id": "b", "type": "SUPPORTED_BY"}]
    svc, _, _ = _make_service(members=members, edges=edges, gds_rows=None)
    communities = svc.detect()
    assert svc._has_gds() is False
    assert len(communities) >= 1
    assert set(communities[0].member_ids) == {"a", "b"}


def test_detect_groups_connected_components():
    """A chain becomes one community; an isolated node is dropped below min_size."""
    members = [
        {"id": "a", "name": "A", "type": "Assumption", "criticality": "", "labels": []},
        {"id": "b", "name": "B", "type": "Assumption", "criticality": "", "labels": []},
        {"id": "c", "name": "C", "type": "Assumption", "criticality": "", "labels": []},
        # ``solo`` is not in any edge -> its connected component is {solo} only.
        {"id": "solo", "name": "Solo", "type": "Risk", "criticality": "", "labels": []},
    ]
    edges = [
        {"source_id": "a", "target_id": "b", "type": "RELATED_TO"},
        {"source_id": "b", "target_id": "c", "type": "RELATED_TO"},
    ]
    svc, _, _ = _make_service(members=members, edges=edges, gds_rows=None)
    communities = svc.detect(min_size=2)
    # Exactly one community (a-b-c).  Solo is below min_size, so it's dropped.
    assert len(communities) == 1
    assert set(communities[0].member_ids) == {"a", "b", "c"}
    assert all("solo" not in c.member_ids for c in communities)


def test_summarize_populates_summary_and_embedding():
    """The LLM stub returns a fixed summary; the embed_fn returns a 768-d vec."""
    svc, _, llm = _make_service()
    base = Community(
        id="community-test",
        member_ids=("a", "b"),
        size=2,
        risk_exposure=0.0,
    )
    enriched = svc.summarize(base)
    assert enriched.summary == "summary text"
    assert len(enriched.embedding) == 768
    # The LLM was invoked exactly once with the prompt builder's text.
    assert len(llm.calls) == 1
    # The default prompt frames the cluster as a "cluster of related ...
    # entities"; we assert on both nouns so a prompt-text rewrite need only
    # keep one of them.
    assert any(kw in llm.calls[0].lower() for kw in ("cluster", "community"))


def test_materialize_writes_community_and_in_community_edges():
    """The two write helpers emit MERGE Community / MERGE IN_COMMUNITY shapes."""
    svc, neo, _ = _make_service()
    community = Community(
        id="community-xyz",
        member_ids=("a", "b"),
        size=2,
        summary="brief summary",
        embedding=tuple([0.1] * 8),
        risk_exposure=0.5,
    )
    svc.materialize([community])
    captured = [q for q, _ in neo.writes]
    assert any(
        "MERGE (c:Community {id: $id}" in q for q in captured
    ), f"Expected community MERGE in {captured}"
    assert any(
        "MERGE (e)-[r:IN_COMMUNITY]->" in q for q in captured
    ), f"Expected IN_COMMUNITY MERGE in {captured}"
    # And the params should round-trip cleanly.
    community_call = next(
        params for q, params in neo.writes if "MERGE (c:Community" in q
    )
    assert community_call["id"] == "community-xyz"
    assert community_call["size"] == 2
    assert community_call["risk_exposure"] == 0.5


def test_search_uses_vector_index_call():
    """``search`` delegates to the community_embedding vector index."""
    search_rows = [
        {"id": "community-a", "summary": "Cluster A", "size": 5, "risk_exposure": 0.4, "score": 0.91},
        {"id": "community-b", "summary": "Cluster B", "size": 3, "risk_exposure": 0.1, "score": 0.80},
    ]
    svc, neo, _ = _make_service(search_rows=search_rows)
    results = svc.search([0.1] * 8, k=2)
    captured = [q for q, _ in neo.writes]
    assert any(
        "db.index.vector.queryNodes('community_embedding'" in q for q in captured
    ), f"Expected community_embedding index call in {captured}"
    assert [c.id for c in results] == ["community-a", "community-b"]
    assert results[0].risk_exposure == pytest.approx(0.4)


def test_risk_exposure_share_of_high_criticality_members():
    """4 members; 2 Risk + 1 Assumption(high) -> exposure = 3/4 = 0.75."""
    members = [
        {"id": "r1", "name": "Risk1", "type": "Risk", "criticality": "", "labels": []},
        {"id": "r2", "name": "Risk2", "type": "Risk", "criticality": "", "labels": []},
        {"id": "a1", "name": "AssumeHigh", "type": "Assumption", "criticality": "high", "labels": []},
        {"id": "a2", "name": "AssumeLow", "type": "Assumption", "criticality": "low", "labels": []},
    ]
    # Make all four reachable via a chain so they cluster together.
    edges = [
        {"source_id": "r1", "target_id": "r2", "type": "RELATED_TO"},
        {"source_id": "r2", "target_id": "a1", "type": "RELATED_TO"},
        {"source_id": "a1", "target_id": "a2", "type": "RELATED_TO"},
    ]
    svc, _, _ = _make_service(members=members, edges=edges, gds_rows=None)
    communities = svc.detect(min_size=2)
    assert len(communities) == 1
    assert communities[0].size == 4
    assert communities[0].risk_exposure == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Hybrid retriever integration test
# ---------------------------------------------------------------------------


class _CommunityServiceStub:
    """Minimal community-service stand-in used by the retriever-side test."""

    def __init__(self, communities: list[Community]):
        self.communities = communities
        self.calls: list[tuple[list[float], int]] = []

    def search(self, question_embedding: list[float], k: int = 5) -> list[Community]:
        self.calls.append((list(question_embedding), int(k)))
        return list(self.communities)[:k]


class _FakeNeoForRetriever:
    """Just enough Neo4j surface for HybridRetriever (no community helpers)."""

    def vector_search_entities(self, query_embedding, k=10, label_filter=None):
        return []

    def get_neighborhood(self, entity_ids, hops=1, allowed_relationships=None):
        return []


class _FakeQdrantForRetriever:
    def semantic_search(self, query, collection, limit):
        return {"available": True, "results": []}

    def embed(self, text):  # pragma: no cover -- not used
        return _deterministic_embed(text)


def test_hybrid_retriever_global_question_prepends_communities():
    """A global question with a community service attached yields a community item."""
    communities = [
        Community(
            id="community-1",
            member_ids=("a", "b"),
            size=2,
            summary="A high-level theme summary.",
            embedding=(),
            risk_exposure=0.5,
        )
    ]
    community_service = _CommunityServiceStub(communities=communities)
    retriever = HybridRetriever(
        neo4j_service=_FakeNeoForRetriever(),
        qdrant_service=_FakeQdrantForRetriever(),
        embed_fn=_deterministic_embed,
        weights=RetrievalWeights(alpha_cosine=0.6, beta_proximity=0.25, gamma_evidence=0.15),
        seed_k=4,
        community_service=community_service,
    )

    # A clearly "global" question -- contains "overall" + "summary".
    result = retriever.retrieve("What are the overall themes across the portfolio?")
    kinds = {item.kind for item in result.items}
    assert "community" in kinds
    community_items = [item for item in result.items if item.kind == "community"]
    assert community_items and community_items[0].id == "community-1"

    # Sanity check: a local-style question should NOT pull community items.
    local_result = retriever.retrieve("Tell me about assumption a")
    local_kinds = {item.kind for item in local_result.items}
    assert "community" not in local_kinds


def test_hybrid_retriever_without_community_service_unchanged():
    """No community service -> behaviour is identical to pre-Phase-7."""
    retriever = HybridRetriever(
        neo4j_service=_FakeNeoForRetriever(),
        qdrant_service=_FakeQdrantForRetriever(),
        embed_fn=_deterministic_embed,
        weights=RetrievalWeights(alpha_cosine=0.6, beta_proximity=0.25, gamma_evidence=0.15),
        seed_k=4,
    )
    result = retriever.retrieve("What are the overall themes across the portfolio?")
    assert all(item.kind != "community" for item in result.items)
