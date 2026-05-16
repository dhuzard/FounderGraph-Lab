"""Tests for the schema-aware text-to-Cypher planner (Phase 3).

Each test seeds a stubbed LLM with one or two canned JSON plans and runs
them through :class:`CypherPlanner`.  We assert that:

- Golden plans pass validation, get a ``LIMIT $max_rows`` injection when
  missing, and execute against a recording driver that returns canned
  rows.
- Adversarial plans (writes, off-ontology references, domain/range
  mismatches, injection probes) are rejected with the expected violation
  ``kind``.
- The single repair attempt fires when the first plan is invalid, and
  the planner reports ``repair_attempted=True`` regardless of whether
  the second plan succeeds.

The driver is a minimal fake that returns whatever rows the test seeds;
the planner does no Cypher evaluation itself, so the rows are simply
round-tripped through ``execute``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.cypher_planner import (
    CypherPlan,
    CypherPlanner,
    CypherViolation,
    PlanResult,
    _extract_labels,
    _extract_relationships,
    _extract_triples,
    _strip_quoted,
    render_ontology_view,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubLLM:
    """Pops queued responses in order.  Each entry may be:

    - a dict   → returned as-is from ``generate_json``.
    - a string → returned as a JSON string (planner will ``json.loads`` it).
    - an Exception → raised.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def generate_json(self, prompt: str) -> Any:
        self.calls.append(prompt)
        if not self.responses:
            raise RuntimeError("StubLLM ran out of canned responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeResult(list):
    """Iterable result that exposes ``data()`` per record like the neo4j driver."""

    def __iter__(self):
        for row in list.__iter__(self):
            yield _FakeRecord(row)


class _FakeRecord:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> dict[str, Any]:
        return dict(self._payload)

    def __iter__(self):
        return iter(self._payload.items())

    def keys(self):
        return list(self._payload.keys())

    def __getitem__(self, key):
        return self._payload[key]


class _FakeSession:
    def __init__(self, parent: "_RecordingDriver", kwargs: dict[str, Any]) -> None:
        self.parent = parent
        self.kwargs = kwargs

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def run(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any) -> _FakeResult:
        self.parent.calls.append({
            "query": query,
            "params": dict(params or {}),
            "session_kwargs": self.kwargs,
            "run_kwargs": kwargs,
        })
        return _FakeResult(self.parent.rows_to_return)


class _RecordingDriver:
    """Records every session / run call and returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows_to_return: list[dict[str, Any]] = list(rows or [])
        self.calls: list[dict[str, Any]] = []
        self.session_kwargs: list[dict[str, Any]] = []

    def session(self, **kwargs: Any) -> _FakeSession:
        self.session_kwargs.append(kwargs)
        return _FakeSession(self, kwargs)

    def close(self) -> None:  # pragma: no cover — never called by planner
        pass


class _FakeNeo4jService:
    """Wraps a :class:`_RecordingDriver` so the planner sees a ``driver`` attr."""

    def __init__(self, driver: _RecordingDriver) -> None:
        self.driver = driver


@pytest.fixture()
def driver() -> _RecordingDriver:
    return _RecordingDriver()


@pytest.fixture()
def neo4j_service(driver: _RecordingDriver) -> _FakeNeo4jService:
    return _FakeNeo4jService(driver)


def _make_planner(
    neo4j_service: _FakeNeo4jService,
    responses: list[Any],
    *,
    max_rows: int = 200,
    timeout: int = 0,
) -> tuple[CypherPlanner, StubLLM]:
    """Convenience: build a planner with a StubLLM seeded with ``responses``.

    ``timeout=0`` (the default) skips the worker-thread cutoff so tests
    that don't need to exercise timeout behaviour stay fast.
    """
    llm = StubLLM(responses)
    planner = CypherPlanner(
        neo4j_service=neo4j_service,
        llm_service=llm,
        query_timeout_seconds=timeout,
        max_rows=max_rows,
    )
    return planner, llm


# ---------------------------------------------------------------------------
# Helper-level sanity checks
# ---------------------------------------------------------------------------


def test_strip_quoted_masks_strings_and_comments() -> None:
    cypher = """
    MATCH (a:Assumption) // pick assumptions
    WHERE a.name = "DELETE THIS" AND a.id <> 'DROP'
    RETURN a
    """
    stripped = _strip_quoted(cypher)
    assert "DELETE" not in stripped
    assert "DROP" not in stripped
    assert "Assumption" in stripped
    # The line-comment text "pick assumptions" should be masked too.
    assert "pick assumptions" not in stripped


def test_extract_labels_and_relationships() -> None:
    cypher = "MATCH (a:Entity:Assumption)-[:SUPPORTED_BY]->(e:Entity:Evidence) RETURN a, e"
    stripped = _strip_quoted(cypher)
    assert set(_extract_labels(stripped)) >= {"Entity", "Assumption", "Evidence"}
    assert "SUPPORTED_BY" in _extract_relationships(stripped)


def test_extract_triples_handles_chained_pattern() -> None:
    cypher = (
        "MATCH (s:CustomerSegment)-[:HAS_PROBLEM]->(p:Problem)<-[:ADDRESSES]-(f:ProductFeature) "
        "RETURN s, p, f"
    )
    triples = _extract_triples(_strip_quoted(cypher))
    assert any(
        t[0] == "CustomerSegment" and t[1] == "HAS_PROBLEM" and t[2] == "Problem"
        for t in triples
    )
    # The right side ``<-[:ADDRESSES]-`` reverses, so the triple should
    # be (ProductFeature, ADDRESSES, Problem).
    assert any(
        t[0] == "ProductFeature" and t[1] == "ADDRESSES" and t[2] == "Problem"
        for t in triples
    )


def test_render_ontology_view_lists_core_concepts() -> None:
    view = render_ontology_view()
    assert "Assumption" in view
    assert "SUPPORTED_BY" in view
    assert "HAS_PROBLEM" in view


# ---------------------------------------------------------------------------
# Golden tests
# ---------------------------------------------------------------------------


def test_plan_unsupported_assumptions(neo4j_service, driver) -> None:
    """Round-trip a valid plan: validate passes, execute returns canned rows."""
    plan_json = {
        "cypher": (
            "MATCH (a:Entity:Assumption) "
            "WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence) "
            "RETURN a.id AS id, a.name AS name LIMIT $max_rows"
        ),
        "params": {},
        "rationale": "Unsupported assumptions have no outgoing SUPPORTED_BY edges.",
    }
    driver.rows_to_return = [
        {"id": "asm-1", "name": "Reimbursement secured"},
        {"id": "asm-2", "name": "Buyers convert"},
    ]
    planner, llm = _make_planner(neo4j_service, [plan_json])

    result = planner.ask("Which assumptions have no supporting evidence?")

    assert result.plan is not None
    assert result.violations == []
    assert result.repair_attempted is False
    assert result.rows == driver.rows_to_return
    # LLM was only called once (no repair needed).
    assert len(llm.calls) == 1
    # The driver call carried the auto-injected $max_rows.
    assert driver.calls[0]["params"]["max_rows"] == 200
    # READ_ACCESS was requested.
    assert driver.session_kwargs[0].get("default_access_mode") in {"READ", "r"}


def test_plan_two_hop_segment_to_feature(neo4j_service, driver) -> None:
    plan_json = {
        "cypher": (
            "MATCH (s:Entity:CustomerSegment)-[:HAS_PROBLEM]->(p:Entity:Problem)"
            "<-[:ADDRESSES]-(f:Entity:ProductFeature) "
            "RETURN s.name AS segment, p.name AS problem, collect(f.name) AS features "
            "LIMIT $max_rows"
        ),
        "params": {},
        "rationale": "Walks segment-HAS_PROBLEM->problem and problem<-ADDRESSES-feature.",
    }
    driver.rows_to_return = [
        {"segment": "Hospital admins", "problem": "Slow intake", "features": ["Auto-intake"]},
    ]
    planner, _ = _make_planner(neo4j_service, [plan_json])

    result = planner.ask("Show segments and the features that address their problems.")

    assert result.violations == []
    assert result.plan is not None
    assert "HAS_PROBLEM" in result.plan.cypher
    assert "ADDRESSES" in result.plan.cypher
    assert result.rows == driver.rows_to_return


def test_plan_inserts_limit_when_missing(neo4j_service, driver) -> None:
    plan_json = {
        "cypher": (
            "MATCH (a:Entity:Assumption) "
            "WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence) "
            "RETURN a.id AS id"
        ),
        "params": {},
        "rationale": "no limit on purpose",
    }
    planner, _ = _make_planner(neo4j_service, [plan_json], max_rows=42)

    result = planner.ask("Find unsupported assumptions.")

    assert result.plan is not None
    assert "LIMIT $max_rows" in result.plan.cypher
    # And the driver received the bound value.
    assert driver.calls[0]["params"]["max_rows"] == 42


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


def _violation_kinds(result: PlanResult) -> set[str]:
    return {v.kind for v in result.violations}


def test_rejects_delete(neo4j_service) -> None:
    bad = {
        "cypher": "MATCH (a:Entity:Assumption) DELETE a",
        "params": {},
        "rationale": "wipe data",
    }
    # Repair attempt yields the same garbage so the planner ultimately
    # returns plan=None with repair_attempted=True.
    planner, _ = _make_planner(neo4j_service, [bad, bad])

    result = planner.ask("delete all assumptions please")
    assert result.plan is None
    assert result.repair_attempted is True
    assert "forbidden_clause" in _violation_kinds(result)


def test_rejects_create(neo4j_service) -> None:
    bad = {
        "cypher": "CREATE (a:Entity:Assumption {id: 'x'}) RETURN a",
        "params": {},
        "rationale": "write",
    }
    planner, _ = _make_planner(neo4j_service, [bad, bad])
    result = planner.ask("create an assumption")
    assert result.plan is None
    assert "forbidden_clause" in _violation_kinds(result)


def test_rejects_off_ontology_label(neo4j_service) -> None:
    bad = {
        "cypher": "MATCH (h:Hacker) RETURN h LIMIT $max_rows",
        "params": {},
        "rationale": "not in ontology",
    }
    planner, _ = _make_planner(neo4j_service, [bad, bad])
    result = planner.ask("find hackers")
    assert result.plan is None
    assert "off_ontology_label" in _violation_kinds(result)


def test_rejects_off_ontology_relationship(neo4j_service) -> None:
    bad = {
        "cypher": "MATCH (a:Entity:Assumption)-[:PWNS]->(e:Entity:Evidence) RETURN a, e LIMIT $max_rows",
        "params": {},
        "rationale": "not in ontology",
    }
    planner, _ = _make_planner(neo4j_service, [bad, bad])
    result = planner.ask("find pwns edges")
    assert result.plan is None
    assert "off_ontology_relationship" in _violation_kinds(result)


def test_rejects_domain_range_violation(neo4j_service) -> None:
    """HAS_PROBLEM is CustomerSegment -> Problem; Startup -> Risk must fail."""
    bad = {
        "cypher": "MATCH (s:Entity:Startup)-[:HAS_PROBLEM]->(r:Entity:Risk) RETURN s, r LIMIT $max_rows",
        "params": {},
        "rationale": "wrong domain/range",
    }
    planner, _ = _make_planner(neo4j_service, [bad, bad])
    result = planner.ask("startups with problems")
    assert result.plan is None
    assert "domain_range" in _violation_kinds(result)


def test_rejects_injection_attempt(neo4j_service) -> None:
    bad = {
        "cypher": "MATCH (a:Entity:Assumption) RETURN a;\nDROP DATABASE neo4j",
        "params": {},
        "rationale": "ha",
    }
    planner, _ = _make_planner(neo4j_service, [bad, bad])
    result = planner.ask("anything")
    assert result.plan is None
    # Either the injection probe or the forbidden DROP keyword fires.
    kinds = _violation_kinds(result)
    assert "injection" in kinds or "forbidden_clause" in kinds


def test_repair_succeeds(neo4j_service, driver) -> None:
    first_bad = {
        "cypher": "MATCH (h:Hacker) RETURN h LIMIT $max_rows",
        "params": {},
        "rationale": "off ontology",
    }
    repair_good = {
        "cypher": (
            "MATCH (a:Entity:Assumption) "
            "WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence) "
            "RETURN a.id AS id LIMIT $max_rows"
        ),
        "params": {},
        "rationale": "corrected",
    }
    driver.rows_to_return = [{"id": "asm-1"}]
    planner, llm = _make_planner(neo4j_service, [first_bad, repair_good])

    result = planner.ask("Which assumptions have no supporting evidence?")

    assert result.plan is not None
    assert result.repair_attempted is True
    assert result.violations == []
    assert result.rows == [{"id": "asm-1"}]
    # The repair prompt should reference the violation kind.
    assert len(llm.calls) == 2
    assert "Repair note" in llm.calls[1]


def test_repair_fails_double(neo4j_service) -> None:
    first = {
        "cypher": "MATCH (a:Entity:Assumption) DELETE a",
        "params": {},
        "rationale": "bad",
    }
    second = {
        "cypher": "MATCH (h:Hacker) RETURN h LIMIT $max_rows",
        "params": {},
        "rationale": "still bad",
    }
    planner, llm = _make_planner(neo4j_service, [first, second])

    result = planner.ask("hack the graph")

    assert result.plan is None
    assert result.repair_attempted is True
    # The final violations come from the second plan (off-ontology label),
    # not the first (forbidden clause).
    assert "off_ontology_label" in _violation_kinds(result)
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# Misc — coerce_plan + parse failures
# ---------------------------------------------------------------------------


def test_plan_none_on_parse_failure(neo4j_service) -> None:
    """When the LLM returns garbage twice the planner reports parse_error."""
    planner, _ = _make_planner(
        neo4j_service,
        # First response is the string ``not json`` (the planner tries
        # ``json.loads`` and fails); second one is also unparseable.
        [{"not": "a plan"}, "still not a plan"],
    )

    result = planner.ask("?")
    assert result.plan is None
    # Either the first plan triggered a parse_error directly, or the
    # repair attempt did.
    assert any(v.kind in {"parse_error", "forbidden_clause", "off_ontology_label"} for v in result.violations)


def test_coerce_plan_handles_json_string() -> None:
    plan = CypherPlanner._coerce_plan(
        json.dumps(
            {
                "cypher": "MATCH (a:Entity:Assumption) RETURN a LIMIT $max_rows",
                "params": {},
                "rationale": "ok",
            }
        )
    )
    assert plan is not None
    assert plan.cypher.startswith("MATCH")
    assert "Assumption" in plan.referenced_labels


def test_coerce_plan_rejects_missing_cypher() -> None:
    assert CypherPlanner._coerce_plan({"params": {}, "rationale": "no cypher"}) is None
    assert CypherPlanner._coerce_plan(None) is None
    assert CypherPlanner._coerce_plan(123) is None
