"""Entity resolution service (Phase 6).

Reads the validated entity slice of the graph, clusters likely duplicates by
embedding-cosine + name-token Jaccard, asks the LLM to confirm each pair, and
ranks the surviving proposals so a human reviewer can approve them.

Approvals write reversible ``(:Entity)-[:SAME_AS]->(:Entity)`` edges via
:class:`app.services.neo4j_service.Neo4jService`.  A second, explicit step
(``consolidate``) performs a destructive APOC merge -- the UI guards this
behind a confirmation checkbox so duplicates can be reviewed and undone
before any data is rewritten.

The resolver is intentionally driver-agnostic: the LLM is any object exposing
``generate_json(prompt) -> dict``, and the embedding function is any callable
``str -> Sequence[float]``.  Tests inject deterministic fakes for both.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, Sequence


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeProposal:
    """One proposed (canonical, duplicate) merge with scoring metadata.

    ``canonical_id`` is the entity expected to survive a future
    consolidation; ``duplicate_id`` is the one that would be folded into it.
    ``score`` blends embedding similarity, name-token Jaccard, and the LLM
    verdict into a single confidence number used for UI ranking.
    """

    canonical_id: str
    duplicate_id: str
    canonical_name: str
    duplicate_name: str
    entity_type: str
    cosine_similarity: float
    name_jaccard: float
    llm_verdict: str        # "yes" | "no" | "uncertain"
    llm_rationale: str
    score: float            # combined confidence


# ---------------------------------------------------------------------------
# Protocols (kept narrow so tests can inject minimal fakes)
# ---------------------------------------------------------------------------


class _LLMLike(Protocol):
    def generate_json(self, prompt: str) -> Any: ...


class _Neo4jLike(Protocol):
    def get_all_entities(self, limit: int = ...) -> list[dict[str, Any]]: ...

    def write_same_as(
        self,
        canonical_id: str,
        duplicate_id: str,
        confidence: float = ...,
    ) -> None: ...

    def consolidate(self, canonical_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Reviewer confidence ranks used to pick the canonical side of a pair.  Higher
# is better; unknown / missing falls back to 0.
_CONFIDENCE_RANK = {
    "strong": 3,
    "moderate": 2,
    "weak": 1,
    "ungraded": 0,
    "": 0,
    None: 0,
}


def _name_tokens(name: str) -> set[str]:
    """Return the lower-cased alphanumeric token set of a name string."""
    if not name:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(str(name))}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two sets; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Returns 0.0 when either vector is empty or has zero norm so the rest of
    the pipeline never trips on a divide-by-zero.
    """
    if not u or not v:
        return 0.0
    length = min(len(u), len(v))
    dot = 0.0
    nu = 0.0
    nv = 0.0
    for i in range(length):
        ui = float(u[i])
        vi = float(v[i])
        dot += ui * vi
        nu += ui * ui
        nv += vi * vi
    if nu <= 0.0 or nv <= 0.0:
        return 0.0
    return dot / math.sqrt(nu * nv)


def _verdict_weight(verdict: str) -> float:
    """Convert an LLM verdict string into a [0, 1] weight for scoring."""
    v = (verdict or "").strip().lower()
    if v == "yes":
        return 1.0
    if v == "uncertain":
        return 0.5
    return 0.0


