"""Hybrid GraphRAG retriever (Phase 4).

Three-stage retrieval over the FounderGraph stack:

1. **Seed**   -- run the question through Ollama embeddings once, then fan it
   out into both Neo4j (entity-summary vector index) and Qdrant (chunk
   vector store) to collect the top-``seed_k`` candidates from each side.
2. **Expand** -- for every entity seed, follow 1-2 typed hops in Neo4j using
   ``apoc.path.subgraphAll``.  Every traversed node becomes an expansion
   candidate; its proximity score decays with hop depth (``1/hop``).
3. **Re-rank** -- combine
   ``alpha * cosine + beta * proximity + gamma * evidence_strength`` per
   candidate and return the top-``final_top_k`` items.  Items keep enough
   metadata (id, kind, text, payload) so the agent layer can both render
   them in the prompt and reuse them as a :class:`RetrievalContext` for the
   Phase 5 citation verifier.

Why both vector stores?  Chunks are *long-form text* -- they are the right
unit to surface in a prompt, but they're too noisy as a graph anchor.  Entity
summaries are *short, typed, structured* nodes -- the right unit to walk the
graph from.  Running a single embedding pass and routing it through both
indexes keeps latency down while taking advantage of each store's strengths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.services.citation_verifier import RetrievalContext


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalWeights:
    """Tunable weights for the final re-rank score.

    Defaults live in ``app/config.py`` so operators can tune them per env
    without touching code.  ``alpha`` is the dominant signal -- raw cosine
    similarity to the question -- while ``beta`` lets a tight 1-hop neighbour
    of a strong seed bubble up even if its own cosine is mediocre.
    ``gamma`` is a domain-specific boost for ``Evidence`` entities so a
    'high'-strength supporting Evidence outranks a weak/untyped one.
    """

    alpha_cosine: float
    beta_proximity: float
    gamma_evidence: float


@dataclass(frozen=True)
class RetrievedItem:
    """A single re-ranked retrieval candidate.

    ``kind`` is one of ``entity`` / ``chunk`` / ``community``.  Community
    items only appear when the retriever was constructed with a
    :class:`CommunityService` and the question is flagged as *global* by
    :meth:`HybridRetriever._is_global_question`.
    """

    kind: str  # "entity" | "chunk" | "community"
    id: str
    text: str
    score: float
    payload: dict[str, Any]


@dataclass
class HybridResult:
    """Bundle returned from :meth:`HybridRetriever.retrieve`."""

    items: list[RetrievedItem]
    seed_entity_ids: list[str] = field(default_factory=list)
    seed_chunk_ids: list[str] = field(default_factory=list)
    expansion_node_ids: list[str] = field(default_factory=list)
    cypher_traces: list[str] = field(default_factory=list)

    def to_retrieval_context(self) -> RetrievalContext:
        """Adapt to the Phase 5 grounded-citation contract.

        Verifier consumers care about which ids the LLM was allowed to cite --
        we surface every entity (seeds + expansion) and every chunk.
        """
        entity_ids: set[str] = set()
        chunk_ids: set[str] = set()
        for item in self.items:
            if item.kind == "entity" and item.id:
                entity_ids.add(str(item.id))
            elif item.kind == "chunk" and item.id:
                chunk_ids.add(str(item.id))
        # Seeds and expansions are always allowed even if the re-ranker pruned
        # them out of the final top-K -- they were part of the grounded set.
        entity_ids.update(self.seed_entity_ids)
        entity_ids.update(self.expansion_node_ids)
        chunk_ids.update(self.seed_chunk_ids)
        return RetrievalContext(
            entity_ids=frozenset(eid for eid in entity_ids if eid),
            chunk_ids=frozenset(cid for cid in chunk_ids if cid),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Plain cosine similarity in Python; no numpy dep at retrieval time."""
    al = list(a)
    bl = list(b)
    if not al or not bl or len(al) != len(bl):
        return 0.0
    num = sum(x * y for x, y in zip(al, bl))
    da = sum(x * x for x in al) ** 0.5
    db = sum(y * y for y in bl) ** 0.5
    if da == 0.0 or db == 0.0:
        return 0.0
    return float(num / (da * db))


