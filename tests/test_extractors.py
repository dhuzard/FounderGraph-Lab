from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.extractors import ExtractionError, extract_text_from_path, is_supported_file


def test_extract_text_file(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    source.write_text("hello\nworld", encoding="utf-8")

    assert extract_text_from_path(source) == "hello\nworld"


def test_extract_markdown_file(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Title\n\nBody", encoding="utf-8")

    assert extract_text_from_path(source) == "# Title\n\nBody"


def test_extract_csv_file(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    source.write_text("name,role\nAda,Founder\n", encoding="utf-8")

    assert extract_text_from_path(source) == "name, role\nAda, Founder"


def test_extract_html_file(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_text(
        "<html><head><style>.x{}</style></head><body><h1>Title</h1><p>Rich <b>text</b></p></body></html>",
        encoding="utf-8",
    )

    assert extract_text_from_path(source) == "Title\nRich text"


def test_google_workspace_shortcut_explains_export_needed(tmp_path: Path) -> None:
    source = tmp_path / "strategy.gdoc"
    source.write_text('{"doc_id":"abc"}', encoding="utf-8")

    with pytest.raises(ExtractionError, match="Google Workspace shortcut"):
        extract_text_from_path(source)


def test_unsupported_file_raises(tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    source.write_bytes(b"not supported")

    with pytest.raises(ExtractionError):
        extract_text_from_path(source)


def test_supported_extensions() -> None:
    assert is_supported_file("deck.PDF")
    assert is_supported_file("memo.docx")
    assert is_supported_file("data.csv")
    assert is_supported_file("notes.txt")
    assert is_supported_file("notes.md")
    assert is_supported_file("report.html")
    assert is_supported_file("deck.pptx")
    assert is_supported_file("model.xlsx")
    assert not is_supported_file("image.png")
