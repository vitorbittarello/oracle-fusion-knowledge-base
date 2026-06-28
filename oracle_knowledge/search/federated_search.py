from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from oracle_knowledge.common import read_json
from oracle_knowledge.indexing import resolve_index_source
from oracle_knowledge.search.hybrid_search import HybridSearch, SearchConfig
from oracle_knowledge.search.indexed_store import IndexedGraphBundleStore, IndexedGraphStore
from oracle_knowledge.search.semantic_context import SemanticTextSelector


@dataclass(frozen=True)
class FederatedSearchConfig:
    master_search_limit: int = 40
    master_seed_limit: int = 12
    master_seed_min_relative_score: float = 0.10
    fallback_roots_per_layer: int = 3
    local_columns_per_table: int = 5
    local_questions_per_subject_area: int = 2
    local_operations_per_resource: int = 3
    local_operation_candidates_per_resource: int = 16


class FederatedGraphSearch:
    """Orquestra a navegação entre master e grafos especializados."""

    def __init__(
        self,
        graph_dir: str | Path,
        *,
        config: FederatedSearchConfig | None = None,
        semantic_text_selector: SemanticTextSelector | None = None,
        index_path: str | Path | None = None,
        use_index: bool = True,
        require_index: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.graph_dir = Path(graph_dir)
        self.config = config or FederatedSearchConfig()
        self.semantic_text_selector = semantic_text_selector or SemanticTextSelector()
        self.progress = progress
        self.master_graph = read_json(self.graph_dir / "master_graph.json", {})
        if not self.master_graph or "nodes" not in self.master_graph:
            raise ValueError(f"master_graph.json inválido em {self.graph_dir}")
        self.master_search = HybridSearch(
            self.master_graph,
            SearchConfig(graph_hops=0),
            semantic_text_selector=self.semantic_text_selector,
        )
        self._graphs: dict[str, dict[str, Any]] = {"master": self.master_graph}
        self._searches: dict[str, HybridSearch] = {"master": self.master_search}
        self.index_store: IndexedGraphStore | IndexedGraphBundleStore | None = None
        self._rest_operation_diagnostics: dict[str, Any] = {}

        resolved_index = resolve_index_source(self.graph_dir, index_path)
        if use_index and resolved_index.is_file():
            if resolved_index.suffix.casefold() == ".json":
                self.index_store = IndexedGraphBundleStore(
                    self.graph_dir,
                    bundle_path=resolved_index,
                    semantic_text_selector=self.semantic_text_selector,
                )
            else:
                self.index_store = IndexedGraphStore(
                    self.graph_dir,
                    index_path=resolved_index,
                    semantic_text_selector=self.semantic_text_selector,
                )
        elif index_path is not None or require_index:
            raise FileNotFoundError(
                f"Índice SQLite ou manifesto não encontrado: {resolved_index}. "
                "Execute build-index antes da pesquisa."
            )

    def _emit_progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    @property
    def backend_name(self) -> str:
        if isinstance(self.index_store, IndexedGraphBundleStore):
            return "sqlite_bundle"
        return "sqlite" if self.index_store is not None else "json"

    def close(self) -> None:
        if self.index_store is not None:
            self.index_store.close()
            self.index_store = None

    def __enter__(self) -> "FederatedGraphSearch":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _layer_path(self, layer: str) -> Path:
        filename = (self.master_graph.get("layers") or {}).get(layer)
        if not filename:
            filename = {
                "physical": "physical.json",
                "otbi_analytics": "otbi_analytics.json",
                "rest": "rest.json",
                "business": "business.json",
            }[layer]
        return self.graph_dir / filename

    def _load_layer(self, layer: str) -> dict[str, Any]:
        if layer not in self._graphs:
            graph = read_json(self._layer_path(layer), {})
            self._graphs[layer] = graph
            self._searches[layer] = HybridSearch(
                graph,
                SearchConfig(graph_hops=0),
                semantic_text_selector=self.semantic_text_selector,
            )
        return self._graphs[layer]

    @staticmethod
    def _node_text(node: dict[str, Any]) -> str:
        return "\n".join(
            value
            for value in (
                str(node.get("title") or node.get("name") or "").strip(),
                str(node.get("qualified_name") or "").strip(),
                str(node.get("search_text") or "").strip(),
                HybridSearch._context_summary_source(node),
            )
            if value
        )

    def _semantic_top(
        self,
        query: str,
        nodes: list[dict[str, Any]],
        limit: int,
        *,
        query_vector: np.ndarray | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        if not nodes or limit <= 0:
            return []
        documents = [self._node_text(node) for node in nodes]
        if query_vector is None:
            scores = self.semantic_text_selector.score_documents(
                query,
                documents,
            )
        else:
            scores = (
                self.semantic_text_selector
                .score_documents_with_query_vector(
                    query_vector,
                    documents,
                )
            )
        ranked = sorted(
            zip(nodes, scores),
            key=lambda item: (-float(item[1]), str(item[0].get("title") or "")),
        )
        return [(node, float(score)) for node, score in ranked[:limit]]

    def _semantic_top_grouped(
        self,
        query: str,
        grouped_nodes: dict[str, list[dict[str, Any]]],
        limit: int,
        *,
        query_vector: np.ndarray | None = None,
        text_builder: Callable[[dict[str, Any]], str] | None = None,
    ) -> dict[str, list[tuple[dict[str, Any], float]]]:
        if not grouped_nodes or limit <= 0:
            return {}

        flattened: list[dict[str, Any]] = []
        memberships: list[str] = []
        for group_id, nodes in grouped_nodes.items():
            for node in nodes:
                memberships.append(group_id)
                flattened.append(node)

        if not flattened:
            return {group_id: [] for group_id in grouped_nodes}

        build_text = text_builder or self._node_text
        documents = [build_text(node) for node in flattened]
        if query_vector is None:
            scores = self.semantic_text_selector.score_documents(
                query,
                documents,
            )
        else:
            scores = (
                self.semantic_text_selector
                .score_documents_with_query_vector(
                    query_vector,
                    documents,
                )
            )

        ranked_by_group: dict[str, list[tuple[dict[str, Any], float]]] = {
            group_id: [] for group_id in grouped_nodes
        }
        for group_id, node, score in zip(
            memberships,
            flattened,
            scores,
            strict=True,
        ):
            ranked_by_group[group_id].append((node, float(score)))

        for group_id, ranked in ranked_by_group.items():
            ranked.sort(
                key=lambda item: (
                    -item[1],
                    str(item[0].get("title") or ""),
                )
            )
            ranked_by_group[group_id] = ranked[:limit]

        return ranked_by_group

    @staticmethod
    def _rest_operation_text(node: dict[str, Any]) -> str:
        parameter_names = [
            str(item.get("name") or "").strip()
            for item in (node.get("parameters") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ][:24]
        attribute_names = [
            str(item.get("name") or "").strip()
            for item in (node.get("attributes") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ][:32]
        hierarchy = [
            str(value).strip()
            for value in (node.get("resource_hierarchy") or [])
            if str(value).strip()
        ]
        description = str(node.get("description") or "").strip()[:1200]
        return "\n".join(
            value
            for value in (
                str(node.get("title") or node.get("name") or "").strip(),
                str(node.get("method") or "").strip(),
                str(node.get("endpoint_path") or "").strip(),
                " ".join(hierarchy),
                description,
                "Parameters: " + " ".join(parameter_names) if parameter_names else "",
                "Attributes: " + " ".join(attribute_names) if attribute_names else "",
            )
            if value
        )

    @staticmethod
    def _matches_modules(node: dict[str, Any], module_ids: set[str] | None) -> bool:
        return not module_ids or bool(module_ids.intersection(set(node.get("modules") or [])))

    def _master_routes(
        self,
        query: str,
        module_ids: set[str] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        results = self.master_search.search(
            query,
            limit=self.config.master_search_limit,
            module_ids=module_ids,
            graph_hops=0,
        )
        business = [
            row for row in results
            if row.get("node_type") in {"business_entity", "business_attribute", "validated_rule"}
            and float(row.get("direct_score") or 0.0) > 0.0
        ]
        if business:
            best = max(float(row.get("direct_score") or 0.0) for row in business)
            floor = best * self.config.master_seed_min_relative_score
            business = [
                row for row in business
                if float(row.get("direct_score") or 0.0) >= floor
            ][: self.config.master_seed_limit]

        master_nodes = {node["id"]: node for node in self.master_graph.get("nodes", [])}
        outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.master_graph.get("edges", []):
            outgoing[edge["source"]].append(edge)
            incoming[edge["target"]].append(edge)

        routed: dict[str, dict[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        diagnostic_keys: set[tuple[str, str, str | None]] = set()

        def add_route(node_id: str, score: float, reason: str, source_id: str | None = None) -> None:
            node = master_nodes.get(node_id)
            if not node:
                return
            current = routed.get(node_id)
            if current is None or score > current["score"]:
                routed[node_id] = {"node": node, "score": score, "reason": reason}
            diagnostic_key = (node_id, reason, source_id)
            if diagnostic_key not in diagnostic_keys:
                diagnostic_keys.add(diagnostic_key)
                diagnostics.append({"node_id": node_id, "title": node.get("title"), "reason": reason, "source_id": source_id})

        for rank, row in enumerate(business, start=1):
            seed_score = 1.0 / rank
            add_route(row["id"], 1.0 + seed_score, "master_business_seed")
            node_type = row.get("node_type")
            parent_entities: list[str] = []
            if node_type == "business_attribute":
                for edge in incoming.get(row["id"], []):
                    if edge.get("type") == "has_attribute":
                        parent_entities.append(edge["source"])
                        add_route(edge["source"], 0.95 + seed_score, "attribute_parent_entity", row["id"])
                bridge_types = {"mapped_to_attribute", "uses_column"}
            elif node_type == "business_entity":
                parent_entities.append(row["id"])
                bridge_types = {"mapped_to_entity", "uses_table", "uses_column"}
            else:
                bridge_types = {"uses_table", "uses_column"}

            for edge in outgoing.get(row["id"], []):
                if edge.get("type") in bridge_types:
                    add_route(edge["target"], 0.90 + seed_score, edge["type"], row["id"])

            for entity_id in parent_entities:
                for edge in outgoing.get(entity_id, []):
                    if edge.get("type") in {"mapped_to_entity", "uses_table", "uses_column"}:
                        add_route(edge["target"], 0.85 + seed_score, edge["type"], entity_id)

        return business, routed, diagnostics

    def _fallback_roots(
        self,
        query: str,
        layer: str,
        module_ids: set[str] | None,
        query_vector: np.ndarray,
    ) -> list[tuple[dict[str, Any], float]]:
        if self.index_store is not None:
            return self.index_store.semantic_roots(
                query,
                layer,
                module_ids=module_ids,
                limit=self.config.fallback_roots_per_layer,
                query_vector=query_vector,
            )

        graph = self._load_layer(layer)
        root_types = {
            "physical": {"physical_table"},
            "otbi_analytics": {"otbi_subject_area"},
            "rest": {"rest_resource"},
        }[layer]
        nodes = [
            node for node in graph.get("nodes", [])
            if node.get("node_type") in root_types and self._matches_modules(node, module_ids)
        ]
        return self._semantic_top(
            query,
            nodes,
            self.config.fallback_roots_per_layer,
            query_vector=query_vector,
        )

    def _expand_physical_indexed(
        self,
        query: str,
        seed_ids: set[str],
        module_ids: set[str] | None,
        query_vector: np.ndarray,
    ) -> list[tuple[dict[str, Any], float, str]]:
        if self.index_store is None:
            return []

        selected: dict[str, tuple[dict[str, Any], float, str]] = {}
        table_ids: set[str] = set()
        routed_columns: list[dict[str, Any]] = []
        column_parent_ids: dict[str, str] = {}

        def add(node: dict[str, Any], score: float, reason: str) -> None:
            if not self._matches_modules(node, module_ids):
                return
            current = selected.get(node["id"])
            if current is None or score > current[1]:
                selected[node["id"]] = (node, score, reason)

        seed_nodes = self.index_store.fetch_nodes("physical", seed_ids)
        for seed_id in seed_ids:
            node = seed_nodes.get(seed_id)
            if not node:
                continue
            add(node, 0.95, "master_bridge")
            if node.get("node_type") in {"physical_table", "physical_table_stub"}:
                table_ids.add(seed_id)
            elif node.get("node_type") == "physical_column":
                routed_columns.append(node)

        if routed_columns:
            parent_rows = self.index_store.parents(
                "physical",
                {column["id"] for column in routed_columns},
                {"contains_column"},
            )
            for edge, parent in parent_rows:
                child_id = str(edge.get("target") or "")
                if child_id:
                    column_parent_ids[child_id] = parent["id"]
                table_ids.add(parent["id"])
                add(parent, 0.90, "column_parent_table")

        explicit_tables_with_columns = {
            str(column.get("table_name") or "").upper()
            for column in routed_columns
            if column.get("table_name")
        }

        tables = self.index_store.fetch_nodes("physical", table_ids)
        table_ids_for_semantic_columns = {
            table_id
            for table_id, table in tables.items()
            if str(
                table.get("name") or table.get("title") or ""
            ).upper() not in explicit_tables_with_columns
        }
        column_rows = self.index_store.children(
            "physical",
            table_ids_for_semantic_columns,
            {"contains_column"},
        )
        columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge, column in column_rows:
            parent_id = str(edge.get("source") or "")
            if parent_id:
                columns_by_table[parent_id].append(column)

        ranked_columns = self._semantic_top_grouped(
            query,
            columns_by_table,
            self.config.local_columns_per_table,
            query_vector=query_vector,
        )
        for table_id, ranked in ranked_columns.items():
            for column, score in ranked:
                add(column, 0.70 + score * 0.20, "semantic_table_column")
                routed_columns.append(column)
                column_parent_ids[column["id"]] = table_id

        selected_column_names: dict[str, set[str]] = defaultdict(set)
        for column in routed_columns:
            parent_id = column_parent_ids.get(column["id"])
            if parent_id:
                selected_column_names[parent_id].add(
                    str(column.get("name") or "").upper()
                )

        foreign_key_rows = self.index_store.children(
            "physical",
            set(selected_column_names),
            {"foreign_key_to"},
        )
        for edge, target in foreign_key_rows:
            parent_id = str(edge.get("source") or "")
            evidence = edge.get("evidence") or {}
            source_column = str(
                evidence.get("source_column") or ""
            ).upper()
            if source_column in selected_column_names.get(parent_id, set()):
                add(target, 0.82, "column_foreign_key")

        return list(selected.values())

    def _expand_physical(
        self,
        query: str,
        seed_ids: set[str],
        module_ids: set[str] | None,
        query_vector: np.ndarray,
    ) -> list[tuple[dict[str, Any], float, str]]:
        if self.index_store is not None:
            return self._expand_physical_indexed(
                query,
                seed_ids,
                module_ids,
                query_vector,
            )

        graph = self._load_layer("physical")
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in graph.get("edges", []):
            outgoing[edge["source"]].append(edge)
            incoming[edge["target"]].append(edge)

        selected: dict[str, tuple[dict[str, Any], float, str]] = {}
        table_ids: set[str] = set()
        routed_columns: list[dict[str, Any]] = []

        def add(node: dict[str, Any], score: float, reason: str) -> None:
            if not self._matches_modules(node, module_ids):
                return
            current = selected.get(node["id"])
            if current is None or score > current[1]:
                selected[node["id"]] = (node, score, reason)

        for seed_id in seed_ids:
            node = nodes.get(seed_id)
            if not node:
                continue
            add(node, 0.95, "master_bridge")
            if node.get("node_type") in {"physical_table", "physical_table_stub"}:
                table_ids.add(seed_id)
            elif node.get("node_type") == "physical_column":
                routed_columns.append(node)
                for edge in incoming.get(seed_id, []):
                    if edge.get("type") == "contains_column":
                        table_ids.add(edge["source"])
                        parent = nodes.get(edge["source"])
                        if parent:
                            add(parent, 0.90, "column_parent_table")

        explicit_tables_with_columns = {
            str(column.get("table_name") or "").upper()
            for column in routed_columns
            if column.get("table_name")
        }

        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for table_id in list(table_ids):
            table = nodes.get(table_id)
            if not table:
                continue
            table_name = str(table.get("name") or table.get("title") or "").upper()
            if table_name in explicit_tables_with_columns:
                continue
            columns_by_table[table_id] = [
                nodes[edge["target"]]
                for edge in outgoing.get(table_id, [])
                if edge.get("type") == "contains_column" and edge.get("target") in nodes
            ]

        ranked_columns = self._semantic_top_grouped(
            query,
            columns_by_table,
            self.config.local_columns_per_table,
            query_vector=query_vector,
        )
        for table_id, ranked in ranked_columns.items():
            for column, score in ranked:
                add(column, 0.70 + score * 0.20, "semantic_table_column")
                routed_columns.append(column)

        for column in routed_columns:
            table_name = str(column.get("table_name") or "").upper()
            column_name = str(column.get("name") or "").upper()
            parent_id = next((tid for tid in table_ids if str(nodes.get(tid, {}).get("name") or "").upper() == table_name), None)
            if not parent_id:
                continue
            for edge in outgoing.get(parent_id, []):
                evidence = edge.get("evidence") or {}
                if edge.get("type") != "foreign_key_to":
                    continue
                if str(evidence.get("source_column") or "").upper() != column_name:
                    continue
                target = nodes.get(edge["target"])
                if target:
                    add(target, 0.82, "column_foreign_key")

        return list(selected.values())

    def _expand_otbi(
        self,
        query: str,
        seed_ids: set[str],
        module_ids: set[str] | None,
        query_vector: np.ndarray,
    ) -> list[tuple[dict[str, Any], float, str]]:
        if self.index_store is not None:
            selected: list[tuple[dict[str, Any], float, str]] = []
            nodes = self.index_store.fetch_nodes("otbi_analytics", seed_ids)
            valid_seed_ids: set[str] = set()
            for seed_id in seed_ids:
                node = nodes.get(seed_id)
                if not node or not self._matches_modules(node, module_ids):
                    continue
                selected.append((node, 0.95, "master_bridge"))
                valid_seed_ids.add(seed_id)

            question_rows = self.index_store.parents(
                "otbi_analytics",
                valid_seed_ids,
                {"answered_by"},
            )
            questions_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for edge, question in question_rows:
                subject_id = str(edge.get("target") or "")
                if subject_id:
                    questions_by_subject[subject_id].append(question)
            ranked_questions = self._semantic_top_grouped(
                query,
                questions_by_subject,
                self.config.local_questions_per_subject_area,
                query_vector=query_vector,
            )
            for ranked in ranked_questions.values():
                for question, score in ranked:
                    selected.append((question, 0.65 + score * 0.20, "subject_area_question"))
            return selected

        graph = self._load_layer("otbi_analytics")
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in graph.get("edges", []):
            incoming[edge["target"]].append(edge)
        selected: list[tuple[dict[str, Any], float, str]] = []
        questions_by_subject: dict[str, list[dict[str, Any]]] = {}
        for seed_id in seed_ids:
            node = nodes.get(seed_id)
            if not node or not self._matches_modules(node, module_ids):
                continue
            selected.append((node, 0.95, "master_bridge"))
            questions_by_subject[seed_id] = [
                nodes[edge["source"]]
                for edge in incoming.get(seed_id, [])
                if edge.get("type") == "answered_by" and edge.get("source") in nodes
            ]

        ranked_questions = self._semantic_top_grouped(
            query,
            questions_by_subject,
            self.config.local_questions_per_subject_area,
            query_vector=query_vector,
        )
        for ranked in ranked_questions.values():
            for question, score in ranked:
                selected.append((question, 0.65 + score * 0.20, "subject_area_question"))
        return selected

    def _expand_rest(
        self,
        query: str,
        seed_ids: set[str],
        module_ids: set[str] | None,
        query_vector: np.ndarray,
    ) -> list[tuple[dict[str, Any], float, str]]:
        if self.index_store is not None:
            selected: list[tuple[dict[str, Any], float, str]] = []
            nodes = self.index_store.fetch_nodes("rest", seed_ids)
            valid_seed_ids: set[str] = set()
            for seed_id in seed_ids:
                node = nodes.get(seed_id)
                if not node or not self._matches_modules(node, module_ids):
                    continue
                selected.append((node, 0.95, "master_bridge"))
                valid_seed_ids.add(seed_id)

            source_queries = {
                seed_id: " ".join(
                    value
                    for value in (
                        query.strip(),
                        str(nodes[seed_id].get("title") or "").strip(),
                        str(nodes[seed_id].get("name") or "").strip(),
                    )
                    if value
                )
                for seed_id in valid_seed_ids
            }
            (
                operation_rows_by_resource,
                linked_counts,
                fts_counts,
            ) = self.index_store.prefilter_children(
                "rest",
                source_queries,
                {"has_operation"},
                limit_per_source=(
                    self.config.local_operation_candidates_per_resource
                ),
            )
            operations_by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for resource_id, operation_rows in operation_rows_by_resource.items():
                for _, operation in operation_rows:
                    operations_by_resource[resource_id].append(operation)

            self._rest_operation_diagnostics = {
                "resource_count": len(valid_seed_ids),
                "linked_operation_count": sum(linked_counts.values()),
                "fts_candidate_count": sum(fts_counts.values()),
                "semantic_candidate_count": sum(
                    len(values) for values in operations_by_resource.values()
                ),
                "candidate_limit_per_resource": (
                    self.config.local_operation_candidates_per_resource
                ),
            }
            self._emit_progress(
                "[SEARCH] REST operações: "
                f"{self._rest_operation_diagnostics['linked_operation_count']} ligadas, "
                f"{self._rest_operation_diagnostics['fts_candidate_count']} via FTS5, "
                f"{self._rest_operation_diagnostics['semantic_candidate_count']} "
                "candidatas semânticas."
            )
            ranked_operations = self._semantic_top_grouped(
                query,
                operations_by_resource,
                self.config.local_operations_per_resource,
                query_vector=query_vector,
                text_builder=self._rest_operation_text,
            )
            for ranked in ranked_operations.values():
                for operation, score in ranked:
                    selected.append((operation, 0.65 + score * 0.20, "resource_operation"))
            return selected

        graph = self._load_layer("rest")
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in graph.get("edges", []):
            outgoing[edge["source"]].append(edge)
        selected: list[tuple[dict[str, Any], float, str]] = []
        operations_by_resource: dict[str, list[dict[str, Any]]] = {}
        for seed_id in seed_ids:
            node = nodes.get(seed_id)
            if not node or not self._matches_modules(node, module_ids):
                continue
            selected.append((node, 0.95, "master_bridge"))
            operations_by_resource[seed_id] = [
                nodes[edge["target"]]
                for edge in outgoing.get(seed_id, [])
                if edge.get("type") == "has_operation" and edge.get("target") in nodes
            ]

        ranked_operations = self._semantic_top_grouped(
            query,
            operations_by_resource,
            self.config.local_operations_per_resource,
            query_vector=query_vector,
            text_builder=self._rest_operation_text,
        )
        for ranked in ranked_operations.values():
            for operation, score in ranked:
                selected.append((operation, 0.65 + score * 0.20, "resource_operation"))
        return selected

    @staticmethod
    def _result(node: dict[str, Any], score: float, rank: int) -> dict[str, Any]:
        return {
            "id": node["id"],
            "node_type": node.get("node_type"),
            "title": node.get("title") or node.get("name") or node["id"],
            "score": round(float(score), 6),
            "search_rank": rank,
            "semantic_score": None,
            "context_score": round(float(score), 6),
            "summary": HybridSearch._summary(node),
            "source": node.get("source", {}),
            "sources": node.get("sources", []),
            "modules": node.get("modules", []),
            "node": node,
        }

    def build_prompt_context(
        self,
        query: str,
        *,
        limit: int = 20,
        max_characters: int = 14000,
        module_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        timings: dict[str, float] = {}
        self._rest_operation_diagnostics = {}

        def complete_stage(name: str, started: float) -> None:
            elapsed = time.perf_counter() - started
            timings[name] = round(elapsed, 4)
            self._emit_progress(
                f"[SEARCH] {name}: {elapsed:.2f}s"
            )

        stage_started = time.perf_counter()
        business, routed, diagnostics = self._master_routes(query, module_ids)
        complete_stage("master_routing", stage_started)

        self._emit_progress("[SEARCH] Codificando a consulta semântica...")
        stage_started = time.perf_counter()
        query_vector = self.semantic_text_selector.encode_query(query)
        complete_stage("query_embedding", stage_started)

        candidates: dict[str, tuple[dict[str, Any], float, str, int]] = {}

        def add(node: dict[str, Any], score: float, reason: str, priority: int) -> None:
            current = candidates.get(node["id"])
            if (
                current is None
                or priority < current[3]
                or (priority == current[3] and score > current[1])
            ):
                candidates[node["id"]] = (node, score, reason, priority)

        for row in business:
            add(row["node"], 1.10 + float(row.get("direct_score") or 0.0), "master_business_seed", 0)
        for item in routed.values():
            add(item["node"], item["score"], item["reason"], 1)

        layer_seed_ids: dict[str, set[str]] = defaultdict(set)
        for node_id, item in routed.items():
            layer = item["node"].get("graph_layer")
            if layer in {"physical", "otbi_analytics", "rest"}:
                layer_seed_ids[layer].add(node_id)

        fallback: list[dict[str, Any]] = []
        stage_started = time.perf_counter()
        for layer in ("physical", "otbi_analytics", "rest"):
            if not layer_seed_ids[layer]:
                for node, semantic_score in self._fallback_roots(
                    query,
                    layer,
                    module_ids,
                    query_vector,
                ):
                    layer_seed_ids[layer].add(node["id"])
                    fallback.append({"layer": layer, "node_id": node["id"], "title": node.get("title"), "semantic_score": round(semantic_score, 6)})
                    add(node, 0.70 + semantic_score * 0.20, "semantic_layer_root", 2)
        complete_stage("fallback_roots", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_physical(
            query,
            layer_seed_ids["physical"],
            module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("physical_expansion", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_otbi(
            query,
            layer_seed_ids["otbi_analytics"],
            module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("otbi_expansion", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_rest(
            query,
            layer_seed_ids["rest"],
            module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("rest_expansion", stage_started)

        stage_started = time.perf_counter()
        ordered = sorted(
            candidates.values(),
            key=lambda item: (item[3], -item[1], str(item[0].get("title") or "")),
        )
        results = [self._result(node, score, rank) for rank, (node, score, _, _) in enumerate(ordered, start=1)]
        payload = self.master_search.build_prompt_context_from_results(
            query,
            results,
            limit=limit,
            max_characters=max_characters,
            query_vector=query_vector,
        )
        complete_stage("context_rendering", stage_started)

        timings["total"] = round(
            time.perf_counter() - total_started,
            4,
        )
        self._emit_progress(
            f"[SEARCH] total: {timings['total']:.2f}s"
        )
        payload["routing"] = {
            "backend": self.backend_name,
            "index_path": (
                str(self.index_store.index_path)
                if self.index_store is not None
                else None
            ),
            "index_paths": (
                self.index_store.index_paths
                if isinstance(self.index_store, IndexedGraphBundleStore)
                else None
            ),
            "master_business_seeds": [row["id"] for row in business],
            "master_routes": diagnostics,
            "semantic_fallback_roots": fallback,
            "candidate_count": len(results),
            "rest_operation_diagnostics": self._rest_operation_diagnostics,
            "timings_seconds": timings,
        }
        return payload
