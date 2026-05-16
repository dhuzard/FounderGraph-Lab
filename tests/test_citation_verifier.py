"""Tests for the grounded citation verifier (Phase 5)."""

from __future__ import annotations

from app.services.citation_verifier import (
    Finding,
    RetrievalContext,
    VerifiedAudit,
    build_context,
    parse_audit,
    verify,
)


# ---------------------------------------------------------------------------
# parse_audit
# ---------------------------------------------------------------------------


def test_parse_audit_strict_json() -> None:
    """Well-formed JSON returns a dict and no error."""
    text = (
        '{"summary": "All good.", '
        '"findings": [{"claim": "x", "evidence_entity_ids": ["asm-1"], '
        '"source_chunk_ids": [], "confidence": 0.9, "severity": "high"}]}'
    )
    parsed, error = parse_audit(text)
    assert error is None
    assert isinstance(parsed, dict)
    assert parsed["summary"] == "All good."
    assert parsed["findings"][0]["claim"] == "x"


def test_parse_audit_balanced_extraction() -> None:
    """JSON wrapped in prose is still extracted via balanced-brace fallback."""
    text = (
        "Here's the audit you asked for: "
        '{"summary": "wrapped", "findings": []} '
        "hope this helps!"
    )
    parsed, error = parse_audit(text)
    assert error is None
    assert parsed == {"summary": "wrapped", "findings": []}


def test_parse_audit_balanced_extraction_with_nested_braces() -> None:
    """Nested objects inside the audit don't confuse the brace matcher."""
    text = (
        "Sure thing! "
        '{"summary": "ok", "findings": [{"claim": "c", '
        '"evidence_entity_ids": ["a"], "source_chunk_ids": [], '
        '"confidence": 0.5, "severity": "low", "meta": {"nested": true}}]}'
        " end."
    )
    parsed, error = parse_audit(text)
    assert error is None
    assert parsed is not None
    assert parsed["findings"][0]["meta"] == {"nested": True}


def test_parse_audit_strips_markdown_fence() -> None:
    """Triple-backtick fenced JSON output is still parsed."""
    text = '```json\n{"summary": "fenced", "findings": []}\n```'
    parsed, error = parse_audit(text)
    assert error is None
    assert parsed == {"summary": "fenced", "findings": []}


def test_parse_audit_malformed() -> None:
    """Bad JSON returns (None, error)."""
    text = "This is just prose, no braces at all."
    parsed, error = parse_audit(text)
    assert parsed is None
    assert error is not None and error  # non-empty error message


def test_parse_audit_malformed_braces() -> None:
    """Unparseable brace block still returns an error."""
    text = "Trust me: {this is not: valid json at all,,,}"
    parsed, error = parse_audit(text)
    assert parsed is None
    assert error is not None


def test_parse_audit_empty_input() -> None:
    parsed, error = parse_audit("")
    assert parsed is None
    assert error is not None


def test_parse_audit_non_object_top_level() -> None:
    """Top-level JSON arrays are rejected (we expect an object)."""
    parsed, error = parse_audit('[{"a": 1}]')
    assert parsed is None
    assert error is not None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _context(entities: list[str], chunks: list[str]) -> RetrievalContext:
    return RetrievalContext(
        entity_ids=frozenset(entities),
        chunk_ids=frozenset(chunks),
    )


def test_verify_drops_hallucinated_entity_ids() -> None:
    """A finding citing an id not present in context goes to ungrounded."""
    audit = {
        "summary": "Hallucination test",
        "findings": [
            {
                "claim": "Assumption X has no support",
                "evidence_entity_ids": ["evd-fake"],
                "source_chunk_ids": [],
                "confidence": 0.6,
                "severity": "high",
            }
        ],
    }
    context = _context(entities=["evd-real"], chunks=[])
    audit_result = verify(audit, context)

    assert isinstance(audit_result, VerifiedAudit)
    assert audit_result.verified_findings == []
    assert len(audit_result.ungrounded_findings) == 1
    ungrounded = audit_result.ungrounded_findings[0]
    assert ungrounded.verified is False
    assert "evd-fake" in ungrounded.ungrounded_ids


