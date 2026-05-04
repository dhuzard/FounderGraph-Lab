from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app.services.entity_extractor import EVIDENCE_GRADES, REVIEWER_CONFIDENCES
from app.services.validation_store import (
    DEFAULT_KNOWLEDGE_DIR,
    VALIDATION_STATUSES,
    ValidationStore,
)
from app.services.demo_seed import seed_demo_candidates


st.set_page_config(page_title="Validate Knowledge", layout="wide")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_records(
    records: list[dict[str, Any]],
    *,
    status_filter: list[str],
    type_filter: list[str],
    doc_filter: list[str],
    grade_filter: list[str],
) -> list[dict[str, Any]]:
    out = records
    if status_filter:
        out = [r for r in out if r.get("status", r.get("validation_status", "pending")) in status_filter]
    if type_filter:
        out = [r for r in out if r.get("type", "") in type_filter]
    if doc_filter:
        out = [r for r in out if r.get("source_document_id", r.get("source_file", "")) in doc_filter]
    if grade_filter:
        out = [r for r in out if r.get("evidence_grade", "") in grade_filter]
    return out


def _entity_label_map(entities: list[dict[str, Any]]) -> dict[str, str]:
    """Build id → label lookup for relation display."""
    return {
        str(e.get("id", "")): str(e.get("label") or e.get("name") or e.get("id", ""))
        for e in entities
        if e.get("id")
    }