def _confidence_rank(value: Any) -> int:
    return _CONFIDENCE_RANK.get(value if value in _CONFIDENCE_RANK else "", 0)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class EntityResolver:
    """Group duplicate-likely entities and propose reversible SAME_AS merges.

    The resolver only proposes; the writer (``approve`` / ``consolidate``)
    invokes the Neo4j helpers.  Callers should treat ``propose_merges`` as
    side-effect-free (it does issue one LLM call per surviving pair).
    """

    def __init__(
        self,
        neo4j_service: _Neo4jLike,
        llm_service: _LLMLike,
        embed_fn: Callable[[str], Sequence[float]],
        cosine_threshold: float = 0.92,
        jaccard_threshold: float = 0.5,
    ) -> None:
        self.neo4j = neo4j_service
        self.llm = llm_service
        self.embed = embed_fn
        self.cosine_threshold = float(cosine_threshold)
        self.jaccard_threshold = float(jaccard_threshold)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def propose_merges(self, scope: str | None = None) -> list[MergeProposal]:
        """Return ranked merge proposals across validated entities.

        ``scope`` optionally restricts the candidate pool to a single entity
        ``type`` (e.g. ``"Assumption"``).  Pairs are only considered within a
        single type so we never propose merging a Founder with a Risk.
        """
        entities = self._load_validated_entities()
        if scope:
            entities = [e for e in entities if str(e.get("type") or "") == scope]

        proposals: list[MergeProposal] = []
        for entity_type, group in self._group_by_type(entities).items():
            proposals.extend(self._propose_for_group(entity_type, group))

        proposals.sort(key=lambda p: p.score, reverse=True)
        return proposals

    def _load_validated_entities(self) -> list[dict[str, Any]]:
        """Pull the validated entity slice (best-effort) and shape it."""
        # Neo4jService.get_all_entities returns at most ``limit`` rows; the
        # resolver wants the full validated set, so we ask for a large slice.
        try:
            rows = list(self.neo4j.get_all_entities(limit=1000))
        except TypeError:
            # Some fakes ignore the keyword.
            rows = list(self.neo4j.get_all_entities())
        out: list[dict[str, Any]] = []
        for row in rows:
            status = (
                row.get("validation_status")
                or row.get("status")
                or ""
            )
            if status and status != "validated":
                continue
            entity_id = row.get("id")
            name = row.get("name") or row.get("label")
            entity_type = row.get("type")
            if not entity_id or not name or not entity_type:
                continue
            out.append({
                "id": str(entity_id),
                "name": str(name),
                "type": str(entity_type),
                "reviewer_confidence": row.get("reviewer_confidence") or "",
            })
        return out

    @staticmethod
    def _group_by_type(
        entities: Iterable[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            groups.setdefault(e["type"], []).append(e)
        return groups

    def _propose_for_group(
        self,
        entity_type: str,
        group: list[dict[str, Any]],
    ) -> list[MergeProposal]:
        if len(group) < 2:
            return []
        # Embed each name once; the embed function is allowed to be expensive.
        embeddings = [self.embed(e["name"]) for e in group]
        proposals: list[MergeProposal] = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                cosine = _cosine(embeddings[i], embeddings[j])
                if cosine < self.cosine_threshold:
                    continue
                ja = _name_tokens(a["name"])
                jb = _name_tokens(b["name"])
                jaccard = _jaccard(ja, jb)
                if jaccard < self.jaccard_threshold:
                    continue
                verdict, rationale = self._confirm_with_llm(entity_type, a, b)
                canonical, duplicate = self._pick_canonical(a, b)
                score = (
                    0.5 * cosine
                    + 0.3 * jaccard
                    + 0.2 * _verdict_weight(verdict)
                )
                proposals.append(
                    MergeProposal(
                        canonical_id=canonical["id"],
                        duplicate_id=duplicate["id"],
                        canonical_name=canonical["name"],
                        duplicate_name=duplicate["name"],
                        entity_type=entity_type,
                        cosine_similarity=cosine,
                        name_jaccard=jaccard,
                        llm_verdict=verdict,
                        llm_rationale=rationale,
                        score=score,
                    )
                )
        return proposals

    # ------------------------------------------------------------------
    # LLM confirmation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        entity_type: str,
        a: dict[str, Any],
        b: dict[str, Any],
    ) -> str:
        """Build the JSON-only same-as confirmation prompt."""
        return (
            f"Are these two {entity_type} entities the same?\n"
            f"Entity A: {a['name']}\n"
            f"Entity B: {b['name']}\n"
            "Respond with strict JSON of the form "
            '{"verdict": "yes" | "no" | "uncertain", "rationale": "..."}.'
        )

    def _confirm_with_llm(
        self,
        entity_type: str,
        a: dict[str, Any],
        b: dict[str, Any],
    ) -> tuple[str, str]:
        prompt = self._build_prompt(entity_type, a, b)
        try:
            response = self.llm.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001 — LLM failures must not crash the batch
            return "uncertain", f"LLM error: {exc}"

        if isinstance(response, str):
            # Some LLM stubs return raw JSON text; tolerate it.
            try:
                response = json.loads(response)
            except json.JSONDecodeError:
                return "uncertain", "LLM returned non-JSON text"
        if not isinstance(response, dict):
            return "uncertain", "LLM response was not a JSON object"

        verdict = str(response.get("verdict", "")).strip().lower()
        if verdict not in {"yes", "no", "uncertain"}:
            verdict = "uncertain"
        rationale = str(response.get("rationale", "")).strip()
        return verdict, rationale

    # ------------------------------------------------------------------
    # Canonical selection
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_canonical(
        a: dict[str, Any],
        b: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Pick the canonical / duplicate side of a candidate pair.

        Higher ``reviewer_confidence`` wins; ties break on longer name (more
        informative label).  A final stable tie-break uses entity id so the
        choice is deterministic across runs.
        """
        ra = _confidence_rank(a.get("reviewer_confidence"))
        rb = _confidence_rank(b.get("reviewer_confidence"))
        if ra != rb:
            return (a, b) if ra > rb else (b, a)
        la = len(str(a.get("name") or ""))
        lb = len(str(b.get("name") or ""))
        if la != lb:
            return (a, b) if la > lb else (b, a)
        return (a, b) if str(a["id"]) <= str(b["id"]) else (b, a)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def approve(self, proposal: MergeProposal) -> None:
        """Write the reversible SAME_AS edge for a single proposal."""
        self.neo4j.write_same_as(
            proposal.canonical_id,
            proposal.duplicate_id,
            confidence=proposal.score,
        )

    def consolidate(self, canonical_id: str) -> None:
        """Hard-merge all SAME_AS duplicates into ``canonical_id`` via APOC."""
        self.neo4j.consolidate(canonical_id)