def _evidence_strength_score(payload: dict[str, Any] | None) -> float:
    """Map a strength tag to a [0,1] boost.

    The score is meaningful only for ``Evidence`` entities; for everything
    else we default to a neutral 0.5 so the gamma term contributes the same
    constant baseline -- ordering is then driven entirely by alpha+beta.
    """
    if not payload:
        return 0.5
    raw = (
        payload.get("strength")
        or payload.get("evidence_strength")
        or payload.get("grade")
        or payload.get("evidence_grade")
    )
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm in {"high", "strong"}:
            return 1.0
        if norm == "medium":
            return 0.6
        if norm in {"low", "weak"}:
            return 0.3
    return 0.5


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Three-stage hybrid retriever: seed -> expand -> re-rank.

    Parameters
    ----------
    neo4j_service:
        A :class:`Neo4jService` (or any duck-typed peer) providing
        ``vector_search_entities`` and ``get_neighborhood``.
    qdrant_service:
        A :class:`QdrantService` exposing ``semantic_search``.
    embed_fn:
        Callable ``str -> list[float]`` -- typically
        ``QdrantService(...).embed``.  Reusing the same model on both sides
        keeps cosine scores comparable.
    weights:
        :class:`RetrievalWeights` overriding the alpha/beta/gamma defaults.
    seed_k:
        Number of seeds to pull from each store.
    expansion_hops:
        Max graph distance for the typed expansion stage.
    allowed_relationships:
        Optional whitelist of relationship types for the expansion stage;
        ``None`` lets APOC traverse every edge.
    final_top_k:
        Cap on the re-ranked output (default 30).
    """

    def __init__(
        self,
        neo4j_service: Any,
        qdrant_service: Any,
        embed_fn: Callable[[str], list[float]],
        weights: RetrievalWeights | None = None,
        seed_k: int | None = None,
        expansion_hops: int | None = None,
        allowed_relationships: list[str] | None = None,
        final_top_k: int = 30,
        community_service: Any | None = None,
    ) -> None:
        # Import here to avoid a circular: config -> nothing in this module.
        from app import config as _cfg

        self.neo4j = neo4j_service
        self.qdrant = qdrant_service
        self.embed_fn = embed_fn
        self.weights = weights or RetrievalWeights(
            alpha_cosine=_cfg.HYBRID_ALPHA_COSINE,
            beta_proximity=_cfg.HYBRID_BETA_PROXIMITY,
            gamma_evidence=_cfg.HYBRID_GAMMA_EVIDENCE_STRENGTH,
        )
        self.seed_k = seed_k if seed_k is not None else _cfg.HYBRID_SEED_K
        self.expansion_hops = (
            expansion_hops if expansion_hops is not None else _cfg.HYBRID_EXPANSION_HOPS
        )
        self.allowed_relationships = allowed_relationships
        self.final_top_k = final_top_k
        # Phase 7 -- when a community summariser is wired in, ``retrieve``
        # routes "global"-flavoured questions through community summaries
        # in addition to the standard entity + chunk seeds.  ``None`` keeps
        # the pre-Phase-7 behaviour intact.
        self.community_service = community_service

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def retrieve(
        self,
        question: str,
        ontology_filter: dict[str, Any] | None = None,
    ) -> HybridResult:
        """Run the three-stage retrieval pipeline.

        ``ontology_filter`` may carry:

        * ``label`` -- a single string label passed to
          ``vector_search_entities`` to filter the Neo4j seed set.
        * ``labels`` -- a list of labels; the union is computed by issuing
          one vector search per label and merging by id (best-score wins).

        Entities whose label is not in the requested set are dropped from
        the seed set.  Chunks are unaffected by the filter -- Qdrant payloads
        don't carry the same typed label.
        """
        from app.services.qdrant_service import DOCUMENT_COLLECTION

        if not question or not question.strip():
            return HybridResult(items=[])

        # Stage 0 -- embed the question once.
        query_vec = self.embed_fn(question)
        traces: list[str] = []

        # ------------------------------------------------------------------
        # Stage A: seed from both stores
        # ------------------------------------------------------------------
        entity_seeds = self._seed_entities(query_vec, ontology_filter, traces)
        chunk_seeds = self._seed_chunks(question, DOCUMENT_COLLECTION, traces)

        # ------------------------------------------------------------------
        # Stage B: typed graph expansion from entity seeds
        # ------------------------------------------------------------------
        seed_entity_ids = [str(s["id"]) for s in entity_seeds if s.get("id")]
        expansion_rows: list[dict[str, Any]] = []
        if seed_entity_ids:
            try:
                expansion_rows = self.neo4j.get_neighborhood(
                    seed_entity_ids,
                    hops=self.expansion_hops,
                    allowed_relationships=self.allowed_relationships,
                )
                traces.append(
                    f"apoc.path.subgraphAll(seeds={len(seed_entity_ids)}, "
                    f"hops={self.expansion_hops}) -> {len(expansion_rows)} edges"
                )
            except Exception as exc:  # noqa: BLE001 -- graph expansion is best-effort
                traces.append(f"expansion failed: {exc!r}")
                expansion_rows = []

        # Convert expansion edges into per-node candidates with hop=1 -- we
        # don't currently get a deeper depth signal back from APOC because
        # the flattened RETURN drops the level info, so any node touched by
        # the subgraph walk inherits the same "1/expansion_hops" proximity
        # baseline (=1.0 when hops=1, =0.5 when hops=2 ...).  This is a
        # pragmatic choice: APOC's ``path`` variant carries the depth but
        # the bulk row shape is friendlier to test.
        proximity_baseline = 1.0 / max(1, self.expansion_hops)
        seen_seed_ids = set(seed_entity_ids)
        expansion_candidates: dict[str, dict[str, Any]] = {}
        for row in expansion_rows:
            for side in ("source", "target"):
                node_id = row.get(f"{side}_id")
                if not node_id or node_id in seen_seed_ids:
                    continue
                if node_id in expansion_candidates:
                    continue
                expansion_candidates[node_id] = {
                    "id": node_id,
                    "name": row.get(f"{side}_name") or "",
                    "type": "",
                    "proximity": proximity_baseline,
                }
        expansion_node_ids = list(expansion_candidates.keys())

        # ------------------------------------------------------------------
        # Stage C: graph-aware re-rank
        # ------------------------------------------------------------------
        items: list[RetrievedItem] = []

        # Entity seeds -- cosine reported by the vector index directly.
        for seed in entity_seeds:
            cosine = float(seed.get("score") or 0.0)
            proximity = 1.0  # the seed IS the anchor
            payload = {
                "name": seed.get("name"),
                "type": seed.get("type"),
                "kind": "entity_seed",
                # Propagate evidence-strength fields so the gamma scorer can
                # boost high-strength Evidence over weak/untyped seeds.
                "strength": seed.get("strength"),
                "evidence_strength": seed.get("evidence_strength"),
                "evidence_grade": seed.get("evidence_grade"),
            }
            gamma_score = _evidence_strength_score(payload)
            final = (
                self.weights.alpha_cosine * cosine
                + self.weights.beta_proximity * proximity
                + self.weights.gamma_evidence * gamma_score
            )
            text = self._entity_text(seed)
            items.append(
                RetrievedItem(
                    kind="entity",
                    id=str(seed.get("id")),
                    text=text,
                    score=final,
                    payload=payload,
                )
            )

        # Expansion nodes -- no direct cosine to the query (we don't have
        # their embedding in hand at retrieval time without a second index
        # round-trip), so they contribute through proximity + gamma only.
        for cand in expansion_candidates.values():
            cosine = 0.0
            proximity = float(cand["proximity"])
            payload = {
                "name": cand.get("name"),
                "type": cand.get("type"),
                "kind": "expansion",
            }
            # If the expanded node carries an Evidence strength annotation
            # from a co-fetched lookup, the gamma score lights up; otherwise
            # it stays neutral.
            gamma_score = _evidence_strength_score(payload)
            final = (
                self.weights.alpha_cosine * cosine
                + self.weights.beta_proximity * proximity
                + self.weights.gamma_evidence * gamma_score
            )
            items.append(
                RetrievedItem(
                    kind="entity",
                    id=str(cand["id"]),
                    text=self._entity_text(cand),
                    score=final,
                    payload=payload,
                )
            )

        # Chunks -- cosine reported by Qdrant directly; proximity is 0.0
        # (they're not on the graph) and gamma is neutral (chunks don't
        # carry evidence_strength).
        seed_chunk_ids: list[str] = []
        for chunk in chunk_seeds:
            cosine = float(chunk.get("score") or 0.0)
            proximity = 0.0
            payload = dict(chunk.get("payload") or {})
            payload.setdefault("kind", "chunk")
            gamma_score = _evidence_strength_score(payload)
            final = (
                self.weights.alpha_cosine * cosine
                + self.weights.beta_proximity * proximity
                + self.weights.gamma_evidence * gamma_score
            )
            chunk_id = (
                payload.get("chunk_id")
                or payload.get("source_chunk_id")
                or chunk.get("id")
            )
            chunk_id_str = str(chunk_id) if chunk_id else ""
            if chunk_id_str:
                seed_chunk_ids.append(chunk_id_str)
            items.append(
                RetrievedItem(
                    kind="chunk",
                    id=chunk_id_str,
                    text=str(chunk.get("text") or payload.get("text") or ""),
                    score=final,
                    payload=payload,
                )
            )

        items.sort(key=lambda it: it.score, reverse=True)
        items = items[: self.final_top_k]

        # ------------------------------------------------------------------
        # Stage D (Phase 7): community summaries for global questions
        # ------------------------------------------------------------------
        community_items = self._maybe_community_items(question, query_vec, traces)
        if community_items:
            # Prepend so the LLM consumes the global summary BEFORE the
            # narrow entity/chunk evidence -- this matches Microsoft's
            # GraphRAG observation that a high-level summary anchors the
            # subsequent finer-grained retrieval.
            items = community_items + items
            # Truncate again to keep the contract; community items count
            # toward the top-K budget.
            items = items[: self.final_top_k]

        return HybridResult(
            items=items,
            seed_entity_ids=seed_entity_ids,
            seed_chunk_ids=seed_chunk_ids,
            expansion_node_ids=expansion_node_ids,
            cypher_traces=traces,
        )

    # ------------------------------------------------------------------
    # Phase 7 — global question routing
    # ------------------------------------------------------------------

    # Keywords that flag a question as "global" rather than "local".  The
    # vocabulary is intentionally a hard-coded English substring list; we
    # accept the linguistic crudeness in exchange for zero LLM round-trips
    # on the routing decision.
    _GLOBAL_HINTS: tuple[str, ...] = (
        "overall",
        "across",
        "in general",
        "summary",
        "summarize",
        "communities",
        "clusters",
        "themes",
        "landscape",
        "big picture",
        "in aggregate",
    )

    @classmethod
    def _is_global_question(cls, question: str) -> bool:
        """True if the question reads as graph-wide rather than entity-local."""
        if not question:
            return False
        lowered = question.lower()
        return any(hint in lowered for hint in cls._GLOBAL_HINTS)

    def _maybe_community_items(
        self,
        question: str,
        query_vec: list[float],
        traces: list[str],
    ) -> list[RetrievedItem]:
        """Vector-search community summaries when the question is global.

        Returns an empty list when no community service is wired in or when
        the question doesn't trip the global heuristic.  Failures are caught
        so a missing community index never breaks a normal retrieval call.
        """
        if self.community_service is None or not self._is_global_question(question):
            return []
        try:
            communities = self.community_service.search(query_vec, k=self.seed_k)
        except Exception as exc:  # noqa: BLE001
            traces.append(f"community_service.search failed: {exc!r}")
            return []
        traces.append(
            f"community_service.search(k={self.seed_k}) -> {len(communities)}"
        )
        out: list[RetrievedItem] = []
        for community in communities:
            # ``community`` is duck-typed: the service returns
            # :class:`Community` dataclasses but a stub may yield bare dicts.
            cid = str(getattr(community, "id", None) or community.get("id"))  # type: ignore[union-attr]
            summary = str(
                getattr(community, "summary", None)
                or (community.get("summary") if isinstance(community, dict) else "")
                or ""
            )
            size = int(
                getattr(community, "size", 0)
                or (community.get("size") if isinstance(community, dict) else 0)
                or 0
            )
            risk_exposure = float(
                getattr(community, "risk_exposure", 0.0)
                or (
                    community.get("risk_exposure")
                    if isinstance(community, dict)
                    else 0.0
                )
                or 0.0
            )
            score = float(
                getattr(community, "score", None)
                or (community.get("score") if isinstance(community, dict) else 0.0)
                or 0.0
            )
            out.append(
                RetrievedItem(
                    kind="community",
                    id=cid,
                    text=summary or f"Community {cid}",
                    score=score,
                    payload={
                        "size": size,
                        "risk_exposure": risk_exposure,
                        "kind": "community",
                    },
                )
            )
        return out

    # ------------------------------------------------------------------
    # Stage A helpers
    # ------------------------------------------------------------------

    def _seed_entities(
        self,
        query_vec: list[float],
        ontology_filter: dict[str, Any] | None,
        traces: list[str],
    ) -> list[dict[str, Any]]:
        """Pull top-k entity seeds, applying the optional ontology filter.

        When the filter specifies multiple labels we issue one vector query
        per label and merge by id (keeping the best score) so each label
        gets a fair shot at the seed budget.
        """
        labels = self._resolve_labels(ontology_filter)
        try:
            if not labels:
                rows = self.neo4j.vector_search_entities(
                    query_vec, k=self.seed_k, label_filter=None
                )
                traces.append(f"vector_search_entities(label=None, k={self.seed_k})")
                return list(rows or [])

            merged: dict[str, dict[str, Any]] = {}
            for label in labels:
                rows = self.neo4j.vector_search_entities(
                    query_vec, k=self.seed_k, label_filter=label
                )
                traces.append(
                    f"vector_search_entities(label={label!r}, k={self.seed_k})"
                )
                for row in rows or []:
                    rid = str(row.get("id") or "")
                    if not rid:
                        continue
                    prev = merged.get(rid)
                    if prev is None or float(row.get("score") or 0.0) > float(
                        prev.get("score") or 0.0
                    ):
                        merged[rid] = row
            # Keep the global top-k after merging across labels.
            ranked = sorted(
                merged.values(),
                key=lambda r: float(r.get("score") or 0.0),
                reverse=True,
            )
            return ranked[: self.seed_k]
        except Exception as exc:  # noqa: BLE001 -- degrade gracefully
            traces.append(f"vector_search_entities failed: {exc!r}")
            return []

    def _seed_chunks(
        self,
        question: str,
        collection: str,
        traces: list[str],
    ) -> list[dict[str, Any]]:
        """Pull top-k Qdrant chunks for ``question`` from ``collection``."""
        try:
            response = self.qdrant.semantic_search(
                question, collection=collection, limit=self.seed_k
            )
        except Exception as exc:  # noqa: BLE001
            traces.append(f"qdrant.semantic_search failed: {exc!r}")
            return []
        traces.append(
            f"qdrant.semantic_search(collection={collection!r}, k={self.seed_k}) -> "
            f"{'ok' if response and response.get('available') else 'unavailable'}"
        )
        if not response or not response.get("available"):
            return []
        out: list[dict[str, Any]] = []
        for result in response.get("results") or []:
            # SearchResult is a dataclass; we attribute-grab to keep this
            # decoupled from its concrete type.
            payload = getattr(result, "payload", None) or {}
            out.append(
                {
                    "id": getattr(result, "id", None),
                    "score": float(getattr(result, "score", 0.0) or 0.0),
                    "text": getattr(result, "text", "") or payload.get("text", ""),
                    "payload": dict(payload),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_labels(ontology_filter: dict[str, Any] | None) -> list[str]:
        if not ontology_filter:
            return []
        if "label" in ontology_filter and ontology_filter["label"]:
            return [str(ontology_filter["label"])]
        if "labels" in ontology_filter and ontology_filter["labels"]:
            return [str(l) for l in ontology_filter["labels"] if l]
        return []

    @staticmethod
    def _entity_text(row: dict[str, Any]) -> str:
        """Compose a short summary text for the prompt.

        The retriever does not have access to the entity's ``description``
        without a second hop, so we format what the vector index returned:
        ``type: name`` is enough for the LLM to ground a finding around.
        """
        name = row.get("name") or row.get("label") or row.get("id") or ""
        etype = row.get("type") or ""
        if etype and name:
            return f"{etype}: {name}"
        return str(name or etype or "")


__all__ = [
    "HybridRetriever",
    "HybridResult",
    "RetrievalWeights",
    "RetrievedItem",
]
