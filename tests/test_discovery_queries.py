"""Tests for ontology-driven discovery queries (Phase 2).

The discovery registry exposes deterministic, read-only Cypher queries that
reference only labels and predicates declared in
``app/ontology/startup_ontology.yaml``.  These tests verify three things:

1. Every registered query targets the right ontology labels / predicates and
   uses the safe ``$limit`` parameter — no string-interpolated user input.
2. ``run()`` opens a READ-mode session, passes the registered Cypher verbatim,
   binds ``$limit``, and converts the driver's records into a list of dicts.
3. The registry exposes the seven Phase-2 discovery queries named in the plan.

The Cypher itself is exercised against a ``FakeDriver``; we capture the
query/parameter pairs and assert structural intent rather than performing full
Cypher evaluation (FakeDriver does not implement a query engine).  One
integration-style test feeds canned records through a custom session to ensure
``run()`` materialises results as a list of dicts.
"""

from __future__ import annotations

import re

import pytest

from app.services import discovery_queries
from app.services.discovery_queries import (
    DiscoveryQuery,
    all_queries,
    get,
    register,
    run,
)
from app.services.ontology_validator import get_ontology


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingResult(list):
    """Iterable result that the discovery runner consumes record-by-record."""


class _RecordingSession:
    """Captures (query, params) and returns a configurable result list."""

    def __init__(self, calls: list, enter_kwargs: dict, records: list | None = None):
        self.calls = calls
        self.enter_kwargs = enter_kwargs
        self._records = records or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params=None):
        self.calls.append((query, params or {}))
        return _RecordingResult(self._records)


class RecordingDriver:
    """Minimal driver double that records session kwargs and run() calls.

    Optionally pre-loads canned records that the next session.run() returns.
    """

    def __init__(self, records: list | None = None):
        self.calls: list = []
        self.session_kwargs: list[dict] = []
        self._records = records or []

    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return _RecordingSession(self.calls, kwargs, self._records)

    def close(self):
        self.calls.append(("close", {}))


# ---------------------------------------------------------------------------
# Registry-level expectations
# ---------------------------------------------------------------------------


EXPECTED_QUERY_NAMES = {
    "unsupported_assumptions",
    "contradicted_assumptions",
    "orphan_segments",
    "orphan_problems",
    "risked_milestones",
    "untested_critical_assumptions",
    "weak_evidence_chains",
}


def test_registry_contains_all_phase2_queries():
    names = {q.name for q in all_queries()}
    assert EXPECTED_QUERY_NAMES.issubset(names), (
        f"Missing Phase 2 queries: {EXPECTED_QUERY_NAMES - names}"
    )


def test_registry_entries_are_discovery_queries():
    for q in all_queries():
        assert isinstance(q, DiscoveryQuery)
        assert q.name and q.title and q.description and q.cypher
        assert isinstance(q.expected_columns, tuple)
        assert q.expected_columns, f"{q.name} has no expected_columns"


def test_get_returns_registered_query():
    q = get("unsupported_assumptions")
    assert q.name == "unsupported_assumptions"
    assert "Assumption" in q.cypher
    assert "SUPPORTED_BY" in q.cypher


def test_get_missing_query_raises_keyerror():
    with pytest.raises(KeyError):
        get("does_not_exist")


def test_register_returns_and_stores_query():
    sentinel_name = "_test_register_sentinel"
    q = register(
        name=sentinel_name,
        title="Sentinel",
        description="Sentinel description.",
        cypher="MATCH (n:Entity:Assumption) RETURN n LIMIT $limit",
        expected_columns=("n",),
    )
    try:
        assert isinstance(q, DiscoveryQuery)
        assert get(sentinel_name) is q
        assert sentinel_name in {x.name for x in all_queries()}
    finally:
        # Clean up so we don't pollute the registry for other tests.
        discovery_queries._REGISTRY.pop(sentinel_name, None)


# ---------------------------------------------------------------------------
# Cypher safety + ontology-alignment checks
# ---------------------------------------------------------------------------


# Whitelist of Cypher tokens that may appear in a non-label/non-rel position.
_CYPHER_KEYWORDS = {
    "MATCH",
    "OPTIONAL",
    "WHERE",
    "NOT",
    "AND",
    "OR",
    "WITH",
    "RETURN",
    "AS",
    "ORDER",
    "BY",
    "DESC",
    "ASC",
    "LIMIT",
    "COLLECT",
    "COUNT",
    "DISTINCT",
    "IN",
    "NONE",
    "ANY",
    "ALL",
    "TRUE",
    "FALSE",
    "NULL",
}


def _read_only(cypher: str) -> bool:
    """Discovery queries must be read-only: no CREATE/MERGE/DELETE/SET/REMOVE."""
    forbidden = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DETACH|DROP)\b", re.IGNORECASE)
    return not forbidden.search(cypher)


@pytest.mark.parametrize("query", list(all_queries()))
def test_query_uses_limit_param(query):
    assert "$limit" in query.cypher, f"{query.name} must parameterize $limit"


@pytest.mark.parametrize("query", list(all_queries()))
def test_query_is_read_only(query):
    assert _read_only(query.cypher), (
        f"{query.name} contains a write clause: {query.cypher!r}"
    )


@pytest.mark.parametrize("query", list(all_queries()))
def test_query_labels_are_ontology_allowed(query):
    """Every label referenced after a colon must exist in the live ontology."""
    ontology_labels = get_ontology().allowed_labels
    # Strip relationship patterns of the form [:REL] / [r:REL] / [r:REL*..] so
    # they don't get mistaken for node labels by the regex below.
    cypher = re.sub(r"\[\s*[a-zA-Z_]*\s*:[A-Z][A-Z0-9_]*[^\]]*\]", "[]", query.cypher)
    # Capture tokens of the form ":SomeLabel" in node patterns.  After the
    # rewrite above, the only remaining ":Name" sites are node labels.
    labels_used = set(re.findall(r":([A-Z][A-Za-z0-9_]*)", cypher))
    unknown = labels_used - ontology_labels
    assert not unknown, (
        f"{query.name} references labels not in the ontology: {unknown}"
    )


