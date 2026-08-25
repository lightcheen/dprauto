"""Neo4j persistence adapter for backend-neutral repository knowledge graphs."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from dprauto.domain.models import SourceReference
from dprauto.errors import AdapterError
from dprauto.intelligence.models import (
    KnowledgeEdge,
    KnowledgeEdgeKind,
    KnowledgeNode,
    KnowledgeNodeKind,
    KnowledgeQuery,
    RepositoryKnowledgeGraph,
)


class Neo4jKnowledgeGraphStore:
    """Persist isolated graph identities with parameterized, batched Cypher."""

    def __init__(
        self,
        driver: Any,
        *,
        batch_size: int = 500,
        initialize_schema: bool = True,
        owns_driver: bool = False,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Neo4j batch_size must be positive")
        self.driver = driver
        self.batch_size = batch_size
        self.owns_driver = owns_driver
        if initialize_schema:
            self._initialize_schema()

    @classmethod
    def from_uri(
        cls,
        uri: str,
        username: str,
        password: str,
        *,
        batch_size: int = 500,
    ) -> "Neo4jKnowledgeGraphStore":
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise AdapterError("Neo4j support requires neo4j==5.28.4") from exc
        driver = GraphDatabase.driver(uri, auth=(username, password))
        return cls(driver, batch_size=batch_size, owns_driver=True)

    def close(self) -> None:
        if self.owns_driver:
            self.driver.close()

    def _initialize_schema(self) -> None:
        queries = (
            "CREATE CONSTRAINT dprauto_graph_id IF NOT EXISTS "
            "FOR (g:DPRAutoGraph) REQUIRE g.graph_id IS UNIQUE",
            "CREATE CONSTRAINT dprauto_node_id IF NOT EXISTS "
            "FOR (n:DPRAutoNode) REQUIRE n.node_id IS UNIQUE",
            "CREATE INDEX dprauto_node_graph IF NOT EXISTS "
            "FOR (n:DPRAutoNode) ON (n.graph_id)",
            "CREATE INDEX dprauto_node_path IF NOT EXISTS "
            "FOR (n:DPRAutoNode) ON (n.path)",
        )
        try:
            with self.driver.session() as session:
                for query in queries:
                    session.run(query).consume()
        except Exception as exc:
            raise AdapterError(f"failed to initialize Neo4j knowledge graph schema: {exc}") from exc

    def replace(self, graph: RepositoryKnowledgeGraph) -> None:
        graph_payload = {
            "graph_id": graph.graph_id,
            "source_locator": graph.source.locator,
            "source_revision": graph.source.revision or "",
            "source_subdirectory": graph.source.subdirectory or "",
            "source_fingerprint": graph.source_fingerprint,
            "root_node_id": graph.root_node_id,
            "metadata_json": _json(graph.metadata),
        }
        nodes = tuple(_node_payload(graph.graph_id, node) for node in graph.nodes)
        edges = tuple(_edge_payload(graph.graph_id, edge) for edge in graph.edges)
        try:
            with self.driver.session() as session:
                session.execute_write(
                    self._replace_transaction,
                    graph_payload,
                    nodes,
                    edges,
                    self.batch_size,
                )
        except Exception as exc:
            raise AdapterError(f"failed to replace Neo4j graph {graph.graph_id}: {exc}") from exc

    @staticmethod
    def _replace_transaction(
        tx: Any,
        graph: Mapping[str, Any],
        nodes: tuple[Mapping[str, Any], ...],
        edges: tuple[Mapping[str, Any], ...],
        batch_size: int,
    ) -> None:
        tx.run(
            "MATCH (n:DPRAutoNode {graph_id: $graph_id}) DETACH DELETE n",
            graph_id=graph["graph_id"],
        )
        tx.run(
            "MATCH (g:DPRAutoGraph {graph_id: $graph_id}) DETACH DELETE g",
            graph_id=graph["graph_id"],
        )
        tx.run(
            "CREATE (g:DPRAutoGraph) SET g = $graph",
            graph=dict(graph),
        )
        node_query = """
        UNWIND $nodes AS item
        CREATE (n:DPRAutoNode)
        SET n = item
        """
        for batch in _batches(nodes, batch_size):
            tx.run(node_query, nodes=list(batch))
        edge_query = """
        UNWIND $edges AS edge
        MATCH (source:DPRAutoNode {node_id: edge.source_id})
        MATCH (target:DPRAutoNode {node_id: edge.target_id})
        CREATE (source)-[relationship:DPRAUTO_REL]->(target)
        SET relationship.graph_id = edge.graph_id,
            relationship.kind = edge.kind,
            relationship.metadata_json = edge.metadata_json
        """
        for batch in _batches(edges, batch_size):
            tx.run(edge_query, edges=list(batch))

    def load(self, graph_id: str) -> RepositoryKnowledgeGraph | None:
        try:
            with self.driver.session() as session:
                graph_record = session.run(
                    "MATCH (g:DPRAutoGraph {graph_id: $graph_id}) "
                    "RETURN properties(g) AS graph",
                    graph_id=graph_id,
                ).single()
                if graph_record is None:
                    return None
                graph_data = dict(_record_value(graph_record, "graph"))
                node_records = session.run(
                    "MATCH (n:DPRAutoNode {graph_id: $graph_id}) "
                    "RETURN properties(n) AS node ORDER BY n.node_id",
                    graph_id=graph_id,
                )
                edge_records = session.run(
                    "MATCH (source:DPRAutoNode {graph_id: $graph_id})"
                    "-[relationship:DPRAUTO_REL {graph_id: $graph_id}]->"
                    "(target:DPRAutoNode) "
                    "RETURN source.node_id AS source_id, target.node_id AS target_id, "
                    "relationship.kind AS kind, relationship.metadata_json AS metadata_json "
                    "ORDER BY source_id, target_id, kind",
                    graph_id=graph_id,
                )
                nodes = tuple(
                    _node_from_payload(_record_value(record, "node"))
                    for record in node_records
                )
                edges = tuple(
                    KnowledgeEdge(
                        source_id=str(_record_value(record, "source_id")),
                        target_id=str(_record_value(record, "target_id")),
                        kind=KnowledgeEdgeKind(str(_record_value(record, "kind"))),
                        metadata=_load_json(_record_value(record, "metadata_json")),
                    )
                    for record in edge_records
                )
        except Exception as exc:
            raise AdapterError(f"failed to load Neo4j graph {graph_id}: {exc}") from exc
        return RepositoryKnowledgeGraph(
            graph_id=graph_id,
            source=SourceReference(
                str(graph_data["source_locator"]),
                revision=str(graph_data.get("source_revision") or "") or None,
                subdirectory=str(graph_data.get("source_subdirectory") or "") or None,
            ),
            source_fingerprint=str(graph_data["source_fingerprint"]),
            root_node_id=str(graph_data["root_node_id"]),
            nodes=nodes,
            edges=edges,
            metadata=_load_json(graph_data.get("metadata_json")),
        )

    def query(self, graph_id: str, query: KnowledgeQuery) -> tuple[KnowledgeNode, ...]:
        cypher = """
        MATCH (n:DPRAutoNode {graph_id: $graph_id})
        WHERE (size($kinds) = 0 OR n.kind IN $kinds)
          AND ($path_prefix = '.' OR n.path = $path_prefix
               OR n.path STARTS WITH $path_prefix + '/')
          AND (size($languages) = 0 OR toLower(n.language) IN $languages)
          AND (size($node_types) = 0 OR toLower(n.node_type) IN $node_types)
          AND ($text = '' OR toLower(
                coalesce(n.path, '') + '\n' + coalesce(n.node_type, '') + '\n' +
                coalesce(n.symbol, '') + '\n' + coalesce(n.text, '')
              ) CONTAINS $text)
        RETURN properties(n) AS node
        ORDER BY n.path, n.start_line, n.node_id
        LIMIT $limit
        """
        parameters = {
            "graph_id": graph_id,
            "kinds": [kind.value for kind in query.kinds],
            "path_prefix": query.path_prefix.rstrip("/") or ".",
            "languages": [language.casefold() for language in query.languages],
            "node_types": [node_type.casefold() for node_type in query.node_types],
            "text": query.text.casefold(),
            "limit": query.limit,
        }
        try:
            with self.driver.session() as session:
                records = session.run(cypher, **parameters)
                return tuple(
                    _node_from_payload(_record_value(record, "node"))
                    for record in records
                )
        except Exception as exc:
            raise AdapterError(f"failed to query Neo4j graph {graph_id}: {exc}") from exc

    def delete(self, graph_id: str) -> None:
        try:
            with self.driver.session() as session:
                session.execute_write(self._delete_transaction, graph_id)
        except Exception as exc:
            raise AdapterError(f"failed to delete Neo4j graph {graph_id}: {exc}") from exc

    @staticmethod
    def _delete_transaction(tx: Any, graph_id: str) -> None:
        tx.run(
            "MATCH (n:DPRAutoNode {graph_id: $graph_id}) DETACH DELETE n",
            graph_id=graph_id,
        )
        tx.run(
            "MATCH (g:DPRAutoGraph {graph_id: $graph_id}) DETACH DELETE g",
            graph_id=graph_id,
        )


def _batches(values: tuple[Any, ...], size: int) -> Iterable[tuple[Any, ...]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): _restore_immutable_json(item)
        for key, item in parsed.items()
    }


def _restore_immutable_json(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_restore_immutable_json(item) for item in value)
    if isinstance(value, dict):
        return {
            str(key): _restore_immutable_json(item)
            for key, item in value.items()
        }
    return value


def _node_payload(graph_id: str, node: KnowledgeNode) -> dict[str, Any]:
    return {
        "graph_id": graph_id,
        "node_id": node.node_id,
        "kind": node.kind.value,
        "path": node.path,
        "language": node.language,
        "node_type": node.node_type,
        "symbol": node.symbol,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "start_column": node.start_column,
        "end_column": node.end_column,
        "text": node.text,
        "metadata_json": _json(node.metadata),
    }


def _node_from_payload(value: Mapping[str, Any]) -> KnowledgeNode:
    return KnowledgeNode(
        node_id=str(value["node_id"]),
        kind=KnowledgeNodeKind(str(value["kind"])),
        path=str(value["path"]),
        language=str(value.get("language") or ""),
        node_type=str(value.get("node_type") or ""),
        symbol=str(value.get("symbol") or ""),
        start_line=_optional_int(value.get("start_line")),
        end_line=_optional_int(value.get("end_line")),
        start_column=_optional_int(value.get("start_column")),
        end_column=_optional_int(value.get("end_column")),
        text=str(value.get("text") or ""),
        metadata=_load_json(value.get("metadata_json")),
    )


def _edge_payload(graph_id: str, edge: KnowledgeEdge) -> dict[str, Any]:
    return {
        "graph_id": graph_id,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "kind": edge.kind.value,
        "metadata_json": _json(edge.metadata),
    }


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        return record[key]
    return record[key]
