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
        for warning in paths.get("warnings", []):
            st.warning(warning)
        if not paths.get("warnings"):
            st.success(f"Export bundle created: {paths['zip']}")
        else:
            st.info(f"Export bundle created (with warnings): {paths['zip']}")
        for key, value in paths.items():
            if key != "warnings":
                st.write(f"**{key}**: `{value}`")


if __name__ == "__main__":
    main()
