#!/usr/bin/env python3
"""HITL CLI to initialize the FounderGraph-Lab ontology for a specific startup.

Usage:
    python scripts/init_ontology.py
    python scripts/init_ontology.py --docs /path/to/your/docs
    python scripts/init_ontology.py --reset   # start fresh, ignore existing YAML

The script walks you through:
  1. Startup context (name, domain, goals)
  2. Document discovery and text extraction
  3. LLM-assisted entity type suggestions (skipped if Ollama is unavailable)
  4. Interactive entity type review (keep / drop / rename / add)
  5. Custom relation review
  6. Save to app/ontology/startup_ontology.yaml
  7. Optional Neo4j schema initialization
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import webbrowser
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

# Ensure project root is on sys.path when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.ontology_service import (  # noqa: E402
    EntityClassDef,
    OntologyConfig,
    load_ontology,
    save_ontology,
)
from app.services.file_store import FileStoreError, ingest_document  # noqa: E402
from app.services.init_bridge import save_init_bridge  # noqa: E402
from scripts.reset_demo_state import reset_demo_state  # noqa: E402

# ---------------------------------------------------------------------------
# Terminal formatting helpers
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"


def _c(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + RESET


def hr(char: str = "─", width: int = 70) -> None:
    print(_c(char * width, DIM))


def section(title: str) -> None:
    print()
    hr()
    print(_c(f"  {title}", BOLD, CYAN))
    hr()


def ok(msg: str) -> None:
    print(_c(f"  ✓ {msg}", GREEN))


def warn(msg: str) -> None:
    print(_c(f"  ⚠ {msg}", YELLOW))


def err(msg: str) -> None:
    print(_c(f"  ✗ {msg}", RED))


def wrap(text: str, indent: int = 4) -> str:
    prefix = " " * indent
    return textwrap.fill(text, width=74, initial_indent=prefix, subsequent_indent=prefix)


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    display = f"\n  {_c(prompt, BOLD)}"
    if default:
        display += _c(f" [{default}]", DIM)
    display += "\n  > "
    try:
        value = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return value or default


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str = "1") -> str:
    print(f"\n  {_c(prompt, BOLD)}")
    for key, label in options:
        print(f"    {_c(key + ')', DIM)} {label}")
    display = "  > "
    try:
        value = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return value or default


def ask_multi(prompt: str, options: list[tuple[str, str]], defaults: str = "") -> list[str]:
    print(f"\n  {_c(prompt, BOLD)}")
    print(_c("  (comma-separated numbers, e.g. 1,3,4)", DIM))
    for key, label in options:
        print(f"    {_c(key + ')', DIM)} {label}")
    display = "  > "
    try:
        raw = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    raw = raw or defaults
    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            selected.append(part)
    return selected


def ask_yn(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    display = f"\n  {_c(prompt, BOLD)} {_c(hint, DIM)}\n  > "
    try:
        raw = input(display).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not raw:
        return default
    return raw.startswith("y")


# ---------------------------------------------------------------------------
# Document discovery and extraction
# ---------------------------------------------------------------------------

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".csv"}


def discover_documents(root: Path) -> list[Path]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            found.append(path)
    return found


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".csv":
            import csv
            rows = []
            with path.open(encoding="utf-8", errors="replace") as fh:
                reader = csv.reader(fh)
                for i, row in enumerate(reader):
                    rows.append(", ".join(row))
                    if i > 200:
                        break
            return "\n".join(rows)
        if suffix == ".pdf":
            import fitz
            doc = fitz.open(str(path))
            return "\n".join(page.get_text() for page in doc)
        if suffix == ".docx":
            import docx
            document = docx.Document(str(path))
            return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:
        return f"[extraction failed: {exc}]"
    return ""


# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------

_SUGGEST_PROMPT = """\
You are helping configure a startup knowledge graph.

Startup domain: {domain}
Goals: {goals}
Startup description: {description}

Default entity types already included:
{default_types}

Document excerpts (first 800 chars each):
{excerpts}

