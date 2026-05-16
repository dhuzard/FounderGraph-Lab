"""MCP server exposing Qdrant semantic search.

Phase 8.3: a single ``semantic_search`` tool wraps
``QdrantService.semantic_search`` so external MCP hosts can fetch
chunk-level evidence without re-implementing the embedding pipeline.

Degrades gracefully when ``mcp`` is not installed: importing the module
succeeds but :func:`serve` raises :class:`app.mcp.MCPUnavailableError`
with an install hint.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised only when MCP is installed
    from mcp.server.lowlevel import Server  # type: ignore
    from mcp.server.stdio import stdio_server  # type: ignore

    _MCP_AVAILABLE = True
except Exception:  # noqa: BLE001
    Server = None  # type: ignore[assignment]
    stdio_server = None  # type: ignore[assignment]
    _MCP_AVAILABLE = False


def semantic_search(
    query: str,
    collection: str = "startup_documents",
    limit: int = 6,
) -> dict[str, Any]:
    """Run a vector search over a Qdrant collection.

    Returns the raw ``QdrantService.semantic_search`` payload (an
    ``available`` flag plus a list of ``SearchResult``-shaped dicts).
    """
    from app.services.qdrant_service import QdrantService

    service = QdrantService()
    return service.semantic_search(query, collection=collection, limit=limit)


def serve() -> None:
    """Run the stdio MCP loop with the semantic-search tool registered."""
    if not _MCP_AVAILABLE:
        from app.mcp import MCPUnavailableError

        raise MCPUnavailableError(
            "The 'mcp' package is required to run the Qdrant MCP server. "
            "Install it with: pip install mcp"
        )

    server = Server("foundergraph-qdrant")  # type: ignore[misc]

    @server.tool()  # type: ignore[misc]
    def _semantic_search(
        query: str,
        collection: str = "startup_documents",
        limit: int = 6,
    ) -> dict[str, Any]:
        return semantic_search(query, collection=collection, limit=limit)

    import asyncio

    async def _run() -> None:
        async with stdio_server() as (read, write):  # type: ignore[misc]
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    serve()
