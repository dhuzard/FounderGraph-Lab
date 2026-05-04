.PHONY: up down logs test lint format pull-models init reset-demo

PYTHON ?= python

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

reset-demo:
	$(PYTHON) scripts/reset_demo_state.py
