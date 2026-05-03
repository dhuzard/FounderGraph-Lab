from __future__ import annotations

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.export_service import export_all


_ARTIFACT_LABELS = {
    "manifest": "manifest.json",
    "graph_json": "graph.json",
    "graph_jsonld": "graph.jsonld (JSON-LD)",
    "assumptions_csv": "assumptions.csv",
    "evidence_matrix_csv": "evidence_matrix.csv",
    "risk_register_csv": "risk_register.csv",
    "audits_dir": "audits/",
    "zip": "export.zip (full bundle)",
}


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="FounderGraph Exports", layout="wide")
    st.title("Exports")
    st.caption("Generate graph, assumptions, evidence, risk, audit, and zip artifacts.")

    if st.button("Create export bundle", type="primary"):
        paths = export_all()

        no_knowledge = [w for w in paths.get("warnings", []) if "No validated knowledge" in w]
        if no_knowledge:
            st.error(no_knowledge[0])
            st.info("Validate entities and relations on the Validate Knowledge page first.")
            return

        other_warnings = [w for w in paths.get("warnings", []) if "No validated knowledge" not in w]
        for warning in other_warnings:
            st.warning(warning)

        st.success(f"Export bundle created: {paths['zip']}")

        st.subheader("Artifacts")
        for key in ["manifest", "graph_json", "graph_jsonld", "assumptions_csv",
                    "evidence_matrix_csv", "risk_register_csv", "audits_dir", "zip"]:
            value = paths.get(key)
            if value is not None:
                label = _ARTIFACT_LABELS.get(key, key)
                st.write(f"**{label}**: `{value}`")


if __name__ == "__main__":
    main()
