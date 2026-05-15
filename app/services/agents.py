"""Read-only founder graph agent workflows."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services.citation_verifier import (
    RetrievalContext,
    VerifiedAudit,
    build_context,
    parse_audit,
    verify,
)
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
        "model": os.getenv(
            "OLLAMA_SYNTH_MODEL",
            os.getenv("LLM_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b")),
        ),
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as resp:
            response = json.loads(resp.read().decode("utf-8"))
        return {"available": True, "text": str(response.get("response", "")).strip()}
    except urllib.error.HTTPError as exc:
        model = os.getenv("OLLAMA_SYNTH_MODEL", os.getenv("LLM_MODEL", os.getenv("OLLAMA_MODEL", "llama3.1:8b")))
        return {
            "available": False,
            "error": f"{exc}. Is Ollama model '{model}' pulled?",
            "text": "",
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc), "text": ""}


def _ontology_context() -> str:
    """Return a compact ontology summary to ground the LLM in domain vocabulary."""
    try:
        from app.services.ontology_validator import OntologyLoader
        loader = OntologyLoader()
        domain = loader._data.get("domain", "")
        goals = loader._data.get("goals", [])
        labels = sorted(loader.allowed_labels - {"Entity", "Document"})
        relations = sorted(loader.allowed_relationships)
        classes_block = loader._data.get("classes", {})
        class_lines = [
            f"  - {name}: {defn.get('description', '')} (fields: {', '.join(defn.get('fields', []))})"
            for name, defn in classes_block.items()
            if defn.get("description") or defn.get("fields")
        ]
        return (
            f"Domain: {domain}\n"
            f"Goals: {', '.join(goals)}\n"
            f"Entity classes:\n" + "\n".join(class_lines) + "\n"
            f"Relation types: {', '.join(relations)}"
        )
    except Exception:
        return ""


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
        chunk_id = payload.get("chunk_id") or payload.get("source_chunk_id") or result.id
        snippets.append(
            f"- chunk_id={chunk_id} score={result.score:.3f} source={source}: {result.text[:900]}"
        )
    return "\n".join(snippets) if snippets else "No vector snippets found."


def _snippet_dicts(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt Qdrant SearchResult objects to plain dicts for ``build_context``."""
    if not search.get("available"):
        return []
    out: list[dict[str, Any]] = []
    for result in search.get("results", []) or []:
        payload = getattr(result, "payload", None) or {}
        out.append(
            {
                "id": getattr(result, "id", None),
                "payload": payload,
            }
        )
    return out


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


