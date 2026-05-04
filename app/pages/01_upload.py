from __future__ import annotations

import os
from pathlib import Path

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.extractors import GOOGLE_WORKSPACE_SHORTCUTS, SUPPORTED_EXTENSIONS, is_supported_file
from app.services.file_store import FileStoreError, ingest_document
from app.services.init_bridge import load_init_bridge, save_init_bridge
from app.config import STAGING_DIR
from scripts.export_google_drive_folder import export_google_shortcut, resolve_credentials_path


def _ingest_file_from_path(file_path: Path) -> tuple[bool, str]:
    """Open a local file and run it through ingest_document. Returns (ok, message)."""
    try:
        with file_path.open("rb") as fh:
            result = ingest_document(fh, filename=file_path.name)
        return True, str(result.source_document.markdown_path)
    except FileStoreError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"Cannot read {file_path.name}: {exc}"


def _collect_files(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file() and is_supported_file(p.name)
    )


def _scan_folder(folder: Path) -> tuple[list[Path], list[Path], list[Path]]:
    all_files = sorted(p for p in folder.rglob("*") if p.is_file())
    supported = [p for p in all_files if is_supported_file(p.name)]
    google_shortcuts = [p for p in all_files if p.suffix.lower() in GOOGLE_WORKSPACE_SHORTCUTS]
    unsupported = [p for p in all_files if p not in supported and p not in google_shortcuts]
    return supported, google_shortcuts, unsupported


def _convert_google_shortcuts(
    shortcuts: list[Path],
    *,
    root_folder: Path,
    credentials_path: Path | None,
) -> tuple[list[Path], list[tuple[str, str]], list[tuple[str, str]]]:
    if not shortcuts:
        return [], [], []
    if credentials_path is None:
        skipped = [(path.name, "No Google service account JSON path configured") for path in shortcuts]
        return [], skipped, []

    converted: list[Path] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for shortcut in shortcuts:
        try:
            relative_parent = shortcut.parent.relative_to(root_folder)
        except ValueError:
            relative_parent = Path()
        output_dir = STAGING_DIR / "google_shortcut_exports" / relative_parent
        try:
            converted_file = export_google_shortcut(
                shortcut,
                output_dir=output_dir,
                service_account_file=credentials_path,
            )
            converted.append(converted_file)
        except Exception as exc:
            failed.append((shortcut.name, str(exc)))

    return converted, skipped, failed


