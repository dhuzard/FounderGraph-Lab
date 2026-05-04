from __future__ import annotations

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
        service.close()
    except Exception as exc:
        st.error(f"Unable to load graph: {exc}")
        st.stop()

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
