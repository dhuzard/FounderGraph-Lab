"""MCP server exposing read-only Neo4j tools.

Phase 8.3: surface ``query_graph`` and ``get_unsupported_assumptions`` as
MCP tools so external hosts can pull validated graph context without
embedding a Neo4j driver themselves.  All queries run through a
``READ_ACCESS`` session — the same ontology and safety guarantees as the
Streamlit pages.

This module intentionally degrades gracefully:

* If ``mcp`` is not installed, importing the module succeeds but calling
  :func:`serve` raises :class:`app.mcp.MCPUnavailableError` with an
  install hint.
* Tool implementations lazy-import :mod:`app.services.neo4j_service` so
  the MCP layer never forces a Neo4j connection at import time.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised only when the MCP SDK is installed
    from mcp.server.lowlevel import Server  # type: ignore
    from mcp.server.stdio import stdio_server  # type: ignore

    _MCP_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import error means MCP is unavailable
    Server = None  # type: ignore[assignment]
    stdio_server = None  # type: ignore[assignment]
    _MCP_AVAILABLE = False


def query_graph(cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run ``cypher`` against Neo4j in READ_ACCESS mode.

    The caller is responsible for ensuring the query is read-only and
    obeys ontology guardrails — typically by routing through the
    text-to-Cypher planner before invoking this tool.
    """
    from neo4j import READ_ACCESS

    from app.services.neo4j_service import Neo4jService

    service = Neo4jService()
    try:
        with service.driver.session(default_access_mode=READ_ACCESS) as session:
            result = session.run(cypher, params or {})
            rows: list[dict[str, Any]] = []
            for record in result:
                try:
                    rows.append(dict(record))
                except (TypeError, ValueError):
                    data_fn = getattr(record, "data", None)
                    rows.append(data_fn() if callable(data_fn) else dict(record))
            return rows
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def get_unsupported_assumptions() -> list[dict[str, Any]]:
    """Return rows from the ``unsupported_assumptions`` discovery query."""
    from app.services import discovery_queries
    from app.services.neo4j_service import Neo4jService

    service = Neo4jService()
    try:
        return discovery_queries.run("unsupported_assumptions", service.driver)
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001
            pass


def serve() -> None:
    """Run the stdio MCP loop, registering the read-only graph tools.

    Raises :class:`app.mcp.MCPUnavailableError` when the SDK is missing.
    """
    if not _MCP_AVAILABLE:
        from app.mcp import MCPUnavailableError

        raise MCPUnavailableError(
            "The 'mcp' package is required to run the Neo4j MCP server. "
            "Install it with: pip install mcp"
        )

    server = Server("foundergraph-neo4j")  # type: ignore[misc]

    @server.tool()  # type: ignore[misc]
    def _query_graph(cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return query_graph(cypher, params)

    @server.tool()  # type: ignore[misc]
    def _get_unsupported_assumptions() -> list[dict[str, Any]]:
        return get_unsupported_assumptions()

    import asyncio

    async def _run() -> None:
        async with stdio_server() as (read, write):  # type: ignore[misc]
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - manual launch only
    serve()
