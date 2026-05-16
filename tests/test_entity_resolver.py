"""Tests for the Phase 6 entity resolver.

The resolver groups validated entities of the same type, computes cosine +
Jaccard similarity, asks an LLM to confirm each candidate pair, and proposes
reversible SAME_AS merges.  These tests use the FakeDriver + FakeLLM stubs
from conftest plus a deterministic, hash-based embed function so the cosine
arithmetic is reproducible without a real embedding model.

We exercise six guarantees:

  1. Near-identical names cluster into a proposal with cosine >= 0.92.
  2. Pairs across different ontology ``type`` values are never proposed.
  3. An ``uncertain`` LLM verdict lowers the combined score relative to ``yes``.
  4. ``approve(proposal)`` emits the expected SAME_AS MERGE Cypher.
  5. ``consolidate(canonical_id)`` invokes ``apoc.refactor.mergeNodes``.
  6. A pair below the cosine threshold yields zero proposals.
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest

from app.services.entity_resolver import (
    EntityResolver,
    MergeProposal,
)
from app.services.neo4j_service import Neo4jService


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """LLM stub that pops verdicts in order; defaults to ``yes`` when empty.

    The resolver issues one LLM call per surviving (cosine + Jaccard) pair so
    each test can pre-program the verdicts deterministically.
    """

    def __init__(self, verdicts: list[dict[str, str]] | None = None):
        self.verdicts = list(verdicts or [])
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict[str, str]:
        self.prompts.append(prompt)
        if self.verdicts:
            return self.verdicts.pop(0)
        return {"verdict": "yes", "rationale": "default fixture verdict"}


class _StubNeo4j:
    """Neo4jService-shaped stub that returns pre-loaded entity rows.

    The resolver only calls ``get_all_entities`` / ``write_same_as`` /
    ``consolidate`` so we mirror exactly that surface.  ``write_same_as``
    delegates to a real Neo4jService backed by the FakeDriver so we can
    capture the produced Cypher.
    """

    def __init__(self, rows: list[dict[str, Any]], real_service: Neo4jService):
        self._rows = list(rows)
        self.real = real_service
        self.consolidated: list[str] = []

    def get_all_entities(self, limit: int = 1000) -> list[dict[str, Any]]:
        return list(self._rows)

    def write_same_as(
        self,
        canonical_id: str,
        duplicate_id: str,
        confidence: float = 0.0,
    ) -> None:
        self.real.write_same_as(canonical_id, duplicate_id, confidence=confidence)

    def consolidate(self, canonical_id: str) -> None:
        self.consolidated.append(canonical_id)
        self.real.consolidate(canonical_id)


def _make_neo4j_service(driver) -> Neo4jService:
    """Neo4jService with a whitelist wide enough for the resolver tests."""
    return Neo4jService(
        driver=driver,
        allowed_labels={"Entity", "Assumption", "Founder", "Risk"},
        allowed_relationships={"SAME_AS", "SUPERSEDED_BY"},
    )


# ---------------------------------------------------------------------------
# Deterministic embed function
#
# Hash-based 32-d vectors give us reproducible cosine values without any
# external embedding model.  Identical names get identical vectors (cosine
# 1.0); near-identical names share most token-derived components so cosine
# stays well above the 0.92 threshold; unrelated names hash into disjoint
# component subsets so cosine collapses toward 0.
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def hashed_embed(name: str, dims: int = 32) -> list[float]:
    """Hash each lower-cased token into a fixed slot.

    Each surviving token contributes a unit weight in its hash-mapped slot.
    The vector is then L2-normalised so cosine reduces to the fraction of
    shared slots -- which mirrors how a real bag-of-words embedding behaves
    on short names and is enough to drive the resolver's thresholds.
    """
    vec = [0.0] * dims
    tokens = [t.lower() for t in _TOKEN_RE.findall(name or "")]
    if not tokens:
        return vec
    for tok in tokens:
        # ``abs(hash(...))`` is process-stable thanks to PYTHONHASHSEED=0 in
        # the generator script; tests don't depend on that, they just need
        # the same string to produce the same vector inside one process.
        slot = abs(hash(tok)) % dims
        vec[slot] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_driver():
    from tests.conftest import FakeDriver

    return FakeDriver()


@pytest.fixture()
def neo4j_service(fake_driver):
    return _make_neo4j_service(fake_driver)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_propose_merges_clusters_duplicates(neo4j_service):
    """Two assumptions with near-identical names should yield one proposal."""
    rows = [
        {
            "id": "a-1",
            "name": "Customers will pay for premium tier",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "moderate",
        },
        {
            "id": "a-2",
            "name": "Customers will pay for premium tier",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "weak",
        },
    ]
    stub = _StubNeo4j(rows, neo4j_service)
    llm = _ScriptedLLM([{"verdict": "yes", "rationale": "Identical claim."}])
    resolver = EntityResolver(stub, llm, hashed_embed)

    proposals = resolver.propose_merges()

    assert len(proposals) == 1
    p = proposals[0]
    assert {p.canonical_id, p.duplicate_id} == {"a-1", "a-2"}
    assert p.entity_type == "Assumption"
    assert p.cosine_similarity >= 0.92
    assert p.name_jaccard >= 0.5
    assert p.llm_verdict == "yes"
    # Higher reviewer_confidence wins canonical: "moderate" > "weak" → a-1.
    assert p.canonical_id == "a-1"
    assert p.duplicate_id == "a-2"


def test_propose_merges_skips_different_types(neo4j_service):
    """Same name, different ontology type must produce zero proposals."""
    rows = [
        {
            "id": "f-1",
            "name": "Acme Co",
            "type": "Founder",
            "validation_status": "validated",
        },
        {
            "id": "r-1",
            "name": "Acme Co",
            "type": "Risk",
            "validation_status": "validated",
        },
    ]
    stub = _StubNeo4j(rows, neo4j_service)
    llm = _ScriptedLLM([])  # Should never be invoked.
    resolver = EntityResolver(stub, llm, hashed_embed)

    proposals = resolver.propose_merges()

    assert proposals == []
    assert llm.prompts == [], "LLM must not be consulted across types"


def test_llm_uncertain_lowers_score(neo4j_service):
    """An ``uncertain`` verdict should produce a proposal whose score is
    strictly below the same proposal's ``yes``-baseline."""
    rows = [
        {
            "id": "a-1",
            "name": "Hospitals adopt the platform",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "moderate",
        },
        {
            "id": "a-2",
            "name": "Hospitals adopt the platform",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "moderate",
        },
    ]
    # Baseline run with "yes".
    yes_resolver = EntityResolver(
        _StubNeo4j(rows, neo4j_service),
        _ScriptedLLM([{"verdict": "yes", "rationale": "."}]),
        hashed_embed,
    )
    baseline = yes_resolver.propose_merges()[0]

    # Same inputs, "uncertain" verdict.
    fresh_service = _make_neo4j_service(neo4j_service.driver.__class__())
    uncertain_resolver = EntityResolver(
        _StubNeo4j(rows, fresh_service),
        _ScriptedLLM([{"verdict": "uncertain", "rationale": "Could be sibling."}]),
        hashed_embed,
    )
    uncertain = uncertain_resolver.propose_merges()[0]

    assert uncertain.llm_verdict == "uncertain"
    assert uncertain.score < baseline.score, (
        f"uncertain score {uncertain.score} should be below yes score {baseline.score}"
    )


