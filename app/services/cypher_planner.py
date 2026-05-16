"""Schema-aware text-to-Cypher planner with ontology guardrails (Phase 3).

The :class:`CypherPlanner` turns a natural-language question into a single
read-only Cypher query whose labels, relationship types, and domain/range
pairs are guaranteed to match the runtime ontology.  The flow is:

1. ``plan(question)`` — render the prompt at
   ``app/prompts/cypher_plan.md`` against the live ontology view and ask
   the LLM for a JSON object ``{cypher, params, rationale}``.
2. ``validate(plan)`` — pure-Python guardrails: forbidden clauses, label
   whitelist, relationship whitelist, declared domain/range, suspicious
   substrings, and an automatic ``LIMIT $max_rows`` injection.
3. ``execute(plan)`` — open a ``READ_ACCESS`` session and run the query
   with a transaction-level timeout.  Falls back to a threaded cutoff
   when the driver does not accept ``timeout=...``.
4. ``ask(question)`` — orchestrates plan → validate → (one repair
   attempt) → validate → execute, returning a :class:`PlanResult`.

The validator deliberately leans on string-level checks rather than a real
Cypher parser.  Tokenization happens via a single regex that strips
quoted strings, line comments, and backtick-quoted identifiers BEFORE
keyword and label/relationship extraction, so identifiers named
``create_at`` no longer trigger a false positive on the ``CREATE``
keyword.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.ontology_validator import OntologyLoader, get_ontology


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = _PROJECT_ROOT / "app" / "prompts" / "cypher_plan.md"

# Tokens that must NEVER appear in a generated query.  ``CALL`` is included
# unconditionally — we reject all subqueries for simplicity; a future
# revision may carve out read-only ``CALL { ... }`` subqueries.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "CREATE",
    "MERGE",
    "SET",
    "DELETE",
    "REMOVE",
    "DETACH",
    "DROP",
    "CALL",
    "FOREACH",
)
# Multi-word forbidden phrases (handled separately because the tokenizer
# splits on whitespace and dropping the space would also fire on
# unrelated identifiers like ``load`` or ``csv``).
FORBIDDEN_PHRASES: tuple[str, ...] = ("LOAD CSV",)

# Allowed top-level Cypher clause keywords.  ``OPTIONAL MATCH`` is split
# into the two tokens by the tokenizer so we list both halves.
ALLOWED_CLAUSE_KEYWORDS: frozenset[str] = frozenset(
    {
        "MATCH",
        "OPTIONAL",
        "WHERE",
        "WITH",
        "RETURN",
        "ORDER",
        "BY",
        "LIMIT",
        "SKIP",
        "UNWIND",
    }
)

# Suspicious substrings that suggest an injection attempt.  Checked
# *after* string-stripping so an Evidence node containing ``DROP`` as a
# literal value won't trigger it; the post-strip raw is what matters.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r";\s*\n"),
    re.compile(r";\s*(DROP|DELETE|CREATE|MERGE|SET|REMOVE)\b", re.IGNORECASE),
    re.compile(r"`"),  # backticks are reserved for label/rel quoting,
    # which the planner does not emit; an LLM-supplied backtick in the
    # post-strip view is suspicious.
)

# Identifier shape — matches a label or relationship token (must start
# with a letter or underscore; no Unicode lookalikes).
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class CypherPlan:
    """A planned Cypher query plus the parameters and rationale."""

    cypher: str
    params: dict[str, Any]
    rationale: str
    referenced_labels: tuple[str, ...]
    referenced_relationships: tuple[str, ...]


@dataclass(frozen=True)
class CypherViolation:
    """A single guardrail violation.

    ``kind`` is one of: ``forbidden_clause``, ``off_ontology_label``,
    ``off_ontology_relationship``, ``domain_range``, ``parse_error``,
    ``timeout``, ``injection``.
    """

    kind: str
    detail: str


@dataclass
class PlanResult:
    """Outcome of a full ``CypherPlanner.ask()`` call."""

    plan: CypherPlan | None
    rows: list[dict[str, Any]] | None
    violations: list[CypherViolation]
    repair_attempted: bool = False


# ---------------------------------------------------------------------------
# Helpers — tokenization and pattern extraction
# ---------------------------------------------------------------------------


def _strip_quoted(cypher: str) -> str:
    """Return ``cypher`` with quoted strings replaced by spaces.

    Tokenization for forbidden-keyword detection must not look inside
    string literals: a property value of ``"DELETE pending"`` is not a
    ``DELETE`` clause.  We replace strings with whitespace so character
    offsets are preserved (helps when other regex passes use positions).
    Handles single, double, and backtick-quoted spans; line and block
    comments are also wiped.
    """
    out = []
    i = 0
    n = len(cypher)
    while i < n:
        ch = cypher[i]
        if ch in ('"', "'", "`"):
            quote = ch
            j = i + 1
            while j < n and cypher[j] != quote:
                # Cypher allows backslash escapes in string literals.
                if cypher[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            # Replace the entire span (including the quotes) with spaces.
            out.append(" " * (min(j, n - 1) - i + 1))
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and cypher[i + 1] == "/":
            # Line comment — replace to end-of-line.
            j = cypher.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and i + 1 < n and cypher[i + 1] == "*":
            j = cypher.find("*/", i + 2)
            if j == -1:
                j = n
            else:
                j += 2
            out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tokens(stripped: str) -> list[str]:
    """Return identifier-shaped tokens from a string-stripped Cypher view."""
    return _IDENT_RE.findall(stripped)


def _extract_labels(stripped: str) -> list[str]:
    """Find every node-label reference in ``stripped`` Cypher.

    Matches both single and chained labels: ``(n:Foo)``, ``(n:Foo:Bar)``,
    ``(:Foo)``, etc.  Returns the bare label tokens in source order
    (with duplicates removed while preserving order).
    """
    # First, mask out relationship patterns like ``[r:REL]`` and
    # ``-[:REL*1..3]-`` so their ``:REL`` does not get picked up.
    rel_masked = re.sub(r"\[[^\]]*\]", lambda m: " " * len(m.group(0)), stripped)
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r":\s*([A-Za-z_][A-Za-z0-9_]*)", rel_masked):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            found.append(token)
    return found


def _extract_relationships(stripped: str) -> list[str]:
    """Find every relationship type reference in ``stripped`` Cypher.

    Handles ``[:REL]``, ``[r:REL]``, ``[r:REL*1..3]``, ``[:A|B]`` (with
    pipe-separated alternatives), and ``[:REL {prop: $x}]``.
    """
    found: list[str] = []
    seen: set[str] = set()
    for bracket in re.finditer(r"\[([^\]]*)\]", stripped):
        inner = bracket.group(1)
        # Strip the optional binding name (``r:REL`` → ``:REL``).
        if ":" not in inner:
            continue
        # Take everything after the FIRST colon; split on ``|`` for
        # alternatives, and stop at the first non-identifier char per
        # alternative (so ``REL*1..3`` and ``REL {x:1}`` both yield REL).
        after_colon = inner.split(":", 1)[1]
        # Drop a property-map suffix.
        after_colon = after_colon.split("{", 1)[0]
        for alt in after_colon.split("|"):
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", alt)
            if not match:
                continue
            token = match.group(1)
            if token not in seen:
                seen.add(token)
                found.append(token)
    return found


# A "labeled node" is a pattern like ``(x:Foo)`` or ``(:Foo:Bar)``.  The
# regex captures the inner labels so we can pair them with adjacent
# relationships for domain/range checks.
_NODE_PATTERN = re.compile(
    r"\(\s*(?:[A-Za-z_][A-Za-z0-9_]*)?\s*((?::\s*[A-Za-z_][A-Za-z0-9_]*\s*)+)\)"
)
_REL_PATTERN = re.compile(
    r"(<-|-)\s*\[([^\]]*)\]\s*(->|-)"
)


def _extract_triples(stripped: str) -> list[tuple[str, str, str, bool]]:
    """Return ``(left_label, rel_type, right_label, directed)`` triples.

    Walks the query and pairs every ``(...)-[:REL]->(...)`` or
    ``(...)<-[:REL]-(...)`` or ``(...)--(...)`` fragment.  For chained
    patterns ``(a)-[:R1]->(b)-[:R2]->(c)`` two triples are emitted (the
    middle node ``(b)`` participates in both).  Only triples with
    explicit labels on both endpoints are returned — bare
    ``()-[]->()`` patterns can't be domain/range-checked.

    The fourth element is ``True`` for ``->``/``<-`` (directed) and
    ``False`` for ``--`` (undirected); the planner treats undirected
    edges as matching either direction during validation.

    Implementation: we walk the string with a manual cursor instead of
    one big regex so chained patterns share their middle node — Python's
    ``re.finditer`` returns non-overlapping matches and would skip the
    second hop of a chained pattern.
    """
    triples: list[tuple[str, str, str, bool]] = []
    # Collect every node match and every relationship match, then pair
    # them by position so chained patterns yield consecutive triples.
    nodes = [(m.start(), m.end(), m) for m in _NODE_PATTERN.finditer(stripped)]
    rels = [(m.start(), m.end(), m) for m in _REL_PATTERN.finditer(stripped)]

    for r_start, r_end, rel_match in rels:
        # Find the node immediately before the relationship and the one
        # immediately after.  ``immediately`` here means: the closest
        # node whose end is <= r_start, and the closest whose start >=
        # r_end, with no other rel-bracket in between.
        left = None
        for n_start, n_end, n_match in nodes:
            if n_end <= r_start:
                left = (n_start, n_end, n_match)
            else:
                break
        right = None
        for n_start, n_end, n_match in nodes:
            if n_start >= r_end:
                right = (n_start, n_end, n_match)
                break
        if not left or not right:
            continue
        # Sanity check: nothing but whitespace and the two arrow markers
        # should sit between the left node's end and the right node's start.
        between = stripped[left[1] : right[0]]
        # Strip the relationship bracket itself to avoid matching a
        # second rel inside the gap.
        cleaned = (
            between[: r_start - left[1]] + between[r_end - left[1] :]
        )
        if "[" in cleaned or "(" in cleaned or ")" in cleaned:
            continue

        left_labels = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", left[2].group(1))
        right_labels = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", right[2].group(1))
        rel_inner = rel_match.group(2)
        left_arrow = rel_match.group(1)
        right_arrow = rel_match.group(3)
        if not rel_inner or ":" not in rel_inner:
            continue
        rel_type = rel_inner.split(":", 1)[1].split("{", 1)[0]
        token_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", rel_type)
        if not token_match:
            continue
        rel_token = token_match.group(1)
        if not left_labels or not right_labels:
            continue

        directed_right = right_arrow == "->"
        directed_left = left_arrow == "<-"
        directed = directed_right or directed_left

        left_choice = next((x for x in left_labels if x != "Entity"), left_labels[0])
        right_choice = next((x for x in right_labels if x != "Entity"), right_labels[0])

        if directed_left and not directed_right:
            triples.append((right_choice, rel_token, left_choice, directed))
        else:
            triples.append((left_choice, rel_token, right_choice, directed))
    return triples


def render_ontology_view(ontology: OntologyLoader | None = None) -> str:
    """Render the ontology block injected into the planner prompt.

    Pulls class descriptions from the generated ``schema.json`` (via the
    runtime loader) and lists every relationship's declared domain/range.
    Falls back to bare names when no description is available.
    """
    ontology = ontology or get_ontology()
    classes = ontology._data.get("classes", {}) or {}
    relations = ontology._data.get("relations", []) or []

    label_lines: list[str] = ["Allowed entity labels (every node also has the base `Entity` label):"]
    for name in sorted(ontology.allowed_labels - {"Entity", "Document"}):
        desc = ""
        spec = classes.get(name)
        if isinstance(spec, dict):
            desc = spec.get("description") or ""
        if desc:
            label_lines.append(f"- {name}: {desc}")
        else:
            label_lines.append(f"- {name}")

    rel_lines: list[str] = ["", "Allowed relationship types (domain -> range):"]
    seen_rels: set[str] = set()
    for rel in sorted(relations, key=lambda r: r.get("predicate", "")):
        pred = rel.get("predicate", "")
        if not pred or pred in seen_rels:
            continue
        seen_rels.add(pred)
        subj = rel.get("subject", "Entity")
        obj = rel.get("object", "Entity")
        rel_lines.append(f"- {pred}: {subj} -> {obj}")
    # Surface fallback predicates that may not be in the relations list
    # but are valid (e.g. MENTIONS / SOURCE_OF / RELATED_TO when the
    # ontology is loaded from the legacy YAML).
    for pred in sorted(ontology.allowed_relationships):
        if pred in seen_rels:
            continue
        rel_lines.append(f"- {pred}: Entity -> Entity")

    return "\n".join(label_lines + rel_lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class CypherPlanner:
    """Plan, validate, and execute ontology-guarded read-only Cypher.

    Parameters
    ----------
    neo4j_service:
        Object exposing ``driver`` (with a ``.session(...)`` context
        manager).  We do NOT call any write helpers — only ``session.run``
        in READ_ACCESS mode.
    llm_service:
        Anything implementing the :class:`app.services.llm_service.LLMService`
        protocol — i.e. ``generate_json(prompt) -> Any`` returning a JSON
        object.
    ontology:
        Optional :class:`OntologyLoader`; defaults to the module-level
        singleton.
    query_timeout_seconds:
        Hard cutoff for any single ``execute`` call.  Used as
        ``session.run(..., timeout=...)`` when the driver accepts it,
        otherwise enforced via a worker thread.
    max_rows:
        Value bound to the auto-injected ``$max_rows`` parameter.
    """

    def __init__(
        self,
        neo4j_service: Any,
        llm_service: Any,
        ontology: OntologyLoader | None = None,
        query_timeout_seconds: int = 5,
        max_rows: int = 200,
    ) -> None:
        self.neo4j_service = neo4j_service
        self.llm_service = llm_service
        self.ontology = ontology or get_ontology()
        self.query_timeout_seconds = int(query_timeout_seconds)
        self.max_rows = int(max_rows)
        self._prompt_template = self._load_prompt()

    # ------------------------------------------------------------------
    # Prompt loading + LLM call
    # ------------------------------------------------------------------

    def _load_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text(encoding="utf-8")
        # Defensive fallback so tests that monkeypatch the prompt path
        # still receive a usable template.
        return "Question: {{question}}\nOntology: {{ontology_view}}\nReturn JSON."

    def _build_prompt(self, question: str, repair_note: str = "") -> str:
        ontology_view = render_ontology_view(self.ontology)
        prompt = self._prompt_template.replace("{{question}}", question).replace(
            "{{ontology_view}}", ontology_view
        )
        if repair_note:
            prompt = f"{prompt}\n\n# Repair note\n\n{repair_note}\n"
        return prompt

    def plan(self, question: str, repair_note: str = "") -> CypherPlan | None:
        """Ask the LLM for a JSON plan; return None on parse failure."""
        prompt = self._build_prompt(question, repair_note=repair_note)
        try:
            payload = self.llm_service.generate_json(prompt)
        except Exception:  # noqa: BLE001 — LLM failure is a planning failure
            return None
        return self._coerce_plan(payload)

    @staticmethod
    def _coerce_plan(payload: Any) -> CypherPlan | None:
        """Turn raw LLM JSON into a :class:`CypherPlan` (or None if shape is off)."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        cypher = payload.get("cypher")
        if not isinstance(cypher, str) or not cypher.strip():
            return None
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        rationale = payload.get("rationale") or ""
        if not isinstance(rationale, str):
            rationale = str(rationale)
        stripped = _strip_quoted(cypher)
        labels = tuple(_extract_labels(stripped))
        rels = tuple(_extract_relationships(stripped))
        return CypherPlan(
            cypher=cypher.strip(),
            params=dict(params),
            rationale=rationale.strip(),
            referenced_labels=labels,
            referenced_relationships=rels,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, plan: CypherPlan) -> list[CypherViolation]:
        """Return every guardrail violation; an empty list means accept."""
        violations: list[CypherViolation] = []
        cypher = plan.cypher
        stripped = _strip_quoted(cypher)

        # 1. Forbidden keyword tokens.  Tokens come from the stripped view
        # so identifiers inside strings can't trigger false positives.
        tokens_upper = {t.upper() for t in _tokens(stripped)}
        for kw in FORBIDDEN_KEYWORDS:
            if kw in tokens_upper:
                violations.append(
                    CypherViolation(
                        kind="forbidden_clause",
                        detail=f"Forbidden keyword: {kw}",
                    )
                )
        # Multi-word forbidden phrases — search the stripped view directly
        # because the tokenizer drops whitespace.
        upper_stripped = stripped.upper()
        for phrase in FORBIDDEN_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", upper_stripped):
                violations.append(
                    CypherViolation(
                        kind="forbidden_clause",
                        detail=f"Forbidden phrase: {phrase}",
                    )
                )

        # 2. Injection-style substrings.
        for pat in _INJECTION_PATTERNS:
            if pat.search(stripped):
                violations.append(
                    CypherViolation(
                        kind="injection",
                        detail=f"Suspicious pattern: {pat.pattern}",
                    )
                )

        # 3. Label whitelist.
        allowed_labels = self.ontology.allowed_labels | {"Entity"}
        for label in plan.referenced_labels:
            if label not in allowed_labels:
                violations.append(
                    CypherViolation(
                        kind="off_ontology_label",
                        detail=f"Label '{label}' is not in the ontology",
                    )
                )

        # 4. Relationship whitelist.
        allowed_rels = self.ontology.allowed_relationships
        for rel in plan.referenced_relationships:
            if rel not in allowed_rels:
                violations.append(
                    CypherViolation(
                        kind="off_ontology_relationship",
                        detail=f"Relationship '{rel}' is not in the ontology",
                    )
                )

        # 5. Domain/range — only check triples whose relationship was
        # itself accepted; an off-ontology rel already fired above.
        domain_range = self.ontology.domain_range_map
        for left, rel, right, directed in _extract_triples(stripped):
            if rel not in allowed_rels:
                continue
            pairs = domain_range.get(rel)
            if not pairs:
                # No declared domain/range — permissive (matches the
                # legacy branch in ontology_validator.validate_relation_detail).
                continue
            permissive = {"Entity", "Document"}
            if left in permissive or right in permissive:
                continue

            def _pair_ok(s: str, o: str) -> bool:
                for subj, obj in pairs:
                    if subj in permissive or obj in permissive:
                        return True
                    if s == subj and o == obj:
                        return True
                return False

            if _pair_ok(left, right):
                continue
            if not directed and _pair_ok(right, left):
                continue
            violations.append(
                CypherViolation(
                    kind="domain_range",
                    detail=(
                        f"({left})-[:{rel}]->({right}) violates declared "
                        f"domain/range pairs {pairs}"
                    ),
                )
            )

        return violations

    # ------------------------------------------------------------------
    # LIMIT injection
    # ------------------------------------------------------------------

    @staticmethod
    def _has_limit(cypher: str) -> bool:
        stripped = _strip_quoted(cypher)
        return bool(re.search(r"\bLIMIT\b", stripped, flags=re.IGNORECASE))

    def _ensure_limit(self, plan: CypherPlan) -> CypherPlan:
        """Return a plan with ``LIMIT $max_rows`` appended when missing.

        ``max_rows`` is a planner-injected parameter (the prompt tells the
        LLM not to include it in ``params``).  We re-extract labels/rels
        on the new cypher so downstream consumers see consistent
        metadata.
        """
        if self._has_limit(plan.cypher):
            return plan
        new_cypher = plan.cypher.rstrip().rstrip(";") + " LIMIT $max_rows"
        stripped = _strip_quoted(new_cypher)
        return CypherPlan(
            cypher=new_cypher,
            params=dict(plan.params),
            rationale=plan.rationale,
            referenced_labels=tuple(_extract_labels(stripped)),
            referenced_relationships=tuple(_extract_relationships(stripped)),
        )

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, plan: CypherPlan) -> list[dict[str, Any]]:
        """Run ``plan`` against the driver in READ_ACCESS mode."""
        try:
            from neo4j import READ_ACCESS
        except ImportError:  # pragma: no cover — tests stub READ_ACCESS as "READ"
            READ_ACCESS = "READ"  # type: ignore[assignment]
        driver = getattr(self.neo4j_service, "driver", self.neo4j_service)
        params = {**plan.params, "max_rows": self.max_rows}

        def _run() -> list[dict[str, Any]]:
            with driver.session(default_access_mode=READ_ACCESS) as session:
                # Try to pass timeout through to the driver first; if the
                # session doesn't accept it (fakes, older drivers) fall back
                # to a vanilla call wrapped in a worker-thread cutoff.
                try:
                    result = session.run(
                        plan.cypher, params, timeout=self.query_timeout_seconds
                    )
                except TypeError:
                    result = session.run(plan.cypher, params)
                rows: list[dict[str, Any]] = []
                for record in result:
                    if hasattr(record, "data") and callable(record.data):
                        try:
                            rows.append(record.data())
                            continue
                        except TypeError:
                            pass
                    try:
                        rows.append(dict(record))
                    except (TypeError, ValueError):
                        rows.append({"value": record})
                return rows

        # If a positive timeout is set, run on a worker thread so a
        # mis-behaving driver can't hang the planner forever.
        if self.query_timeout_seconds and self.query_timeout_seconds > 0:
            result_holder: dict[str, Any] = {}

            def _worker() -> None:
                try:
                    result_holder["rows"] = _run()
                except Exception as exc:  # noqa: BLE001 — preserve for the caller
                    result_holder["error"] = exc

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            thread.join(self.query_timeout_seconds + 0.5)
            if thread.is_alive():
                raise TimeoutError(
                    f"Cypher execution exceeded {self.query_timeout_seconds}s"
                )
            if "error" in result_holder:
                raise result_holder["error"]
            return result_holder.get("rows", [])
        return _run()

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def ask(self, question: str) -> PlanResult:
        """Plan → validate → (one repair attempt) → validate → execute."""
        plan = self.plan(question)
        if plan is None:
            return PlanResult(
                plan=None,
                rows=None,
                violations=[
                    CypherViolation(kind="parse_error", detail="LLM did not return a usable JSON plan")
                ],
                repair_attempted=False,
            )

        violations = self.validate(plan)
        repair_attempted = False
        if violations:
            repair_attempted = True
            repair_note = (
                "Your previous plan was rejected by the validator. "
                "Violations: "
                + "; ".join(f"{v.kind}: {v.detail}" for v in violations)
                + ". Produce a corrected plan that obeys every hard rule."
            )
            plan = self.plan(question, repair_note=repair_note)
            if plan is None:
                return PlanResult(
                    plan=None,
                    rows=None,
                    violations=violations
                    + [CypherViolation(kind="parse_error", detail="repair attempt did not return JSON")],
                    repair_attempted=True,
                )
            violations = self.validate(plan)
            if violations:
                return PlanResult(
                    plan=None,
                    rows=None,
                    violations=violations,
                    repair_attempted=True,
                )

        # Insert LIMIT after validation (post-validation injection does
        # not change the label/rel set, so the validator's verdict stays
        # correct).
        plan = self._ensure_limit(plan)
        try:
            rows = self.execute(plan)
        except TimeoutError as exc:
            return PlanResult(
                plan=plan,
                rows=None,
                violations=[CypherViolation(kind="timeout", detail=str(exc))],
                repair_attempted=repair_attempted,
            )
        return PlanResult(
            plan=plan,
            rows=rows,
            violations=[],
            repair_attempted=repair_attempted,
        )
