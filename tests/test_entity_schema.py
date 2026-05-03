import json

import pytest
from pydantic import ValidationError

from app.services.entity_extractor import (
    CandidateKnowledgeEntity,
    CandidateKnowledgeRelation,
    EntityExtractor,
    _dump_model,
    stable_entity_id,
)
from app.services.llm_service import LLMInvalidJSONError


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_json(self, prompt):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_candidate_entity_schema_accepts_minimal_valid_entity():
    entity = CandidateKnowledgeEntity.model_validate(
        {
            "id": "acme-ai",
            "name": "Acme AI",
            "type": "Company",
            "confidence": 0.91,
        }
    )

    assert entity.id == "acme-ai"
    assert entity.properties == {}


def test_candidate_relation_schema_rejects_missing_target():
    with pytest.raises(Exception):
        CandidateKnowledgeRelation.model_validate(
            {
                "source_entity_id": "founder",
                "type": "FOUNDED",
            }
        )


def test_extractor_writes_strict_json_staging_files(tmp_path):
    llm = FakeLLM(
        [
            {
                "is_startup_document": True,
                "document_type": "startup_profile",
                "confidence": 0.9,
                "rationale": "mentions funding and founder",
            },
            {
                "entities": [
                    {
                        "id": "acme-ai",
                        "name": "Acme AI",
                        "type": "Company",
                        "confidence": 0.95,
                    }
                ]
            },
            {
                "relations": [
                    {
                        "source_entity_id": "alice",
                        "target_entity_id": "acme-ai",
                        "type": "FOUNDED",
                        "confidence": 0.8,
                    }
                ]
            },
        ]
    )
    extractor = EntityExtractor(llm_service=llm, staging_dir=tmp_path)

    result = extractor.extract_to_staging("Alice founded Acme AI.", {"source_document": "doc-1"})

    assert result.wrote_files is True
    entities = json.loads((tmp_path / "candidate_entities.json").read_text())
    relations = json.loads((tmp_path / "candidate_relations.json").read_text())
    # Entity ID is now a stable UUIDv5 keyed on (doc_id, normalised_label, type).
    # doc_id is resolved from metadata key "source_document" = "doc-1".
    expected_entity_id = stable_entity_id("doc-1", "Acme AI", "Company")
    # Relations whose endpoints are TMP-xxx get their IDs replaced with the
    # stable UUIDs; "alice" has no matching entity so it stays as-is.
    # source_document_id is propagated from the doc_id resolved at extraction time.
    assert entities == [
        {
            "evidence_grade": "paraphrase",  # 0.95 → paraphrase
            "id": expected_entity_id,
            "label": "Acme AI",
            "name": "Acme AI",
            "source_document_id": "doc-1",
            "temporary_id": expected_entity_id,
            "type": "Company",
        }
    ]
    assert relations == [
        {
            "evidence_grade": "paraphrase",  # 0.8 → paraphrase
            "object_temporary_id": expected_entity_id,
            "predicate": "FOUNDED",
            "source_document_id": "doc-1",
            "source_entity_id": "alice",
            "subject_temporary_id": "alice",
            "target_entity_id": expected_entity_id,
            "type": "FOUNDED",
        }
    ]


def test_ontology_entity_and_relation_shapes_are_supported(tmp_path):
    llm = FakeLLM(
        [
            {
                "document_type": "PitchDeck",
                "secondary_types": [],
                "summary": "Metadata interoperability startup.",
                "tags": ["metadata"],
                "confidence": "high",
            },
            {
                "entities": [
                    {
                        "temporary_id": "TMP-001",
                        "type": "Assumption",
                        "label": "CROs will pay for metadata interoperability",
                        "description": "The startup assumes CROs will pay for integration.",
                        "source_snippet": "CROs need metadata interoperability.",
                        "confidence": "medium",
                        "tags": ["pricing"],
                    }
                ]
            },
            {
                "relations": [
                    {
                        "subject_temporary_id": "TMP-001",
                        "predicate": "SUPPORTED_BY",
                        "object_temporary_id": "TMP-002",
                        "source_snippet": "Interview evidence supports the need.",
                        "confidence": "low",
                    }
                ]
            },
        ]
    )
    extractor = EntityExtractor(llm_service=llm, staging_dir=tmp_path)

    result = extractor.extract_to_staging("CROs need metadata interoperability.")

    assert result.classification.document_type == "PitchDeck"
    assert result.entities[0].label == "CROs will pay for metadata interoperability"
    assert result.relations[0].predicate == "SUPPORTED_BY"


