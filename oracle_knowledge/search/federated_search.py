from __future__ import annotations

import re
import time
import unicodedata
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
from oracle_knowledge.query_planning import decompose_query, diagnose_query_encoding
from oracle_knowledge.topology import TopologyNavigator, classify_provenance
from oracle_knowledge.search.semantic_documents import (
    default_semantic_document_text,
    rest_operation_semantic_document_text,
)


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
    adf_top_k: int = 5
    adf_min_lexical_score: float = 0.18
    semantic_fallback_min_score: float = 0.30
    max_attribute_candidates: int = 64
    attribute_fts_candidates_per_table: int = 8
    attribute_semantic_candidates: int = 8
    attribute_semantic_min_score: float = 0.42
    attribute_lexical_resolve_min_score: float = 0.82
    attribute_lexical_ambiguity_margin: float = 0.10


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
        self._semantic_inference_diagnostics: dict[str, dict[str, int]] = {}
        self.topology = TopologyNavigator(self.graph_dir)

        resolved_index = resolve_index_source(
            self.graph_dir,
            index_path,
            model_name=self.semantic_text_selector.config.model_name,
        )
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
        return default_semantic_document_text(node)

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
        layer: str | None = None,
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
        score_values: list[float | None] = [None for _ in flattened]
        missing_indexes = list(range(len(flattened)))

        if (
            layer
            and self.index_store is not None
            and query_vector is not None
        ):
            persisted_scores = self.index_store.semantic_segment_scores(
                layer,
                [str(node.get("id") or "") for node in flattened],
                query_vector,
            )
            missing_indexes = []
            for index, node in enumerate(flattened):
                node_id = str(node.get("id") or "")
                if node_id in persisted_scores:
                    score_values[index] = float(persisted_scores[node_id])
                else:
                    missing_indexes.append(index)

            diagnostics = self._semantic_inference_diagnostics.setdefault(
                layer,
                {
                    "persisted_candidates": 0,
                    "live_candidates": 0,
                    "live_batches": 0,
                },
            )
            diagnostics["persisted_candidates"] += (
                len(flattened) - len(missing_indexes)
            )

        if missing_indexes:
            documents = [
                build_text(flattened[index])
                for index in missing_indexes
            ]
            if query_vector is None:
                live_scores = self.semantic_text_selector.score_documents(
                    query,
                    documents,
                )
            else:
                live_scores = (
                    self.semantic_text_selector
                    .score_documents_with_query_vector(
                        query_vector,
                        documents,
                    )
                )
            for index, score in zip(
                missing_indexes,
                live_scores,
                strict=True,
            ):
                score_values[index] = float(score)

            if layer:
                diagnostics = self._semantic_inference_diagnostics.setdefault(
                    layer,
                    {
                        "persisted_candidates": 0,
                        "live_candidates": 0,
                        "live_batches": 0,
                    },
                )
                diagnostics["live_candidates"] += len(missing_indexes)
                diagnostics["live_batches"] += 1

        scores = [float(value or 0.0) for value in score_values]

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
        return rest_operation_semantic_document_text(node)

    @staticmethod
    def _node_modules(node: dict[str, Any]) -> set[str]:
        modules = {str(value) for value in node.get("modules") or [] if value}
        if node.get("module_id"):
            modules.add(str(node["module_id"]))
        return modules

    @classmethod
    def _matches_modules(cls, node: dict[str, Any], module_ids: set[str] | None) -> bool:
        return not module_ids or bool(module_ids.intersection(cls._node_modules(node)))

    @staticmethod
    def _fold_text(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or "").casefold())
        text = "".join(character for character in text if not unicodedata.combining(character))
        return " ".join(re.findall(r"[a-z0-9_$#]+", text))

    @classmethod
    def _lexical_score(cls, query: str, node: dict[str, Any]) -> float:
        query_terms = set(cls._fold_text(query).split())
        node_text = cls._fold_text(
            " ".join(
                str(value or "")
                for value in (
                    node.get("title"),
                    node.get("name"),
                    node.get("aliases"),
                    node.get("search_text"),
                )
            )
        )
        node_terms = set(node_text.split())
        if not query_terms or not node_terms:
            return 0.0
        overlap = len(query_terms & node_terms) / max(1, len(node_terms))
        exact_bonus = 0.35 if node_text and node_text in cls._fold_text(query) else 0.0
        return min(1.0, overlap + exact_bonus)

    @classmethod
    def _attribute_lexical_relevance(
        cls,
        requested_label: str,
        canonical_attribute: str,
        node: dict[str, Any],
    ) -> dict[str, Any]:
        """Measure lexical evidence without treating FTS membership as proof.

        FTS5 is used only to reduce the candidate set.  This method scores the
        actual technical name, aliases and descriptive text so that every FTS
        hit no longer receives the same artificial score.
        """

        query_phrases = list(
            dict.fromkeys(
                value
                for value in (
                    cls._fold_text(requested_label),
                    cls._fold_text(str(canonical_attribute or "").replace("_", " ")),
                )
                if value
            )
        )
        query_token_sets = [set(value.split()) for value in query_phrases if value]
        query_tokens = set().union(*query_token_sets) if query_token_sets else set()

        def final_identifier(value: Any) -> str:
            text = str(value or "")
            return text.rsplit(".", 1)[-1].replace("_", " ")

        name_phrases = list(
            dict.fromkeys(
                value
                for value in (
                    cls._fold_text(node.get("attribute_id")),
                    cls._fold_text(final_identifier(node.get("qualified_name"))),
                    cls._fold_text(final_identifier(node.get("name"))),
                    cls._fold_text(final_identifier(node.get("title"))),
                )
                if value
            )
        )
        alias_values = node.get("aliases") or []
        if not isinstance(alias_values, (list, tuple, set)):
            alias_values = [alias_values]
        alias_phrases = [cls._fold_text(value) for value in alias_values if value]
        comparison_phrases = list(dict.fromkeys([*name_phrases, *alias_phrases]))

        exact_phrase = any(
            query_phrase == candidate_phrase
            for query_phrase in query_phrases
            for candidate_phrase in comparison_phrases
        )
        exact_token_set = any(
            query_set and query_set == set(candidate_phrase.split())
            for query_set in query_token_sets
            for candidate_phrase in comparison_phrases
        )

        best_name_score = 0.0
        for candidate_phrase in comparison_phrases:
            candidate_tokens = set(candidate_phrase.split())
            if not query_tokens or not candidate_tokens:
                continue
            coverage = len(query_tokens & candidate_tokens) / len(query_tokens)
            precision = len(query_tokens & candidate_tokens) / len(candidate_tokens)
            best_name_score = max(best_name_score, 0.75 * coverage + 0.25 * precision)

        descriptive_text = cls._fold_text(
            " ".join(
                str(value or "")
                for value in (
                    node.get("description"),
                    node.get("summary"),
                    node.get("search_text"),
                )
            )
        )
        descriptive_tokens = set(descriptive_text.split())
        text_coverage = (
            len(query_tokens & descriptive_tokens) / len(query_tokens)
            if query_tokens and descriptive_tokens
            else 0.0
        )

        if exact_phrase or exact_token_set:
            score = 1.0
        else:
            score = min(0.99, 0.88 * best_name_score + 0.12 * text_coverage)
        return {
            "score": float(score),
            "exact_phrase": bool(exact_phrase),
            "exact_token_set": bool(exact_token_set),
            "name_score": float(best_name_score),
            "text_coverage": float(text_coverage),
        }

    def _community_roots(
        self,
        query: str,
        layer: str,
        module_ids: set[str] | None,
        query_vector: np.ndarray,
        community_id: str,
    ) -> list[tuple[dict[str, Any], float]]:
        root_types = {
            "physical": {"physical_table"},
            "otbi_analytics": {"otbi_subject_area"},
            "rest": {"rest_resource", "adf_resource"},
        }[layer]
        eligible = [
            node
            for node in self.topology.nodes.values()
            if node.get("node_type") in root_types
            and self.topology.contains(community_id, str(node.get("id") or ""))
            and self._matches_modules(node, module_ids)
        ]
        if not eligible:
            return []

        lexical = {
            str(node.get("id") or ""): self._lexical_score(query, node)
            for node in eligible
        }
        semantic: dict[str, float] = {}
        if self.index_store is not None:
            semantic = self.index_store.semantic_segment_scores(
                layer,
                [str(node.get("id") or "") for node in eligible],
                query_vector,
            )
        ranked: list[tuple[dict[str, Any], float]] = []
        for node in eligible:
            node_id = str(node.get("id") or "")
            lexical_score = lexical.get(node_id, 0.0)
            semantic_score = semantic.get(node_id)
            if semantic_score is None:
                combined = lexical_score
            else:
                combined = max(lexical_score, 0.35 * lexical_score + 0.65 * semantic_score)
            if combined >= self.config.semantic_fallback_min_score:
                ranked.append((node, float(combined)))
        ranked.sort(key=lambda item: (-item[1], str(item[0].get("title") or item[0].get("id") or "")))
        return ranked[: self.config.fallback_roots_per_layer]

    def _adf_first(
        self,
        query: str,
        community_id: str | None,
        routed_ids: set[str],
    ) -> tuple[list[tuple[dict[str, Any], float]], dict[str, Any]]:
        all_adf = [
            node
            for node in self.topology.nodes.values()
            if node.get("node_type") == "adf_resource"
        ]
        if not all_adf:
            return [], {
                "catalog_status": "empty",
                "evaluated": [],
                "accepted": [],
                "discarded": [],
            }
        local = [
            node
            for node in all_adf
            if community_id
            and self.topology.contains(community_id, str(node.get("id") or ""))
        ]
        evaluated = []
        accepted = []
        discarded = []
        ranked = sorted(
            ((node, self._lexical_score(query, node)) for node in local),
            key=lambda item: (-item[1], str(item[0].get("id") or "")),
        )[: self.config.adf_top_k]
        for node, score in ranked:
            node_id = str(node.get("id") or "")
            explicit_bridge = node_id in routed_ids
            is_accepted = explicit_bridge or score >= self.config.adf_min_lexical_score
            row = {
                "node_id": node_id,
                "score": round(float(score), 6),
                "explicit_bridge": explicit_bridge,
                "decision": "accepted" if is_accepted else "discarded",
            }
            evaluated.append(row)
            if is_accepted:
                accepted.append(node_id)
            else:
                discarded.append({**row, "reason": "below_minimum_score"})
        status = "evaluated" if local else "no_candidates_in_active_community"
        return (
            [(node, score) for node, score in ranked if str(node.get("id") or "") in accepted],
            {
                "catalog_status": status,
                "global_resource_count": len(all_adf),
                "community_resource_count": len(local),
                "evaluated": evaluated,
                "accepted": accepted,
                "discarded": discarded,
            },
        )

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

        for routed_id, routed_row in list(routed.items()):
            for edge in outgoing.get(routed_id, []):
                if edge.get("type") != "environment_variant_of":
                    continue
                add_route(
                    edge["target"],
                    max(0.70, float(routed_row["score"]) * 0.82),
                    "environment_variant_of",
                    routed_id,
                )

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
            "rest": {"rest_resource", "adf_resource"},
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
            layer="physical",
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
            layer="physical",
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
                layer="otbi_analytics",
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
            layer="otbi_analytics",
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
                layer="rest",
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
            layer="rest",
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
        self._semantic_inference_diagnostics = {}

        def complete_stage(name: str, started: float) -> None:
            elapsed = time.perf_counter() - started
            timings[name] = round(elapsed, 4)
            self._emit_progress(f"[SEARCH] {name}: {elapsed:.2f}s")

        query_diagnostics = diagnose_query_encoding(query)
        normalized_query = str(query_diagnostics["normalized_query"])
        all_nodes = list(self.topology.nodes.values())
        query_plan = decompose_query(normalized_query, all_nodes)
        primary_entity_id = query_plan.get("primary_entity_id")
        active_community = self.topology.community(primary_entity_id)
        community_id = active_community.get("community_id") if active_community else None
        candidate_module = query_plan.get("candidate_module")
        effective_module_ids = set(module_ids or [])
        if not effective_module_ids and candidate_module:
            effective_module_ids.add(str(candidate_module))
        effective_module_ids = effective_module_ids or None

        path_cache: dict[str, dict[str, Any] | None] = {}

        def structural_path(node_id: str) -> dict[str, Any] | None:
            if not primary_entity_id or not node_id:
                return None
            if node_id == primary_entity_id:
                return {"nodes": [node_id], "edges": [], "complete": True}
            if community_id and not self.topology.contains(community_id, node_id):
                return None
            if node_id not in path_cache:
                path_cache[node_id] = self.topology.path(
                    primary_entity_id,
                    node_id,
                    allowed_community=community_id,
                )
            return path_cache[node_id]

        rejected_by_id: dict[str, dict[str, Any]] = {}

        def reject(
            node_id: str,
            *,
            reason: str,
            score: float | None = None,
            stage: str,
        ) -> None:
            if not node_id or node_id in rejected_by_id:
                return
            rejected_by_id[node_id] = {
                "node_id": node_id,
                "reason": reason,
                "semantic_score": round(float(score), 6) if score is not None else None,
                "community_id": self.topology.memberships.get(node_id),
                "stage": stage,
            }

        stage_started = time.perf_counter()
        business, routed, route_diagnostics = self._master_routes(
            normalized_query,
            effective_module_ids,
        )
        complete_stage("master_routing", stage_started)

        accepted_business: list[dict[str, Any]] = []
        for row in business:
            node_id = str(row.get("id") or "")
            if not primary_entity_id or node_id == primary_entity_id or structural_path(node_id):
                accepted_business.append(row)
            else:
                reject(
                    node_id,
                    reason="no_structural_path",
                    score=float(row.get("direct_score") or 0.0),
                    stage="master_business_routing",
                )
        business = accepted_business

        accepted_routed: dict[str, dict[str, Any]] = {}
        for node_id, item in routed.items():
            if not primary_entity_id or node_id == primary_entity_id or structural_path(node_id):
                accepted_routed[node_id] = item
            else:
                reject(
                    node_id,
                    reason="no_structural_path",
                    score=float(item.get("score") or 0.0),
                    stage="master_bridge_routing",
                )
        routed = accepted_routed
        accepted_route_diagnostics = [
            row for row in route_diagnostics if str(row.get("node_id") or "") in routed
        ]
        discarded_route_diagnostics = [
            row for row in route_diagnostics if str(row.get("node_id") or "") not in routed
        ]

        adf_candidates, adf_diagnostics = self._adf_first(
            normalized_query,
            community_id,
            set(routed),
        )
        for node, score in adf_candidates:
            routed[node["id"]] = {
                "node": node,
                "score": 0.92 + float(score) * 0.08,
                "reason": "adf_first",
            }

        self._emit_progress("[SEARCH] Codificando a consulta semântica...")
        stage_started = time.perf_counter()
        query_vector = self.semantic_text_selector.encode_query(normalized_query)
        complete_stage("query_embedding", stage_started)

        candidates: dict[str, tuple[dict[str, Any], float, str, int]] = {}

        def add(node: dict[str, Any], score: float, reason: str, priority: int) -> None:
            node_id = str(node.get("id") or "")
            if not node_id:
                return
            if primary_entity_id and node_id != primary_entity_id and structural_path(node_id) is None:
                reject(
                    node_id,
                    reason="no_structural_path",
                    score=score,
                    stage=reason,
                )
                return
            current = candidates.get(node_id)
            if (
                current is None
                or priority < current[3]
                or (priority == current[3] and score > current[1])
            ):
                candidates[node_id] = (node, score, reason, priority)

        for row in business:
            add(
                row["node"],
                1.10 + float(row.get("direct_score") or 0.0),
                "master_business_seed",
                0,
            )
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
            if layer_seed_ids[layer]:
                continue
            if community_id:
                roots = self._community_roots(
                    normalized_query,
                    layer,
                    effective_module_ids,
                    query_vector,
                    community_id,
                )
                scope = "active_community"
            else:
                roots = self._fallback_roots(
                    normalized_query,
                    layer,
                    effective_module_ids,
                    query_vector,
                )
                scope = "global_no_entity"
            for node, semantic_score in roots:
                node_id = str(node.get("id") or "")
                layer_seed_ids[layer].add(node_id)
                fallback.append(
                    {
                        "layer": layer,
                        "node_id": node_id,
                        "title": node.get("title"),
                        "semantic_score": round(float(semantic_score), 6),
                        "scope": scope,
                        "community_id": community_id if scope == "active_community" else None,
                    }
                )
                add(node, 0.70 + float(semantic_score) * 0.20, "semantic_layer_root", 2)
        complete_stage("fallback_roots", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_physical(
            normalized_query,
            layer_seed_ids["physical"],
            effective_module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("physical_expansion", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_otbi(
            normalized_query,
            layer_seed_ids["otbi_analytics"],
            effective_module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("otbi_expansion", stage_started)

        stage_started = time.perf_counter()
        for node, score, reason in self._expand_rest(
            normalized_query,
            layer_seed_ids["rest"],
            effective_module_ids,
            query_vector,
        ):
            add(node, score, reason, 2 if reason == "master_bridge" else 3)
        complete_stage("rest_expansion", stage_started)

        stage_started = time.perf_counter()
        ordered = sorted(
            candidates.values(),
            key=lambda item: (item[3], -item[1], str(item[0].get("title") or "")),
        )
        accepted_results: list[dict[str, Any]] = []
        semantic_candidates: list[dict[str, Any]] = []
        for node, score, reason, _priority in ordered:
            node_id = str(node.get("id") or "")
            path = structural_path(node_id)
            accepted = not primary_entity_id or node_id == primary_entity_id or path is not None
            if not accepted:
                reject(
                    node_id,
                    reason="no_structural_path",
                    score=score,
                    stage="structural_gating",
                )
                continue
            result = self._result(node, score, len(accepted_results) + 1)
            result["selection_reason"] = reason
            accepted_results.append(result)
            if reason.startswith("semantic_") or reason in {
                "semantic_table_column",
                "subject_area_question",
                "resource_operation",
            }:
                semantic_candidates.append(
                    {
                        "node_id": node_id,
                        "community_id": self.topology.memberships.get(node_id),
                        "semantic_score": round(float(score), 6),
                        "path_found": path is not None or node_id == primary_entity_id,
                        "decision": "accepted",
                        "scope": "active_community" if community_id else "global_no_entity",
                    }
                )
        complete_stage("structural_gating", stage_started)

        stage_started = time.perf_counter()
        payload = self.master_search.build_prompt_context_from_results(
            normalized_query,
            accepted_results,
            limit=limit,
            max_characters=max_characters,
            query_vector=query_vector,
        )
        complete_stage("context_rendering", stage_started)

        stage_started = time.perf_counter()
        community_nodes = [
            node
            for node in all_nodes
            if not community_id
            or self.topology.contains(community_id, str(node.get("id") or ""))
        ]
        community_nodes_by_id = {
            str(node.get("id") or ""): node for node in community_nodes if node.get("id")
        }
        accepted_by_id = {str(row.get("id") or ""): row for row in accepted_results}

        direct_entity_tables = set()
        if primary_entity_id:
            for target_id in self.topology.linked_targets(
                primary_entity_id,
                {"mapped_to_entity", "mapped_to_table", "uses_table"},
                community_id=community_id,
            ):
                target = self.topology.nodes.get(target_id, {})
                if target.get("node_type") in {"physical_table", "physical_table_stub"}:
                    direct_entity_tables.add(target_id)

        evidence_endpoint_types = {
            "physical_column",
            "physical_table",
            "physical_table_stub",
            "adf_resource",
            "rest_resource",
            "rest_operation",
            "otbi_subject_area",
        }

        def format_path(path: dict[str, Any]) -> dict[str, Any]:
            endpoint_type = ""
            if path.get("nodes"):
                endpoint_type = str(
                    self.topology.nodes.get(path["nodes"][-1], {}).get("node_type") or ""
                )
            complete = bool(path.get("edges")) and endpoint_type in evidence_endpoint_types
            return {
                "nodes": [
                    {
                        "id": node_id,
                        "title": self.topology.nodes.get(node_id, {}).get("title")
                        or self.topology.nodes.get(node_id, {}).get("name")
                        or node_id,
                        "node_type": self.topology.nodes.get(node_id, {}).get("node_type"),
                    }
                    for node_id in path["nodes"]
                ],
                "edges": [
                    {
                        "type": edge.get("type"),
                        "source": edge.get("source"),
                        "target": edge.get("target"),
                        "provenance": classify_provenance(edge),
                        "confidence": float(edge.get("confidence") or 0.0),
                        "source_type": edge.get("source_type"),
                        "source_reference": edge.get("source_reference"),
                        "explanation": edge.get("explanation"),
                    }
                    for edge in path["edges"]
                ],
                "complete": complete,
                "community_id": community_id,
            }

        def table_grain_kind(table_id: str) -> str:
            table = self.topology.nodes.get(table_id, {})
            result_grain = table.get("result_grain") or {}
            if not result_grain and isinstance(table.get("evidence"), dict):
                result_grain = table["evidence"].get("result_grain") or {}
            grain_columns = {
                str(value).upper() for value in result_grain.get("grain_columns") or []
            }
            title = str(table.get("title") or table.get("name") or "").upper()
            if any("LINE_ID" in value for value in grain_columns) or "_LINES_" in title:
                return "line"
            if any("HEADER_ID" in value for value in grain_columns) or "_HEADERS_" in title:
                return "header"
            return "unknown"

        def physical_path_for_column(
            column_id: str,
            fallback_path: dict[str, Any] | None,
        ) -> tuple[dict[str, Any] | None, str | None]:
            owner = self.topology.physical_owner_table(
                column_id,
                community_id=community_id,
            )
            if owner is None:
                return fallback_path, None
            table_id, ownership_edge = owner
            table_path = structural_path(table_id)
            if not table_path:
                return fallback_path, table_id
            return (
                {
                    "nodes": [*table_path["nodes"], column_id],
                    "edges": [*table_path["edges"], ownership_edge],
                    "complete": True,
                },
                table_id,
            )

        def grain_compatibility(
            path: dict[str, Any] | None,
            owner_table_id: str | None = None,
        ) -> tuple[str, str]:
            if not path or not query_plan.get("requested_grain"):
                return "unknown", "Não há caminho físico e grão solicitado suficientes para comparação."
            tables = [
                node_id
                for node_id in path.get("nodes", [])
                if self.topology.nodes.get(node_id, {}).get("node_type")
                in {"physical_table", "physical_table_stub"}
            ]
            if owner_table_id and owner_table_id not in tables:
                tables.append(owner_table_id)
            if not tables:
                return "unknown", "O caminho selecionado não alcança uma tabela física comprovada."
            kinds = {table_grain_kind(table_id) for table_id in tables}
            if "line" in kinds:
                return (
                    "risk",
                    "O caminho alcança tabela com grão de linha; a expansão pode duplicar uma linha por acordo.",
                )
            if "header" in kinds:
                return (
                    "compatible",
                    "O caminho alcança tabela de cabeçalho compatível com uma linha por acordo.",
                )
            if direct_entity_tables and set(tables).issubset(direct_entity_tables):
                return (
                    "unknown",
                    "A tabela está diretamente ligada à entidade, mas o grafo não declara grão de cabeçalho ou linha.",
                )
            return "unknown", "O grafo não possui evidência suficiente para provar compatibilidade de grão."

        attribute_evidence = []
        details = query_plan.get("requested_attribute_details") or [
            {
                "requested_label": attribute,
                "canonical_attribute": attribute,
                "matched_node_id": None,
                "status": "unresolved",
            }
            for attribute in query_plan.get("requested_attributes", [])
        ]

        table_column_ids: dict[str, list[str]] = {}
        eligible_physical_columns: set[str] = set()
        for table_id in sorted(direct_entity_tables):
            column_ids = self.topology.linked_targets(
                table_id,
                {"contains_column", "has_column"},
                community_id=community_id,
            )
            column_ids = [
                node_id
                for node_id in column_ids
                if self.topology.nodes.get(node_id, {}).get("node_type") == "physical_column"
            ]
            table_column_ids[table_id] = column_ids
            eligible_physical_columns.update(column_ids)

        attribute_vectors: dict[int, np.ndarray] = {}
        if self.index_store is not None and eligible_physical_columns and details:
            vector_started = time.perf_counter()
            vector_queries = [
                f"{detail.get('requested_label') or detail.get('canonical_attribute')} "
                f"{query_plan.get('primary_entity_name') or query_plan.get('primary_entity') or ''}"
                for detail in details
            ]
            vectors = self.semantic_text_selector.encode_queries(vector_queries)
            attribute_vectors = {index: vector for index, vector in enumerate(vectors)}
            complete_stage("attribute_query_embeddings", vector_started)

        for detail_index, detail in enumerate(details):
            canonical = str(detail.get("canonical_attribute") or "")
            requested_label = str(detail.get("requested_label") or canonical)
            matched_node_id = str(detail.get("matched_node_id") or "")
            candidate_scores: dict[str, dict[str, Any]] = {}

            def add_candidate(
                node_id: str,
                *,
                source: str,
                lexical_score: float = 0.0,
                semantic_score: float | None = None,
            ) -> None:
                if not node_id or (community_id and not self.topology.contains(community_id, node_id)):
                    return
                row = candidate_scores.setdefault(
                    node_id,
                    {
                        "node_id": node_id,
                        "sources": set(),
                        "lexical_score": 0.0,
                        "semantic_score": None,
                    },
                )
                row["sources"].add(source)
                row["lexical_score"] = max(float(row["lexical_score"]), lexical_score)
                if semantic_score is not None:
                    current = row.get("semantic_score")
                    row["semantic_score"] = max(
                        float(semantic_score),
                        float(current) if current is not None else float("-inf"),
                    )

            if matched_node_id:
                add_candidate(matched_node_id, source="curated_business_attribute")
                for node_id in self.topology.linked_targets(
                    matched_node_id,
                    {"mapped_to_attribute", "uses_column", "mapped_to_table"},
                    community_id=community_id,
                ):
                    add_candidate(node_id, source="curated_attribute_mapping")
                attribute_node = self.topology.nodes.get(matched_node_id, {})
                qualified_names = {
                    str(value).upper() for value in attribute_node.get("columns") or [] if value
                }
                for node in community_nodes:
                    identifiers = {
                        str(node.get("qualified_name") or "").upper(),
                        str(node.get("title") or "").upper(),
                        str(node.get("name") or "").upper(),
                    }
                    if qualified_names & identifiers:
                        add_candidate(str(node.get("id") or ""), source="qualified_name_mapping")

            requested_folded = self._fold_text(requested_label)
            canonical_folded = self._fold_text(canonical)
            for node in community_nodes:
                node_id = str(node.get("id") or "")
                if not node_id:
                    continue
                phrases = {
                    self._fold_text(node.get("attribute_id")),
                    self._fold_text(node.get("name")),
                    self._fold_text(node.get("title")),
                }
                phrases.update(self._fold_text(value) for value in node.get("aliases") or [])
                phrases.discard("")
                if requested_folded in phrases or canonical_folded in phrases:
                    add_candidate(node_id, source="exact_local_alias", lexical_score=1.0)

            if self.index_store is not None and table_column_ids:
                source_queries = {
                    table_id: f"{requested_label} {canonical.replace('_', ' ')}"
                    for table_id in table_column_ids
                }
                prefetched, _linked_counts, fts_counts = self.index_store.prefilter_children(
                    "physical",
                    source_queries,
                    {"contains_column", "has_column"},
                    limit_per_source=self.config.attribute_fts_candidates_per_table,
                )
                for table_id, rows in prefetched.items():
                    for _edge, node in rows[: int(fts_counts.get(table_id, 0))]:
                        add_candidate(
                            str(node.get("id") or ""),
                            source="community_fts5",
                        )

            vector = attribute_vectors.get(detail_index)
            if vector is not None and eligible_physical_columns:
                semantic_scores = self.index_store.semantic_segment_scores(
                    "physical",
                    sorted(eligible_physical_columns),
                    vector,
                )
                ranked_semantic = sorted(
                    semantic_scores.items(),
                    key=lambda item: (-float(item[1]), item[0]),
                )[: self.config.attribute_semantic_candidates]
                for node_id, score in ranked_semantic:
                    if float(score) >= self.config.attribute_semantic_min_score:
                        add_candidate(
                            node_id,
                            source="community_persisted_semantic",
                            semantic_score=float(score),
                        )

            for node_id, diagnostics in candidate_scores.items():
                node = self.topology.nodes.get(node_id, {})
                lexical = self._attribute_lexical_relevance(
                    requested_label,
                    canonical,
                    node,
                )
                diagnostics["lexical_details"] = lexical
                diagnostics["lexical_score"] = max(
                    float(diagnostics.get("lexical_score") or 0.0),
                    float(lexical["score"]),
                )
                if lexical["exact_phrase"] or lexical["exact_token_set"]:
                    diagnostics["sources"].add("exact_technical_name")
                elif float(lexical["score"]) >= self.config.attribute_lexical_resolve_min_score:
                    diagnostics["sources"].add("strong_lexical_match")

            def evidence_tier(diagnostics: dict[str, Any]) -> tuple[int, str]:
                sources = set(diagnostics.get("sources") or [])
                if sources & {"curated_attribute_mapping", "qualified_name_mapping"}:
                    return 5, "curated_mapping"
                if sources & {
                    "exact_local_alias",
                    "exact_technical_name",
                    "curated_business_attribute",
                }:
                    return 4, "exact_or_curated_concept"
                if "strong_lexical_match" in sources:
                    return 3, "strong_lexical_match"
                if "community_fts5" in sources:
                    return 2, "fts_candidate"
                if "community_persisted_semantic" in sources:
                    return 1, "semantic_suggestion"
                return 0, "structural_candidate"

            path_rows: list[dict[str, Any]] = []
            endpoint_priority = {
                "physical_column": 0,
                "adf_resource": 1,
                "rest_resource": 1,
                "rest_operation": 1,
                "otbi_subject_area": 1,
                "physical_table": 2,
                "physical_table_stub": 2,
                "business_attribute": 3,
            }
            for node_id, diagnostics in sorted(candidate_scores.items()):
                if not primary_entity_id:
                    continue
                mapping_path = structural_path(node_id)
                if not mapping_path:
                    continue
                endpoint_type = str(self.topology.nodes.get(node_id, {}).get("node_type") or "")
                owner_table_id = None
                selected_candidate_path = mapping_path
                if endpoint_type == "physical_column":
                    selected_candidate_path, owner_table_id = physical_path_for_column(
                        node_id,
                        mapping_path,
                    )
                tier, basis = evidence_tier(diagnostics)
                semantic_score = diagnostics.get("semantic_score")
                lexical_score = float(diagnostics.get("lexical_score") or 0.0)
                grain_kind = table_grain_kind(owner_table_id) if owner_table_id else "unknown"
                header_preference = {"header": 2, "unknown": 1, "line": 0}.get(
                    grain_kind,
                    1,
                )
                path_rows.append(
                    {
                        "node_id": node_id,
                        "path": selected_candidate_path,
                        "owner_table_id": owner_table_id,
                        "endpoint_type": endpoint_type,
                        "endpoint_priority": endpoint_priority.get(endpoint_type, 4),
                        "tier": tier,
                        "basis": basis,
                        "lexical_score": lexical_score,
                        "semantic_score": (
                            float(semantic_score) if semantic_score is not None else None
                        ),
                        "header_preference": header_preference,
                        "path_length": len(selected_candidate_path.get("edges", [])),
                    }
                )
            path_rows.sort(
                key=lambda item: (
                    -int(item["tier"]),
                    int(item["endpoint_priority"]),
                    -int(item["header_preference"]),
                    -float(item["lexical_score"]),
                    -(
                        float(item["semantic_score"])
                        if item["semantic_score"] is not None
                        else -1.0
                    ),
                    int(item["path_length"]),
                    str(item["node_id"]),
                )
            )

            top_row = path_rows[0] if path_rows else None
            promotion_eligible = False
            ambiguous_reason = None
            if top_row is not None:
                top_tier = int(top_row["tier"])
                if top_tier >= 4:
                    promotion_eligible = True
                elif top_tier == 3:
                    comparable = [
                        row
                        for row in path_rows[1:]
                        if int(row["tier"]) == top_tier
                        and int(row["endpoint_priority"])
                        == int(top_row["endpoint_priority"])
                        and int(row["header_preference"])
                        == int(top_row["header_preference"])
                    ]
                    margin = (
                        float(top_row["lexical_score"])
                        - float(comparable[0]["lexical_score"])
                        if comparable
                        else 1.0
                    )
                    promotion_eligible = (
                        float(top_row["lexical_score"])
                        >= self.config.attribute_lexical_resolve_min_score
                        and margin >= self.config.attribute_lexical_ambiguity_margin
                    )
                    if not promotion_eligible:
                        ambiguous_reason = (
                            "Há mais de um candidato lexicalmente plausível sem margem "
                            "suficiente para escolher uma coluna com segurança."
                        )
                elif top_tier == 2:
                    ambiguous_reason = (
                        "O FTS5 apenas pré-selecionou candidatos; não há evidência lexical "
                        "forte ou mapeamento curado para promover uma coluna."
                    )
                elif top_tier == 1:
                    ambiguous_reason = (
                        "A similaridade semântica é usada somente como sugestão e não prova "
                        "que a coluna representa o atributo solicitado."
                    )

            selected_row = top_row if promotion_eligible else None
            selected_raw = selected_row["path"] if selected_row else None
            endpoint_id = str(selected_row["node_id"]) if selected_row else None
            owner_table_id = selected_row["owner_table_id"] if selected_row else None
            selected_path = format_path(selected_raw) if selected_raw else None
            alternative_rows = path_rows[1:3] if selected_row else path_rows[:3]
            alternatives = [format_path(item["path"]) for item in alternative_rows]
            endpoint = self.topology.nodes.get(endpoint_id or "", {})
            endpoint_type = str(endpoint.get("node_type") or "")
            endpoint_name = str(
                endpoint.get("name")
                or endpoint.get("qualified_name")
                or endpoint.get("title")
                or ""
            ).upper()
            identifier_only = (
                endpoint_type == "physical_column"
                and endpoint_name.rsplit(".", 1)[-1].endswith("_ID")
                and not canonical.casefold().endswith("_id")
            )
            if not path_rows:
                status = "unresolved"
            elif not promotion_eligible:
                status = "ambiguous"
            elif endpoint_type in {"business_attribute", "physical_table", "physical_table_stub"}:
                status = "partial"
            elif identifier_only:
                status = "partial"
            elif selected_path and selected_path.get("complete"):
                status = "resolved"
            else:
                status = "partial"
            grain_status, grain_reason = grain_compatibility(selected_raw, owner_table_id)
            evidence = []
            if endpoint_id:
                result = accepted_by_id.get(endpoint_id)
                evidence.append(
                    {
                        "node_id": endpoint_id,
                        "node_type": endpoint_type,
                        "title": endpoint.get("title") or endpoint.get("name") or endpoint_id,
                        "source": (result or {}).get("source") or endpoint.get("source") or {},
                        "role": "identifier_only" if identifier_only else "selected_evidence",
                    }
                )
            candidate_diagnostics = []
            for item in path_rows[:8]:
                node_id = str(item["node_id"])
                scores = candidate_scores[node_id]
                candidate_diagnostics.append(
                    {
                        "node_id": node_id,
                        "sources": sorted(scores["sources"]),
                        "evidence_tier": int(item["tier"]),
                        "selection_basis": str(item["basis"]),
                        "lexical_score": round(float(scores["lexical_score"]), 6),
                        "semantic_score": (
                            round(float(scores["semantic_score"]), 6)
                            if scores.get("semantic_score") is not None
                            else None
                        ),
                        "owner_table_id": item["owner_table_id"],
                        "promotion_eligible": bool(
                            item is top_row and promotion_eligible
                        ),
                        "selected": node_id == endpoint_id,
                    }
                )
            attribute_evidence.append(
                {
                    "attribute": canonical,
                    "requested_label": requested_label,
                    "status": status,
                    "selected_path": selected_path,
                    "alternative_paths": alternatives,
                    "evidence": evidence,
                    "candidate_diagnostics": candidate_diagnostics,
                    "provenance": sorted(
                        {
                            edge["provenance"]
                            for path in ([selected_path] if selected_path else []) + alternatives
                            for edge in path.get("edges", [])
                        },
                        key=lambda value: {"VALIDATED": 0, "EXTRACTED": 1, "INFERRED": 2, "AMBIGUOUS": 3}.get(value, 9),
                    ),
                    "grain_compatibility": grain_status,
                    "grain_explanation": grain_reason,
                    "top_candidate_id": (
                        str(top_row["node_id"]) if top_row is not None else None
                    ),
                    "selection_basis": (
                        str(selected_row["basis"])
                        if selected_row is not None
                        else (str(top_row["basis"]) if top_row is not None else None)
                    ),
                    "resolution_confidence": (
                        "high"
                        if selected_row is not None and int(selected_row["tier"]) >= 5
                        else (
                            "medium"
                            if selected_row is not None and int(selected_row["tier"]) >= 3
                            else ("low" if top_row is not None else None)
                        )
                    ),
                    "resolution_note": (
                        "O caminho termina em um identificador; falta caminho estrutural até o nome descritivo solicitado."
                        if identifier_only
                        else ambiguous_reason
                    ),
                }
            )
        complete_stage("attribute_paths", stage_started)

        payload["query"] = normalized_query
        if query != normalized_query and isinstance(payload.get("context"), str):
            payload["context"] = payload["context"].replace(query, normalized_query)
        payload["query_diagnostics"] = query_diagnostics
        payload["query_plan"] = {
            key: value for key, value in query_plan.items() if key != "encoding"
        }
        payload["entity"] = (
            {
                "node_id": primary_entity_id,
                "entity_id": query_plan.get("primary_entity"),
                "name": query_plan.get("primary_entity_name"),
            }
            if primary_entity_id
            else {}
        )
        payload["community"] = self.topology.compact_community(active_community)
        payload["god_nodes"] = (active_community or {}).get("god_nodes", [])
        payload["attribute_evidence"] = attribute_evidence
        payload["rejected_candidates"] = list(rejected_by_id.values())
        payload["semantic_candidates"] = semantic_candidates
        payload["gaps"] = [
            item["attribute"] for item in attribute_evidence if item["status"] != "resolved"
        ]

        timings["total"] = round(time.perf_counter() - total_started, 4)
        self._emit_progress(f"[SEARCH] total: {timings['total']:.2f}s")
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
            "effective_module_ids": sorted(effective_module_ids or []),
            "master_business_seeds": [row["id"] for row in business],
            "master_routes": accepted_route_diagnostics,
            "discarded_master_routes": discarded_route_diagnostics,
            "semantic_fallback_roots": fallback,
            "semantic_scope": "active_community" if community_id else "global_no_entity",
            "candidate_count": len(candidates) + len(rejected_by_id),
            "structurally_accepted_count": len(accepted_results),
            "active_community_id": community_id,
            "adf_first": adf_diagnostics,
            "rest_operation_diagnostics": self._rest_operation_diagnostics,
            "semantic_inference_diagnostics": self._semantic_inference_diagnostics,
            "embedding_recalculated": False,
            "attribute_query_vector_count": len(attribute_vectors),
            "document_embeddings_recalculated": False,
            "timings_seconds": timings,
        }
        return payload