Suggest up to 5 ADDITIONAL domain-specific entity types that would be \
valuable for this startup and are NOT already in the default list above.
For each suggestion, return: name (PascalCase, no spaces), description \
(1 sentence), why_needed (1 sentence).

Return strict JSON only:
{{"suggestions": [{{"name": "...", "description": "...", "why_needed": "..."}}]}}
"""


def _ollama_available(ollama_url: str) -> bool:
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=4):
            return True
    except Exception:
        return False


def _ollama_generate(ollama_url: str, model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode()).get("response", "")
    except Exception as exc:
        return f"[error: {exc}]"


def llm_suggest_entity_types(
    config: OntologyConfig,
    excerpts: list[str],
    startup_description: str,
    ollama_url: str,
    model: str,
) -> list[dict]:
    prompt = _SUGGEST_PROMPT.format(
        domain=config.domain or "general startup",
        goals=", ".join(config.goals) or "knowledge management",
        description=startup_description,
        default_types="\n".join(f"  - {n}: {v.description}" for n, v in config.entity_classes.items()),
        excerpts="\n\n".join(f"[{i+1}] {ex[:800]}" for i, ex in enumerate(excerpts[:6])),
    )
    raw = _ollama_generate(ollama_url, model, prompt)
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end]).get("suggestions", [])
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Phase: context questions
# ---------------------------------------------------------------------------

DOMAINS = [
    ("1", "Biotech / Life Sciences"),
    ("2", "SaaS / B2B Software"),
    ("3", "Deep Tech / Hardware / AI"),
    ("4", "Marketplace / Platform"),
    ("5", "Climate / Clean Tech"),
    ("6", "FinTech"),
    ("7", "Other"),
]

GOALS = [
    ("1", "Validate key assumptions (assumption testing)"),
    ("2", "Prepare for investor due diligence"),
    ("3", "Apply for grants / public funding"),
    ("4", "Systematize customer discovery"),
    ("5", "Track progress toward milestones"),
    ("6", "Map technical and regulatory risks"),
    ("7", "Build internal knowledge base"),
]

GOAL_LABELS = {
    "1": "assumption_validation",
    "2": "investor_readiness",
    "3": "grant_applications",
    "4": "customer_discovery",
    "5": "milestone_tracking",
    "6": "risk_mapping",
    "7": "knowledge_management",
}


def gather_context(existing: OntologyConfig) -> tuple[str, str, str, list[str]]:
    name = ask("What is your startup's name?", default="")
    description = ask(
        "Describe your startup in 1–2 sentences "
        "(what problem, who are the customers, what is the solution):",
    )
    domain_key = ask_choice("What is your primary domain?", DOMAINS, default="1")
    domain_label = dict(DOMAINS).get(domain_key, DOMAINS[0][1])

    goal_keys = ask_multi(
        "What are your primary goals for this knowledge graph?",
        GOALS,
        defaults="1,2",
    )
    goals = [GOAL_LABELS.get(k, k) for k in goal_keys if k]

    return name, description, domain_label, goals


# ---------------------------------------------------------------------------
# Phase: entity type review
# ---------------------------------------------------------------------------

def _goal_recommended_types(goals: list[str]) -> set[str]:
    mapping = {
        "assumption_validation": {"Assumption", "Evidence", "Experiment"},
        "investor_readiness": {"Investor", "FinancialHypothesis", "Milestone"},
        "grant_applications": {"GrantCall", "Milestone"},
        "customer_discovery": {"CustomerSegment", "Problem", "ValueProposition"},
        "milestone_tracking": {"Milestone", "Decision"},
        "risk_mapping": {"Risk", "RegulatoryConstraint", "TechnicalDependency"},
        "knowledge_management": set(),
    }
    result: set[str] = set()
    for goal in goals:
        result |= mapping.get(goal, set())
    return result


CORE_TYPES = {"Startup", "Founder", "CustomerSegment", "Problem", "Assumption", "Evidence"}


def review_entity_types(
    config: OntologyConfig,
    goals: list[str],
    suggestions: list[dict],
) -> OntologyConfig:
    section("Entity Type Review")
    recommended = _goal_recommended_types(goals)
    print(wrap(
        "Review each entity type. Press Enter to keep, 'd' to drop, "
        "'r' to rename, 'e' to edit description.",
        indent=2,
    ))

    # Mark each type with its origin
    kept: dict[str, EntityClassDef] = {}
    names = list(config.entity_classes.keys())

    for name in names:
        cls = config.entity_classes[name]
        hint = ""
        if name in CORE_TYPES:
            hint = _c(" [core]", DIM)
        elif name in recommended:
            hint = _c(" [recommended for your goals]", GREEN)
        label = f"{_c(name, BOLD)}{hint}: {_c(cls.description, DIM)}"
        action = ask(label, default="keep").lower()

        if action.startswith("d") and name not in CORE_TYPES:
            warn(f"Dropped {name}")
            continue
        elif action.startswith("d") and name in CORE_TYPES:
            warn(f"{name} is a core type and cannot be dropped.")
            kept[name] = cls
        elif action.startswith("r"):
            new_name = ask(f"  New name for {name}:").strip()
            if new_name:
                ok(f"Renamed {name} → {new_name}")
                kept[new_name] = cls
            else:
                kept[name] = cls
        elif action.startswith("e"):
            new_desc = ask(f"  New description for {name}:", default=cls.description)
            kept[name] = EntityClassDef(description=new_desc, fields=cls.fields)
            ok(f"Updated description for {name}")
        else:
            kept[name] = cls

    # LLM suggestions
    if suggestions:
        section("LLM-Suggested Entity Types")
        print(wrap(
            "The LLM found these domain-specific types in your documents. "
            "Press Enter or 'y' to add, 'n' to skip, 'r' to rename before adding.",
            indent=2,
        ))
        for sug in suggestions:
            sname = sug.get("name", "").strip()
            sdesc = sug.get("description", "")
            swhy = sug.get("why_needed", "")
            if not sname or sname in kept:
                continue
            print(f"\n    {_c(sname, BOLD, YELLOW)}: {sdesc}")
            if swhy:
                print(f"      {_c('Why:', DIM)} {swhy}")
            action = ask("Add this type?", default="y").lower()
            if action.startswith("n"):
                continue
            if action.startswith("r"):
                sname = ask("  Name:", default=sname).strip() or sname
            kept[sname] = EntityClassDef(description=sdesc)
            ok(f"Added {sname}")

    # Manual additions
    print()
    while ask_yn("Add a custom entity type?", default=False):
        cname = ask("  Type name (PascalCase):").strip()
        if not cname:
            continue
        cdesc = ask(f"  Description for {cname}:", default="").strip()
        kept[cname] = EntityClassDef(description=cdesc)
        ok(f"Added {cname}")

    config.entity_classes = kept
    return config


# ---------------------------------------------------------------------------
# Phase: relation review
# ---------------------------------------------------------------------------

def review_relations(config: OntologyConfig) -> OntologyConfig:
    section("Relation Review")
    entity_names = sorted(config.entity_classes.keys())

    print(wrap("Current relations in the ontology:", indent=2))
    for i, rel in enumerate(config.relations, 1):
        print(f"    {i:2}. {rel.subject} →{_c(rel.predicate, CYAN)}→ {rel.object}")

    print()
    while ask_yn("Add a custom relation?", default=False):
        print(f"\n    Available entity types: {', '.join(entity_names)}")
        subject = ask("  Subject type (e.g. ClinicalTrial):").strip()
        predicate = ask("  Predicate in UPPER_SNAKE_CASE (e.g. VALIDATES):").strip().upper().replace(" ", "_")
        obj = ask("  Object type (e.g. Assumption):").strip()
        if subject and predicate and obj:
            config.add_relation(subject, predicate, obj)
            ok(f"Added: {subject} →{predicate}→ {obj}")

    return config


# ---------------------------------------------------------------------------
# Phase: Neo4j schema init
# ---------------------------------------------------------------------------

def init_neo4j_schema(config: OntologyConfig) -> None:
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "foundergraph_password")
    try:
        from app.services.neo4j_service import Neo4jService, Neo4jConfig
    except ImportError:
        err("neo4j package not installed — skipping schema init.")
        return

    print(f"\n  Connecting to {neo4j_uri}...")
    try:
        cfg = Neo4jConfig(uri=neo4j_uri, username=neo4j_user, password=neo4j_password)
        svc = Neo4jService(
            config=cfg,
            allowed_labels=config.allowed_labels(),
            allowed_relationships=config.allowed_relationships(),
        )
        svc.ensure_schema()
        svc.close()
        ok("Neo4j schema constraints and indexes created.")
    except Exception as exc:
        err(f"Neo4j schema init failed: {exc}")
        warn("Run `make up` first, then re-run `make init` or call ensure_schema() manually.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def banner() -> None:
    print(_c("""
