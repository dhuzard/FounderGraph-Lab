from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
VAULT_DIR = Path(os.getenv("VAULT_DIR", ROOT_DIR / "vault"))

ORIGINAL_FILES_DIR = DATA_DIR / "original_files"
EXTRACTED_TEXT_DIR = DATA_DIR / "extracted_text"
STAGING_DIR = DATA_DIR / "staging"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
EXPORTS_DIR = DATA_DIR / "exports"

VAULT_DOCUMENTS_DIR = VAULT_DIR / "documents"
VAULT_ENTITIES_DIR = VAULT_DIR / "entities"
VAULT_AUDITS_DIR = VAULT_DIR / "audits"

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "foundergraph_password")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.1:8b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")

# Embedding dimensionality for the Ollama embed model.  Hardcoded into the
# Neo4j vector index DDL because Neo4j does not accept a parameter for the
# ``vector.dimensions`` index option.  ``nomic-embed-text`` returns 768-d
# vectors; if a future model has a different dimension, regenerate the DDL.
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "768"))

# Phase 4 hybrid retrieval weights.  Tunable so the user can shift emphasis
# between raw vector similarity (alpha), graph proximity (beta), and how
# strongly an Evidence node grades its support (gamma).  Defaults keep
# vector seeds dominant while still letting a tight 1-hop neighbour or a
# "high"-strength Evidence node bubble up.
HYBRID_ALPHA_COSINE = float(os.getenv("HYBRID_ALPHA_COSINE", "0.6"))
HYBRID_BETA_PROXIMITY = float(os.getenv("HYBRID_BETA_PROXIMITY", "0.25"))
HYBRID_GAMMA_EVIDENCE_STRENGTH = float(os.getenv("HYBRID_GAMMA_EVIDENCE_STRENGTH", "0.15"))
HYBRID_SEED_K = int(os.getenv("HYBRID_SEED_K", "8"))
HYBRID_EXPANSION_HOPS = int(os.getenv("HYBRID_EXPANSION_HOPS", "1"))

DOCUMENTS_JSON = STAGING_DIR / "documents.json"
CANDIDATE_ENTITIES_JSON = STAGING_DIR / "candidate_entities.json"
CANDIDATE_RELATIONS_JSON = STAGING_DIR / "candidate_relations.json"
VALIDATED_ENTITIES_JSON = KNOWLEDGE_DIR / "validated_entities.json"
VALIDATED_RELATIONS_JSON = KNOWLEDGE_DIR / "validated_relations.json"


def ensure_directories() -> None:
    for path in [
        ORIGINAL_FILES_DIR,
        EXTRACTED_TEXT_DIR,
        STAGING_DIR,
        KNOWLEDGE_DIR,
        EXPORTS_DIR,
        VAULT_DOCUMENTS_DIR,
        VAULT_ENTITIES_DIR,
        VAULT_AUDITS_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def ensure_json_files() -> None:
    ensure_directories()
    defaults = {
        DOCUMENTS_JSON: [],
        CANDIDATE_ENTITIES_JSON: [],
        CANDIDATE_RELATIONS_JSON: [],
        VALIDATED_ENTITIES_JSON: [],
        VALIDATED_RELATIONS_JSON: [],
    }
    for path, value in defaults.items():
        if not path.exists():
            path.write_text("[]\n" if value == [] else str(value), encoding="utf-8")