def test_verify_keeps_fully_grounded_findings() -> None:
    """Every cited id present in the context => finding is verified."""
    audit = {
        "summary": "All grounded",
        "findings": [
            {
                "claim": "Assumption X is supported.",
                "evidence_entity_ids": ["asm-1", "evd-1"],
                "source_chunk_ids": ["doc-a:chunk-0"],
                "confidence": 0.85,
                "severity": "medium",
            }
        ],
    }
    context = _context(
        entities=["asm-1", "evd-1"],
        chunks=["doc-a:chunk-0"],
    )
    result = verify(audit, context)

    assert len(result.verified_findings) == 1
    assert result.ungrounded_findings == []
    finding = result.verified_findings[0]
    assert finding.verified is True
    assert finding.ungrounded_ids == []
    assert finding.severity == "medium"
    assert finding.confidence == 0.85


def test_verify_partial_grounding_is_ungrounded() -> None:
    """If even one cited id is missing, the finding is ungrounded."""
    audit = {
        "summary": "",
        "findings": [
            {
                "claim": "mixed citations",
                "evidence_entity_ids": ["asm-1", "asm-missing"],
                "source_chunk_ids": ["doc-a:chunk-0"],
                "confidence": 0.5,
                "severity": "low",
            }
        ],
    }
    context = _context(entities=["asm-1"], chunks=["doc-a:chunk-0"])
    result = verify(audit, context)

    assert result.verified_findings == []
    assert len(result.ungrounded_findings) == 1
    assert result.ungrounded_findings[0].ungrounded_ids == ["asm-missing"]


def test_verify_handles_missing_findings_key() -> None:
    result = verify({"summary": "no findings array"}, _context([], []))
    assert result.summary == "no findings array"
    assert result.verified_findings == []
    assert result.ungrounded_findings == []


def test_verify_finding_without_citations_is_ungrounded() -> None:
    """A finding with no cited ids cannot be verified."""
    audit = {
        "findings": [
            {
                "claim": "no citations",
                "evidence_entity_ids": [],
                "source_chunk_ids": [],
                "confidence": 0.2,
                "severity": "low",
            }
        ]
    }
    result = verify(audit, _context(["asm-1"], ["doc-a:chunk-0"]))
    assert result.verified_findings == []
    assert len(result.ungrounded_findings) == 1


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


def test_build_context_pulls_ids_from_common_keys() -> None:
    """Graph rows with ``a.id`` plus snippets with ``payload.chunk_id`` are merged."""
    graph_rows = [
        {"a.id": "asm-1", "b.id": "evd-1"},
        {"id": "exp-1"},
        {"label": "Some label without an id"},  # ignored
    ]
    vector_snippets = [
        {
            "id": "doc-a:chunk-0",  # top-level Qdrant id
            "payload": {
                "chunk_id": "doc-a:chunk-0",
                "source_chunk_id": "doc-a:chunk-1",
                "entity_id": "ent-from-payload",
            },
        },
        {
            "id": "doc-b:chunk-2",
            "payload": {"document_id": "doc-b"},
        },
    ]
    context = build_context(graph_rows, vector_snippets)

    assert "asm-1" in context.entity_ids
    assert "evd-1" in context.entity_ids
    assert "exp-1" in context.entity_ids
    assert "ent-from-payload" in context.entity_ids
    assert "doc-a:chunk-0" in context.chunk_ids
    assert "doc-a:chunk-1" in context.chunk_ids
    assert "doc-b:chunk-2" in context.chunk_ids


def test_build_context_accepts_object_like_snippets() -> None:
    """Tolerates SearchResult-style objects with attribute access."""

    class FakeResult:
        def __init__(self, id_: str, payload: dict) -> None:
            self.id = id_
            self.payload = payload

    snippets = [FakeResult("doc-x:chunk-5", {"entity_id": "evd-7"})]
    context = build_context(graph_rows=None, vector_snippets=snippets)

    assert "doc-x:chunk-5" in context.chunk_ids
    assert "evd-7" in context.entity_ids


def test_build_context_handles_empty_inputs() -> None:
    context = build_context([], [])
    assert context.entity_ids == frozenset()
    assert context.chunk_ids == frozenset()


def test_build_context_pulls_collection_values() -> None:
    """Graph rows that return lists of ids (e.g. ``collect(a.id)``) are expanded."""
    rows = [{"id": ["asm-1", "asm-2"]}]
    context = build_context(rows, [])
    assert {"asm-1", "asm-2"}.issubset(context.entity_ids)


# ---------------------------------------------------------------------------
# Finding dataclass smoke test
# ---------------------------------------------------------------------------


def test_finding_defaults() -> None:
    f = Finding(
        claim="c",
        evidence_entity_ids=["a"],
        source_chunk_ids=["b"],
        confidence=0.5,
    )
    assert f.severity == "medium"
    assert f.verified is False
    assert f.ungrounded_ids == []
