from __future__ import annotations

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.agents import WORKFLOWS
from app.services.qdrant_service import index_startup_knowledge


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="FounderGraph-Lab Agents", layout="wide")
    st.title("Agent Audits")
    st.caption("Read-only workflows combine Neo4j context, Qdrant snippets, and Ollama synthesis when available.")

    workflow_name = st.selectbox("Workflow", list(WORKFLOWS))
    if st.button("Index vault in Qdrant"):
        with st.spinner("Indexing Markdown vault..."):
            status = index_startup_knowledge("/app")
        document_results = status.get("documents", [])
        indexed = sum(item.get("indexed", 0) for item in document_results)
        unavailable = [item for item in document_results if not item.get("available", True)]
        if unavailable:
            st.warning(f"Indexed {indexed} chunks before vector indexing became unavailable.")
            st.caption(unavailable[0].get("error", "Unknown vector indexing error"))
        else:
            st.success(f"Indexed {indexed} document chunks.")

    if st.button("Run workflow", type="primary"):
        with st.spinner("Running read-only agent workflow..."):
            result = WORKFLOWS[workflow_name]()
        st.success(f"Saved audit: {result['path']}")
        if not result["graph"].get("available"):
            st.warning(f"Neo4j unavailable: {result['graph'].get('error')}")
        if not result["snippets"].get("available"):
            st.warning(f"Qdrant/Ollama unavailable: {result['snippets'].get('error')}")
        if not result["ollama"].get("available"):
            st.info(f"Ollama synthesis unavailable; fallback Markdown was saved. {result['ollama'].get('error')}")


if __name__ == "__main__":
    main()
