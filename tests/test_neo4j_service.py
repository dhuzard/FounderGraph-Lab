import pytest

from app.services.neo4j_service import Neo4jServiceError


def test_refuses_non_validated_entity_write(fake_neo4j_service):
    with pytest.raises(Neo4jServiceError, match="non-validated"):
        fake_neo4j_service.upsert_entity({"id": "e1", "name": "Draft Co", "label": "Company", "status": "pending"})


def test_refuses_non_whitelisted_label(fake_neo4j_service):
    with pytest.raises(Neo4jServiceError, match="Label is not whitelisted"):
        fake_neo4j_service.upsert_entity({"id": "e1", "name": "Bad", "label": "BadLabel", "status": "validated"})


def test_upsert_entity_uses_parameterized_query_and_preserves_provenance(fake_neo4j_service):
    fake_neo4j_service.upsert_entity(
        {
            "id": "e1",
            "name": "Acme",
            "label": "Company",
            "status": "validated",
            "source_snippet": "Acme raised a seed round.",
            "provenance": {"document_id": "doc1", "offset": 42},
        }
    )

    query, params = fake_neo4j_service.driver.calls[-1]
    assert "SET e:Company" in query
    assert "$name" in query
    assert "Acme" not in query
    assert params["source_snippet"] == "Acme raised a seed round."
    assert params["provenance_json"] == '{"document_id": "doc1", "offset": 42}'


def test_refuses_non_whitelisted_relationship_type(fake_neo4j_service):
    with pytest.raises(Neo4jServiceError, match="Relationship type is not whitelisted"):
        fake_neo4j_service.upsert_relation(
            {
                "id": "r1",
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "type": "OWNS; MATCH (n) DETACH DELETE n",
                "status": "validated",
            }
        )


def test_upsert_relation_uses_whitelisted_type_and_parameters(fake_neo4j_service):
    fake_neo4j_service.upsert_relation(
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

    query, params = fake_neo4j_service.driver.calls[-1]
    assert "[r:FOUNDED" in query
    assert "$source_id" in query
    assert "founder1" not in query
    assert params["source_id"] == "founder1"
    assert params["target_id"] == "company1"
    assert params["source_snippet"] == "Founder started the company."