def test_approve_writes_same_as_edge(neo4j_service):
    """approve(proposal) should produce a MERGE on `:SAME_AS` with the
    canonical/duplicate ids bound as parameters."""
    rows = [
        {
            "id": "a-1",
            "name": "We can charge premium",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "moderate",
        },
        {
            "id": "a-2",
            "name": "We can charge premium",
            "type": "Assumption",
            "validation_status": "validated",
            "reviewer_confidence": "weak",
        },
    ]
    stub = _StubNeo4j(rows, neo4j_service)
    llm = _ScriptedLLM([{"verdict": "yes", "rationale": "Same."}])
    resolver = EntityResolver(stub, llm, hashed_embed)

    proposals = resolver.propose_merges()
    assert proposals
    resolver.approve(proposals[0])

    queries = [
        q for q, _ in neo4j_service.driver.calls
        if isinstance(q, str) and "SAME_AS" in q
    ]
    params = [
        p for _, p in neo4j_service.driver.calls
        if isinstance(p, dict) and "canonical" in p
    ]
    assert any("MERGE (a)-[r:`SAME_AS`]->(b)" in q for q in queries), (
        f"Expected backtick-quoted SAME_AS MERGE, got: {queries}"
    )
    assert any("ON CREATE SET r.created_at = datetime()" in q for q in queries)
    assert any(
        p.get("canonical") == "a-1" and p.get("duplicate") == "a-2"
        for p in params
    )


def test_consolidate_invokes_apoc_merge_nodes(neo4j_service):
    """consolidate() must surface apoc.refactor.mergeNodes in the captured query."""
    stub = _StubNeo4j([], neo4j_service)
    resolver = EntityResolver(stub, _ScriptedLLM([]), hashed_embed)

    resolver.consolidate("canonical-1")

    queries = [
        q for q, _ in neo4j_service.driver.calls
        if isinstance(q, str)
    ]
    assert any("apoc.refactor.mergeNodes" in q for q in queries), (
        f"Expected apoc.refactor.mergeNodes in queries, got: {queries}"
    )
    # The cluster collection should use a variable-length SAME_AS traversal.
    assert any("[:SAME_AS*]" in q for q in queries)


def test_resolver_respects_thresholds(neo4j_service):
    """Pairs below the cosine threshold must not produce a proposal."""
    rows = [
        {
            "id": "a-1",
            "name": "Customers adopt premium tier",
            "type": "Assumption",
            "validation_status": "validated",
        },
        {
            "id": "a-2",
            "name": "Regulators approve clinical trial",
            "type": "Assumption",
            "validation_status": "validated",
        },
    ]
    stub = _StubNeo4j(rows, neo4j_service)
    llm = _ScriptedLLM([])  # Should never be consulted.
    resolver = EntityResolver(stub, llm, hashed_embed)

    proposals = resolver.propose_merges()
    assert proposals == []
    assert llm.prompts == [], "LLM must not be consulted when cosine is below threshold"


def test_write_same_as_rejects_self_merge(neo4j_service):
    """The Neo4j helper must refuse to write a self-loop."""
    from app.services.neo4j_service import Neo4jServiceError

    with pytest.raises(Neo4jServiceError):
        neo4j_service.write_same_as("a-1", "a-1")
    with pytest.raises(Neo4jServiceError):
        neo4j_service.write_same_as("", "a-1")


def test_merge_proposal_is_frozen():
    """MergeProposal is a frozen dataclass — accidental mutation must raise."""
    p = MergeProposal(
        canonical_id="a-1",
        duplicate_id="a-2",
        canonical_name="X",
        duplicate_name="X",
        entity_type="Assumption",
        cosine_similarity=1.0,
        name_jaccard=1.0,
        llm_verdict="yes",
        llm_rationale="",
        score=1.0,
    )
    with pytest.raises(Exception):
        p.canonical_id = "other"  # type: ignore[misc]
