from __future__ import annotations

import streamlit as st

from app.services.file_store import FileStoreError, ingest_document


st.set_page_config(page_title="Upload Documents", page_icon="FG", layout="wide")
st.title("Document Upload")

uploaded_files = st.file_uploader(
    "Upload source documents",
    type=["pdf", "docx", "csv", "txt", "md"],
    accept_multiple_files=True,
)

if uploaded_files:
    for uploaded_file in uploaded_files:
        with st.status(f"Ingesting {uploaded_file.name}", expanded=False) as status:
            try:
                result = ingest_document(
                    uploaded_file,
                    filename=uploaded_file.name,
                    mime_type=getattr(uploaded_file, "type", None),
                )
            except FileStoreError as exc:
                status.update(label=f"Failed: {uploaded_file.name}", state="error")
                st.error(str(exc))
                continue

            status.update(label=f"Ingested: {uploaded_file.name}", state="complete")
            st.success(f"Stored Markdown at {result.source_document.markdown_path}")
