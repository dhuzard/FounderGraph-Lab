.PHONY: up down logs test lint format pull-models init init-trialmesh reset-demo

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
