"""LinkML artifact + Cypher-DDL drift tests (Phase 1)."""

from __future__ import annotations

import filecmp
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "app" / "ontology" / "startup_ontology.linkml.yaml"
GENERATED_DIR = PROJECT_ROOT / "app" / "ontology" / "generated"
GENERATOR_SCRIPT = PROJECT_ROOT / "scripts" / "generate_ontology_artifacts.py"


@pytest.fixture(scope="module")
def fresh_artifacts(tmp_path_factory):
    """Regenerate the artifacts into a tmp dir for comparison.

    Runs the generator as a subprocess (the script re-exec's itself with
    ``PYTHONHASHSEED=0`` to guarantee byte-stable output, so we deliberately
    go through the CLI entrypoint rather than importing the module).
    """
    if not SCHEMA_PATH.exists():
        pytest.skip("LinkML schema missing -- nothing to regenerate against")
    tmp = tmp_path_factory.mktemp("ontology-artifacts")
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR_SCRIPT),
            "--schema",
            str(SCHEMA_PATH),
            "--out-dir",
            str(tmp),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        pytest.skip(
            f"Generator failed (likely missing linkml CLI dep): {result.stderr[:400]}"
        )
    return tmp


def test_generate_artifacts_idempotent(fresh_artifacts, tmp_path_factory):
    """Running the generator twice must produce byte-identical output.

    Re-uses the ``fresh_artifacts`` fixture for run #1 so we only pay the
    LinkML import cost once across the suite; the second generation goes
    into a sibling tmp dir and we diff the four artifacts byte-for-byte.
    """
    out2 = tmp_path_factory.mktemp("gen2")
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR_SCRIPT),
            "--schema",
            str(SCHEMA_PATH),
            "--out-dir",
            str(out2),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        pytest.skip(
            f"Generator failed (missing linkml deps?): {result.stderr[:400]}"
        )

    for name in ("models.py", "schema.json", "shapes.ttl", "cypher_constraints.cypher"):
        a, b = fresh_artifacts / name, out2 / name
        assert filecmp.cmp(a, b, shallow=False), (
            f"{name} differs between two consecutive generator runs -- "
            "fix the generator before relying on the drift check."
        )


def test_linkml_artifacts_in_sync(fresh_artifacts):
    """The committed artifacts must match a fresh regeneration.

    This is the same gate ``make generate-check`` runs in CI.  When it fails
    locally, run ``make generate`` and commit the diff.
    """
    if not GENERATED_DIR.exists():
        pytest.skip("Generated artifacts not committed yet -- run `make generate`")
    for name in ("models.py", "schema.json", "shapes.ttl", "cypher_constraints.cypher"):
        committed = GENERATED_DIR / name
        fresh = fresh_artifacts / name
        if not committed.exists():
            pytest.fail(
                f"Committed artifact missing: {committed}.  "
                "Run `make generate` and commit the result."
            )
        assert filecmp.cmp(fresh, committed, shallow=False), (
            f"{name} drift detected -- run `make generate` to refresh "
            "app/ontology/generated/."
        )


def test_pydantic_models_round_trip():
    """An Assumption built from generated models survives serialize/parse."""
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(GENERATED_DIR))
    try:
        import models  # type: ignore
    finally:
        sys.path.pop(0)

    a = models.Assumption(
        id="a-round-trip",
        criticality="high",
        name="Customers care about FAIR data",
        description="Hypothesis under test",
        validation_status="pending",
    )
    dumped = a.model_dump()
    assert dumped["id"] == "a-round-trip"
    assert dumped["criticality"] == "high"

    # Re-hydrate from the JSON form and confirm equality on the key fields.
    rehydrated = models.Assumption.model_validate_json(a.model_dump_json())
    assert rehydrated.id == a.id
    assert rehydrated.criticality == a.criticality
    assert rehydrated.name == a.name


def test_cypher_ddl_matches_runtime_indexes():
    """Each declared predicate must have one ``CREATE INDEX rel_<NAME>_id``
    statement and there must be exactly one ``CREATE CONSTRAINT entity_id``.
    """
    ddl_path = GENERATED_DIR / "cypher_constraints.cypher"
    if not ddl_path.exists():
        pytest.skip("DDL not generated -- run `make generate`")

    text = ddl_path.read_text(encoding="utf-8")
    # Constraint check -- exactly one entity_id constraint.
    entity_id_matches = re.findall(
        r"CREATE CONSTRAINT entity_id\b", text
    )
    assert len(entity_id_matches) == 1, (
        f"Expected exactly one ``CREATE CONSTRAINT entity_id`` line, found "
        f"{len(entity_id_matches)}"
    )

    # Each declared relation predicate must produce one CREATE INDEX rel_<NAME>_id.
    schema_json = GENERATED_DIR / "schema.json"
    assert schema_json.exists(), "schema.json missing -- run `make generate`"
    payload = json.loads(schema_json.read_text(encoding="utf-8"))
    declared = {entry["predicate"] for entry in payload.get("x-foundergraph-relations", [])}
    # Utility predicates included by the generator regardless of LinkML declaration.
    declared |= {"MENTIONS", "SOURCE_OF", "RELATED_TO", "SUPERSEDED_BY"}
    for predicate in declared:
        pattern = rf"CREATE INDEX rel_{re.escape(predicate)}_id\b"
        matches = re.findall(pattern, text)
        assert len(matches) == 1, (
            f"Predicate {predicate!r} must appear in exactly one ``CREATE "
            f"INDEX rel_<NAME>_id`` line, found {len(matches)} in the DDL."
        )
