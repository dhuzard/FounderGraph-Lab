#!/usr/bin/env python3
"""Backup and reset demo state files for a clean ingestion session."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
VAULT_DIR = PROJECT_ROOT / "vault"

JSON_FILES = [
    DATA_DIR / "knowledge" / "validated_entities.json",
    DATA_DIR / "knowledge" / "validated_relations.json",
    DATA_DIR / "staging" / "documents.json",
    DATA_DIR / "staging" / "candidate_entities.json",
    DATA_DIR / "staging" / "candidate_relations.json",
    DATA_DIR / "staging" / "shacl_violations.json",
]

VAULT_FOLDERS = [
    VAULT_DIR / "documents",
    VAULT_DIR / "entities",
    VAULT_DIR / "audits",
]


@dataclass
class ResetSummary:
    backup_dir: Path
    reset_files: int
    removed_vault_files: int


def _ensure_empty_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]\n", encoding="utf-8")


def reset_demo_state(*, clear_audits: bool = False) -> ResetSummary:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = DATA_DIR / "backups" / f"demo-reset-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    reset_files = 0
    removed_vault_files = 0

    for path in JSON_FILES:
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
        _ensure_empty_json(path)
        reset_files += 1

    for folder in VAULT_FOLDERS:
        if folder.name == "audits" and not clear_audits:
            continue
        if not folder.exists():
            continue
        for item in folder.rglob("*"):
            if item.is_file():
                rel = item.relative_to(PROJECT_ROOT)
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                item.unlink()
                removed_vault_files += 1

    return ResetSummary(
        backup_dir=backup_dir,
        reset_files=reset_files,
        removed_vault_files=removed_vault_files,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset demo state to a clean baseline")
    parser.add_argument(
        "--clear-audits",
        action="store_true",
        help="Also clear vault/audits files (default keeps audits)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = reset_demo_state(clear_audits=args.clear_audits)
    print(f"Backup: {summary.backup_dir}")
    print(f"Reset JSON files: {summary.reset_files}")
    print(f"Removed vault files: {summary.removed_vault_files}")


if __name__ == "__main__":
    main()
