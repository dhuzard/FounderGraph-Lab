from __future__ import annotations

import os
from pathlib import Path

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.agents import AUDIT_DIR, WORKFLOWS
from app.services.citation_verifier import Finding, VerifiedAudit
from app.services.qdrant_service import index_startup_knowledge


def _finding_row(finding: Finding) -> dict[str, object]:
    return {
        "claim": finding.claim,
        "severity": finding.severity,
        "confidence": round(float(finding.confidence), 3),
        "verified": "✓" if finding.verified else "⚠ ungrounded",
        "entity_ids": ", ".join(finding.evidence_entity_ids),
        "chunk_ids": ", ".join(finding.source_chunk_ids),
        "missing_ids": ", ".join(finding.ungrounded_ids),
    }


def _render_verified_audit(structured: VerifiedAudit | None) -> None:
    """Render the structured Phase-5 audit alongside the legacy Markdown view."""
    if structured is None:
        return

    if structured.parse_error:
        st.warning(
            "LLM output not in JSON format — showing legacy Markdown only. "
            f"Parser said: {structured.parse_error}"
        )
        return

    if structured.summary:
        st.markdown("### Summary")
        st.write(structured.summary)

    verified = structured.verified_findings or []
    ungrounded = structured.ungrounded_findings or []
    total = len(verified) + len(ungrounded)

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Findings", total)
    metric_col2.metric("Verified", len(verified))
    metric_col3.metric("Ungrounded", len(ungrounded))

    if total == 0:
        st.info("No findings returned by the model.")
        return

    st.markdown("### Findings")
    rows = [_finding_row(f) for f in verified] + [_finding_row(f) for f in ungrounded]
    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "claim": st.column_config.TextColumn("Claim", width="large"),
            "severity": st.column_config.TextColumn("Severity"),
            "confidence": st.column_config.NumberColumn("Confidence", format="%.2f"),
            "verified": st.column_config.TextColumn(
                "Verified",
                help="Verified = every cited id is present in the retrieved context.",
            ),
            "entity_ids": st.column_config.TextColumn("Entity citations"),
            "chunk_ids": st.column_config.TextColumn("Chunk citations"),
            "missing_ids": st.column_config.TextColumn(
                "Missing ids",
                help="Cited ids that were NOT in the retrieval context.",
            ),
        },
    )

    if ungrounded:
        with st.expander(f"⚠ {len(ungrounded)} ungrounded finding(s) — details", expanded=False):
            for finding in ungrounded:
                missing = ", ".join(finding.ungrounded_ids) or "(no citations supplied)"
                st.markdown(
                    f"- **{finding.claim}** — missing: `{missing}`"
                )


