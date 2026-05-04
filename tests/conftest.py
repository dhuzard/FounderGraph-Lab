"""Shared pytest fixtures and stubs for FounderGraph-Lab tests."""

from __future__ import annotations

import pytest

from app.services.neo4j_service import Neo4jService


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic LLM stub — pops pre-loaded responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)

    def generate_json(self, prompt):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Neo4j stubs
# ---------------------------------------------------------------------------

class FakeResult(list):
    pass


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params=None):
        self.calls.append((query, params or {}))
        return FakeResult()


class FakeDriver:
    def __init__(self):
        self.calls = []

    def session(self, **kwargs):
        self.calls.append(("session", kwargs))
        return FakeSession(self.calls)

    def close(self):
        self.calls.append(("close", {}))


@pytest.fixture()
def fake_neo4j_service():
    """Neo4jService backed by an in-memory FakeDriver."""
    return Neo4jService(
        driver=FakeDriver(),
        allowed_labels={"Entity", "Company"},
        allowed_relationships={"FOUNDED"},
    )
