"""Discovery — ontology-driven gap analysis.

Phase 2 of the GraphRAG upgrade: this page renders one tile per discovery
query registered in :mod:`app.services.discovery_queries` and lets the user
execute each Cypher block on demand.  Every query is deterministic, read-only,
and references only labels/predicates from the live ontology — no LLM is
involved.
"""

from __future__ import annotations

try:
    import streamlit as st
except ImportError:  # pragma: no cover - streamlit only required in app context
    st = None  # type: ignore[assignment]

from app.services import discovery_queries
from app.services.neo4j_service import Neo4jService


def _result_count(rows: list[dict]) -> int:
    """Return a row count that is safe for any iterable result."""
    try:
        return len(rows)
    except TypeError:
        return sum(1 for _ in rows)


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="Discovery — Ontology Gaps", layout="wide")
    st.title("Discovery — ontology-driven gap analysis.")
    st.caption(
        "Deterministic Cypher queries derived from the ontology. "
        "No LLM involved."
    )

    # ------------------------------------------------------------------
    # Connect to Neo4j once per render.  We surface a clear error banner
    # rather than crashing the page if the driver cannot reach the DB.
    # ------------------------------------------------------------------
    service: Neo4jService | None = None
    connection_error: str | None = None
    try:
        service = Neo4jService()
    except Exception as exc:  # noqa: BLE001 - surface any driver-construction error
        connection_error = str(exc)

    if connection_error:
        st.error(
            f"Unable to connect to Neo4j: {connection_error}. "
            "Configure NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD and reload."
        )

    queries = list(discovery_queries.all_queries())
    st.markdown(
        f"**{len(queries)} discovery queries** registered against the active ontology."
    )

    with st.sidebar:
        st.header("Discovery controls")
        limit = st.slider(
            "Max rows per query",
            min_value=10,
            max_value=500,
            value=100,
            step=10,
        )
        st.caption(
            "Each query runs in READ access mode and is parameterized with $limit."
        )

    for query in queries:
        with st.container():
            st.subheader(query.title)
            st.write(query.description)

            run_clicked = st.button(
                "Run",
                key=f"run-{query.name}",
                help=f"Execute discovery query `{query.name}`",
            )

            with st.expander("Cypher", expanded=False):
                st.code(query.cypher, language="cypher")
                st.caption(
                    f"Expected columns: {', '.join(query.expected_columns)}"
                )

            if run_clicked:
                if service is None:
                    st.warning(
                        "Neo4j driver is not available — cannot execute this query."
                    )
                else:
                    try:
                        rows = discovery_queries.run(
                            query.name,
                            service.driver,
                            limit=limit,
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Query failed: {exc}")
                    else:
                        count = _result_count(rows)
                        if count == 0:
                            st.success(
                                "No gaps found — nothing matches this query."
                            )
                        else:
                            st.metric("Rows", count)
                            st.dataframe(rows, use_container_width=True)
            st.divider()

    if service is not None:
        try:
            service.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


if __name__ == "__main__":
    main()
