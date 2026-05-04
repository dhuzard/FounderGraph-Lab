from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

from app.services.file_store import EXTRACTED_TEXT_DIR, VAULT_DOCUMENTS_DIR, ensure_storage_dirs
from app.services.entity_extractor import EntityExtractor
from app.services.llm_service import LLMServiceError


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

    st.subheader("LLM Candidate Extraction")
    st.caption("Extract entities and relations from Markdown vault documents into the validation queue.")

    def _metadata_from_markdown(path: Path, markdown: str) -> dict[str, str]:
        def match_value(key: str) -> str:
            match = re.search(rf'^{key}:\s+"?([^"\n]+)"?', markdown, flags=re.MULTILINE)
            return match.group(1).strip() if match else ""

        return {
            "source_document_id": match_value("source_id") or path.stem,
            "source_document": path.stem,
            "source_file": match_value("source_path") or str(path),
            "markdown_path": str(path),
        }

    def _text_for_extraction(path: Path, markdown: str) -> str:
        extracted_match = re.search(r'^extracted_text_path:\s+"([^"]+)"', markdown, flags=re.MULTILINE)
        extracted_path = Path(extracted_match.group(1)) if extracted_match else None
        if extracted_path and extracted_path.exists():
            return extracted_path.read_text(encoding="utf-8")
        return re.sub(r"^---.*?---", "", markdown, flags=re.DOTALL).strip()

    def _extract_one(path: Path) -> tuple[int, int]:
        markdown = path.read_text(encoding="utf-8")
        result = EntityExtractor().extract_to_staging(
            _text_for_extraction(path, markdown),
            _metadata_from_markdown(path, markdown),
        )
        return len(result.entities), len(result.relations)

    col_selected, col_batch = st.columns([1, 1])
    with col_selected:
        if st.button("Extract selected document", type="primary"):
            try:
                entity_count, relation_count = _extract_one(Path(selected))
                st.success(f"Staged {entity_count} entities and {relation_count} relations.")
            except LLMServiceError as exc:
                st.error(f"LLM extraction failed: {exc}")
    with col_batch:
        batch_size = st.number_input("Batch latest documents", min_value=1, max_value=50, value=5, step=1)
        if st.button("Extract batch"):
            totals = {"entities": 0, "relations": 0, "failed": 0}
            progress = st.progress(0.0)
            for index, path in enumerate(markdown_files[: int(batch_size)], start=1):
                try:
                    entity_count, relation_count = _extract_one(path)
                    totals["entities"] += entity_count
                    totals["relations"] += relation_count
                except LLMServiceError:
                    totals["failed"] += 1
                progress.progress(index / int(batch_size))
            if totals["failed"]:
                st.warning(
                    f"Staged {totals['entities']} entities and {totals['relations']} relations; "
                    f"{totals['failed']} document(s) failed."
                )
            else:
                st.success(f"Staged {totals['entities']} entities and {totals['relations']} relations.")
