.PHONY: up down logs test lint format pull-models init

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	PYTHONPATH=. python3 -m pytest

lint:
	ruff check .

format:
	ruff format .

pull-models:
	docker exec -it foundergraph_ollama ollama pull llama3.1:8b
	docker exec -it foundergraph_ollama ollama pull nomic-embed-text

init:
	PYTHONPATH=. python3 scripts/init_ontology.py