def _list_previous_audits(slug: str) -> list[Path]:
    """Return previous audit files matching the slug, newest first."""
    if not AUDIT_DIR.exists():
        return []
    return sorted(AUDIT_DIR.glob(f"*-{slug}.md"), reverse=True)


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="FounderGraph-Lab Agents", layout="wide")
    st.title("Agent Audits")
    st.caption("Read-only workflows combine Neo4j context, Qdrant snippets, and Ollama synthesis when available.")

    # --- Agent catalog ---
    _WORKFLOW_CATALOG = [
        {
            "name": "Decision Intelligence",
            "when": "Need a confidence-weighted decision brief before a board meeting, investor call, or pivot decision.",
            "outputs": "Confidence matrix table · Critical gaps ordered by severity · Recommended decisions with evidence basis · Red flags",
            "graph_focus": "Assumption → Evidence, Risk → Milestone, Decision → Evidence (multi-hop)",
        },
        {
            "name": "Unsupported Assumption Agent",
            "when": "Preparing for an investor meeting and want to know which assumptions have zero evidence.",
            "outputs": "List of Assumptions with no SUPPORTED_BY links · Evidence grade · Reviewer confidence",
            "graph_focus": "Assumption nodes only where SUPPORTED_BY count = 0",
        },
        {
            "name": "Assumption Audit",
            "when": "Full review of all assumptions with their supporting and contradicting evidence.",
            "outputs": "Evidence matrix · Confidence changes · Validation experiments · Decision risks",
            "graph_focus": "Assumption → Evidence (both directions) + Experiment → Assumption + Risk → Assumption",
        },
        {
            "name": "Next Experiment Suggestion Agent",
            "when": "Planning your next sprint of validation work and want to prioritise by impact.",
            "outputs": "Assumption gaps · Existing experiment status · Suggested new experiments with success criteria",
            "graph_focus": "Assumption + Experiment → GENERATES → Evidence chains",
        },
        {
            "name": "Due Diligence Checklist Agent",
            "when": "Preparing a data room or responding to investor due diligence requests.",
            "outputs": "IP coverage · Regulatory constraints · Financial hypotheses · Assumption coverage by evidence grade",
            "graph_focus": "Assumption + Risk → Milestone + IPAsset + RegulatoryConstraint + FinancialHypothesis",
        },
        {
            "name": "Pitch Audit",
            "when": "Reviewing pitch narrative for clarity, evidence, and investor-readiness.",
            "outputs": "Executive readout · Strengths · Gaps and contradictions · Evidence-backed recommendations",
            "graph_focus": "Startup + Founder + Market + Assumption + Evidence",
        },
        {
            "name": "Customer Discovery Agent",
            "when": "Planning customer interviews or auditing problem-solution fit.",
            "outputs": "Segment × Problem matrix · Unaddressed problems · Suggested interview questions",
            "graph_focus": "CustomerSegment → HAS_PROBLEM → Problem ← ADDRESSES ← ProductFeature",
        },
        {
            "name": "Grant Strategy",
            "when": "Identifying non-dilutive funding and mapping venture evidence to grant narratives.",
            "outputs": "Best-fit grant angles · Evidence to reuse · Claims needing substantiation · Application actions",
            "graph_focus": "Startup + Grant + Milestone + Impact + Market",
        },
    ]

    with st.expander("Which agent should I run? (agent catalog)", expanded=False):
        st.markdown(
            "Each workflow combines **Neo4j graph context** (multi-hop Cypher), "
            "**Qdrant vector snippets** (semantic search over your documents), and "
            "**Ollama synthesis** grounded in your ontology schema."
        )
        st.markdown("**Recommended sequence:** Unsupported Assumption → Next Experiment → Assumption Audit → Decision Intelligence → Pitch/Due Diligence → Grant Strategy")
        st.divider()
        for agent in _WORKFLOW_CATALOG:
            col_a, col_b = st.columns([1, 2])
            with col_a:
                st.markdown(f"**{agent['name']}**")
                st.caption(f"Graph focus: {agent['graph_focus']}")
            with col_b:
                st.markdown(f"**Use when:** {agent['when']}")
                st.markdown(f"**Outputs:** {agent['outputs']}")
            st.divider()

    col_left, col_right = st.columns([2, 1])

    with col_left:
        workflow_name = st.selectbox("Workflow", list(WORKFLOWS))

    with col_right:
        if st.button("Index vault in Qdrant"):
            progress = st.progress(0, text="Preparing Qdrant indexing...")
            current_file = st.empty()

            def _show_progress(event: dict) -> None:
                total = max(int(event.get("total") or 0), 1)
                index = int(event.get("index") or 0)
                phase = str(event.get("phase") or "documents")
                path = str(event.get("path") or "")
                progress.progress(
                    min(index / total, 1.0),
                    text=f"Indexing {phase}: {index}/{total}",
                )
                result = event.get("result") or {}
                indexed = result.get("indexed")
                if indexed is None:
                    current_file.info(f"Embedding `{path}`")
                else:
                    current_file.info(f"Indexed `{path}` ({indexed} chunk(s))")

            with st.status("Indexing Markdown vault in Qdrant...", expanded=True) as indexing_status:
                st.write("Embedding documents with Ollama and upserting chunks to Qdrant.")
                status = index_startup_knowledge("/app", on_progress=_show_progress)
                indexing_status.update(label="Qdrant indexing finished", state="complete")

            document_results = status.get("documents", [])
            indexed = sum(item.get("indexed", 0) for item in document_results)
            unavailable = [item for item in document_results if not item.get("available", True)]
            if unavailable:
                st.warning(f"Indexed {indexed} chunks before vector indexing became unavailable.")
                st.caption(unavailable[0].get("error", "Unknown vector indexing error"))
            else:
                st.success(f"Indexed {indexed} document chunks.")
            progress.empty()

    # Workflow description hints
    _WORKFLOW_HINTS = {
        "Decision Intelligence": "Cross-domain synthesis — maps evidence confidence to concrete decisions with risk-weighted prioritization.",
        "Unsupported Assumption Agent": "Finds Assumptions with no linked Evidence node — your highest-priority validation gaps.",
        "Assumption Audit": "Full assumption map: supporting vs. contradicting evidence, confidence grades, and linked experiments.",
        "Next Experiment Suggestion Agent": "Surfaces the experiments most likely to move the needle on critical unsupported assumptions.",
        "Due Diligence Checklist Agent": "Investor-readiness view: IP, regulatory, financial hypotheses, and assumption coverage.",
        "Pitch Audit": "Narrative coherence check: strengths, gaps, contradictions, and evidence-backed recommendations.",
        "Customer Discovery Agent": "Segment → Problem → Feature paths, revealing discovery questions and unaddressed pain points.",
        "Grant Strategy": "Maps venture evidence to grant narratives and identifies claims needing substantiation.",
    }
    hint = _WORKFLOW_HINTS.get(workflow_name, "")
    if hint:
        st.info(hint)

    run_col, _ = st.columns([1, 3])
    with run_col:
        run_clicked = st.button("Run workflow", type="primary", use_container_width=True)

    if run_clicked:
        phase_steps = {
            "prompt": 0.10,
            "graph": 0.30,
            "vectors": 0.50,
            "synthesis": 0.78,
            "save": 0.92,
            "done": 1.0,
        }
        workflow_progress = st.progress(0, text="Preparing workflow...")
        workflow_detail = st.empty()

        def _show_workflow_progress(event: dict) -> None:
            phase = str(event.get("phase") or "")
            message = str(event.get("message") or "Running workflow")
            workflow_progress.progress(
                phase_steps.get(phase, 0.05),
                text=message,
            )
            workflow_detail.info(message)

        with st.status(f"Running {workflow_name}...", expanded=True) as workflow_status:
            st.write("Collecting graph context, searching Qdrant, then generating the audit.")
            result = WORKFLOWS[workflow_name](on_progress=_show_workflow_progress)
            workflow_status.update(label=f"{workflow_name} complete", state="complete")
        workflow_progress.empty()
        workflow_detail.empty()

        # --- Service availability badges ---
        badge_col1, badge_col2, badge_col3 = st.columns(3)
        with badge_col1:
            if result["graph"].get("available"):
                row_count = len(result["graph"].get("rows", []))
                st.metric("Graph rows", row_count)
            else:
                st.warning(f"Neo4j unavailable: {result['graph'].get('error')}")
        with badge_col2:
            if result["snippets"].get("available"):
                snippet_count = len(result["snippets"].get("results", []))
                top_score = max(
                    (r.score for r in result["snippets"].get("results", [])),
                    default=0.0,
                )
                st.metric("Vector snippets", snippet_count, help=f"Top similarity score: {top_score:.3f}")
            else:
                st.warning(f"Qdrant unavailable: {result['snippets'].get('error')}")
        with badge_col3:
            if result["ollama"].get("available"):
                st.metric("Synthesis", "Ollama ✓")
            else:
                st.metric("Synthesis", "Fallback")
                st.caption(result["ollama"].get("error", ""))

        # --- Inline audit output ---
        audit_path = Path(result["path"])
        st.success(f"Audit saved: `{audit_path.name}`")
        audit_text = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""

        # Phase 5: render structured findings (verified vs. ungrounded) when available.
        structured = result.get("structured")
        _render_verified_audit(structured)

        with st.expander("Raw LLM output (legacy Markdown view)", expanded=structured is None or (structured and structured.parse_error)):
            st.markdown(audit_text)

        # --- Previous audits ---
        slug = audit_path.stem.split("-", 1)[-1] if "-" in audit_path.stem else audit_path.stem
        # slug is everything after the timestamp prefix (YYYYMMDD-HHMMSS-)
        parts = audit_path.stem.split("-", 2)
        slug = parts[2] if len(parts) == 3 else audit_path.stem
        previous = _list_previous_audits(slug)[1:]  # skip the one we just created
        if previous:
            with st.expander(f"Previous audits ({len(previous)} found)"):
                for prev in previous[:5]:
                    with st.container():
                        st.caption(prev.name)
                        prev_text = prev.read_text(encoding="utf-8")
                        st.markdown(prev_text[:2000] + (" …" if len(prev_text) > 2000 else ""))
                        st.divider()


if __name__ == "__main__":
    main()
