"""Read-only founder graph agent workflows."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.qdrant_service import DOCUMENT_COLLECTION, QdrantService


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = _PROJECT_ROOT / "app" / "prompts"
AUDIT_DIR = Path(os.getenv("FOUNDERGRAPH_AUDIT_DIR", str(_PROJECT_ROOT / "vault" / "audits")))


def _read_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _neo4j_read(cypher: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a read-only Neo4j query if the driver is installed and configured."""

    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not uri or not password:
        return {"available": False, "error": "NEO4J_URI/NEO4J_PASSWORD not configured", "rows": []}

    try:
        from neo4j import GraphDatabase, READ_ACCESS
    except ImportError:
        return {"available": False, "error": "neo4j package is not installed", "rows": []}

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session(default_access_mode=READ_ACCESS) as session:
            rows = [record.data() for record in session.run(cypher, parameters or {})]
        driver.close()
        return {"available": True, "rows": rows}
    except Exception as exc:  # pragma: no cover - depends on external Neo4j
        return {"available": False, "error": str(exc), "rows": []}


def _ollama_generate(prompt: str) -> dict[str, Any]:
    url = f"{os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')}/api/generate"
    body = json.dumps({
        "model": os.getenv("OLLAMA_SYNTH_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1")),
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as resp:
            response = json.loads(resp.read().decode("utf-8"))
        return {"available": True, "text": str(response.get("response", "")).strip()}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc), "text": ""}


def _format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No graph rows available."
    lines = []
    for row in rows[:30]:
        lines.append("- " + "; ".join(f"{key}: {value}" for key, value in row.items()))
    return "\n".join(lines)


def _format_snippets(search: dict[str, Any]) -> str:
    if not search.get("available"):
        return f"Vector search unavailable: {search.get('error', 'unknown error')}"
    snippets = []
    for result in search.get("results", []):
        payload = result.payload
        source = payload.get("source_path") or payload.get("document_id") or result.id
        snippets.append(f"- score={result.score:.3f} source={source}: {result.text[:900]}")
    return "\n".join(snippets) if snippets else "No vector snippets found."


def _save_audit(slug: str, title: str, body: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = AUDIT_DIR / f"{timestamp}-{slug}.md"
    path.write_text(f"# {title}\n\n{body.strip()}\n", encoding="utf-8")
    return path


def _fallback_markdown(title: str, prompt: str, graph: dict[str, Any], snippets: dict[str, Any]) -> str:
    return f"""Generated without Ollama synthesis.

## Prompt
{prompt.strip() or title}

## Graph Context
{_format_rows(graph.get("rows", []))}

## Evidence Snippets
{_format_snippets(snippets)}

## Availability
- Neo4j: {"available" if graph.get("available") else graph.get("error")}
- Qdrant: {"available" if snippets.get("available") else snippets.get("error")}
"""


def run_agent_workflow(slug: str, title: str, prompt_file: str, query: str, cypher: str) -> dict[str, Any]:
    prompt = _read_prompt(prompt_file)
    graph = _neo4j_read(cypher)
    snippets = QdrantService().semantic_search(query, collection=DOCUMENT_COLLECTION, limit=6)
    synthesis_prompt = f"""{prompt}

Use only the graph context and evidence snippets below. Do not invent facts.

Graph context:
{_format_rows(graph.get("rows", []))}

Evidence snippets:
{_format_snippets(snippets)}

Return a concise Markdown audit with findings, evidence, risks, and next actions.
"""
    generated = _ollama_generate(synthesis_prompt)
    body = generated["text"] if generated.get("available") and generated.get("text") else _fallback_markdown(title, prompt, graph, snippets)
    path = _save_audit(slug, title, body)
    return {"path": str(path), "graph": graph, "snippets": snippets, "ollama": generated}


def pitch_audit() -> dict[str, Any]:
    return run_agent_workflow(
        slug="pitch-audit",
        title="Pitch Audit",
        prompt_file="pitch_audit.md",
        query="startup pitch traction market problem solution business model risks",
        cypher=(
            "MATCH (n) "
            "WHERE n:Startup OR n:Founder OR n:Market OR n:Assumption OR n:Evidence "
            "RETURN labels(n) AS labels, properties(n) AS properties LIMIT 50"
        ),
    )


def unsupported_assumption_audit() -> dict[str, Any]:
    return run_agent_workflow(
        slug="unsupported-assumptions",
        title="Unsupported Assumption Audit",
        prompt_file="assumption_audit.md",
        query="unsupported startup assumptions missing evidence validation experiments customer questions",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence) "
            "RETURN a.id AS id, a.label AS label, a.description AS description, "
            "a.evidence_grade AS evidence_grade, a.reviewer_confidence AS reviewer_confidence, "
            "a.source_file AS source_file, a.source_snippet AS source_snippet"
        ),
    )