╔══════════════════════════════════════════════════════════════════╗
║         FounderGraph-Lab — Ontology Initializer                 ║
║  Configure the knowledge graph for your specific startup.        ║
╚══════════════════════════════════════════════════════════════════╝
""", BOLD, CYAN))
    print(wrap(
        "This takes 5–15 minutes. At the end, app/ontology/startup_ontology.yaml "
        "will be tailored to your startup's domain and goals. "
        "All extraction, validation, and Neo4j writes will use your custom schema.",
        indent=2,
    ))
    print()
    warn("Init is used for ontology suggestions/configuration only by default, not ingestion.")
    warn("You can optionally ingest analyzed files into the vault in a later prompt.")


def ingest_files_to_vault(files: list[Path]) -> tuple[int, int]:
    ok_count = 0
    fail_count = 0
    for path in files:
        try:
            with path.open("rb") as fh:
                ingest_document(fh, filename=path.name)
            ok_count += 1
        except (FileStoreError, OSError):
            fail_count += 1
    return ok_count, fail_count


def main(args: argparse.Namespace) -> None:
    banner()

    # Load or reset existing ontology
    if args.reset:
        config = load_ontology()
        warn("Resetting to default ontology (--reset flag set).")
    else:
        config = load_ontology()

    # ── Phase 1: Context ───────────────────────────────────────────────────
    section("1 / 6  Startup Context")
    startup_name, startup_description, domain, goals = gather_context(config)
    config.domain = domain
    config.goals = goals
    print()
    ok(f"Domain: {domain}")
    ok(f"Goals: {', '.join(goals) if goals else '(none selected)'}")

    # ── Phase 2: Document discovery ────────────────────────────────────────
    section("2 / 6  Document Discovery")
    default_doc_path = str(args.docs or (_PROJECT_ROOT / "sample_data"))
    doc_path_str = ask(
        "Path to your startup documents",
        default=default_doc_path,
    )
    doc_path = Path(doc_path_str).expanduser().resolve()

    if not doc_path.exists():
        err(f"Path not found: {doc_path}")
        sys.exit(1)

    should_offer_clean_reset = "sample_data" in str(doc_path).replace("\\", "/")
    if ask_yn(
        "Start from a clean demo state before continuing?",
        default=should_offer_clean_reset,
    ):
        summary = reset_demo_state(clear_audits=False)
        ok(f"Demo state reset. Backup saved at {summary.backup_dir.relative_to(_PROJECT_ROOT)}")
        ok(f"Reset JSON files: {summary.reset_files}; cleared vault files: {summary.removed_vault_files}")

    files = discover_documents(doc_path)
    if not files:
        warn(f"No supported files found in {doc_path}. Continuing without document analysis.")
        texts: list[str] = []
    else:
        print(f"\n  Found {len(files)} file(s):")
        for f in files[:20]:
            print(f"    • {f.name}")
        if len(files) > 20:
            print(f"    … and {len(files) - 20} more")

        # ── Phase 3: Text extraction ───────────────────────────────────────
        section("3 / 6  Text Extraction")
        texts = []
        for f in files:
            text = extract_text(f)
            char_count = len(text)
            if char_count > 100:
                ok(f"{f.name} → {char_count:,} chars")
                texts.append(text)
            else:
                warn(f"{f.name} → {char_count} chars (skipped — too short)")

    should_ingest = ask_yn(
        "Also ingest analyzed documents into vault now?",
        default=False,
    )

    # ── Phase 4: LLM analysis ──────────────────────────────────────────────
    section("4 / 6  LLM Analysis")
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("LLM_MODEL", "llama3.1:8b")
    suggestions: list[dict] = []

    if not texts:
        warn("No documents to analyze — skipping LLM suggestions.")
    elif not _ollama_available(ollama_url):
        warn(f"Ollama not reachable at {ollama_url} — skipping LLM suggestions.")
        warn("Run `make up` to start Ollama, then re-run `make init` to get suggestions.")
    else:
        print(f"\n  Analyzing with {model} …  (may take 60–120 s)")
        suggestions = llm_suggest_entity_types(
            config=config,
            excerpts=texts,
            startup_description=startup_description,
            ollama_url=ollama_url,
            model=model,
        )
        if suggestions:
            ok(f"LLM suggested {len(suggestions)} entity type(s).")
        else:
            warn("LLM returned no suggestions (will proceed with defaults).")

    # ── Phase 5: Entity type review ────────────────────────────────────────
    section("5 / 6  Entity Type Review")
    config = review_entity_types(config, goals, suggestions)

    # ── Phase 5b: Relation review ──────────────────────────────────────────
    config = review_relations(config)

    # ── Phase 6: Save ──────────────────────────────────────────────────────
    section("6 / 6  Save & Initialize")
    print(f"\n  Entity types  : {len(config.entity_classes)}")
    print(f"  Allowed labels: {len(config.allowed_labels())}")
    print(f"  Relations     : {len(config.relations)}")
    print(f"  Predicates    : {len(config.allowed_relationships())}")

    yaml_path = save_ontology(config)
    ok(f"Ontology saved → {yaml_path.relative_to(_PROJECT_ROOT)}")

    bridge_path = save_init_bridge(
        source_folder=str(doc_path),
        note="Init completed. Use Upload -> Ingest folder with this path.",
        show_drive_cta=True,
    )
    ok(f"Saved init bridge context → {bridge_path.relative_to(_PROJECT_ROOT)}")

    if should_ingest and files:
        section("Optional ingestion")
        ok_count, fail_count = ingest_files_to_vault(files)
        ok(f"Ingested {ok_count} file(s) into vault.")
        if fail_count:
            warn(f"{fail_count} file(s) failed to ingest.")

    if ask_yn("\nInitialize Neo4j schema now? (requires `make up`)", default=False):
        init_neo4j_schema(config)

    app_url = os.getenv("FOUNDERGRAPH_APP_URL", "http://localhost:8501").rstrip("/")
    upload_url = f"{app_url}/?ingest_folder={quote(str(doc_path))}"

    if ask_yn("\nOpen Upload prefilled with this folder path now?", default=False):
        try:
            webbrowser.open(upload_url)
            ok("Opened browser to app URL with prefilled ingest path.")
        except Exception as exc:
            warn(f"Could not open browser automatically: {exc}")
            warn(f"Open manually: {upload_url}")

    if ask_yn("\nOpen Drive Sync now to export from Drive and ingest?", default=False):
        try:
            webbrowser.open(f"{app_url}/")
            ok("Opened app. Use sidebar -> Drive Sync.")
        except Exception as exc:
            warn(f"Could not open browser automatically: {exc}")
            warn(f"Open manually: {app_url}")

    print()
    hr("═")
    print(_c("  Setup complete!", BOLD, GREEN))
    print()
    print("  Next steps:")
    print("    1.  make up              — start all services")
    print("    2.  Open http://localhost:8501")
    print("    3.  (Optional) Drive Sync → Upload → Extract → Validate → Graph → Agents")
    print(f"    4.  One-click Upload bridge URL: {upload_url}")
    hr("═")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FounderGraph-Lab ontology initializer")
    parser.add_argument("--docs", type=Path, default=None, help="Path to startup document directory")
    parser.add_argument("--reset", action="store_true", help="Reset YAML to defaults before editing")
    main(parser.parse_args())
