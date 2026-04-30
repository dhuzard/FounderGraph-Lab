from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app.services.validation_store import (
    DEFAULT_KNOWLEDGE_DIR,
    VALIDATION_STATUSES,
    ValidationStore,
)
from app.services.demo_seed import seed_demo_candidates


st.set_page_config(page_title="Validate Knowledge", layout="wide")


def editable_frame(records: list[dict[str, Any]], kind: str) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    rows = []
    for record in records:
        row = dict(record)
        row["provenance"] = json.dumps(record.get("provenance", {}), ensure_ascii=False)
        row["metadata"] = json.dumps(record.get("metadata", {}), ensure_ascii=False)
        rows.append(row)

    column_config = {
        "status": st.column_config.SelectboxColumn("status", options=list(VALIDATION_STATUSES)),
        "source_snippet": st.column_config.TextColumn("source_snippet", width="large"),
        "description": st.column_config.TextColumn("description", width="large"),
    }
    if kind == "relation":
        column_config["type"] = st.column_config.TextColumn("type", help="Must match graph ontology whitelist.")
    else:
        column_config["label"] = st.column_config.TextColumn("label", help="Must match graph ontology whitelist.")

    return st.data_editor(
        pd.DataFrame(rows),
        key=f"{kind}_editor",
        column_config=column_config,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
    )


def records_from_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = frame.fillna("").to_dict(orient="records")
    parsed = []
    for record in records:
        item = dict(record)
        for key in ("provenance", "metadata"):
            value = item.get(key)
            if isinstance(value, str):
                try:
                    item[key] = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError:
                    item[key] = {"raw": value}
        parsed.append(item)
    return parsed


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

entities = store.load_entities()
relations = store.load_relations()

entity_validated = sum(1 for item in entities if item.get("status") == "validated")
relation_validated = sum(1 for item in relations if item.get("status") == "validated")

metric_cols = st.columns(4)
metric_cols[0].metric("Entity candidates", len(entities))
metric_cols[1].metric("Validated entities", entity_validated)
metric_cols[2].metric("Relation candidates", len(relations))
metric_cols[3].metric("Validated relations", relation_validated)

tab_entities, tab_relations = st.tabs(["Entities", "Relations"])

with tab_entities:
    st.subheader("Entities")
    entity_frame = editable_frame(entities, "entity")
    if st.button("Save entity validations", type="primary", disabled=entity_frame.empty):
        path = store.save_entities(records_from_frame(entity_frame))
        st.success(f"Saved {path}")

with tab_relations:
    st.subheader("Relations")
    relation_frame = editable_frame(relations, "relation")
    if st.button("Save relation validations", type="primary", disabled=relation_frame.empty):
        path = store.save_relations(records_from_frame(relation_frame))
        st.success(f"Saved {path}")

with st.expander("Write policy"):
    st.write(
        "Only records marked `validated` should be exported to Neo4j. "
        "Provenance and source snippets are retained for auditability."
    )
