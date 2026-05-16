"""Ontology-driven discovery Cypher queries.

Phase 2 of the GraphRAG upgrade: deterministic, read-only Cypher queries that
surface ontology-grounded gaps (unsupported assumptions, orphan segments,
untested critical assumptions, etc.) without invoking an LLM.

Each registered :class:`DiscoveryQuery` references only labels and predicates
present in ``app/ontology/startup_ontology.yaml``.  The registry is built at
import time via :func:`register`; callers iterate :func:`all_queries` to render
discovery tiles in the Streamlit UI and execute a query by name with
:func:`run`.

The single-parameter (``$limit``) calling convention keeps the queries safe to
expose verbatim — there is no string interpolation of user input into the
Cypher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from neo4j import READ_ACCESS


_FIRST_MATCH_RE = re.compile(r"\bMATCH\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:", re.IGNORECASE)


def _inject_temporal_filter(cypher: str, alias: str) -> str:
    """Append a bi-temporal predicate referencing the first MATCH alias.

    The discovery queries all start with ``MATCH (alias:Entity:...)``; we
    extend whatever WHERE clause follows with a clause that restricts the
    alias to nodes valid at ``$valid_at``.  Untouched if no MATCH is found.
    """
    temporal = (
        f"(coalesce({alias}.valid_from, datetime('1970-01-01')) <= datetime($valid_at) "
        f"AND ({alias}.valid_to IS NULL OR datetime($valid_at) < {alias}.valid_to))"
    )
    # If a WHERE already exists for the first MATCH, AND it in; else inject one.
    if re.search(r"\bWHERE\b", cypher, re.IGNORECASE):
        return re.sub(
            r"\bWHERE\b",
            f"WHERE {temporal} AND ",
            cypher,
            count=1,
            flags=re.IGNORECASE,
        )
    # Inject a WHERE right after the first node pattern's closing parenthesis.
    return re.sub(
        r"(\bMATCH\s*\([^)]+\))",
        rf"\1 WHERE {temporal} ",
        cypher,
        count=1,
        flags=re.IGNORECASE,
    )


@dataclass(frozen=True)
class DiscoveryQuery:
    """A named, read-only Cypher query targeting an ontology-defined gap."""

    name: str
    title: str
    description: str
    cypher: str
    expected_columns: tuple[str, ...]


_REGISTRY: dict[str, DiscoveryQuery] = {}


def register(
    name: str,
    title: str,
    description: str,
    cypher: str,
    expected_columns: tuple[str, ...],
) -> DiscoveryQuery:
    """Register a discovery query and return the resulting DiscoveryQuery."""
    q = DiscoveryQuery(
        name=name,
        title=title,
        description=description,
        cypher=cypher,
        expected_columns=expected_columns,
    )
    _REGISTRY[name] = q
    return q


def all_queries() -> Iterable[DiscoveryQuery]:
    """Return every registered DiscoveryQuery in insertion order."""
    return _REGISTRY.values()


def get(name: str) -> DiscoveryQuery:
    """Return the DiscoveryQuery registered under ``name``.

    Raises KeyError if no such query exists.
    """
    return _REGISTRY[name]


def run(
    name: str,
    driver: Any,
    limit: int = 100,
    valid_at: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a registered discovery query against ``driver`` and return rows.

    The driver may be a real neo4j ``Driver`` or any object exposing the same
    ``session(default_access_mode=...) -> ContextManager`` shape.  Queries run
    in READ access mode and pass ``$limit`` as the sole parameter.

    When ``valid_at`` is supplied (ISO-8601 string), the first MATCH clause is
    rewritten to add a bi-temporal predicate restricting the lead alias to
    nodes valid at that timestamp.  ``$valid_at`` is bound as a parameter.
    """
    q = _REGISTRY[name]
    cypher = q.cypher
    params: dict[str, Any] = {"limit": int(limit)}
    if valid_at is not None:
        first = _FIRST_MATCH_RE.search(cypher)
        if first is not None:
            cypher = _inject_temporal_filter(cypher, first.group(1))
            params["valid_at"] = str(valid_at)
    with driver.session(default_access_mode=READ_ACCESS) as session:
        result = session.run(cypher, params)
        rows: list[dict[str, Any]] = []
        for record in result:
            # neo4j Record supports dict(record); fall back to .data() if present.
            try:
                rows.append(dict(record))
            except (TypeError, ValueError):
                data_fn = getattr(record, "data", None)
                rows.append(data_fn() if callable(data_fn) else dict(record))
        return rows


