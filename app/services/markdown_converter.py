from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def document_to_markdown(source_document: Any, extracted_text: str) -> str:
    filename = _get(source_document, "original_filename", "filename", default="document")
    title = Path(str(filename)).stem or "document"
    front_matter = provenance_front_matter(source_document)
    body = (extracted_text or "").strip()

    return f"---\n{front_matter}---\n\n# {title}\n\n{body}\n"


def provenance_front_matter(source_document: Any) -> str:
    values = {
        "source_id": _get(source_document, "id", default=""),
        "source_filename": _get(source_document, "original_filename", "filename", default=""),
        "source_path": _get(source_document, "stored_original_path", "original_path", "source_path", default=""),
        "file_type": _get(source_document, "file_type", default=""),
        "mime_type": _get(source_document, "mime_type", default=""),
        "sha256": _get(source_document, "sha256", default=""),
        "size_bytes": _get(source_document, "size_bytes", default=0),
        "extracted_text_path": _get(source_document, "extracted_text_path", default=""),
        "markdown_path": _get(source_document, "markdown_path", default=""),
        "ingested_at": _get(source_document, "created_at", "date_uploaded", default=_utc_now()),
    }
    return "".join(f"{key}: {_yaml_scalar(value)}\n" for key, value in values.items())


def markdown_filename(filename: str, digest: str | None = None) -> str:
    stem = _slugify(Path(filename).stem)
    suffix = f"-{digest[:12]}" if digest else ""
    return f"{stem}{suffix}.md"


def _get(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "document"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)

    text = value.isoformat() if isinstance(value, datetime) else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
