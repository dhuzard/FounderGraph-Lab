"""Re-export of the LinkML-generated relation class.

See ``app/models/entity.py`` for the rationale.  This module re-exports the
generated ``Relation`` base class under the legacy ``KnowledgeRelation`` name
plus a ``RelationType`` literal alias listing every declared predicate.
"""

from __future__ import annotations

from typing import Literal

try:
    from app.ontology.generated.models import (  # type: ignore[import-not-found]
        Relation as KnowledgeRelation,
    )
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    from pydantic import BaseModel

    class KnowledgeRelation(BaseModel):  # type: ignore[no-redef]
        id: str


# All predicates declared in the LinkML schema.  Whoever changes the schema
# should re-run ``make generate`` -- the CI drift check will then nudge them
# to update this list.
RelationType = Literal[
    "TARGETS",
    "HAS_PROBLEM",
    "ADDRESSES",
    "BASED_ON",
    "PROVIDES",
    "SUPPORTED_BY",
    "CONTRADICTED_BY",
    "TESTS",
    "GENERATES",
    "THREATENS",
    "DEPENDS_ON",
    "COMPETES_ON",
    "PROTECTS",
    "FUNDS",
    "MENTIONS",
    "SOURCE_OF",
    "RELATED_TO",
    "SUPERSEDED_BY",
]


__all__ = ["KnowledgeRelation", "RelationType"]