# ---------------------------------------------------------------------------
# Registered queries.  Each Cypher block references only labels and predicates
# from app/ontology/startup_ontology.yaml.
# ---------------------------------------------------------------------------

register(
    name="unsupported_assumptions",
    title="Unsupported assumptions",
    description="Critical assumptions with zero supporting evidence.",
    cypher=(
        "MATCH (a:Entity:Assumption) "
        "WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence) "
        "RETURN a.id AS id, a.name AS name, a.criticality AS criticality, "
        "a.evidence_grade AS evidence_grade "
        "ORDER BY a.criticality DESC "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name", "criticality", "evidence_grade"),
)

register(
    name="contradicted_assumptions",
    title="Contradicted assumptions",
    description="Assumptions with both supporting and contradicting evidence (decision conflicts).",
    cypher=(
        "MATCH (a:Entity:Assumption)-[:SUPPORTED_BY]->(:Entity:Evidence) "
        "WITH a "
        "MATCH (a)-[:CONTRADICTED_BY]->(c:Entity:Evidence) "
        "RETURN a.id AS id, a.name AS name, count(DISTINCT c) AS contradiction_count "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name", "contradiction_count"),
)

register(
    name="orphan_segments",
    title="Orphan customer segments",
    description="Customer segments with no linked problem.",
    cypher=(
        "MATCH (s:Entity:CustomerSegment) "
        "WHERE NOT (s)-[:HAS_PROBLEM]->(:Entity:Problem) "
        "RETURN s.id AS id, s.name AS name "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name"),
)

register(
    name="orphan_problems",
    title="Orphan problems",
    description="Problems with no addressing product feature.",
    cypher=(
        "MATCH (p:Entity:Problem) "
        "WHERE NOT (:Entity:ProductFeature)-[:ADDRESSES]->(p) "
        "RETURN p.id AS id, p.name AS name, p.severity AS severity "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name", "severity"),
)

# NOTE: The plan calls for a MITIGATES predicate (Experiment -[:MITIGATES]-> Risk),
# but startup_ontology.yaml does not define such a relation.  The closest
# available outgoing Experiment predicate is TESTS (Experiment -[:TESTS]-> Assumption).
# We reuse TESTS here with Risk as the target — the ontology's domain/range gate
# does not constrain pure read queries, so the Cypher executes safely against any
# Experiment-TESTS-Risk edge a user may have validated, and naturally returns
# zero "mitigated" risks when no such edges exist (i.e. every risked milestone
# remains unmitigated).  Revisit once a proper MITIGATES predicate lands in the
# ontology YAML.
register(
    name="risked_milestones",
    title="Risked milestones",
    description="Milestones threatened by a risk with no mitigating experiment.",
    cypher=(
        "MATCH (m:Entity:Milestone)<-[:THREATENS]-(r:Entity:Risk) "
        "WHERE NOT (:Entity:Experiment)-[:TESTS]->(r) "
        "RETURN m.id AS id, m.name AS name, collect(r.name) AS unmitigated_risks "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name", "unmitigated_risks"),
)

register(
    name="untested_critical_assumptions",
    title="Untested critical assumptions",
    description="High-criticality assumptions with no testing experiment.",
    cypher=(
        "MATCH (a:Entity:Assumption {criticality: 'high'}) "
        "WHERE NOT (:Entity:Experiment)-[:TESTS]->(a) "
        "RETURN a.id AS id, a.name AS name "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name"),
)

register(
    name="weak_evidence_chains",
    title="Weak evidence chains",
    description="High-criticality assumptions supported only by low-strength evidence.",
    cypher=(
        "MATCH (a:Entity:Assumption {criticality: 'high'})-[:SUPPORTED_BY]->(e:Entity:Evidence) "
        "WITH a, collect(e.strength) AS strengths "
        "WHERE NONE(s IN strengths WHERE s IN ['high','strong']) "
        "RETURN a.id AS id, a.name AS name, strengths "
        "LIMIT $limit"
    ),
    expected_columns=("id", "name", "strengths"),
)