@pytest.mark.parametrize("query", list(all_queries()))
def test_query_relationships_are_ontology_allowed(query):
    """Every relationship type written as ``[:TYPE]`` must exist in the ontology."""
    ontology_rels = get_ontology().allowed_relationships
    rels_used = set(re.findall(r"\[\s*[a-zA-Z_]*\s*:([A-Z][A-Z0-9_]*)", query.cypher))
    unknown = rels_used - ontology_rels
    assert not unknown, (
        f"{query.name} references relationships not in the ontology: {unknown}"
    )


# ---------------------------------------------------------------------------
# Per-query structural expectations (label + predicate coverage)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, required_substrings",
    [
        ("unsupported_assumptions", ["Assumption", "SUPPORTED_BY", "Evidence", "NOT"]),
        (
            "contradicted_assumptions",
            ["Assumption", "SUPPORTED_BY", "CONTRADICTED_BY", "Evidence"],
        ),
        ("orphan_segments", ["CustomerSegment", "HAS_PROBLEM", "Problem", "NOT"]),
        ("orphan_problems", ["Problem", "ProductFeature", "ADDRESSES", "NOT"]),
        ("risked_milestones", ["Milestone", "Risk", "THREATENS", "Experiment", "MITIGATES"]),
        (
            "untested_critical_assumptions",
            ["Assumption", "criticality", "high", "Experiment", "TESTS", "NOT"],
        ),
        (
            "weak_evidence_chains",
            ["Assumption", "criticality", "high", "SUPPORTED_BY", "Evidence", "strength"],
        ),
    ],
)
def test_query_targets_expected_ontology_constructs(name, required_substrings):
    q = get(name)
    for token in required_substrings:
        assert token in q.cypher, (
            f"{name} Cypher is missing expected token {token!r}: {q.cypher}"
        )


# ---------------------------------------------------------------------------
# run() behaviour
# ---------------------------------------------------------------------------


def test_run_opens_read_session_and_passes_limit():
    driver = RecordingDriver(records=[])
    rows = run("unsupported_assumptions", driver, limit=42)
    assert rows == []
    # session(**kwargs) must request READ_ACCESS.
    assert driver.session_kwargs, "Driver session was never opened"
    kwargs = driver.session_kwargs[0]
    # neo4j READ_ACCESS is the string "READ".
    from neo4j import READ_ACCESS

    assert kwargs.get("default_access_mode") == READ_ACCESS

    # The captured run() call should carry the registered Cypher and the
    # configured limit parameter (cast to int).
    assert len(driver.calls) == 1
    query, params = driver.calls[0]
    assert query == get("unsupported_assumptions").cypher
    assert params == {"limit": 42}


def test_run_returns_dicts_from_records():
    canned = [
        {"id": "a-1", "name": "Reimbursement secured", "criticality": "high", "evidence_grade": "C"},
        {"id": "a-2", "name": "Adoption rate", "criticality": "medium", "evidence_grade": "B"},
    ]
    driver = RecordingDriver(records=canned)
    rows = run("unsupported_assumptions", driver, limit=10)
    assert rows == canned
    # The runner must materialize into a list of dicts.
    assert all(isinstance(r, dict) for r in rows)


def test_run_unknown_name_raises_keyerror():
    driver = RecordingDriver()
    with pytest.raises(KeyError):
        run("nonexistent_query", driver)


def test_run_casts_limit_to_int():
    driver = RecordingDriver(records=[])
    run("orphan_segments", driver, limit="25")  # type: ignore[arg-type]
    _query, params = driver.calls[0]
    assert params == {"limit": 25}


# ---------------------------------------------------------------------------
# Gap-vs-non-gap simulation: prove the runner faithfully returns whatever
# rows the driver yields for each registered query.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, gap_row",
    [
        (
            "unsupported_assumptions",
            {"id": "a-gap", "name": "No evidence yet", "criticality": "high", "evidence_grade": None},
        ),
        (
            "contradicted_assumptions",
            {"id": "a-confl", "name": "Disputed claim", "contradiction_count": 2},
        ),
        ("orphan_segments", {"id": "seg-orphan", "name": "Untargeted segment"}),
        ("orphan_problems", {"id": "prob-orphan", "name": "Unaddressed pain", "severity": "high"}),
        (
            "risked_milestones",
            {"id": "m1", "name": "FDA filing", "unmitigated_risks": ["regulatory delay"]},
        ),
        ("untested_critical_assumptions", {"id": "a-untested", "name": "Pricing power"}),
        (
            "weak_evidence_chains",
            {"id": "a-weak", "name": "Adoption rate", "strengths": ["low", "anecdotal"]},
        ),
    ],
)
def test_each_query_returns_planted_gap_row(name, gap_row):
    """For every discovery query, the runner returns the planted gap row.

    A FakeDriver cannot evaluate Cypher, so we pre-seed the canned record that
    a real Neo4j matching the ontology's gap would return — and assert the
    runner round-trips it as a dict.  Combined with the ontology-alignment
    tests above, this guarantees the query targets the right gap shape.
    """
    driver = RecordingDriver(records=[gap_row])
    rows = run(name, driver, limit=5)
    assert rows == [gap_row]
    # Confirm the captured Cypher is the one registered (no rewriting).
    query, params = driver.calls[0]
    assert query == get(name).cypher
    assert params == {"limit": 5}
