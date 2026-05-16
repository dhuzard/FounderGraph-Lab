"""Grounded citation verifier for agent audits.

Parses LLM-generated audit JSON, splits findings into verified vs. ungrounded
based on whether every cited entity / chunk id is present in the retrieval
context used to ground the call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalContext:
    """The grounded set of ids the LLM was allowed to cite."""

    entity_ids: frozenset[str]
    chunk_ids: frozenset[str]


@dataclass
class Finding:
    claim: str
    evidence_entity_ids: list[str]
    source_chunk_ids: list[str]
    confidence: float
    severity: str = "medium"
    verified: bool = False
    ungrounded_ids: list[str] = field(default_factory=list)


@dataclass
class VerifiedAudit:
    summary: str
    verified_findings: list[Finding]
    ungrounded_findings: list[Finding]
    raw_json: dict | None
    parse_error: str | None = None


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _extract_first_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block in ``text`` if any.

    Only JSON-style double-quoted strings are tracked. Stray apostrophes in
    surrounding prose (``"Here's the audit: {...}"``) must not be treated as
    string delimiters.
    """

    depth = 0
    start: int | None = None
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : index + 1]

    return None


def parse_audit(llm_text: str) -> tuple[dict | None, str | None]:
    """Best-effort JSON parser for LLM audit output.

    Tries strict ``json.loads`` first, then falls back to extracting the first
    balanced ``{...}`` block from prose. Returns ``(parsed_dict, None)`` on
    success or ``(None, error_message)`` on failure.
    """

    if llm_text is None:
        return None, "empty LLM output"
    text = llm_text.strip()
    if not text:
        return None, "empty LLM output"

    # Strip Markdown fences if present.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # Attempt strict parse first.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as strict_err:
        block = _extract_first_object(text)
        if block is None:
            return None, f"no JSON object found: {strict_err.msg}"
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as fallback_err:
            return None, f"malformed JSON object: {fallback_err.msg}"

    if not isinstance(parsed, dict):
        return None, "top-level JSON value is not an object"
    return parsed, None


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------


_ENTITY_ID_KEYS = (
    "id",
    "entity_id",
    "a.id",
    "b.id",
    "n.id",
    "node_id",
    "source",
    "target",
)
_CHUNK_ID_KEYS = (
    "chunk_id",
    "source_chunk_id",
    "chunkId",
    "source_chunk",
)


def _coerce_ids(value: Any) -> Iterable[str]:
    """Yield string ids from a scalar or iterable value, skipping blanks."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (str(value),)
    if isinstance(value, dict):
        # Common pattern: nested {"id": ...} shapes
        out: list[str] = []
        for key in _ENTITY_ID_KEYS + _CHUNK_ID_KEYS:
            if key in value:
                out.extend(_coerce_ids(value[key]))
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        out = []
        for item in value:
            out.extend(_coerce_ids(item))
        return out
    return ()


def build_context(
    graph_rows: list[dict] | None,
    vector_snippets: list[dict] | None,
) -> RetrievalContext:
    """Build a :class:`RetrievalContext` from graph rows and Qdrant snippets.

    ``graph_rows`` are the dict-shaped rows returned by Neo4j queries.
    ``vector_snippets`` are dict-shaped representations of Qdrant search hits
    (each typically has top-level ``id`` and a ``payload`` dict containing the
    chunk metadata).
    """

    entity_ids: set[str] = set()
    chunk_ids: set[str] = set()

    for row in graph_rows or []:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if key in _ENTITY_ID_KEYS:
                entity_ids.update(_coerce_ids(value))
            elif key in _CHUNK_ID_KEYS:
                chunk_ids.update(_coerce_ids(value))

    for snippet in vector_snippets or []:
        if not isinstance(snippet, dict):
            # Tolerate SearchResult-like objects with attribute access.
            payload = getattr(snippet, "payload", None)
            sid = getattr(snippet, "id", None)
            if sid is not None:
                chunk_ids.update(_coerce_ids(sid))
            if isinstance(payload, dict):
                for key in _CHUNK_ID_KEYS + ("id",):
                    if key in payload:
                        chunk_ids.update(_coerce_ids(payload[key]))
                for key in _ENTITY_ID_KEYS:
                    if key in payload:
                        entity_ids.update(_coerce_ids(payload[key]))
            continue

        if "id" in snippet:
            chunk_ids.update(_coerce_ids(snippet["id"]))
        payload = snippet.get("payload")
        if isinstance(payload, dict):
            for key in _CHUNK_ID_KEYS + ("id",):
                if key in payload:
                    chunk_ids.update(_coerce_ids(payload[key]))
            for key in _ENTITY_ID_KEYS:
                if key in payload:
                    entity_ids.update(_coerce_ids(payload[key]))
        for key in _CHUNK_ID_KEYS:
            if key in snippet:
                chunk_ids.update(_coerce_ids(snippet[key]))
        for key in _ENTITY_ID_KEYS:
            if key in snippet:
                entity_ids.update(_coerce_ids(snippet[key]))

    return RetrievalContext(
        entity_ids=frozenset(entity_ids),
        chunk_ids=frozenset(chunk_ids),
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item)
            elif item is not None:
                out.append(str(item))
        return out
    return [str(value)]


def _coerce_confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_severity(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        normalized = value.strip().lower()
        if normalized in {"high", "medium", "low", "critical"}:
            return normalized
    return "medium"


def verify(audit_json: dict, context: RetrievalContext) -> VerifiedAudit:
    """Split audit findings into verified vs. ungrounded.

    A finding is *verified* iff every cited entity id is in
    ``context.entity_ids`` and every cited chunk id is in ``context.chunk_ids``.
    Cited ids that are absent from the context are recorded on
    ``Finding.ungrounded_ids``.
    """

    summary = ""
    findings_raw: list[Any] = []
    if isinstance(audit_json, dict):
        summary = str(audit_json.get("summary") or "").strip()
        findings_raw = audit_json.get("findings") or []
        if not isinstance(findings_raw, list):
            findings_raw = []

    verified: list[Finding] = []
    ungrounded: list[Finding] = []

    for entry in findings_raw:
        if not isinstance(entry, dict):
            continue
        entity_ids = _as_str_list(entry.get("evidence_entity_ids"))
        chunk_ids = _as_str_list(entry.get("source_chunk_ids"))
        missing = [eid for eid in entity_ids if eid not in context.entity_ids]
        missing += [cid for cid in chunk_ids if cid not in context.chunk_ids]

        finding = Finding(
            claim=str(entry.get("claim") or "").strip(),
            evidence_entity_ids=entity_ids,
            source_chunk_ids=chunk_ids,
            confidence=_coerce_confidence(entry.get("confidence")),
            severity=_coerce_severity(entry.get("severity")),
            verified=not missing and bool(entity_ids or chunk_ids),
            ungrounded_ids=missing,
        )
        if finding.verified:
            verified.append(finding)
        else:
            ungrounded.append(finding)

    return VerifiedAudit(
        summary=summary,
        verified_findings=verified,
        ungrounded_findings=ungrounded,
        raw_json=audit_json if isinstance(audit_json, dict) else None,
    )


__all__ = [
    "Finding",
    "RetrievalContext",
    "VerifiedAudit",
    "build_context",
    "parse_audit",
    "verify",
]
