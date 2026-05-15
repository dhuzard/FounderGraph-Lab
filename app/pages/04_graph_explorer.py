from __future__ import annotations

from datetime import date, datetime

import streamlit as st
import streamlit.components.v1 as components

from app.config import DOCUMENTS_JSON
from app.services.graph_visualizer import graph_to_pyvis_html
from app.services.neo4j_service import (
    Neo4jService,
    Neo4jServiceError,
)
from app.services.validation_store import ValidationStore, load_json


st.set_page_config(page_title="Graph Explorer", layout="wide")
st.title("FounderGraph-Lab Explorer")
st.caption("Explore validated knowledge already written to Neo4j.")


def _filter_snapshot_as_of(
    graph: dict, service: Neo4jService, as_of_iso: str
) -> dict:
    """Filter ``graph`` to entities valid at ``as_of_iso`` (bi-temporal slice).

    Phase 8.1: uses ``Neo4jService.as_of`` to determine the set of node ids
    that were valid on the chosen date and drops edges referencing dropped
    nodes.  If the temporal lookup fails (e.g. driver down), the original
    snapshot is returned unchanged.
    """
    try:
        valid_rows = service.as_of(as_of_iso)
    except Exception:  # noqa: BLE001 - degrade gracefully on any driver error
        return graph
    valid_ids = {str(row.get("id")) for row in valid_rows if row.get("id")}
    if not valid_ids:
        return graph
    nodes = [n for n in graph.get("nodes", []) if str(n.get("id")) in valid_ids]
    edges = [
        e
        for e in graph.get("edges", [])
        if str(e.get("source")) in valid_ids and str(e.get("target")) in valid_ids
    ]
    return {"nodes": nodes, "edges": edges, "truncated": graph.get("truncated", False)}


def _documents_from_validated_entities(entities: list[dict]) -> list[dict]:
    known_documents = load_json(DOCUMENTS_JSON)
    documents = [item for item in known_documents if isinstance(item, dict)] if isinstance(known_documents, list) else []
    existing = {str(item.get("id")) for item in documents}
    for entity in entities:
        document_id = entity.get("source_document_id")
        if document_id and str(document_id) not in existing:
            documents.append(
                {
                    "id": str(document_id),
                    "title": str(document_id),
                    "original_filename": entity.get("source_file", ""),
                    "file_type": "",
                    "original_path": entity.get("source_file", ""),
                    "markdown_path": "",
                    "document_type": "Unknown",
                    "extraction_status": "validated",
                    "confidentiality": "internal",
                    "summary": "",
                }
            )
            existing.add(str(document_id))
    return documents

with st.sidebar:
    st.header("Filters")
    try:
        filter_service = Neo4jService()
        label_options = sorted(filter_service.allowed_labels - {"Document", "Entity"})
        relationship_options = sorted(filter_service.allowed_relationships)
        filter_service.close()
    except Exception:
        label_options = []
        relationship_options = []
    selected_labels = st.multiselect("Entity labels", label_options)
    selected_relationships = st.multiselect("Relations", relationship_options)
    limit = st.slider("Max relations", min_value=10, max_value=500, value=100, step=10)
    as_of_date = st.date_input(
        "View graph as of",
        value=date.today(),
        help=(
            "Bi-temporal slice: only entities with valid_from ≤ this date and "
            "no valid_to (or valid_to in the future) are shown."
        ),
    )
    deadline_days = st.number_input(
        "Deadline within (days)",
        min_value=0,
        max_value=730,
        value=90,
        step=15,
        help="Upper bound for upcoming-milestone deadline windows.",
    )
    load_graph = st.button("Load graph", type="primary")

st.subheader("Write Validated Knowledge")
st.write("This writes only records marked `validated` in `data/knowledge/*.json` to Neo4j.")
if st.button("Write validated JSON to Neo4j"):
    store = ValidationStore()
    entities = store.validated_entities()
    relations = store.validated_relations()
    documents = _documents_from_validated_entities(entities)
    try:
        service = Neo4jService()
        service.ensure_schema()
        service.upsert_validated_knowledge(entities=entities, relations=relations, documents=documents)
        service.close()
        st.success(f"Wrote {len(entities)} entities and {len(relations)} relations to Neo4j.")
    except Exception as exc:
        st.error(f"Unable to write validated knowledge: {exc}")

if load_graph:
    try:
        service = Neo4jService()
        graph = service.graph_snapshot(selected_labels, selected_relationships, limit)
        as_of_iso = datetime.combine(as_of_date, datetime.min.time()).isoformat()
        graph = _filter_snapshot_as_of(graph, service, as_of_iso)
        service.close()
    except Exception as exc:
        st.error(f"Unable to load graph: {exc}")
        st.stop()

    st.caption(f"Bi-temporal view as of **{as_of_date.isoformat()}**.")
    if graph.get("truncated"):
        st.warning(
            f"Graph display capped at {limit} relations. "
            "Increase 'Max relations' or add label/relation filters to see the full picture."
        )
    st.metric("Nodes", len(graph["nodes"]))
    st.metric("Relations", len(graph["edges"]))
    if not graph["edges"]:
        st.warning(
            "No relationships matched the current filters. Clear filters, increase the relation limit, "
            "or write validated knowledge to Neo4j first."
        )
    with st.expander("Graph data", expanded=not graph["edges"]):
        st.subheader("Nodes")
        st.dataframe(graph["nodes"], use_container_width=True)
        st.subheader("Relations")
        st.dataframe(graph["edges"], use_container_width=True)
    components.html(graph_to_pyvis_html(graph), height=760, scrolling=True)

else:
    st.info("Choose filters and load the graph.")

with st.expander("Upcoming milestones (deadline window)"):
    st.caption(
        f"Milestones with deadline within the next {int(deadline_days)} day(s)."
    )
    if st.button("Load upcoming milestones"):
        try:
            service = Neo4jService()
            cypher = (
                "MATCH (m:Entity:Milestone) "
                "WHERE m.deadline IS NOT NULL "
                "  AND datetime(m.deadline) <= datetime() + duration('P' + $days + 'D') "
                "  AND datetime(m.deadline) >= datetime() "
                "RETURN m.id AS id, m.name AS name, m.deadline AS deadline "
                "ORDER BY m.deadline ASC "
                "LIMIT 100"
            )
            rows = service._rows(cypher, {"days": str(int(deadline_days))})
            service.close()
            if rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("No upcoming milestones in the deadline window.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Unable to load milestones: {exc}")

with st.expander("Audit recent writes"):
    if st.button("Load audit trail"):
        try:
            service = Neo4jService()
            st.dataframe(service.audit_recent_writes(), use_container_width=True)
            service.close()
        except Neo4jServiceError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"Unable to load audit trail: {exc}")
