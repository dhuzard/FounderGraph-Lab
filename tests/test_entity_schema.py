import json

import pytest

from app.services.entity_extractor import CandidateKnowledgeEntity, CandidateKnowledgeRelation, EntityExtractor
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
    # Numeric LLM confidence is coerced to evidence_grade; the float is dropped.
    assert entities == [
        {
            "evidence_grade": "paraphrase",  # 0.95 → paraphrase
            "id": "acme-ai",
            "label": "Acme AI",
            "name": "Acme AI",
            "temporary_id": "acme-ai",
            "type": "Company",
        }
    ]
    assert relations == [
        {
            "evidence_grade": "paraphrase",  # 0.8 → paraphrase
            "object_temporary_id": "acme-ai",
            "predicate": "FOUNDED",
            "source_entity_id": "alice",
            "subject_temporary_id": "alice",
            "target_entity_id": "acme-ai",
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
