#!/usr/bin/env python3
"""Export a Google Drive folder tree into local backup files.

What it does:
- Crawls a Drive folder recursively
- Detects Google-native files by MIME type
- Exports each file into one or more target formats
- Optionally downloads non-Google files as-is
- Writes a JSON manifest with all outputs and failures

Auth model:
- Service account JSON key (recommended for automation)
- Share the target Drive folder with the service account email first

Example:
    python scripts/export_google_drive_folder.py \
        --folder-id <DRIVE_FOLDER_ID> \
        --output-dir ./data/drive_backup \
        --service-account-file ./secrets/service-account.json
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/|?*\x00-\x1F]', "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "untitled"


# Google-native MIME types and their export targets.
# Keys are target labels used for sub-folders, values are Drive export MIME types.
GOOGLE_EXPORTS: dict[str, dict[str, str]] = {
    "application/vnd.google-apps.document": {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
        "txt": "text/plain",
    },
    "application/vnd.google-apps.spreadsheet": {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "csv": "text/csv",
        "pdf": "application/pdf",
    },
    "application/vnd.google-apps.presentation": {
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf": "application/pdf",
    },
    "application/vnd.google-apps.drawing": {
        "png": "image/png",
        "pdf": "application/pdf",
    },
}

GOOGLE_SHORTCUT_FORMATS = {
    ".gdoc": ("application/vnd.google-apps.document", "docx"),
    ".gsheet": ("application/vnd.google-apps.spreadsheet", "xlsx"),
    ".gslides": ("application/vnd.google-apps.presentation", "pptx"),
}

FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass
class ExportResult:
    file_id: str
    file_name: str
    mime_type: str
    drive_path: str
    status: str
    outputs: list[str]
    error: str = ""


def _guess_extension(content_type: str, fallback: str = "") -> str:
    guessed = mimetypes.guess_extension(content_type) or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed:
        return guessed
    return fallback


def _service_account_credentials(service_account_file: Path):
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing dependency: google-auth. Install requirements and retry."
        ) from exc

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    return service_account.Credentials.from_service_account_file(str(service_account_file), scopes=scopes)


def _build_drive_client(service_account_file: Path):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing dependency: google-api-python-client. Install requirements and retry."
        ) from exc

    credentials = _service_account_credentials(service_account_file)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def resolve_credentials_path(service_account_file: Path | None = None) -> Path | None:
    if service_account_file is not None:
        return service_account_file
    env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if env_path:
        return Path(env_path)
    return None


def build_drive_client(service_account_file: Path | None = None):
    credentials_path = resolve_credentials_path(service_account_file)
    if credentials_path is None:
        raise RuntimeError("Provide --service-account-file or set GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials_path.exists():
        raise RuntimeError(f"Service account key not found: {credentials_path}")
    return _build_drive_client(credentials_path)


def _download_request_to_path(request: Any, destination: Path) -> None:
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing dependency: google-api-python-client. Install requirements and retry."
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    destination.write_bytes(buffer.getvalue())


def _extract_drive_file_id(raw_text: str) -> str | None:
    patterns = [
        r'"doc_id"\s*:\s*"([^"]+)"',
        r'"resource_id"\s*:\s*"[^:]+:([^"]+)"',
        r'https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)',
        r'https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)',
        r'https://docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text)
        if match:
            return match.group(1)
    return None


def parse_google_shortcut(shortcut_path: Path) -> dict[str, str]:
    suffix = shortcut_path.suffix.lower()
    if suffix not in GOOGLE_SHORTCUT_FORMATS:
        raise RuntimeError(f"Unsupported Google shortcut type: {suffix}")

    raw_text = shortcut_path.read_text(encoding="utf-8", errors="replace")
    file_id = _extract_drive_file_id(raw_text)
    if not file_id:
        raise RuntimeError(f"Could not extract Drive file ID from {shortcut_path.name}")

    mime_type, export_format = GOOGLE_SHORTCUT_FORMATS[suffix]
    return {
        "id": file_id,
        "name": shortcut_path.stem,
        "mimeType": mime_type,
        "export_format": export_format,
    }


def export_google_shortcut(
    shortcut_path: Path,
    output_dir: Path,
    service_account_file: Path | None = None,
) -> Path:
    shortcut = parse_google_shortcut(shortcut_path)
    drive = build_drive_client(service_account_file)
    outputs = _export_google_native(
        drive,
        {
            "id": shortcut["id"],
            "name": shortcut["name"],
            "mimeType": shortcut["mimeType"],
        },
        rel_path=Path(),
        output_dir=output_dir,
        target_formats=[shortcut["export_format"]],
    )
    if not outputs:
        raise RuntimeError(f"No export output produced for {shortcut_path.name}")
    return Path(outputs[0])


def _list_children(drive: Any, folder_id: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    page_token: str | None = None

    while True:
        response = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                corpora="allDrives",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return items


def _export_google_native(
    drive: Any,
    item: dict[str, str],
    rel_path: Path,
    output_dir: Path,
    target_formats: list[str] | None,
) -> list[str]:
    mime_type = item["mimeType"]
    exports = GOOGLE_EXPORTS.get(mime_type, {})

    if target_formats:
        requested = [f for f in target_formats if f in exports]
    else:
        requested = list(exports.keys())

    if not requested:
        return []

    written: list[str] = []
    stem = _safe_name(item["name"])

    for fmt in requested:
        export_mime = exports[fmt]
        ext = _guess_extension(export_mime, fallback=f".{fmt}")
        local_dir = output_dir / fmt / rel_path
        local_file = local_dir / f"{stem}{ext}"
        request = drive.files().export_media(fileId=item["id"], mimeType=export_mime)
        _download_request_to_path(request, local_file)
        written.append(str(local_file))

    return written


def _download_binary_file(
    drive: Any,
    item: dict[str, str],
    rel_path: Path,
    output_dir: Path,
) -> str:
    stem = _safe_name(item["name"])
    ext = _guess_extension(item.get("mimeType", ""))
    local_dir = output_dir / "original" / rel_path
    local_file = local_dir / f"{stem}{ext}"
    request = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    _download_request_to_path(request, local_file)
    return str(local_file)


def crawl_and_export(
    drive: Any,
    folder_id: str,
    output_dir: Path,
    *,
    target_formats: list[str] | None,
    include_non_google: bool,
    rel_path: Path | None = None,
) -> list[ExportResult]:
    rel_path = rel_path or Path()
    results: list[ExportResult] = []

    for item in _list_children(drive, folder_id):
        mime_type = item.get("mimeType", "")
        item_name = item.get("name", "untitled")
        item_id = item.get("id", "")
        current_path = rel_path / _safe_name(item_name)

        if mime_type == FOLDER_MIME:
            nested = crawl_and_export(
                drive,
                item_id,
                output_dir,
                target_formats=target_formats,
                include_non_google=include_non_google,
                rel_path=current_path,
            )
            results.extend(nested)
            continue

        try:
            if mime_type in GOOGLE_EXPORTS:
                outputs = _export_google_native(
                    drive,
                    item,
                    rel_path,
                    output_dir,
                    target_formats=target_formats,
                )
                if outputs:
                    results.append(
                        ExportResult(
                            file_id=item_id,
                            file_name=item_name,
                            mime_type=mime_type,
                            drive_path=str(current_path),
                            status="exported",
                            outputs=outputs,
                        )
                    )
                else:
                    results.append(
                        ExportResult(
                            file_id=item_id,
                            file_name=item_name,
                            mime_type=mime_type,
                            drive_path=str(current_path),
                            status="skipped",
                            outputs=[],
                            error="No matching requested formats for this Google-native file type",
                        )
                    )
            elif include_non_google:
                binary_out = _download_binary_file(drive, item, rel_path, output_dir)
                results.append(
                    ExportResult(
                        file_id=item_id,
                        file_name=item_name,
                        mime_type=mime_type,
                        drive_path=str(current_path),
                        status="downloaded",
                        outputs=[binary_out],
                    )
                )
            else:
                results.append(
                    ExportResult(
                        file_id=item_id,
                        file_name=item_name,
                        mime_type=mime_type,
                        drive_path=str(current_path),
                        status="skipped",
                        outputs=[],
                        error="Non-Google file skipped (use --include-non-google to download binaries)",
                    )
                )
        except Exception as exc:  # pragma: no cover - external I/O
            results.append(
                ExportResult(
                    file_id=item_id,
                    file_name=item_name,
                    mime_type=mime_type,
                    drive_path=str(current_path),
                    status="failed",
                    outputs=[],
                    error=str(exc),
                )
            )

    return results


def _write_manifest(output_dir: Path, folder_id: str, formats: list[str], results: list[ExportResult]) -> Path:
    manifest = {
        "generated_at": _utc_now(),
        "folder_id": folder_id,
        "formats": formats,
        "counts": {
            "exported": sum(1 for r in results if r.status == "exported"),
            "downloaded": sum(1 for r in results if r.status == "downloaded"),
            "failed": sum(1 for r in results if r.status == "failed"),
            "skipped": sum(1 for r in results if r.status == "skipped"),
            "total": len(results),
        },
        "items": [r.__dict__ for r in results],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Google Drive folder to local backup files")
    parser.add_argument("--folder-id", required=True, help="Google Drive folder ID to crawl")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "drive_backup",
        help="Local output directory",
    )
    parser.add_argument(
        "--service-account-file",
        type=Path,
        default=None,
        help="Path to service account JSON key. If omitted, GOOGLE_APPLICATION_CREDENTIALS is used.",
    )
    parser.add_argument(
        "--formats",
        default="docx,pdf,txt,xlsx,csv,pptx,png",
        help="Comma-separated target formats for Google-native exports",
    )
    parser.add_argument(
        "--include-non-google",
        action="store_true",
        help="Also download non-Google files in original format",
    )
    return parser.parse_args()


def run_export(
    *,
    folder_id: str,
    output_dir: Path,
    service_account_file: Path,
    formats: list[str],
    include_non_google: bool,
) -> tuple[Path, dict[str, int], list[ExportResult]]:
    drive = build_drive_client(service_account_file)
    results = crawl_and_export(
        drive,
        folder_id,
        output_dir,
        target_formats=formats,
        include_non_google=include_non_google,
    )
    manifest_path = _write_manifest(output_dir, folder_id, formats, results)

    counts = {
        "exported": sum(1 for r in results if r.status == "exported"),
        "downloaded": sum(1 for r in results if r.status == "downloaded"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "total": len(results),
    }
    return manifest_path, counts, results


def main() -> None:
    args = parse_args()

    credentials_path = resolve_credentials_path(args.service_account_file)
    if credentials_path is None:
        raise SystemExit("Provide --service-account-file or set GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials_path.exists():
        raise SystemExit(f"Service account key not found: {credentials_path}")

    formats = [item.strip().lower() for item in args.formats.split(",") if item.strip()]

    manifest_path, counts, _ = run_export(
        folder_id=args.folder_id,
        output_dir=args.output_dir,
        service_account_file=credentials_path,
        formats=formats,
        include_non_google=args.include_non_google,
    )

    print(f"Export completed. Manifest: {manifest_path}")
    print(
        f"Counts -> exported: {counts['exported']}, downloaded: {counts['downloaded']}, skipped: {counts['skipped']}, failed: {counts['failed']}, total: {counts['total']}"
    )


if __name__ == "__main__":
    main()