def assumption_audit() -> dict[str, Any]:
    return run_agent_workflow(
        slug="assumption-audit",
        title="Assumption Audit",
        prompt_file="assumption_audit.md",
        query="critical assumptions evidence confidence risk validation experiments",
        cypher=(
            "MATCH (a) "
            "WHERE a:Assumption OR a:Evidence OR a:Risk "
            "RETURN labels(a) AS labels, properties(a) AS properties LIMIT 75"
        ),
    )


def customer_discovery() -> dict[str, Any]:
    return run_agent_workflow(
        slug="customer-discovery",
        title="Customer Discovery Questions",
        prompt_file="assumption_audit.md",
        query="customer segment problem buying signal interview questions discovery",
        cypher=(
            "MATCH (s:Entity:CustomerSegment)-[:HAS_PROBLEM]->(p:Entity:Problem) "
            "OPTIONAL MATCH (f:Entity:ProductFeature)-[:ADDRESSES]->(p) "
            "RETURN s.label AS segment, p.label AS problem, collect(f.label) AS linked_features"
        ),
    )


def due_diligence_checklist() -> dict[str, Any]:
    return run_agent_workflow(
        slug="due-diligence-checklist",
        title="Due Diligence Checklist",
        prompt_file="pitch_audit.md",
        query="due diligence unsupported assumptions missing evidence risks financial IP regulatory",
        cypher=(
            "MATCH (n:Entity) "
            "WHERE n:Assumption OR n:Evidence OR n:Risk OR n:FinancialHypothesis "
            "OR n:IPAsset OR n:RegulatoryConstraint OR n:Milestone "
            "RETURN labels(n) AS labels, properties(n) AS properties LIMIT 100"
        ),
    )


def next_experiment_suggestions() -> dict[str, Any]:
    return run_agent_workflow(
        slug="next-experiments",
        title="Next Experiment Suggestions",
        prompt_file="assumption_audit.md",
        query="validation experiment hypothesis success criteria unsupported assumption",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "OPTIONAL MATCH (e:Entity:Experiment)-[:TESTS]->(a) "
            "RETURN a.id AS assumption_id, a.label AS assumption, a.description AS description, "
            "collect(e.label) AS existing_experiments, a.source_snippet AS source_snippet LIMIT 75"
        ),
    )


def grant_strategy() -> dict[str, Any]:
    return run_agent_workflow(
        slug="grant-strategy",
        title="Grant Strategy",
        prompt_file="grant_strategy.md",
        query="grant strategy innovation impact eligibility budget roadmap",
        cypher=(
            "MATCH (n) "
            "WHERE n:Startup OR n:Grant OR n:Milestone OR n:Impact OR n:Market "
            "RETURN labels(n) AS labels, properties(n) AS properties LIMIT 50"
        ),
    )


WORKFLOWS = {
    "Unsupported Assumption Agent": unsupported_assumption_audit,
    "Pitch Audit": pitch_audit,
    "Assumption Audit": assumption_audit,
    "Customer Discovery Agent": customer_discovery,
    "Due Diligence Checklist Agent": due_diligence_checklist,
    "Next Experiment Suggestion Agent": next_experiment_suggestions,
    "Grant Strategy": grant_strategy,
}
