"""
RECAP v2 - Knowledge Graph

NetworkX-based in-memory graph with SQLite persistence.
Nodes = entities (people, orgs, technologies, concepts).
Edges = relations between entities, weighted by co-occurrence.
Supports community detection and neighborhood retrieval.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Set, Tuple

import networkx as nx

from backend.storage.database import Database

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """
    In-memory knowledge graph backed by the SQLite entities/relations tables.

    The graph is loaded from the database on initialization and updated
    in-memory + persisted to DB on each mutation.
    """

    def __init__(self, db: Database):
        """
        Initialize the knowledge graph.

        Args:
            db: Database instance for persistence.
        """
        self.db = db
        self.graph = nx.Graph()
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Load all entities and relations from the database into the graph."""
        with self.db._get_connection() as conn:
            # Load entities as nodes
            entities = conn.execute("SELECT * FROM entities").fetchall()
            for entity in entities:
                self.graph.add_node(
                    entity["name"],
                    entity_type=entity["entity_type"],
                    frequency=entity["frequency"],
                    db_id=entity["id"],
                )

            # Load relations as edges
            relations = conn.execute(
                """
                SELECT er.*, e1.name as source_name, e2.name as target_name
                FROM entity_relations er
                JOIN entities e1 ON er.source_entity_id = e1.id
                JOIN entities e2 ON er.target_entity_id = e2.id
                """
            ).fetchall()
            for rel in relations:
                self.graph.add_edge(
                    rel["source_name"],
                    rel["target_name"],
                    relation_type=rel["relation_type"],
                    weight=rel["weight"],
                    source_url=rel["source_url"],
                )

        logger.info(
            "Loaded knowledge graph: %d nodes, %d edges",
            self.graph.number_of_nodes(),
            self.graph.number_of_edges(),
        )

    # =========================================================================
    # Mutation Operations
    # =========================================================================

    def add_entity(self, name: str, entity_type: str) -> int:
        """
        Add or update an entity in the graph and database.

        Returns the database entity ID.
        """
        # Normalize name
        name = name.strip()
        if not name:
            return -1

        # Persist to database
        entity_id = self.db.upsert_entity(name, entity_type)

        # Update in-memory graph
        if self.graph.has_node(name):
            self.graph.nodes[name]["frequency"] = (
                self.graph.nodes[name].get("frequency", 0) + 1
            )
        else:
            self.graph.add_node(
                name, entity_type=entity_type, frequency=1, db_id=entity_id
            )

        return entity_id

    def add_relation(
        self,
        source: str,
        target: str,
        relation_type: str = "related_to",
        weight: float = 1.0,
        source_url: str = "",
    ) -> None:
        """Add or strengthen a relation between two entities."""
        if source == target or not source.strip() or not target.strip():
            return

        source = source.strip()
        target = target.strip()

        # Ensure both entities exist
        source_entity = self.db.get_entity_by_name(source)
        target_entity = self.db.get_entity_by_name(target)

        if not source_entity or not target_entity:
            logger.warning(
                "Cannot add relation: entity not found (%s -> %s)", source, target
            )
            return

        # Persist to database
        self.db.upsert_relation(
            source_entity["id"],
            target_entity["id"],
            relation_type,
            weight,
            source_url,
        )

        # Update in-memory graph
        if self.graph.has_edge(source, target):
            self.graph[source][target]["weight"] += weight
        else:
            self.graph.add_edge(
                source,
                target,
                relation_type=relation_type,
                weight=weight,
                source_url=source_url,
            )

    def add_entities_and_relations(
        self,
        entities: List[Dict[str, str]],
        source_url: str = "",
    ) -> Tuple[int, int]:
        """
        Batch add entities and create co-occurrence relations between them.

        Entities that appear in the same chunk are considered related.

        Args:
            entities: List of {"name": ..., "entity_type": ...} dicts.
            source_url: URL where entities were found.

        Returns:
            Tuple of (entities_added, relations_added).
        """
        if not entities:
            return 0, 0

        # Deduplicate entities by normalized name
        unique_entities = {}
        for e in entities:
            normalized = e["name"].strip().lower()
            if normalized and len(normalized) > 1:
                unique_entities[normalized] = {
                    "name": e["name"].strip(),
                    "entity_type": e.get("entity_type", "concept"),
                }

        # Add all entities
        entity_ids = {}
        for key, entity in unique_entities.items():
            eid = self.add_entity(entity["name"], entity["entity_type"])
            entity_ids[entity["name"]] = eid

        # Create co-occurrence relations
        names = list(entity_ids.keys())
        relations_added = 0
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                self.add_relation(
                    names[i],
                    names[j],
                    relation_type="co_occurs_with",
                    weight=1.0,
                    source_url=source_url,
                )
                relations_added += 1

        logger.debug(
            "Added %d entities and %d relations from %s",
            len(entity_ids),
            relations_added,
            source_url,
        )
        return len(entity_ids), relations_added

    # =========================================================================
    # Query Operations
    # =========================================================================

    def get_neighbors(
        self,
        entity_name: str,
        max_hops: int = 2,
        max_results: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Get neighboring entities up to max_hops away.

        Args:
            entity_name: The entity to start from.
            max_hops: Maximum graph traversal depth.
            max_results: Maximum number of neighbors to return.

        Returns:
            List of neighbor dicts with name, type, distance, weight.
        """
        if entity_name not in self.graph:
            return []

        neighbors = []
        visited: Set[str] = {entity_name}

        # BFS traversal
        current_level = [entity_name]
        for hop in range(1, max_hops + 1):
            next_level = []
            for node in current_level:
                if node not in self.graph:
                    continue
                for neighbor in list(self.graph.neighbors(node)):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_level.append(neighbor)

                        edge_data = self.graph[node][neighbor]
                        node_data = self.graph.nodes[neighbor]
                        neighbors.append({
                            "name": neighbor,
                            "entity_type": node_data.get("entity_type", "unknown"),
                            "distance": hop,
                            "weight": edge_data.get("weight", 1.0),
                            "relation_type": edge_data.get("relation_type", "related_to"),
                            "frequency": node_data.get("frequency", 1),
                        })

            current_level = next_level

        # Sort by weight (strongest connections first), then by distance
        neighbors.sort(key=lambda x: (-x["weight"], x["distance"]))
        return neighbors[:max_results]

    def find_entities_in_query(self, query: str) -> List[str]:
        """
        Find known entities mentioned in a query string using WORD-BOUNDARY
        matching (a bare substring match makes short names like "AI" or "Go"
        match inside unrelated words). Iterates a snapshot of node names so a
        concurrent ingest cannot raise 'dictionary changed size during iteration'.
        """
        query_lower = query.lower()
        found = []
        for node in list(self.graph.nodes()):
            name = node.lower()
            if len(name) < 3:
                continue  # too short to match reliably by word boundary
            if re.search(r"\b" + re.escape(name) + r"\b", query_lower):
                found.append(node)

        # Prefer longer, more specific matches
        found.sort(key=lambda x: -len(x))
        return found

    def remove_relations_by_urls(self, urls: List[str]) -> None:
        """
        Drop in-memory edges sourced from the given URLs (and any now-isolated
        nodes), keeping the graph consistent with the DB after retention eviction.
        Without this the long-lived in-memory graph keeps surfacing ghost edges
        in /graph and KG search until the process restarts.
        """
        if not urls:
            return
        url_set = set(urls)
        to_remove = [
            (u, v)
            for u, v, data in list(self.graph.edges(data=True))
            if data.get("source_url", "") in url_set
        ]
        self.graph.remove_edges_from(to_remove)
        isolated = [n for n in list(self.graph.nodes()) if self.graph.degree(n) == 0]
        self.graph.remove_nodes_from(isolated)
        if to_remove or isolated:
            logger.info(
                "KG cleanup: removed %d edges and %d orphaned nodes",
                len(to_remove), len(isolated),
            )

    def get_context_for_entities(
        self,
        entity_names: List[str],
        max_hops: int = 2,
        max_per_entity: int = 10,
    ) -> str:
        """
        Generate a text context string from the knowledge graph
        for use in RAG augmentation.

        Args:
            entity_names: Entities to build context around.
            max_hops: Traversal depth.
            max_per_entity: Max neighbors per entity.

        Returns:
            Formatted text describing entity relationships.
        """
        if not entity_names:
            return ""

        lines = []
        seen_relations = set()

        for entity_name in entity_names:
            if entity_name not in self.graph:
                continue

            node_data = self.graph.nodes[entity_name]
            lines.append(
                f"- {entity_name} ({node_data.get('entity_type', 'entity')})"
            )

            neighbors = self.get_neighbors(entity_name, max_hops, max_per_entity)
            for n in neighbors:
                relation_key = frozenset([entity_name, n["name"]])
                if relation_key not in seen_relations:
                    seen_relations.add(relation_key)
                    lines.append(
                        f"  → {n['relation_type']}: {n['name']} "
                        f"({n['entity_type']}, weight={n['weight']:.1f})"
                    )

        if not lines:
            return ""

        return "Knowledge Graph Context:\n" + "\n".join(lines)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get graph statistics."""
        if self.graph.number_of_nodes() == 0:
            return {
                "nodes": 0,
                "edges": 0,
                "components": 0,
                "density": 0.0,
                "top_entities": [],
            }

        # Top entities by degree centrality
        centrality = nx.degree_centrality(self.graph)
        top = sorted(centrality.items(), key=lambda x: -x[1])[:10]

        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "components": nx.number_connected_components(self.graph),
            "density": round(nx.density(self.graph), 4),
            "top_entities": [
                {
                    "name": name,
                    "centrality": round(score, 4),
                    "type": self.graph.nodes[name].get("entity_type", "unknown"),
                }
                for name, score in top
            ],
        }
