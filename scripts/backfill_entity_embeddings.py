#!/usr/bin/env python3
"""Backfill summary embeddings on validated ``(:Entity)`` nodes.

Phase 4: the hybrid retriever needs every validated entity to carry a
vector on its ``embedding`` property so ``db.index.vector.queryNodes`` can
return seed candidates.  This script runs the embedding pass once and is
fully resumable -- entities that already have an ``embedding`` property
are skipped.

Usage::

    python scripts/backfill_entity_embeddings.py [--batch-size N] [--dry-run]

The script reuses ``QdrantService.embed`` (Ollama under the hood) so the
vectors are byte-identical to what the runtime retrieval will produce.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any


DEFAULT_BATCH_SIZE = 50


def _compose_summary(entity: dict[str, Any]) -> str:
    """Compose a short summary string used as the embedding input.

    The shape mirrors the Phase 1 entity Pydantic model: a type prefix (so
    semantically similar names but different ontology types stay apart),
    the human-readable name, and the description (truncated to keep the
    embedding request small).
    """
    etype = str(entity.get("type") or entity.get("label") or "Entity")
    name = str(entity.get("name") or entity.get("label") or entity.get("id") or "")
    description = str(entity.get("description") or "")
    # Cap description length -- Ollama handles long inputs but we want
    # consistent vectors across re-runs, and the description tends to be
    # the noisiest field in the source.
    if len(description) > 1200:
        description = description[:1200] + "..."
    return f"{etype}: {name}. {description}".strip()


def _list_entities_without_embedding(driver, batch_size: int) -> list[dict[str, Any]]:
    """Return up to ``batch_size`` validated entities missing an embedding.

    The script paginates by repeatedly asking for the next chunk; ``SKIP``
    is unnecessary because we re-query after each batch and the WHERE
    clause naturally excludes the already-embedded entities.
    """
    cypher = """
    MATCH (e:Entity)
    WHERE e.embedding IS NULL
      AND (e.validation_status = 'validated' OR e.status = 'validated')
    RETURN e.id AS id, e.name AS name, e.label AS label, e.type AS type,
           e.description AS description
    LIMIT $limit
    """
    with driver.session() as session:
        return [dict(row) for row in session.run(cypher, {"limit": int(batch_size)})]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Entities per batch (default {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List entities that would be embedded but do not write to Neo4j.",
    )
    parser.add_argument(
        "--max-entities",
        type=int,
        default=10_000,
        help="Safety cap on the total number of entities to embed.",
    )
    args = parser.parse_args(argv)

    if not os.getenv("NEO4J_URI") or not os.getenv("NEO4J_PASSWORD"):
        print(
            "error: NEO4J_URI and NEO4J_PASSWORD must be set in the environment.",
            file=sys.stderr,
        )
        return 2

    # Local imports so ``--help`` works without the runtime deps installed.
    from app.services.neo4j_service import Neo4jConfig, Neo4jService
    from app.services.qdrant_service import QdrantService, VectorServiceUnavailable

    cfg = Neo4jConfig.from_env()
    graph = Neo4jService(config=cfg)
    qdrant = QdrantService()

    total_embedded = 0
    total_skipped = 0
    batch_no = 0
    while total_embedded + total_skipped < args.max_entities:
        batch_no += 1
        batch = _list_entities_without_embedding(graph.driver, args.batch_size)
        if not batch:
            break
        print(
            f"[batch {batch_no}] {len(batch)} entities to embed "
            f"(total embedded so far: {total_embedded})"
        )
        for entity in batch:
            entity_id = entity.get("id")
            if not entity_id:
                total_skipped += 1
                continue
            summary = _compose_summary(entity)
            if not summary:
                total_skipped += 1
                continue
            try:
                vector = qdrant.embed(summary)
            except VectorServiceUnavailable as exc:
                print(
                    f"  ! embedding service unavailable ({exc}); aborting.",
                    file=sys.stderr,
                )
                graph.close()
                return 1
            if args.dry_run:
                print(f"  - would embed {entity_id!r} (summary: {summary[:80]!r})")
            else:
                graph.upsert_entity_embedding(str(entity_id), vector)
            total_embedded += 1
        # Guard against an infinite loop if the WHERE clause never narrows
        # (e.g. a write failure leaves the embedding NULL forever).
        if args.dry_run:
            break

    graph.close()
    print(
        f"done: embedded={total_embedded} skipped={total_skipped} "
        f"(dry_run={args.dry_run})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
