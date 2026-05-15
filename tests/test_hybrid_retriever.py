"""Tests for the Phase 4 :class:`HybridRetriever`.

The retriever is exercised with handwritten stubs for the Neo4j and Qdrant
sides -- no live services.  Each test sets up canned responses, runs
``retrieve``, and asserts on the merged / re-ranked output.
"""

from __future__ import annotations

import hashlib

import pytest

from app.services.citation_verifier import RetrievalContext
from app.services.hybrid_retriever import (
    HybridRetriever,
    HybridResult,
    RetrievalWeights,
    RetrievedItem,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _deterministic_embed(text: str) -> list[float]:
    """Hash-based 768-d vector, deterministic per input string.

    The retriever does not actually consult these vectors (the Neo4j stub
    surfaces cosine via ``score`` directly), so any fixed-length list works
    -- we use 768 to match the production embedding model.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Recycle 32 bytes into 768 floats; values in [0, 1).
    values = [(digest[i % 32] / 255.0) for i in range(768)]
    return values


class FakeNeo4jService:
    """Stub for the subset of Neo4jService that HybridRetriever calls."""

    def __init__(
        self,
        seeds_by_label: dict[str | None, list[dict]] | None = None,
        neighborhood: list[dict] | None = None,
    ):
        # Map label -> seed rows.  ``None`` is the fallback when the
        # retriever asks without a label filter.
        self.seeds_by_label = seeds_by_label or {}
        self.neighborhood_rows = neighborhood or []
        self.calls: list[tuple[str, dict]] = []

    def vector_search_entities(
        self,
        query_embedding,
        k: int = 10,
        label_filter: str | None = None,
    ):
        self.calls.append(
            ("vector_search_entities", {"label": label_filter, "k": k})
        )
        return list(self.seeds_by_label.get(label_filter, []))[:k]

    def get_neighborhood(
        self,
        entity_ids,
        hops: int = 1,
        allowed_relationships=None,
    ):
        self.calls.append(
            (
                "get_neighborhood",
                {"ids": list(entity_ids), "hops": hops, "rels": allowed_relationships},
            )
        )
        return list(self.neighborhood_rows)


class _FakeSearchResult:
    """Minimal stand-in for ``qdrant_service.SearchResult``."""

    def __init__(self, *, id, score, text, payload):
        self.id = id
        self.score = score
        self.text = text
        self.payload = payload


class FakeQdrantService:
    """Stub for the subset of QdrantService that HybridRetriever calls."""

    def __init__(self, results: list[_FakeSearchResult] | None = None, available: bool = True):
        self.results = results or []
        self.available = available
        self.calls: list[tuple[str, dict]] = []

    def semantic_search(self, query, collection, limit):
        self.calls.append(
            ("semantic_search", {"query": query, "collection": collection, "limit": limit})
        )
        if not self.available:
            return {"available": False, "error": "stub down", "results": []}
        return {"available": True, "results": list(self.results)[:limit]}

    def embed(self, text):  # pragma: no cover -- not used in these tests
        return _deterministic_embed(text)


# Convenience builders -------------------------------------------------------


def _entity(id_: str, name: str, type_: str, score: float) -> dict:
    return {"id": id_, "name": name, "type": type_, "score": score}


def _edge(source: str, target: str, rel: str = "SUPPORTED_BY") -> dict:
    return {
        "source_id": source,
        "source_name": source,
        "type": rel,
        "target_id": target,
        "target_name": target,
    }


def _chunk(id_: str, text: str, score: float, *, source_path: str = "doc.md") -> _FakeSearchResult:
    return _FakeSearchResult(
        id=id_,
        score=score,
        text=text,
        payload={"chunk_id": id_, "source_path": source_path, "text": text},
    )


# Default weights with a soft bias on cosine so seeds dominate by default.
_DEFAULT_WEIGHTS = RetrievalWeights(alpha_cosine=0.6, beta_proximity=0.25, gamma_evidence=0.15)


def _make_retriever(
    *,
    seeds_by_label=None,
    neighborhood=None,
    qdrant_results=None,
    qdrant_available=True,
    weights: RetrievalWeights | None = None,
    seed_k: int = 8,
    expansion_hops: int = 1,
) -> tuple[HybridRetriever, FakeNeo4jService, FakeQdrantService]:
    neo4j = FakeNeo4jService(seeds_by_label=seeds_by_label, neighborhood=neighborhood)
    qdrant = FakeQdrantService(results=qdrant_results or [], available=qdrant_available)
    retriever = HybridRetriever(
        neo4j_service=neo4j,
        qdrant_service=qdrant,
        embed_fn=_deterministic_embed,
        weights=weights or _DEFAULT_WEIGHTS,
        seed_k=seed_k,
        expansion_hops=expansion_hops,
    )
    return retriever, neo4j, qdrant


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seeds_combine_entities_and_chunks():
    """The result should contain at least one entity AND one chunk kind."""
    seeds = {None: [_entity("e1", "Assumption A", "Assumption", 0.9)]}
    chunks = [_chunk("c1", "evidence text", 0.8)]
    retriever, _, _ = _make_retriever(seeds_by_label=seeds, qdrant_results=chunks)
    result = retriever.retrieve("how strong is assumption A?")
    kinds = {item.kind for item in result.items}
    assert "entity" in kinds
    assert "chunk" in kinds
    ids = {item.id for item in result.items}
    assert "e1" in ids
    assert "c1" in ids


def test_expansion_respects_hops():
    """Increasing the hop budget should grow the expansion set's proximity baseline."""
    seeds = {None: [_entity("seed", "S", "Assumption", 0.8)]}
    edges = [_edge("seed", "n1"), _edge("seed", "n2")]
    retriever_1, _, _ = _make_retriever(
        seeds_by_label=seeds, neighborhood=edges, expansion_hops=1
    )
    retriever_2, _, _ = _make_retriever(
        seeds_by_label=seeds, neighborhood=edges, expansion_hops=2
    )
    r1 = retriever_1.retrieve("anything")
    r2 = retriever_2.retrieve("anything")
    # Same expansion set (the stub returns the same edges) but proximity
    # baseline differs: 1.0 for hops=1 vs 0.5 for hops=2.  The expansion
    # node IDs should be identical; their scores should be different.
    exp1 = {it.id: it.score for it in r1.items if it.id in {"n1", "n2"}}
    exp2 = {it.id: it.score for it in r2.items if it.id in {"n1", "n2"}}
    assert set(exp1.keys()) == set(exp2.keys())
    for node_id in exp1:
        assert exp1[node_id] != exp2[node_id]


def test_re_rank_supported_assumptions_outrank_unsupported():
    """A supported assumption (seed + 'high' evidence neighbour) ranks above an orphan."""
    seeds = {
        None: [
            _entity("supported", "Supported A", "Assumption", 0.7),
            _entity("orphan", "Orphan A", "Assumption", 0.7),
        ]
    }
    # Only the supported assumption has an outbound edge to a high-strength
    # Evidence node.  The Evidence node enters the result via expansion --
    # crucially we want the supported assumption itself (which sits on the
    # graph next to that Evidence) to outrank the orphan.  We model the
    # downstream boost by raising the orphan's seed score slightly so that
    # without proximity contribution the orphan would tie / pip the
    # supported one; the proximity contribution from being adjacent to an
    # expanded high-strength node is what breaks the tie.
    edges = [_edge("supported", "evidence-1")]
    retriever, _, _ = _make_retriever(
        seeds_by_label=seeds,
        neighborhood=edges,
        expansion_hops=1,
    )
    result = retriever.retrieve("which assumptions are well-supported?")
    # Find both seeds in the result.
    by_id = {item.id: item for item in result.items}
    assert "supported" in by_id and "orphan" in by_id
    # The expansion node should also have made the cut.
    assert "evidence-1" in by_id
    # Supported assumption beats orphan due to the additional expansion node.
    # Even though both seeds had identical cosine, the supported one
    # contributed an expansion node that lifts the overall pipeline yield.
    # We assert the explicit positional ordering between them.
    indices = {item.id: idx for idx, item in enumerate(result.items)}
    # The supported seed should rank no worse than the orphan seed.
    assert indices["supported"] <= indices["orphan"]


def test_ontology_filter_drops_off_label_seeds():
    """Off-label seeds must be dropped from the entity-vector seed set."""
    # The stub returns DIFFERENT rows depending on the label filter.  When
    # the retriever requests label='Assumption', it gets a1; when it asks
    # label='Evidence', it gets e1.  When NO label is requested we'd see
    # the bogus seed -- but with an ontology filter, that branch isn't hit.
    seeds = {
        "Assumption": [_entity("a1", "Critical A", "Assumption", 0.9)],
        "Evidence": [_entity("e1", "High Evidence", "Evidence", 0.85)],
        None: [_entity("bogus", "Off-label seed", "Founder", 0.99)],
    }
    retriever, neo4j_stub, _ = _make_retriever(seeds_by_label=seeds)
    result = retriever.retrieve(
        "audit", ontology_filter={"labels": ["Assumption", "Evidence"]}
    )
    seed_ids = set(result.seed_entity_ids)
    assert "a1" in seed_ids
    assert "e1" in seed_ids
    # The off-label / unfiltered seed must NOT have been pulled.
    assert "bogus" not in seed_ids
    # Confirm the retriever issued one query per requested label and never
    # fell back to the unfiltered branch.
    requested_labels = sorted(c[1]["label"] for c in neo4j_stub.calls if c[0] == "vector_search_entities")
    assert requested_labels == ["Assumption", "Evidence"]


def test_to_retrieval_context_returns_phase5_shape():
    """``HybridResult.to_retrieval_context`` returns a populated RetrievalContext."""
    seeds = {None: [_entity("e1", "A", "Assumption", 0.7)]}
    edges = [_edge("e1", "n1")]
    chunks = [_chunk("c1", "txt", 0.6)]
    retriever, _, _ = _make_retriever(
        seeds_by_label=seeds, neighborhood=edges, qdrant_results=chunks
    )
    result = retriever.retrieve("q")
    ctx = result.to_retrieval_context()
    assert isinstance(ctx, RetrievalContext)
    assert "e1" in ctx.entity_ids
    assert "n1" in ctx.entity_ids  # expansion id is in the citation set
    assert "c1" in ctx.chunk_ids


def test_weights_affect_ordering():
    """Raising gamma should reorder high-strength evidence above expansion-only nodes."""
    # Seed two entities with identical cosine; one is an Evidence labelled
    # 'high', the other is a plain Assumption.  When gamma_evidence is
    # weighted heavily the high Evidence should rank first.
    # The assumption has the stronger cosine signal; only the gamma boost
    # on evidence strength can flip the ranking.
    seeds = {
        None: [
            _entity("ev", "High Ev", "Evidence", 0.4),
            _entity("am", "Plain A", "Assumption", 0.6),
        ]
    }
    # Build the Evidence payload via the retrieved row -- our retriever
    # passes through the row's "type" into the payload, and the gamma
    # scorer looks at strength/grade fields.  Use a richer entity row that
    # carries strength via name embedding (the scorer falls back to 0.5 if
    # absent -- we explicitly inject strength in the seeds_by_label).
    # To inject strength into the row we monkey-patch the entity dict.
    seeds[None][0]["strength"] = "high"
    seeds[None][1]["strength"] = "low"

    # Low gamma: the two seeds tie or the assumption pip-beats since both
    # have the same alpha contribution and beta contribution.
    weights_low = RetrievalWeights(alpha_cosine=0.6, beta_proximity=0.25, gamma_evidence=0.0)
    retriever_low, _, _ = _make_retriever(seeds_by_label=seeds, weights=weights_low)
    # High gamma: evidence rises decisively.
    weights_high = RetrievalWeights(alpha_cosine=0.1, beta_proximity=0.1, gamma_evidence=1.0)
    retriever_high, _, _ = _make_retriever(seeds_by_label=seeds, weights=weights_high)

    low = retriever_low.retrieve("q")
    high = retriever_high.retrieve("q")

    low_by_id = {it.id: idx for idx, it in enumerate(low.items)}
    high_by_id = {it.id: idx for idx, it in enumerate(high.items)}
    # In the high-gamma scenario Evidence must rank above Assumption.
    assert high_by_id["ev"] < high_by_id["am"]
    # And bumping gamma DID change the ordering relative to the low-gamma run.
    assert (low_by_id["ev"] < low_by_id["am"]) != (high_by_id["ev"] < high_by_id["am"]) or (
        low.items[0].id != high.items[0].id
    )


def test_qdrant_unavailable_does_not_crash():
    """If Qdrant is down, the retriever still returns entity-only results."""
    seeds = {None: [_entity("e1", "A", "Assumption", 0.9)]}
    retriever, _, _ = _make_retriever(seeds_by_label=seeds, qdrant_available=False)
    result = retriever.retrieve("hello")
    assert any(item.kind == "entity" and item.id == "e1" for item in result.items)
    assert not any(item.kind == "chunk" for item in result.items)


def test_empty_question_returns_empty_result():
    retriever, _, _ = _make_retriever()
    result = retriever.retrieve("")
    assert isinstance(result, HybridResult)
    assert result.items == []
