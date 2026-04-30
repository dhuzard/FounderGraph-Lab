from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.markdown_converter import document_to_markdown, markdown_filename


@dataclass
class DummySourceDocument:
    id: str = "abc123"
    original_filename: str = "Investor Notes.txt"
    file_type: str = "txt"
    mime_type: str = "text/plain"
    size_bytes: int = 42
    sha256: str = "abc123def456"
    stored_original_path: str = "data/original_files/abc-Investor_Notes.txt"
    extracted_text_path: str = "data/extracted_text/abc-Investor_Notes.txt"
    markdown_path: str = "vault/documents/investor-notes-abc123def456.md"
    created_at: str = "2026-04-30T12:00:00+00:00"


def test_document_to_markdown_preserves_provenance() -> None:
    markdown = document_to_markdown(DummySourceDocument(), "Extracted body")

    assert markdown.startswith("---\n")
    assert 'source_filename: "Investor Notes.txt"' in markdown
    assert 'source_path: "data/original_files/abc-Investor_Notes.txt"' in markdown
    assert 'sha256: "abc123def456"' in markdown
    assert "# Investor Notes" in markdown
    assert markdown.endswith("Extracted body\n")


def test_markdown_filename_is_stable_and_safe() -> None:
    assert markdown_filename("Investor Notes.txt", "abc123def4567890") == "investor-notes-abc123def456.md"