def run_agent_workflow(
    slug: str,
    title: str,
    prompt_file: str,
    query: str,
    cypher: str,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if on_progress:
        on_progress({"phase": "prompt", "message": "Loading prompt and ontology context"})
    prompt = _read_prompt(prompt_file)
    ontology = _ontology_context()

    if on_progress:
        on_progress({"phase": "graph", "message": "Reading graph context from Neo4j"})
    graph = _neo4j_read(cypher)

    if on_progress:
        on_progress({"phase": "vectors", "message": "Searching Qdrant evidence snippets"})
    snippets = QdrantService().semantic_search(query, collection=DOCUMENT_COLLECTION, limit=6)

    if on_progress:
        on_progress({"phase": "synthesis", "message": "Generating audit with Ollama"})
    ontology_section = f"\nOntology schema (use these entity classes and relation types when naming findings):\n{ontology}\n" if ontology else ""
    synthesis_prompt = f"""{prompt}
{ontology_section}
Use only the graph context and evidence snippets below. Do not invent facts. When referencing entities, use the ontology class names (e.g. Assumption, Evidence, Risk, Experiment, Decision, Milestone). Cite entity ids exactly as they appear in the graph context (the values shown for ``id``, ``a.id``, ``b.id``) and chunk ids exactly as they appear in the evidence snippets (the ``chunk_id=...`` token before each snippet body).

Graph context:
{_format_rows(graph.get("rows", []))}

Evidence snippets:
{_format_snippets(snippets)}

Respond with the JSON object specified in the prompt above. Do not wrap it in Markdown fences or prose.
"""
    generated = _ollama_generate(synthesis_prompt)
    body = generated["text"] if generated.get("available") and generated.get("text") else _fallback_markdown(title, prompt, graph, snippets)

    # --- Phase 5: parse JSON output + verify citations against the retrieval context.
    context = build_context(graph.get("rows") or [], _snippet_dicts(snippets))
    parsed_json, parse_error = parse_audit(body)
    if parsed_json is not None:
        structured = verify(parsed_json, context)
    else:
        structured = VerifiedAudit(
            summary="",
            verified_findings=[],
            ungrounded_findings=[],
            raw_json=None,
            parse_error=parse_error,
        )

    if on_progress:
        on_progress({"phase": "save", "message": "Saving audit Markdown"})
    path = _save_audit(slug, title, body)
    if on_progress:
        on_progress({"phase": "done", "message": f"Saved audit to {path.name}"})
    return {
        "path": str(path),
        "graph": graph,
        "snippets": snippets,
        "ollama": generated,
        "structured": structured,
        "context": context,
    }


def pitch_audit(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
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
        on_progress=on_progress,
    )


def unsupported_assumption_audit(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
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
        on_progress=on_progress,
    )


def assumption_audit(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    return run_agent_workflow(
        slug="assumption-audit",
        title="Assumption Audit",
        prompt_file="assumption_audit.md",
        query="critical assumptions evidence confidence risk validation experiments",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "OPTIONAL MATCH (a)-[sr:SUPPORTED_BY]->(se:Entity:Evidence) "
            "OPTIONAL MATCH (a)-[cr:CONTRADICTED_BY]->(ce:Entity:Evidence) "
            "OPTIONAL MATCH (exp:Entity:Experiment)-[:TESTS]->(a) "
            "OPTIONAL MATCH (a)<-[:THREATENS]-(r:Entity:Risk) "
            "RETURN a.label AS assumption, a.criticality AS criticality, "
            "a.evidence_grade AS grade, a.reviewer_confidence AS confidence, "
            "a.validation_status AS status, "
            "collect(DISTINCT se.label) AS supporting_evidence, "
            "collect(DISTINCT ce.label) AS contradicting_evidence, "
            "collect(DISTINCT exp.label) AS experiments, "
            "collect(DISTINCT r.label) AS related_risks LIMIT 75"
        ),
        on_progress=on_progress,
    )


def customer_discovery(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
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
        on_progress=on_progress,
    )


def due_diligence_checklist(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    return run_agent_workflow(
        slug="due-diligence-checklist",
        title="Due Diligence Checklist",
        prompt_file="pitch_audit.md",
        query="due diligence unsupported assumptions missing evidence risks financial IP regulatory",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(se:Entity:Evidence) "
            "OPTIONAL MATCH (a)-[:CONTRADICTED_BY]->(ce:Entity:Evidence) "
            "WITH a, collect(DISTINCT se.label) AS support, collect(DISTINCT ce.label) AS contra "
            "OPTIONAL MATCH (r:Entity:Risk)-[:THREATENS]->(m:Entity:Milestone) "
            "OPTIONAL MATCH (ip:Entity:IPAsset) "
            "OPTIONAL MATCH (rc:Entity:RegulatoryConstraint) "
            "OPTIONAL MATCH (fh:Entity:FinancialHypothesis) "
            "RETURN a.label AS assumption, a.evidence_grade AS grade, "
            "a.criticality AS criticality, support, contra, "
            "collect(DISTINCT r.label) AS risks, "
            "collect(DISTINCT m.label) AS threatened_milestones, "
            "collect(DISTINCT ip.name) AS ip_assets, "
            "collect(DISTINCT rc.label) AS regulatory_constraints, "
            "collect(DISTINCT fh.label) AS financial_hypotheses LIMIT 60"
        ),
        on_progress=on_progress,
    )


def next_experiment_suggestions(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    return run_agent_workflow(
        slug="next-experiments",
        title="Next Experiment Suggestions",
        prompt_file="assumption_audit.md",
        query="validation experiment hypothesis success criteria unsupported assumption",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "OPTIONAL MATCH (exp:Entity:Experiment)-[:TESTS]->(a) "
            "OPTIONAL MATCH (exp)-[:GENERATES]->(ev:Entity:Evidence) "
            "OPTIONAL MATCH (a)-[:SUPPORTED_BY|CONTRADICTED_BY]->(e:Entity:Evidence) "
            "RETURN a.label AS assumption, a.criticality AS criticality, "
            "a.evidence_grade AS grade, a.reviewer_confidence AS confidence, "
            "a.description AS description, "
            "collect(DISTINCT exp.label) AS existing_experiments, "
            "collect(DISTINCT {status: exp.status, criteria: exp.success_criteria}) AS experiment_details, "
            "collect(DISTINCT ev.label) AS generated_evidence, "
            "collect(DISTINCT e.label) AS existing_evidence LIMIT 75"
        ),
        on_progress=on_progress,
    )


def grant_strategy(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
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
        on_progress=on_progress,
    )


def decision_intelligence(on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    return run_agent_workflow(
        slug="decision-intelligence",
        title="Decision Intelligence Report",
        prompt_file="decision_intelligence.md",
        query="strategic decision evidence confidence risk milestone investor readiness critical assumption",
        cypher=(
            "MATCH (a:Entity:Assumption) "
            "OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(se:Entity:Evidence) "
            "OPTIONAL MATCH (a)-[:CONTRADICTED_BY]->(ce:Entity:Evidence) "
            "OPTIONAL MATCH (exp:Entity:Experiment)-[:TESTS]->(a) "
            "OPTIONAL MATCH (r:Entity:Risk)-[:THREATENS]->(m:Entity:Milestone) "
            "OPTIONAL MATCH (d:Entity:Decision)-[:BASED_ON]->(de:Entity:Evidence) "
            "RETURN a.label AS assumption, a.criticality AS criticality, "
            "a.evidence_grade AS grade, a.reviewer_confidence AS confidence, "
            "a.validation_status AS validation_status, "
            "collect(DISTINCT se.label) AS supporting, "
            "collect(DISTINCT ce.label) AS contradicting, "
            "collect(DISTINCT exp.label) AS experiments, "
            "collect(DISTINCT {risk: r.label, milestone: m.label, probability: r.probability, impact: r.impact, mitigation: r.mitigation}) AS risk_exposure, "
            "collect(DISTINCT {decision: d.label, basis: de.label}) AS prior_decisions LIMIT 60"
        ),
        on_progress=on_progress,
    )


WORKFLOWS = {
    "Unsupported Assumption Agent": unsupported_assumption_audit,
    "Pitch Audit": pitch_audit,
    "Assumption Audit": assumption_audit,
    "Customer Discovery Agent": customer_discovery,
    "Due Diligence Checklist Agent": due_diligence_checklist,
    "Next Experiment Suggestion Agent": next_experiment_suggestions,
    "Grant Strategy": grant_strategy,
    "Decision Intelligence": decision_intelligence,
}
