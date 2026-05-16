#!/usr/bin/env python3
"""Reset local demo state and ingest the contradictory sample markdown files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.file_store import FileStoreError, ingest_document  # noqa: E402
from app.services.init_bridge import save_init_bridge  # noqa: E402
from scripts.reset_demo_state import reset_demo_state  # noqa: E402

CONTRADICTORY_SAMPLE_FILES = (
    PROJECT_ROOT / "sample_data" / "contradictory_pitch.md",
    PROJECT_ROOT / "sample_data" / "contradictory_interview.md",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset state and ingest contradictory sample markdown files"
    )
    parser.add_argument(
        "--clear-audits",
        action="store_true",
        help="Also clear files in vault/audits while resetting state",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = reset_demo_state(clear_audits=args.clear_audits)
    print(f"Reset demo state. Backup: {summary.backup_dir}")
    print(f"Reset JSON files: {summary.reset_files}")
    print(f"Removed vault files: {summary.removed_vault_files}")

    ingested = 0
    for path in CONTRADICTORY_SAMPLE_FILES:
        if not path.exists():
            print(f"Missing sample file: {path}", file=sys.stderr)
            return 2
        try:
            with path.open("rb") as handle:
                ingest_document(handle, filename=path.name)
        except (FileStoreError, OSError) as exc:
            print(f"Failed to ingest {path.name}: {exc}", file=sys.stderr)
            return 1
        ingested += 1
        print(f"Ingested {path.relative_to(PROJECT_ROOT)}")

    bridge_path = save_init_bridge(
        source_folder=str((PROJECT_ROOT / "sample_data").resolve()),
        note="Contradictory sample seeded via `make demo`.",
        show_drive_cta=False,
    )
    print(f"Saved init bridge context: {bridge_path.relative_to(PROJECT_ROOT)}")
    print(f"Demo ready. Ingested {ingested} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
