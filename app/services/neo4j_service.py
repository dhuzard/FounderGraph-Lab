"""Safe Neo4j graph persistence for validated FounderGraph knowledge."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol


SAFE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
SAFE_LABEL = re.compile(r"^[A-Z][A-Za-z0-9_]*$")

DEFAULT_ALLOWED_LABELS = {
    "Document",
    "Entity",
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
    "GrantCall",
    "Investor",
    "Partner",
    "Competitor",
    "IPAsset",
    "RegulatoryConstraint",
    "TechnicalDependency",
    "FinancialHypothesis",
}
DEFAULT_ALLOWED_RELATIONSHIPS = {
    "RELATED_TO",
    "TARGETS",
    "HAS_PROBLEM",
    "ADDRESSES",
    "BASED_ON",
    "SUPPORTED_BY",
    "CONTRADICTED_BY",
    "TESTS",
    "GENERATES",
    "THREATENS",
    "FUNDS",
    "PROVIDES",
    "COMPETES_ON",
    "PROTECTS",
    "MENTIONS",
    "SOURCE_OF",
    "DEPENDS_ON",
}


class Neo4jServiceError(ValueError):
    """Raised when a graph operation violates safety or validation policy."""


class DriverLike(Protocol):
    def session(self, **kwargs: Any) -> Any:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    username: str
    password: str
    database: str | None = None

    @classmethod
    def from_env(cls) -> "Neo4jConfig":
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            username=os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j")),
            password=os.getenv("NEO4J_PASSWORD", "password"),
            database=os.getenv("NEO4J_DATABASE") or None,
        )


def create_driver(config: Neo4jConfig | None = None) -> DriverLike:
    """Create an official Neo4j driver from environment-backed config."""
    from neo4j import GraphDatabase

    cfg = config or Neo4jConfig.from_env()
    return GraphDatabase.driver(cfg.uri, auth=(cfg.username, cfg.password))


def normalize_relationship_type(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_").upper()
    return token or "RELATED_TO"


def normalize_label(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]+", "", str(value)).strip("_")
    if not token:
        return "Entity"
    return token[:1].upper() + token[1:]


class Neo4jService:
    """Neo4j repository with strict schema whitelisting and parameterized writes."""

    def __init__(
        self,
        driver: DriverLike | None = None,
        config: Neo4jConfig | None = None,
        allowed_labels: set[str] | None = None,
        allowed_relationships: set[str] | None = None,
    ) -> None:
        self.config = config or Neo4jConfig.from_env()
        self.driver = driver or create_driver(self.config)
        self.allowed_labels = allowed_labels or DEFAULT_ALLOWED_LABELS
        self.allowed_relationships = allowed_relationships or DEFAULT_ALLOWED_RELATIONSHIPS

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT startup_id IF NOT EXISTS FOR (s:Startup) REQUIRE s.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
            "CREATE INDEX entity_validation_status IF NOT EXISTS FOR (e:Entity) ON (e.validation_status)",
            "CREATE INDEX entity_source_document_id IF NOT EXISTS FOR (e:Entity) ON (e.source_document_id)",
        ]
        with self._session() as session:
            for statement in statements:
                session.run(statement)

    def upsert_document(self, document: dict[str, Any]) -> None:
        document_id = document.get("id") or document.get("document_id") or document.get("source_path")
        if not document_id:
            raise Neo4jServiceError("Document requires id, document_id, or source_path")
        params = {
            "id": str(document_id),
            "title": document.get("title", ""),
            "source_path": document.get("source_path") or document.get("original_path", ""),
            "source_type": document.get("source_type") or document.get("document_type", ""),
            "metadata_json": json_property(document.get("metadata", {})),
        }
        query = """
        MERGE (d:Document {id: $id})
        SET d.title = $title,
            d.source_path = $source_path,
            d.source_type = $source_type,
            d.metadata_json = $metadata_json,
            d.updated_at = datetime()
        """
        self._run(query, params)

    def upsert_entity(self, entity: dict[str, Any]) -> None:
        self._require_validated(entity, "entity")
        entity_id = entity.get("id")
        if not entity_id:
            raise Neo4jServiceError("Validated entity requires id")
        label = self._safe_label(entity.get("type") or entity.get("label") or "Entity")
        params = {
            "id": str(entity_id),
            "name": entity.get("name") or entity.get("label", ""),
            "display_label": entity.get("label") or entity.get("name", ""),
            "type": entity.get("type", label),
            "description": entity.get("description", ""),
            "source_snippet": entity.get("source_snippet", ""),
            "source_document_id": entity.get("source_document_id"),
            "source_file": entity.get("source_file"),
            "source_location": entity.get("source_location"),
            "provenance_json": json_property(entity.get("provenance", {})),
            "metadata_json": json_property(entity.get("metadata", {})),
            "status": validation_status(entity),
        }
        query = f"""
        MERGE (e:Entity {{id: $id}})
        SET e:{label},
            e.name = $name,
            e.label = $display_label,
            e.type = $type,
            e.description = $description,
            e.source_snippet = $source_snippet,
            e.source_document_id = $source_document_id,
            e.source_file = $source_file,
            e.source_location = $source_location,
            e.provenance_json = $provenance_json,
            e.metadata_json = $metadata_json,
            e.status = $status,
            e.validation_status = $status,
            e.updated_at = datetime()
        """
        self._run(query, params)
        if params.get("source_document_id"):
            self.link_document_entity(str(params["source_document_id"]), str(entity_id), str(params.get("source_snippet") or ""))

    def upsert_relation(self, relation: dict[str, Any]) -> None:
        self._require_validated(relation, "relation")
        source_id = relation.get("source_entity_id") or relation.get("subject_id") or relation.get("source")
        target_id = relation.get("target_entity_id") or relation.get("object_id") or relation.get("target")
        if not source_id or not target_id:
            raise Neo4jServiceError("Validated relation requires source and target entity ids")
        rel_type = self._safe_relationship(relation.get("predicate") or relation.get("type") or relation.get("relation") or "RELATED_TO")
        relation_id = relation.get("id") or f"{source_id}:{rel_type}:{target_id}"
        params = {
            "id": str(relation_id),
            "source_id": str(source_id),
            "target_id": str(target_id),
            "source_snippet": relation.get("source_snippet", ""),
            "source_document_id": relation.get("source_document_id"),
            "source_file": relation.get("source_file"),
            "provenance_json": json_property(relation.get("provenance", {})),
            "metadata_json": json_property(relation.get("metadata", {})),
            "confidence": relation.get("confidence"),
            "status": validation_status(relation),
        }
        query = f"""
        MATCH (source:Entity {{id: $source_id}})
        MATCH (target:Entity {{id: $target_id}})
        MERGE (source)-[r:{rel_type} {{id: $id}}]->(target)
        SET r.source_snippet = $source_snippet,
            r.source_document_id = $source_document_id,
            r.source_file = $source_file,
            r.provenance_json = $provenance_json,
            r.metadata_json = $metadata_json,
            r.confidence = $confidence,
            r.status = $status,
            r.updated_at = datetime()
        """
        self._run(query, params)

    def link_document_entity(self, document_id: str, entity_id: str, snippet: str = "") -> None:
        params = {"document_id": document_id, "entity_id": entity_id, "source_snippet": snippet}
        query = """
        MATCH (d:Document {id: $document_id})
        MATCH (e:Entity {id: $entity_id})
        MERGE (d)-[r:MENTIONS {entity_id: $entity_id}]->(e)
        SET r.source_snippet = $source_snippet,
            r.updated_at = datetime()
        """
        self._run(query, params)

    def upsert_validated_knowledge(
        self,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        documents: list[dict[str, Any]] | None = None,
    ) -> None:
        for document in documents or []:
            self.upsert_document(document)
        for entity in entities:
            self.upsert_entity(entity)
        for relation in relations:
            self.upsert_relation(relation)

    def graph_snapshot(
        self,
        labels: list[str] | None = None,
        relationship_types: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, list[dict[str, Any]]]:
        label_filter = [self._safe_label(label) for label in labels or []]
        rel_filter = [self._safe_relationship(rel) for rel in relationship_types or []]
        params = {"labels": label_filter, "relationship_types": rel_filter, "limit": int(limit)}
        query = """
        MATCH (source:Entity)-[r]->(target:Entity)
        WHERE ($labels = [] OR any(label IN labels(source) WHERE label IN $labels)
               OR any(label IN labels(target) WHERE label IN $labels))
          AND ($relationship_types = [] OR type(r) IN $relationship_types)
        RETURN source, r, target, labels(source) AS source_labels, labels(target) AS target_labels
        LIMIT $limit
        """
        rows = self._rows(query, params)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for row in rows:
            source = dict(row["source"])
            target = dict(row["target"])
            source_id = str(source.get("id"))
            target_id = str(target.get("id"))
            nodes[source_id] = {**source, "labels": row.get("source_labels", [])}
            nodes[target_id] = {**target, "labels": row.get("target_labels", [])}
            rel = dict(row["r"])
            rel["type"] = getattr(row["r"], "type", None) or row.get("type") or ""
            rel["source"] = source_id
            rel["target"] = target_id
            edges.append(rel)
        return {"nodes": list(nodes.values()), "edges": edges}

    def get_all_entities(self, limit: int = 200) -> list[dict[str, Any]]:
        query = """
        MATCH (e:Entity)
        RETURN e.id AS id, e.label AS label, e.name AS name, e.type AS type,
               e.description AS description, e.confidence AS confidence,
               e.validation_status AS validation_status, e.source_file AS source_file,
               e.source_snippet AS source_snippet
        LIMIT $limit
        """
        return self._rows(query, {"limit": int(limit)})

    def get_graph(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        return self.graph_snapshot(limit=limit)

    def get_unsupported_assumptions(self) -> list[dict[str, Any]]:
        query = """
        MATCH (a:Entity:Assumption)
        WHERE NOT (a)-[:SUPPORTED_BY]->(:Entity:Evidence)
        RETURN a.id AS id, a.label AS label, a.description AS description,
               a.confidence AS confidence, a.source_file AS source_file,
               a.source_snippet AS source_snippet
        """
        return self._rows(query, {})

    def get_risks_by_milestone(self) -> list[dict[str, Any]]:
        query = """
        MATCH (r:Entity:Risk)
        OPTIONAL MATCH (r)-[:THREATENS]->(m:Entity:Milestone)
        RETURN r.id AS risk_id, r.label AS risk, r.description AS description,
               collect(m.label) AS milestones, r.source_file AS source_file,
               r.source_snippet AS source_snippet
        """
        return self._rows(query, {})

    def get_features_by_problem(self) -> list[dict[str, Any]]:
        query = """
        MATCH (f:Entity:ProductFeature)-[:ADDRESSES]->(p:Entity:Problem)
        RETURN f.id AS feature_id, f.label AS feature, collect(p.label) AS problems
        """
        return self._rows(query, {})

    def get_evidence_for_assumption(self, assumption_id: str) -> list[dict[str, Any]]:
        query = """
        MATCH (a:Entity:Assumption {id: $assumption_id})-[:SUPPORTED_BY]->(e:Entity:Evidence)
        RETURN e.id AS evidence_id, e.label AS evidence, e.description AS description,
               e.source_file AS source_file, e.source_snippet AS source_snippet
        """
        return self._rows(query, {"assumption_id": assumption_id})

    def audit_recent_writes(self, limit: int = 50) -> list[dict[str, Any]]:
        query = """
        MATCH (n)
        WHERE n.updated_at IS NOT NULL
        RETURN labels(n) AS labels, n.id AS id, n.name AS name, n.updated_at AS updated_at,
               n.provenance_json AS provenance_json, n.source_snippet AS source_snippet
        ORDER BY n.updated_at DESC
        LIMIT $limit
        """
        return self._rows(query, {"limit": int(limit)})

    def _safe_label(self, value: str) -> str:
        label = normalize_label(value)
        if not SAFE_LABEL.match(label) or label not in self.allowed_labels:
            raise Neo4jServiceError(f"Label is not whitelisted: {value}")
        return label

    def _safe_relationship(self, value: str) -> str:
        relationship = normalize_relationship_type(value)
        if not SAFE_TOKEN.match(relationship) or relationship not in self.allowed_relationships:
            raise Neo4jServiceError(f"Relationship type is not whitelisted: {value}")
        return relationship

    @staticmethod
    def _require_validated(record: dict[str, Any], kind: str) -> None:
        if validation_status(record) != "validated":
            raise Neo4jServiceError(f"Refusing to write non-validated {kind}")

    def _session(self) -> Any:
        kwargs = {"database": self.config.database} if self.config.database else {}
        return self.driver.session(**kwargs)

    def _run(self, query: str, params: dict[str, Any]) -> None:
        with self._session() as session:
            session.run(query, params)

    def _rows(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._session() as session:
            return [dict(row) for row in session.run(query, params)]


def validation_status(record: dict[str, Any]) -> str | None:
    return record.get("validation_status", record.get("status"))


def json_property(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)
