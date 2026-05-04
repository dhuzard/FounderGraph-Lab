from __future__ import annotations

from pathlib import Path

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

from app.services.extractors import GOOGLE_WORKSPACE_SHORTCUTS, SUPPORTED_EXTENSIONS, is_supported_file
from app.services.file_store import FileStoreError, ingest_document
from app.services.init_bridge import load_init_bridge, save_init_bridge


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

        if st.button("Scan folder", type="primary", disabled=not folder_input):
            folder = Path(folder_input.strip())

            if not folder.exists():
                st.error(f"Path does not exist: `{folder}`")
            elif not folder.is_dir():
                st.error(f"Not a directory: `{folder}`")
            else:
                files, google_shortcuts, unsupported = _scan_folder(folder)

                if google_shortcuts:
                    st.warning(
                        f"Skipped {len(google_shortcuts)} Google Workspace shortcut file(s) "
                        "(.gdoc/.gslides/.gsheet). Export those files from Google Drive first; "
                        "the local shortcut only contains an ID, not the document text."
                    )
                if unsupported:
                    with st.expander(f"Skipped {len(unsupported)} unsupported file(s)"):
                        for path in unsupported[:200]:
                            st.write(str(path.relative_to(folder)))

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
