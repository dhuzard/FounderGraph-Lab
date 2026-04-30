from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

from app.services.file_store import EXTRACTED_TEXT_DIR, VAULT_DOCUMENTS_DIR, ensure_storage_dirs


st.set_page_config(page_title="Extracted Documents", page_icon="FG", layout="wide")
st.title("Extracted Documents")

ensure_storage_dirs()
markdown_files = sorted(VAULT_DOCUMENTS_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)

if not markdown_files:
    st.info("No extracted documents yet.")
else:
    selected = st.selectbox("Document", markdown_files, format_func=lambda path: path.name)
    markdown_text = Path(selected).read_text(encoding="utf-8")
    st.download_button("Download Markdown", markdown_text, file_name=selected.name)
    st.markdown(markdown_text)

    extracted_path_match = re.search(r'^extracted_text_path:\s+"([^"]+)"', markdown_text, flags=re.MULTILINE)
    text_path = Path(extracted_path_match.group(1)) if extracted_path_match else EXTRACTED_TEXT_DIR / f"{selected.stem}.txt"
    if text_path.exists():
        with st.expander("Extracted plain text"):
            st.text(text_path.read_text(encoding="utf-8"))
