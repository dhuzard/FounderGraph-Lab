.PHONY: up down logs test lint format pull-models init init-trialmesh reset-demo demo generate generate-check

ifeq ($(OS),Windows_NT)
PYTHON ?= py -3
else
PYTHON ?= uv run python
endif

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	$(PYTHON) -m pytest

lint:
	ruff check .

format:
	ruff format .

pull-models:
	docker exec foundergraph_lab_ollama ollama pull llama3.1:8b
	docker exec foundergraph_lab_ollama ollama pull nomic-embed-text

init:
	$(PYTHON) scripts/init_ontology.py

init-trialmesh:
	$(PYTHON) scripts/init_ontology.py --demo trialmesh

reset-demo:
	$(PYTHON) scripts/reset_demo_state.py

demo:
	$(PYTHON) scripts/load_contradictory_demo.py

# Regenerate Pydantic models / JSON-Schema / SHACL shapes / Cypher DDL from
# the LinkML source of truth (app/ontology/startup_ontology.linkml.yaml).
generate:
	$(PYTHON) scripts/generate_ontology_artifacts.py

# CI / pre-commit drift guard: regenerate into a temp dir and diff against
# the committed app/ontology/generated/ tree.  Exits non-zero on drift.
generate-check:
	$(PYTHON) scripts/generate_ontology_artifacts.py --check