def _enrich_relations_for_display(
    relations: list[dict[str, Any]],
    label_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Add human-readable source_label and target_label columns."""
    enriched = []
    for rel in relations:
        row = dict(rel)
        src_id = str(row.get("source_entity_id") or row.get("subject_id") or "")
        tgt_id = str(row.get("target_entity_id") or row.get("object_id") or "")
        row["source_label"] = label_map.get(src_id, src_id)
        row["target_label"] = label_map.get(tgt_id, tgt_id)
        enriched.append(row)
    return enriched


def editable_frame(
    records: list[dict[str, Any]],
    kind: str,
    label_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    rows = []
    for record in records:
        row = dict(record)
        row["provenance"] = json.dumps(record.get("provenance", {}), ensure_ascii=False)
        row["metadata"] = json.dumps(record.get("metadata", {}), ensure_ascii=False)
        if kind == "relation" and label_map is not None:
            src_id = str(row.get("source_entity_id") or row.get("subject_id") or "")
            tgt_id = str(row.get("target_entity_id") or row.get("object_id") or "")
            row["source_label"] = label_map.get(src_id, src_id)
            row["target_label"] = label_map.get(tgt_id, tgt_id)
        rows.append(row)

    # Column ordering: most important columns first
    if kind == "relation":
        priority = ["status", "source_label", "type", "target_label", "evidence_grade", "source_snippet", "reviewer_comment"]
    else:
        priority = ["status", "type", "label", "evidence_grade", "reviewer_confidence", "source_snippet", "description", "reviewer_comment"]

    df = pd.DataFrame(rows)
    existing_priority = [c for c in priority if c in df.columns]
    other_cols = [c for c in df.columns if c not in priority]
    df = df[existing_priority + other_cols]

    column_config: dict[str, Any] = {
        "status": st.column_config.SelectboxColumn(
            "status",
            options=list(VALIDATION_STATUSES),
            help="Only 'validated' records are written to Neo4j.",
        ),
        "evidence_grade": st.column_config.SelectboxColumn(
            "evidence_grade",
            options=[""] + list(EVIDENCE_GRADES),
            help="How directly the document supports this item (set by LLM).",
        ),
        "reviewer_confidence": st.column_config.SelectboxColumn(
            "reviewer_confidence",
            options=[""] + list(REVIEWER_CONFIDENCES),
            help="Your assessment of how trustworthy this item is.",
        ),
        "reviewer_comment": st.column_config.TextColumn(
            "reviewer_comment",
            help="Optional note explaining your decision.",
            width="large",
        ),
        "source_snippet": st.column_config.TextColumn("source_snippet", width="large"),
        "description": st.column_config.TextColumn("description", width="large"),
    }
    if kind == "relation":
        column_config["source_label"] = st.column_config.TextColumn("source", disabled=True)
        column_config["target_label"] = st.column_config.TextColumn("target", disabled=True)
        column_config["type"] = st.column_config.TextColumn(
            "relation type",
            help="Must match the ontology whitelist.",
            disabled=True,
        )
    else:
        column_config["label"] = st.column_config.TextColumn(
            "label",
            help="Must match the ontology entity type whitelist.",
            disabled=True,
        )
        column_config["type"] = st.column_config.TextColumn("type", disabled=True)

    return st.data_editor(
        df,
        key=f"{kind}_editor",
        column_config=column_config,
        num_rows="fixed",
        use_container_width=True,
        hide_index=True,
    )


def records_from_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = frame.fillna("").to_dict(orient="records")
    parsed = []
    for record in records:
        item = {k: v for k, v in record.items() if k not in ("source_label", "target_label")}
        for key in ("provenance", "metadata"):
            value = item.get(key)
            if isinstance(value, str):
                try:
                    item[key] = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError:
                    item[key] = {"raw": value}
        parsed.append(item)
    return parsed


def _apply_bulk_status(
    records: list[dict[str, Any]],
    new_status: str,
    only_if_status: str | None = None,
) -> list[dict[str, Any]]:
    updated = []
    for r in records:
        item = dict(r)
        if only_if_status is None or item.get("status", item.get("validation_status")) == only_if_status:
            item["status"] = new_status
            item["validation_status"] = new_status
        updated.append(item)
    return updated


def _status_of(record: dict[str, Any]) -> str:
    return str(record.get("status") or record.get("validation_status") or "pending")


def _review_lane(record: dict[str, Any]) -> str:
    status = _status_of(record)
    if status != "pending":
        return status
    grade = str(record.get("evidence_grade") or "")
    snippet = str(record.get("source_snippet") or "").strip()
    if grade == "direct_quote" and snippet:
        return "ready"
    if grade in {"inference", "speculation"} or not snippet:
        return "needs_attention"
    return "standard"


def _record_title(record: dict[str, Any], kind: str, label_map: dict[str, str] | None = None) -> str:
    if kind == "relation":
        source = label_map.get(str(record.get("source_entity_id")), str(record.get("source_entity_id", ""))) if label_map else str(record.get("source_entity_id", ""))
        target = label_map.get(str(record.get("target_entity_id")), str(record.get("target_entity_id", ""))) if label_map else str(record.get("target_entity_id", ""))
        return f"{source} -> {record.get('type', '')} -> {target}"
    return f"{record.get('label') or record.get('name') or record.get('id')} ({record.get('type', 'Entity')})"


def _merge_record_update(
    all_records: list[dict[str, Any]],
    updated_record: dict[str, Any],
) -> list[dict[str, Any]]:
    updated_id = str(updated_record.get("id", ""))
    return [updated_record if str(record.get("id", "")) == updated_id else record for record in all_records]


def review_workbench(
    *,
    kind: str,
    records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    save_records: Any,
    label_map: dict[str, str] | None = None,
) -> None:
    if not records:
        return

    lane_counts = {
        "ready": sum(1 for record in records if _review_lane(record) == "ready"),
        "standard": sum(1 for record in records if _review_lane(record) == "standard"),
        "needs_attention": sum(1 for record in records if _review_lane(record) == "needs_attention"),
    }
    lane_cols = st.columns(3)
    lane_cols[0].metric("Ready quote-backed", lane_counts["ready"])
    lane_cols[1].metric("Standard review", lane_counts["standard"])
    lane_cols[2].metric("Needs attention", lane_counts["needs_attention"])

    pending_first = sorted(
        records,
        key=lambda record: (
            _status_of(record) != "pending",
            {"needs_attention": 0, "standard": 1, "ready": 2}.get(_review_lane(record), 3),
            _record_title(record, kind, label_map).lower(),
        ),
    )
    selected = st.selectbox(
        "Focused candidate",
        pending_first,
        format_func=lambda record: f"[{_status_of(record)}] {_record_title(record, kind, label_map)}",
        key=f"{kind}_focused_candidate",
    )

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{_record_title(selected, kind, label_map)}**")
        if selected.get("description"):
            st.write(selected["description"])
        st.caption(
            f"Evidence: {selected.get('evidence_grade') or 'ungraded'} | "
            f"Source: {selected.get('source_document_id') or selected.get('source_file') or 'unknown'}"
        )
        snippet = str(selected.get("source_snippet") or "").strip()
        if snippet:
            st.text_area("Source snippet", snippet, height=160, disabled=True, key=f"{kind}_snippet")
        else:
            st.warning("No source snippet was extracted. This should usually be marked needs_more_evidence.")

    with right:
        with st.form(f"{kind}_focused_review_form"):
            status = st.radio(
                "Decision",
                options=list(VALIDATION_STATUSES),
                index=list(VALIDATION_STATUSES).index(_status_of(selected))
                if _status_of(selected) in VALIDATION_STATUSES else 0,
                horizontal=False,
            )
            confidence = selected.get("reviewer_confidence") or "ungraded"
            if kind == "entity":
                confidence = st.selectbox(
                    "Reviewer confidence",
                    options=list(REVIEWER_CONFIDENCES),
                    index=list(REVIEWER_CONFIDENCES).index(confidence)
                    if confidence in REVIEWER_CONFIDENCES else len(REVIEWER_CONFIDENCES) - 1,
                )
            comment = st.text_area("Reviewer note", value=str(selected.get("reviewer_comment") or ""), height=110)
            submitted = st.form_submit_button("Save focused review", type="primary")
            if submitted:
                updated = dict(selected)
                updated["status"] = status
                updated["validation_status"] = status
                updated["reviewer_comment"] = comment
                if kind == "entity":
                    updated["reviewer_confidence"] = confidence
                path = save_records(_merge_record_update(all_records, updated))
                st.success(f"Saved {path}")
                st.rerun()


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

st.title("Human Knowledge Validation")
st.caption("Review extracted candidates before they are eligible for graph writes.")

knowledge_dir = Path(st.sidebar.text_input("Knowledge JSON directory", str(DEFAULT_KNOWLEDGE_DIR)))
store = ValidationStore(knowledge_dir)

with st.sidebar:
    st.header("Demo data")
    if st.button("Seed demo candidates"):
        entity_path, relation_path = seed_demo_candidates(overwrite=True)
        st.success(f"Seeded {entity_path.name} and {relation_path.name}")
        st.rerun()

    st.divider()
    st.header("Filters")
    status_filter = st.multiselect(
        "Status",
        options=list(VALIDATION_STATUSES),
        default=["pending"],
        help="Leave empty to show all statuses.",
    )
    grade_filter = st.multiselect(
        "Evidence grade",
        options=list(EVIDENCE_GRADES),
        help="Leave empty to show all grades.",
    )

all_entities = store.load_entities()
all_relations = store.load_relations()

entity_types = sorted({e.get("type", "") for e in all_entities if e.get("type")})
doc_ids = sorted({e.get("source_document_id") or e.get("source_file") or "" for e in all_entities} - {""})

with st.sidebar:
    type_filter = st.multiselect("Entity type", options=entity_types)
    doc_filter = st.multiselect("Source document", options=doc_ids)

entity_validated = sum(1 for e in all_entities if e.get("status") == "validated")
relation_validated = sum(1 for r in all_relations if r.get("status") == "validated")

metric_cols = st.columns(4)
metric_cols[0].metric("Entity candidates", len(all_entities))
metric_cols[1].metric("Validated entities", entity_validated)
metric_cols[2].metric("Relation candidates", len(all_relations))
metric_cols[3].metric("Validated relations", relation_validated)

label_map = _entity_label_map(all_entities)

entities = _filter_records(
    all_entities,
    status_filter=status_filter,
    type_filter=type_filter,
    doc_filter=doc_filter,
    grade_filter=grade_filter,
)
relations = _filter_records(
    all_relations,
    status_filter=status_filter,
    type_filter=[],
    doc_filter=doc_filter,
    grade_filter=grade_filter,
)

tab_entities, tab_relations = st.tabs([
    f"Entities ({len(entities)} shown)",
    f"Relations ({len(relations)} shown)",
])

with tab_entities:
    st.subheader("Entities")
    if not entities:
        st.info("No entity candidates match the current filters.")
    else:
        review_workbench(
            kind="entity",
            records=entities,
            all_records=all_entities,
            save_records=store.save_entities,
        )
        st.divider()
        col_approve, col_reject, col_spacer = st.columns([1, 1, 6])
        with col_approve:
            if st.button("Approve all pending", key="approve_all_entities"):
                updated = _apply_bulk_status(all_entities, "validated", only_if_status="pending")
                store.save_entities(updated)
                st.success(f"Approved all pending entities.")
                st.rerun()
        with col_reject:
            if st.button("Reject all low-grade", key="reject_low_entities"):
                to_reject = [
                    r for r in all_entities
                    if r.get("evidence_grade") == "speculation"
                    and r.get("status") == "pending"
                ]
                if to_reject:
                    ids_to_reject = {str(r["id"]) for r in to_reject}
                    updated = [
                        {**r, "status": "rejected", "validation_status": "rejected"}
                        if str(r.get("id", "")) in ids_to_reject else r
                        for r in all_entities
                    ]
                    store.save_entities(updated)
                    st.success(f"Rejected {len(to_reject)} speculation-grade entities.")
                    st.rerun()
                else:
                    st.info("No pending speculation-grade entities to reject.")

        entity_frame = editable_frame(entities, "entity")
        if st.button("Save entity validations", type="primary", disabled=entity_frame.empty):
            edited = records_from_frame(entity_frame)
            # Merge edited records back into the full list (preserving unfiltered records)
            edited_by_id = {str(r.get("id", "")): r for r in edited}
            merged = [edited_by_id.get(str(r.get("id", "")), r) for r in all_entities]
            path = store.save_entities(merged)
            st.success(f"Saved {path}")

with tab_relations:
    st.subheader("Relations")
    if not relations:
        st.info("No relation candidates match the current filters.")
    else:
        review_workbench(
            kind="relation",
            records=relations,
            all_records=all_relations,
            save_records=store.save_relations,
            label_map=label_map,
        )
        st.divider()
        col_approve, col_spacer = st.columns([1, 7])
        with col_approve:
            if st.button("Approve all pending", key="approve_all_relations"):
                updated = _apply_bulk_status(all_relations, "validated", only_if_status="pending")
                store.save_relations(updated)
                st.success("Approved all pending relations.")
                st.rerun()

        relation_frame = editable_frame(relations, "relation", label_map=label_map)
        if st.button("Save relation validations", type="primary", disabled=relation_frame.empty):
            edited = records_from_frame(relation_frame)
            edited_by_id = {str(r.get("id", "")): r for r in edited}
            merged = [edited_by_id.get(str(r.get("id", "")), r) for r in all_relations]
            path = store.save_relations(merged)
            st.success(f"Saved {path}")

with st.expander("Write policy"):
    st.write(
        "Only records marked `validated` are written to Neo4j. "
        "Provenance, source snippets, evidence grades, and reviewer notes are retained for auditability. "
        "Use `needs_more_evidence` to flag items that require additional supporting documents before approval."
    )
