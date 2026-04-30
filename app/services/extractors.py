from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".csv", ".txt", ".md"}


class ExtractionError(RuntimeError):
    """Raised when a supported file cannot be converted to text."""


def is_supported_file(filename: str | Path) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def extract_text_from_path(path: str | Path) -> str:
    source_path = Path(path)
    extension = source_path.suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"Unsupported file type: {extension or 'unknown'}")

    try:
        if extension == ".pdf":
            return _extract_pdf(source_path)
        if extension == ".docx":
            return _extract_docx(source_path)
        if extension == ".csv":
            return _extract_csv(source_path)
        return _read_text(source_path)
    except ExtractionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrapper
        raise ExtractionError(f"Failed to extract text from {source_path.name}: {exc}") from exc


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionError(f"Unable to decode text file: {path.name}")


def _extract_csv(path: Path) -> str:
    raw_text = _read_text(path)
    reader = csv.reader(StringIO(raw_text))
    lines: list[str] = []

    for row in reader:
        clean_cells = [cell.strip() for cell in row]
        lines.append(", ".join(clean_cells))

    return "\n".join(lines)


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ExtractionError("DOCX extraction requires the python-docx package") from exc

    document = Document(path)
    chunks: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            chunks.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                chunks.append(" | ".join(cells))

    return "\n\n".join(chunks)


def _extract_pdf(path: Path) -> str:
    errors: list[str] = []

    try:
        import fitz

        with fitz.open(path) as document:
            pages = [page.get_text("text") or "" for page in document]
        text = "\n\n".join(page.strip() for page in pages if page.strip())
        if text:
            return text
    except ImportError:
        errors.append("pymupdf unavailable")
    except Exception as exc:
        errors.append(f"pymupdf failed: {exc}")

    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n\n".join(page.strip() for page in pages if page.strip())
        if text:
            return text
    except ImportError as exc:
        errors.append("pdfplumber unavailable")
    except Exception as exc:
        errors.append(f"pdfplumber failed: {exc}")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(page.strip() for page in pages if page.strip())
        if text:
            return text
    except ImportError:
        errors.append("pypdf unavailable")
    except Exception as exc:
        errors.append(f"pypdf failed: {exc}")

    detail = "; ".join(errors) if errors else "no extractable text"
    raise ExtractionError(f"PDF extraction failed for {path.name}: {detail}")
