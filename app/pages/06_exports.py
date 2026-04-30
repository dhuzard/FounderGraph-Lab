from __future__ import annotations

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.export_service import export_all


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="FounderGraph Exports", layout="wide")
    st.title("Exports")
    st.caption("Generate graph, assumptions, evidence, risk, audit, and zip artifacts.")

    if st.button("Create export bundle", type="primary"):
        paths = export_all()
        st.success(f"Export bundle created: {paths['zip']}")
        for key, path in paths.items():
            st.write(f"**{key}**: `{path}`")


if __name__ == "__main__":
    main()