def test_invalid_json_response_does_not_write_staging_files(tmp_path):
    llm = FakeLLM([LLMInvalidJSONError("bad json")])
    extractor = EntityExtractor(llm_service=llm, staging_dir=tmp_path)

    with pytest.raises(LLMInvalidJSONError):
        extractor.extract_to_staging("not json")

    assert not (tmp_path / "candidate_entities.json").exists()
    assert not (tmp_path / "candidate_relations.json").exists()


def test_source_document_id_populated_in_staging(tmp_path):
    """extract_to_staging must write source_document_id on every entity and
    relation so that Neo4j can create MENTIONS provenance links."""
    llm = FakeLLM(
        [
            {
                "document_type": "PitchDeck",
                "secondary_types": [],
                "summary": "",
                "tags": [],
                "confidence": "high",
            },
            {
                "entities": [
                    {
                        "temporary_id": "TMP-1",
                        "type": "Assumption",
                        "label": "Users will pay for this",
                        "description": "Pricing assumption",
                        "source_snippet": "Survey results show willingness to pay.",
                        "evidence_grade": "paraphrase",
                    }
                ]
            },
            {
                "relations": []
            },
        ]
    )
    extractor = EntityExtractor(llm_service=llm, staging_dir=tmp_path)
    extractor.extract_to_staging("Some text.", {"source_document_id": "doc-42"})

    entities = json.loads((tmp_path / "candidate_entities.json").read_text())
    assert len(entities) == 1
    assert entities[0]["source_document_id"] == "doc-42", (
        "source_document_id must be written to staging so Neo4j can create "
        "the Document→Entity MENTIONS link at write time"
    )


def test_reviewer_comment_on_relation_model():
    """CandidateKnowledgeRelation must accept reviewer_comment and preserve it
    through _dump_model so the validation UI can save reviewer notes on relations."""
    rel = CandidateKnowledgeRelation.model_validate({
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "RELATED_TO",
        "reviewer_comment": "Verified against the pitch deck transcript.",
    })
    assert rel.reviewer_comment == "Verified against the pitch deck transcript."
    dumped = _dump_model(rel)
    assert dumped.get("reviewer_comment") == "Verified against the pitch deck transcript.", (
        "reviewer_comment must survive _dump_model so it is preserved in staging JSON"
    )


# ---------------------------------------------------------------------------
# Stage 2A hardening invariants (Patch Set 3)
# ---------------------------------------------------------------------------

def test_candidate_models_use_source_document_id_not_source_document():
    """Both candidate models must reject the deprecated source_document field.
    The canonical field is source_document_id; extra='forbid' enforces this."""
    with pytest.raises(ValidationError):
        CandidateKnowledgeEntity.model_validate({
            "id": "e1",
            "name": "Test",
            "type": "Company",
            "source_document": "doc-1",
        })

    with pytest.raises(ValidationError):
        CandidateKnowledgeRelation.model_validate({
            "source_entity_id": "e1",
            "target_entity_id": "e2",
            "type": "FOUNDED",
            "source_document": "doc-1",
        })


def test_candidate_relation_accepts_reviewer_comment():
    """CandidateKnowledgeRelation must accept and store reviewer_comment."""
    rel = CandidateKnowledgeRelation.model_validate({
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "FOUNDED",
        "reviewer_comment": "Directional correctness confirmed.",
    })
    assert rel.reviewer_comment == "Directional correctness confirmed."
    assert rel.source_document_id is None


def test_no_active_confidence_field_in_candidate_models():
    """Numeric LLM confidence must be converted to evidence_grade and the
    confidence field must be set to None — it is not an active evidence field."""
    entity = CandidateKnowledgeEntity.model_validate({
        "id": "e1",
        "name": "Test",
        "type": "Company",
        "confidence": 0.95,
    })
    assert entity.confidence is None, (
        "Numeric confidence must be discarded after conversion to evidence_grade"
    )
    assert entity.evidence_grade == "paraphrase", (
        "0.95 confidence must map to 'paraphrase' evidence_grade"
    )

    relation = CandidateKnowledgeRelation.model_validate({
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "type": "FOUNDED",
        "confidence": 0.3,
    })
    assert relation.confidence is None, (
        "Numeric confidence must be discarded on relations too"
    )
    assert relation.evidence_grade == "speculation"
