"""Re-export of the LinkML-generated entity classes.

Phase 1 of the GraphRAG upgrade made
``app/ontology/startup_ontology.linkml.yaml`` the single source of truth.
The Pydantic models in ``app.ontology.generated.models`` are regenerated from
that schema by ``scripts/generate_ontology_artifacts.py``.

This module is now a thin shim that re-exports the generated ``Entity`` base
class under the legacy name ``KnowledgeEntity`` so existing call sites keep
working, plus an ``EntityType`` literal alias listing every concrete entity
subtype the ontology declares.

Concrete subclasses (``Assumption``, ``Risk``, ``Startup``, etc.) can be
imported directly from ``app.ontology.generated.models``.
"""

from __future__ import annotations

from typing import Literal

# The generated module is created by ``make generate``; importing it lazily
# guards against fresh checkouts where ``models.py`` has not yet been built.
try:
    from app.ontology.generated.models import (  # type: ignore[import-not-found]
        Entity as KnowledgeEntity,
    )
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    # Fallback: define a minimal stand-in so import never crashes.  The CI
    # drift check guarantees the real generated file is present in repo.
    from pydantic import BaseModel

    class KnowledgeEntity(BaseModel):  # type: ignore[no-redef]
        id: str
        name: str | None = None


# Mirrors the LinkML enum ``x-foundergraph-entity-labels`` -- kept as a
# Literal here so downstream code (extractor prompts, type hints) can still
# enumerate the canonical entity subtype names.
EntityType = Literal[
    "Startup",
    "Founder",
    "CustomerSegment",
    "Problem",
    "ValueProposition",
    "ProductFeature",
    "Assumption",
    "Evidence",
    "Risk",
    "Experiment",
    "Decision",
    "Milestone",
    "CustomerInterview",
    "GrantCall",
    "Grant",
    "Impact",
    "Market",
    "Investor",
    "Partner",
    "Competitor",
    "IPAsset",
    "RegulatoryConstraint",
    "TechnicalDependency",
    "FinancialHypothesis",
    "Site",
    "Vendor",
    "Country",
    "Blocker",
]


__all__ = ["KnowledgeEntity", "EntityType"]
