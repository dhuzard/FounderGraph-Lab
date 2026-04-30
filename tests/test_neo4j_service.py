import pytest

from app.services.neo4j_service import Neo4jService, Neo4jServiceError


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


def service():
    return Neo4jService(driver=FakeDriver(), allowed_labels={"Entity", "Company"}, allowed_relationships={"FOUNDED"})


def test_refuses_non_validated_entity_write():
    graph = service()

    with pytest.raises(Neo4jServiceError, match="non-validated"):
        graph.upsert_entity({"id": "e1", "name": "Draft Co", "label": "Company", "status": "pending"})


def test_refuses_non_whitelisted_label():
    graph = service()

    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        graph.upsert_entity({"id": "e1", "name": "Bad", "label": "BadLabel", "status": "validated"})


def test_upsert_entity_uses_parameterized_query_and_preserves_provenance():
    graph = service()
    graph.upsert_entity(
        {
            "id": "e1",
            "name": "Acme",
            "label": "Company",
            "status": "validated",
            "source_snippet": "Acme raised a seed round.",
            "provenance": {"document_id": "doc1", "offset": 42},
        }
    )

    query, params = graph.driver.calls[-1]
    assert "SET e:Company" in query
    assert "$name" in query
    assert "Acme" not in query
    assert params["source_snippet"] == "Acme raised a seed round."
    assert params["provenance_json"] == '{"document_id": "doc1", "offset": 42}'


def test_refuses_non_whitelisted_relationship_type():
    graph = service()

    with pytest.raises(Neo4jServiceError, match="Relationship type is not whitelisted"):
        graph.upsert_relation(
            {
                "id": "r1",
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "type": "OWNS; MATCH (n) DETACH DELETE n",
                "status": "validated",
            }
        )


def test_upsert_relation_uses_whitelisted_type_and_parameters():
    graph = service()
    graph.upsert_relation(
        {
            "id": "r1",
            "source_entity_id": "founder1",
            "target_entity_id": "company1",
            "type": "FOUNDED",
            "status": "validated",
            "source_snippet": "Founder started the company.",
            "provenance": {"document_id": "doc1"},
        }
    )

    query, params = graph.driver.calls[-1]
    assert "[r:FOUNDED" in query
    assert "$source_id" in query
    assert "founder1" not in query
    assert params["source_id"] == "founder1"
    assert params["target_id"] == "company1"
    assert params["source_snippet"] == "Founder started the company."
