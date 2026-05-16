"""MCP server exposing the deterministic discovery query registry.

Phase 8.3: lets an external host enumerate every Phase-2 discovery query
(names + descriptions) and execute any one of them by name.  Because the
registry is the only entrypoint, the MCP tool surface is automatically
guarded by the same ontology-alignment tests that protect the Streamlit
Discovery page.

Degrades gracefully when ``mcp`` is not installed.
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


def list_discovery_queries() -> list[dict[str, Any]]:
    """Return ``{name, title, description}`` for every registered query."""
    from app.services import discovery_queries

    return [
        {
            "name": q.name,
            "title": q.title,
            "description": q.description,
            "expected_columns": list(q.expected_columns),
        }
        for q in discovery_queries.all_queries()
    ]


def discovery_query(name: str, limit: int = 100) -> list[dict[str, Any]]:
    """Run a registered discovery query by name and return its rows."""
    from app.services import discovery_queries
    from app.services.neo4j_service import Neo4jService

    service = Neo4jService()
    try:
        return discovery_queries.run(name, service.driver, limit=limit)
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001
            pass


def serve() -> None:
    """Run the stdio MCP loop with discovery tools registered."""
    if not _MCP_AVAILABLE:
        from app.mcp import MCPUnavailableError

        raise MCPUnavailableError(
            "The 'mcp' package is required to run the Discovery MCP server. "
            "Install it with: pip install mcp"
        )

    server = Server("foundergraph-discovery")  # type: ignore[misc]

    @server.tool()  # type: ignore[misc]
    def _list_discovery_queries() -> list[dict[str, Any]]:
        return list_discovery_queries()

    @server.tool()  # type: ignore[misc]
    def _discovery_query(name: str, limit: int = 100) -> list[dict[str, Any]]:
        return discovery_query(name, limit=limit)

    import asyncio

    async def _run() -> None:
        async with stdio_server() as (read, write):  # type: ignore[misc]
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    serve()
