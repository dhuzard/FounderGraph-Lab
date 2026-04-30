from __future__ import annotations

import hashlib
import inspect
import json
import mimetypes
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.services.extractors import ExtractionError, extract_text_from_path, is_supported_file
from app.services.markdown_converter import document_to_markdown, markdown_filename

try:
    from app.config import DOCUMENTS_JSON
except ImportError:
    DOCUMENTS_JSON = Path(os.getenv("FOUNDERGRAPH_DATA_DIR", "data")) / "staging" / "documents.json"

try:
    from app.models.document import SourceDocument
except ImportError:  # The skeletal repo may not have models yet.

    @dataclass
    class SourceDocument:  # type: ignore[no-redef]
        id: str
        original_filename: str
        file_type: str
        mime_type: str
        size_bytes: int
        sha256: str
        stored_original_path: str
        extracted_text_path: str
        markdown_path: str
        created_at: str
        error: str | None = None


BASE_DATA_DIR = Path(os.getenv("FOUNDERGRAPH_DATA_DIR", "data"))
ORIGINAL_FILES_DIR = BASE_DATA_DIR / "original_files"
EXTRACTED_TEXT_DIR = BASE_DATA_DIR / "extracted_text"
VAULT_DOCUMENTS_DIR = Path(os.getenv("FOUNDERGRAPH_VAULT_DIR", "vault")) / "documents"


@dataclass
class IngestionResult:
    source_document: SourceDocument
    extracted_text: str
    markdown: str


class FileStoreError(RuntimeError):
    """Raised when document persistence or extraction fails."""


def ensure_storage_dirs() -> None:
    for directory in (ORIGINAL_FILES_DIR, EXTRACTED_TEXT_DIR, VAULT_DOCUMENTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def ingest_document(uploaded_file: BinaryIO, filename: str, mime_type: str | None = None) -> IngestionResult:
    if not is_supported_file(filename):
        raise FileStoreError(f"Unsupported file type for {filename}")

    ensure_storage_dirs()
    original_path, digest, size_bytes = store_original(uploaded_file, filename)
    text_path = EXTRACTED_TEXT_DIR / f"{original_path.stem}.txt"
    markdown_path = VAULT_DOCUMENTS_DIR / markdown_filename(filename, digest)

    try:
        extracted_text = extract_text_from_path(original_path)
        text_path.write_text(extracted_text, encoding="utf-8")

        source_document = build_source_document(
            filename=filename,
            mime_type=mime_type or mimetypes.guess_type(filename)[0] or "",
            size_bytes=size_bytes,
            digest=digest,
            original_path=original_path,
            text_path=text_path,
            markdown_path=markdown_path,
        )
        markdown = document_to_markdown(source_document, extracted_text)
        markdown_path.write_text(markdown, encoding="utf-8")
        append_document_record(source_document)
    except ExtractionError as exc:
        raise FileStoreError(str(exc)) from exc
    except OSError as exc:
        raise FileStoreError(f"Failed to write ingestion output for {filename}: {exc}") from exc

    return IngestionResult(source_document=source_document, extracted_text=extracted_text, markdown=markdown)


def append_document_record(source_document: SourceDocument) -> None:
    DOCUMENTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if DOCUMENTS_JSON.exists():
        try:
            records = json.loads(DOCUMENTS_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = []
    if not isinstance(records, list):
        records = []

    record = _model_dump(source_document)
    records = [item for item in records if item.get("id") != record.get("id")]
    records.append(record)
    DOCUMENTS_JSON.write_text(json.dumps(records, indent=2, default=str) + "\n", encoding="utf-8")


def store_original(uploaded_file: BinaryIO, filename: str) -> tuple[Path, str, int]:
    ensure_storage_dirs()
    safe_name = _safe_filename(filename)
    temp_path = ORIGINAL_FILES_DIR / f".{uuid.uuid4().hex}-{safe_name}.tmp"

    hasher = hashlib.sha256()
    size_bytes = 0

    try:
        with temp_path.open("wb") as output:
            _rewind(uploaded_file)
            while True:
                chunk = uploaded_file.read(1024 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                hasher.update(chunk)
                size_bytes += len(chunk)
                output.write(chunk)

        digest = hasher.hexdigest()
        final_path = ORIGINAL_FILES_DIR / f"{digest[:12]}-{safe_name}"
        if final_path.exists():
            temp_path.unlink(missing_ok=True)
        else:
            shutil.move(str(temp_path), final_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise FileStoreError(f"Failed to store original file {filename}: {exc}") from exc

    return final_path, digest, size_bytes


def build_source_document(
    *,
    filename: str,
    mime_type: str,
    size_bytes: int,
    digest: str,
    original_path: Path,
    text_path: Path,
    markdown_path: Path,
) -> SourceDocument:
    date_uploaded = datetime.now(UTC)
    model_payload = {
        "id": digest[:16],
        "title": Path(filename).stem,
        "original_filename": filename,
        "file_type": Path(filename).suffix.lower().lstrip("."),
        "original_path": str(original_path),
        "extracted_text_path": str(text_path),
        "markdown_path": str(markdown_path),
        "date_uploaded": date_uploaded,
        "extraction_status": "converted_to_markdown",
    }
    fallback_payload = {
        "id": digest[:16],
        "original_filename": filename,
        "file_type": Path(filename).suffix.lower().lstrip("."),
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "sha256": digest,
        "stored_original_path": str(original_path),
        "extracted_text_path": str(text_path),
        "markdown_path": str(markdown_path),
        "created_at": date_uploaded.isoformat(),
        "error": None,
    }

    try:
        return SourceDocument(**_compatible_payload(SourceDocument, model_payload))
    except TypeError:
        return SourceDocument(**_compatible_payload(SourceDocument, fallback_payload))


def _safe_filename(filename: str) -> str:
    basename = Path(filename).name.strip().replace("\x00", "")
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in basename)
    return safe or "document"


def _rewind(uploaded_file: BinaryIO) -> None:
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        return


def _compatible_payload(model: type, payload: dict[str, object]) -> dict[str, object]:
    fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", None)
    if fields:
        return {key: value for key, value in payload.items() if key in fields}

    try:
        signature = inspect.signature(model)
    except (TypeError, ValueError):
        return payload

    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return payload

    return {key: value for key, value in payload.items() if key in signature.parameters}


def _model_dump(model: object) -> dict[str, object]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[no-any-return]
    if hasattr(model, "__dict__"):
        return dict(model.__dict__)
    return {}
