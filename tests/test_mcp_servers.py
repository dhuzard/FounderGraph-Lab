"""Tests for the Phase 8 MCP server stubs.

The three MCP servers under ``app/mcp/`` are intentionally importable
even when the ``mcp`` SDK is not installed.  These tests check the
degraded-mode contract:

* importing each module succeeds without requiring ``mcp``;
* every module exposes a callable ``serve()`` entrypoint;
* when the SDK is absent, calling ``serve()`` raises
  :class:`MCPUnavailableError` with a helpful install hint;
* :func:`list_discovery_queries` round-trips the registry without
  touching Neo4j.
"""

from __future__ import annotations

import pytest

from app import mcp as mcp_pkg
from app.mcp import discovery_server, neo4j_server, qdrant_server


def test_servers_expose_callable_serve():
    for module in (neo4j_server, qdrant_server, discovery_server):
        assert callable(module.serve), f"{module.__name__}.serve must be callable"


def test_serve_raises_helpful_error_when_mcp_absent():
    """When the SDK is unavailable, ``serve()`` must raise our typed error."""
    for module in (neo4j_server, qdrant_server, discovery_server):
        if module._MCP_AVAILABLE:
            # Real SDK installed — ``serve()`` would launch stdio; skip it.
            continue
        with pytest.raises(mcp_pkg.MCPUnavailableError) as excinfo:
            module.serve()
        assert "pip install mcp" in str(excinfo.value)


def test_list_discovery_queries_returns_registry_view():
    rows = discovery_server.list_discovery_queries()
    assert rows, "Discovery registry must not be empty"
    names = {row["name"] for row in rows}
    # A handful of Phase-2 queries every shipped registry guarantees.
    for required in ("unsupported_assumptions", "orphan_segments"):
        assert required in names
    for row in rows:
        assert {"name", "title", "description", "expected_columns"} <= set(row)


def test_mcp_unavailable_error_is_runtime_error_subclass():
    assert issubclass(mcp_pkg.MCPUnavailableError, RuntimeError)
