"""MCP (Model Context Protocol) server stubs for FounderGraph-Lab.

Phase 8.3 of the GraphRAG upgrade: expose three stdio MCP servers so an
external host (Claude Desktop, an IDE, or a custom client) can call into
the validated graph, the Qdrant vector store, and the deterministic
discovery query registry.

Each server module is intentionally lazy-imported: when ``mcp`` is not
installed, calling ``serve()`` raises :class:`MCPUnavailableError` with a
helpful install hint instead of crashing at import time.  This keeps the
test suite portable on environments that do not have the MCP SDK.
"""

from __future__ import annotations


class MCPUnavailableError(RuntimeError):
    """Raised when the ``mcp`` package is not installed.

    Surface ``pip install mcp`` to the caller; the message is the only
    contract these stubs make when the SDK is absent.
    """


from app.mcp import discovery_server, neo4j_server, qdrant_server  # noqa: E402

__all__ = [
    "MCPUnavailableError",
    "discovery_server",
    "neo4j_server",
    "qdrant_server",
]
