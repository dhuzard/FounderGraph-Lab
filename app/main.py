from __future__ import annotations

import streamlit as st

from app.config import ensure_json_files


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

    st.subheader("MVP workflow")
    st.write(
        "Upload startup files, extract reusable knowledge, validate it, write validated facts to Neo4j, "
        "visualize the graph, and run the startup claims audit."
    )

    st.info("Use the sidebar pages from top to bottom for the vertical demo.")


if __name__ == "__main__":
    main()