def main() -> None:
    if st is None:
        print("Streamlit is not installed.")
        return

    st.set_page_config(page_title="Upload Documents", page_icon="FG", layout="wide")
    st.title("Document Upload")

    bridge = load_init_bridge()
    prefilled_folder = ""
    query_prefill = str(st.query_params.get("ingest_folder", "")).strip() if hasattr(st, "query_params") else ""
    if query_prefill:
        prefilled_folder = query_prefill
    elif bridge.get("source_folder"):
        prefilled_folder = str(bridge.get("source_folder", ""))

    if prefilled_folder:
        st.info(f"Prefilled from Init: {prefilled_folder}")

    if bridge.get("show_drive_cta", False):
        if st.button("Export from Drive and ingest now", type="secondary"):
            try:
                st.switch_page("pages/00_drive_sync.py")
            except Exception:
                st.warning("Open 'Drive Sync' from the sidebar.")

    tab_files, tab_folder = st.tabs(["Upload files", "Ingest folder"])

    # ------------------------------------------------------------------
    # Tab 1 — individual file upload (original behaviour)
    # ------------------------------------------------------------------
    with tab_files:
        uploaded_files = st.file_uploader(
            "Upload source documents",
            type=[ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS)],
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

    # ------------------------------------------------------------------
    # Tab 2 — recursive folder ingest
    # ------------------------------------------------------------------
    with tab_folder:
        st.markdown(
            "Enter the path to a folder on the server. "
            "All supported files (`"
            + "`, `".join(sorted(SUPPORTED_EXTENSIONS))
            + "`) in that folder and its sub-folders will be ingested. "
            "Already-ingested files are skipped automatically (SHA-256 dedup)."
        )

        if st.button("How to make your folder available"):
            st.info(
                "**Docker users:** add a volume mount in `docker-compose.yml`:\n\n"
                "```yaml\nvolumes:\n  - /your/local/startup-folder:/docs\n```\n\n"
                "Then enter `/docs` below. "
                "**Local dev (no Docker):** just enter the absolute path on your machine."
            )

        folder_input = st.text_input(
            "Folder path",
            value=prefilled_folder,
            placeholder="/docs  or  /home/user/startup-files",
        )
        credentials_default = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        service_account_input = st.text_input(
            "Google service account JSON path (optional)",
            value=credentials_default,
            placeholder="C:/path/to/service-account.json",
            help="If provided, .gdoc/.gsheet/.gslides files will be exported and ingested automatically.",
        )

        if st.button("Scan folder", type="primary", disabled=not folder_input):
            folder = Path(folder_input.strip())

            if not folder.exists():
                st.error(f"Path does not exist: `{folder}`")
            elif not folder.is_dir():
                st.error(f"Not a directory: `{folder}`")
            else:
                files, google_shortcuts, unsupported = _scan_folder(folder)
                credentials_path = resolve_credentials_path(Path(service_account_input.strip())) if service_account_input.strip() else resolve_credentials_path()
                converted_shortcuts, skipped_shortcuts, failed_shortcuts = _convert_google_shortcuts(
                    google_shortcuts,
                    root_folder=folder,
                    credentials_path=credentials_path,
                )
                files = files + converted_shortcuts

                if skipped_shortcuts:
                    st.warning(
                        f"Skipped {len(skipped_shortcuts)} Google Workspace shortcut file(s). "
                        "Add a Google service account JSON path to convert them during ingestion."
                    )
                if converted_shortcuts:
                    st.success(f"Converted {len(converted_shortcuts)} Google Workspace shortcut file(s) for ingestion.")
                if failed_shortcuts:
                    st.error(f"{len(failed_shortcuts)} Google Workspace shortcut file(s) failed to convert.")
                if unsupported:
                    with st.expander(f"Skipped {len(unsupported)} unsupported file(s)"):
                        for path in unsupported[:200]:
                            st.write(str(path.relative_to(folder)))
                if skipped_shortcuts:
                    with st.expander("Skipped Google Workspace shortcut files"):
                        for name, msg in skipped_shortcuts[:200]:
                            st.write(f"⚪ **{name}** — {msg}")
                if failed_shortcuts:
                    with st.expander("Failed Google Workspace conversions", expanded=True):
                        for name, msg in failed_shortcuts[:200]:
                            st.write(f"❌ **{name}** — {msg}")

                if not files:
                    st.warning(
                        f"No supported files found under `{folder}`. "
                        f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
                    )
                else:
                    st.write(f"Found **{len(files)}** file(s). Ingesting…")
                    progress = st.progress(0.0)
                    ok_count = 0
                    fail_count = 0
                    results_log = []

                    for i, file_path in enumerate(files, start=1):
                        ok, msg = _ingest_file_from_path(file_path)
                        if ok:
                            ok_count += 1
                            results_log.append(("✅", file_path.name, msg))
                        else:
                            fail_count += 1
                            results_log.append(("❌", file_path.name, msg))
                        progress.progress(i / len(files))

                    progress.empty()
                    if ok_count:
                        st.success(f"Ingested {ok_count} file(s) successfully.")
                    if fail_count:
                        st.error(f"{fail_count} file(s) failed.")

                    with st.expander("Details", expanded=fail_count > 0):
                        for icon, name, msg in results_log:
                            st.write(f"{icon} **{name}** — {msg}")

                    save_init_bridge(
                        source_folder=str(folder),
                        note="Upload page ingested folder successfully.",
                        show_drive_cta=False,
                    )


if __name__ == "__main__":
    main()
