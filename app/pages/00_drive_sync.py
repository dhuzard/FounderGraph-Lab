from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.services.init_bridge import save_init_bridge
from scripts.export_google_drive_folder import GOOGLE_EXPORTS, run_export


st.set_page_config(page_title="Drive Sync", page_icon="FG", layout="wide")
st.title("Drive Sync")
st.caption("Export Google Drive folder content to local files, then ingest in Upload.")

st.info(
    "Use a service account key JSON with Drive API enabled, and share the target Drive folder "
    "with the service account email first."
)

with st.form("drive_sync_form"):
    folder_id = st.text_input("Drive folder ID", placeholder="1abcDEF...folderId")
    service_account_file = st.text_input(
        "Service account JSON path",
        value="",
        placeholder="/app/secrets/service-account.json or C:/.../service-account.json",
        help="If running in Docker, this path must exist inside the app container.",
    )
    output_dir = st.text_input("Local export output folder", value="data/drive_backup")

    format_options = sorted({fmt for mapping in GOOGLE_EXPORTS.values() for fmt in mapping.keys()})
    selected_formats = st.multiselect(
        "Target export formats",
        options=format_options,
        default=[fmt for fmt in ["docx", "pdf", "txt", "xlsx", "csv", "pptx", "png"] if fmt in format_options],
    )
    include_non_google = st.checkbox("Also download non-Google files in original format", value=True)

    submitted = st.form_submit_button("Export Drive folder", type="primary")

if submitted:
    if not folder_id.strip():
        st.error("Drive folder ID is required.")
        st.stop()
    if not service_account_file.strip():
        st.error("Service account JSON path is required.")
        st.stop()
    if not selected_formats:
        st.error("Select at least one target format.")
        st.stop()

    credentials_path = Path(service_account_file.strip())
    if not credentials_path.exists():
        st.error(f"Service account key not found: {credentials_path}")
        st.stop()

    out_path = Path(output_dir.strip())
    with st.spinner("Exporting from Google Drive..."):
        try:
            manifest_path, counts, results = run_export(
                folder_id=folder_id.strip(),
                output_dir=out_path,
                service_account_file=credentials_path,
                formats=[fmt.lower() for fmt in selected_formats],
                include_non_google=include_non_google,
            )
        except Exception as exc:
            st.error(f"Drive export failed: {exc}")
            st.stop()

    save_init_bridge(
        source_folder=str(out_path.resolve()),
        note="Drive export completed. Use Upload -> Ingest folder to import these files.",
        show_drive_cta=False,
    )

    st.success(f"Export completed. Manifest: {manifest_path}")
    metric_cols = st.columns(5)
    metric_cols[0].metric("Exported", counts["exported"])
    metric_cols[1].metric("Downloaded", counts["downloaded"])
    metric_cols[2].metric("Skipped", counts["skipped"])
    metric_cols[3].metric("Failed", counts["failed"])
    metric_cols[4].metric("Total", counts["total"])

    with st.expander("Result details", expanded=counts["failed"] > 0):
        for item in results[:500]:
            if item.status in {"exported", "downloaded"}:
                st.write(f"✅ {item.file_name} ({item.mime_type}) -> {', '.join(item.outputs)}")
            elif item.status == "skipped":
                st.write(f"⚪ {item.file_name} ({item.mime_type}) - {item.error}")
            else:
                st.write(f"❌ {item.file_name} ({item.mime_type}) - {item.error}")

    st.info("Next: go to Upload -> Ingest folder and use this path:")
    st.code(str(out_path.resolve()))

    if st.button("Open Upload page", type="secondary"):
        try:
            st.switch_page("pages/01_upload.py")
        except Exception:
            st.warning("Could not switch page automatically. Open 'Document Upload' from the sidebar.")
