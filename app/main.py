from __future__ import annotations

from dataclasses import dataclass

import requests
import streamlit as st

from app.config import (
    EMBEDDING_MODEL,
    LLM_MODEL,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    OLLAMA_URL,
    QDRANT_URL,
    ensure_json_files,
)


@dataclass(frozen=True)
class ReadinessCheck:
    label: str
    ok: bool
    detail: str


def _badge(label: str, ok: bool) -> str:
    background = "#1f6f43" if ok else "#7a1f1f"
    text = "READY" if ok else "NOT READY"
    return (
        f"<span style='display:inline-block;padding:0.2rem 0.55rem;border-radius:999px;"
        f"background:{background};color:white;font-size:0.78rem;font-weight:700;letter-spacing:0.02em;'>"
        f"{label}: {text}</span>"
    )


def _check_ollama() -> tuple[ReadinessCheck, ReadinessCheck, ReadinessCheck]:
    try:
        response = requests.get(f"{OLLAMA_URL.rstrip('/')}/api/tags", timeout=4)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        error = str(exc)
        return (
            ReadinessCheck("Ollama", False, error),
            ReadinessCheck(f"LLM model ({LLM_MODEL})", False, "Ollama unavailable"),
            ReadinessCheck(f"Embedding model ({EMBEDDING_MODEL})", False, "Ollama unavailable"),
        )

    models = payload.get("models", []) if isinstance(payload, dict) else []
    names = {str(item.get("name", "")) for item in models if isinstance(item, dict)}
    llm_ok = LLM_MODEL in names or f"{LLM_MODEL}:latest" in names
    embedding_ok = EMBEDDING_MODEL in names or f"{EMBEDDING_MODEL}:latest" in names
    detail = f"{len(names)} model(s) available"
    return (
        ReadinessCheck("Ollama", True, detail),
        ReadinessCheck(f"LLM model ({LLM_MODEL})", llm_ok, "Pulled" if llm_ok else "Missing from Ollama tags"),
        ReadinessCheck(
            f"Embedding model ({EMBEDDING_MODEL})",
            embedding_ok,
            "Pulled" if embedding_ok else "Missing from Ollama tags",
        ),
    )


def _check_qdrant() -> ReadinessCheck:
    try:
        response = requests.get(f"{QDRANT_URL.rstrip('/')}/collections", timeout=4)
        response.raise_for_status()
        payload = response.json()
        collections = payload.get("result", {}).get("collections", []) if isinstance(payload, dict) else []
        return ReadinessCheck("Qdrant", True, f"{len(collections)} collection(s) visible")
    except requests.RequestException as exc:
        return ReadinessCheck("Qdrant", False, str(exc))


def _check_neo4j() -> ReadinessCheck:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return ReadinessCheck("Neo4j", False, "neo4j package not installed")

    driver = None
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
        return ReadinessCheck("Neo4j", True, NEO4J_URI)
    except Exception as exc:
        return ReadinessCheck("Neo4j", False, str(exc))
    finally:
        if driver is not None:
            driver.close()


def render_setup_panel() -> None:
    st.subheader("Setup readiness")
    st.caption("Live checks for core services and required models.")

    if st.button("Refresh readiness", key="refresh_readiness"):
        st.rerun()

    neo4j_check = _check_neo4j()
    qdrant_check = _check_qdrant()
    ollama_check, llm_check, embedding_check = _check_ollama()
    checks = [neo4j_check, ollama_check, qdrant_check, llm_check, embedding_check]

    badge_row = st.columns(len(checks))
    for column, check in zip(badge_row, checks):
        with column:
            st.markdown(_badge(check.label, check.ok), unsafe_allow_html=True)
            st.caption(check.detail)

    if not all(check.ok for check in checks):
        with st.expander("Troubleshooting", expanded=False):
            for check in checks:
                if not check.ok:
                    st.write(f"- {check.label}: {check.detail}")


def main() -> None:
    ensure_json_files()
    st.set_page_config(page_title="FAIR-VCG-mentor", page_icon="FG", layout="wide")
    st.title("FAIR-VCG-mentor")
    st.caption("Local-first startup knowledge graph lab")

    st.markdown(
        """
        FAIR-VCG-mentor turns startup documents into validated, reusable knowledge.

        Core loop:
        `files -> extraction -> Markdown vault -> candidate entities -> human validation -> Neo4j -> Qdrant -> audit agents`
        """
    )

    render_setup_panel()

    st.subheader("MVP workflow")
    st.write(
        "Upload startup files, extract reusable knowledge, validate it, write validated facts to Neo4j, "
        "visualize the graph, and run the startup claims audit."
    )

    st.info("Use the sidebar pages from top to bottom for the vertical demo.")


if __name__ == "__main__":
    main()
