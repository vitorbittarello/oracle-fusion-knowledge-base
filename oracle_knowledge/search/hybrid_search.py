from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from oracle_knowledge.common import (
    confidence_to_score,
    merge_text_fields,
    normalize_text,
    read_json,
    tokenize,
)
from oracle_knowledge.search.semantic_context import (
    SemanticTextSelector,
)

SOURCE_BOOSTS = {
    "validated_environment_rule": 1.35,
    "oracle_functional_documentation": 1.20,
    "oracle_otbi_documentation": 1.17,
    "oracle_rest_documentation": 1.12,
    "oracle_data_dictionary": 1.10,
    "curated_entity_map": 1.15,
}

TYPE_BOOSTS = {
    "validated_rule": 1.30,
    "business_entity": 1.18,
    "physical_table": 1.15,
    "physical_column": 1.05,
    "otbi_business_question": 1.12,
    "otbi_subject_area": 1.10,
    "rest_resource": 1.06,
    "rest_operation": 1.03,
    "functional_section": 1.08,
}


@dataclass
class SearchConfig:
    bm25_weight: float = 0.58
    keyword_weight: float = 0.27
    title_weight: float = 0.15
    graph_weight: float = 0.42
    graph_decay: float = 0.68
    graph_hops: int = 2
    seed_limit: int = 12
    k1: float = 1.5
    b: float = 0.75


class HybridSearch:
    def __init__(
            self,
            graph: dict[str, Any],
            config: SearchConfig | None = None,
            *,
            semantic_text_selector: SemanticTextSelector | None = None,
    ):
        self.graph = graph
        self.config = config or SearchConfig()
        self.semantic_text_selector = (
            semantic_text_selector
            or SemanticTextSelector()
        )
        self.nodes = {node["id"]: node for node in graph.get("nodes", [])}
        self.documents: dict[str, list[str]] = {}
        self.term_frequencies: dict[str, Counter[str]] = {}
        self.document_frequency: Counter[str] = Counter()
        self.document_lengths: dict[str, int] = {}
        self.adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._build_index()

    @classmethod
    def from_file(
            cls,
            path: str,
            config: SearchConfig | None = None,
            *,
            semantic_text_selector: SemanticTextSelector | None = None,
    ) -> "HybridSearch":
        return cls(
            read_json(path, {}),
            config=config,
            semantic_text_selector=semantic_text_selector,
        )

    def _build_index(self) -> None:
        for node_id, node in self.nodes.items():
            search_text = node.get("search_text") or merge_text_fields(
                node.get("title"),
                node.get("name"),
                node.get("description"),
                node.get("text"),
                node.get("keywords"),
            )
            tokens = tokenize(search_text)
            self.documents[node_id] = tokens
            frequency = Counter(tokens)
            self.term_frequencies[node_id] = frequency
            self.document_lengths[node_id] = len(tokens)
            for term in frequency:
                self.document_frequency[term] += 1

        for edge in self.graph.get("edges", []):
            source = edge.get("source")
            target = edge.get("target")
            if source in self.nodes and target in self.nodes:
                self.adjacency[source].append((target, edge))
                reverse = dict(edge)
                reverse["source"], reverse["target"] = target, source
                reverse["reverse"] = True
                self.adjacency[target].append((source, reverse))

        lengths = list(self.document_lengths.values())
        self.average_document_length = sum(lengths) / len(lengths) if lengths else 1.0

    @staticmethod
    def _auxiliary_penalty(
            node: dict[str, Any],
            query_tokens: list[str],
            normalized_query: str,
    ) -> float:
        if node.get("node_type") not in {
            "physical_table",
            "physical_column",
        }:
            return 1.0

        object_name = (
                node.get("table_name")
                or node.get("name")
                or node.get("title")
                or ""
        )

        object_name = str(object_name).upper()

        if node.get("node_type") == "physical_column":
            object_name = str(
                node.get("table_name")
                or object_name.split(".", 1)[0]
            ).upper()

        object_parts = {
            part
            for part in object_name.split("_")
            if part
        }

        normalized_object_name = normalize_text(object_name)

        explicit_object_search = bool(
            normalized_object_name
            and normalized_object_name in normalized_query
        )

        auxiliary_query_terms = {
            "archive",
            "archived",
            "arquivo",
            "historico",
            "historica",
            "history",
            "interface",
            "import",
            "importacao",
            "draft",
            "rascunho",
            "temporary",
            "temporaria",
            "temp",
            "staging",
            "stage",
        }

        auxiliary_intent = bool(
            set(query_tokens).intersection(
                auxiliary_query_terms
            )
        )

        if explicit_object_search or auxiliary_intent:
            return 1.0

        if (
                "INTERFACE" in object_parts
                or object_name.endswith("_INT")
                or object_name.endswith("_GT")
                or object_name.endswith("_TMP")
                or object_name.endswith("_TEMP")
                or object_name.endswith("_STG")
        ):
            return 0.35

        if "DRAFT" in object_parts:
            return 0.45

        if (
                "ARCHIVE" in object_parts
                or "HISTORY" in object_parts
                or "HIST" in object_parts
        ):
            return 0.55

        return 1.0

    def search(
            self,
            query: str,
            *,
            limit: int = 20,
            node_types: set[str] | None = None,
            source_types: set[str] | None = None,
            module_ids: set[str] | None = None,
            graph_hops: int | None = None,
    ) -> list[dict[str, Any]]:
        query_tokens = tokenize(query)

        if not query_tokens:
            return []

        normalized_query = normalize_text(query)
        direct_scores: dict[str, dict[str, float]] = {}

        for node_id, node in self.nodes.items():
            if node_types and node.get("node_type") not in node_types:
                continue

            if module_ids and not module_ids.intersection(
                    set(node.get("modules", []))
            ):
                continue

            source_type = node.get("source", {}).get("source_type")

            if source_types and source_type not in source_types:
                continue

            bm25 = self._bm25(node_id, query_tokens)

            keyword = self._keyword_score(
                node,
                query_tokens,
                normalized_query,
            )

            title = self._title_score(
                node,
                query_tokens,
                normalized_query,
            )

            base = (
                    self.config.bm25_weight * bm25
                    + self.config.keyword_weight * keyword
                    + self.config.title_weight * title
            )

            if base <= 0:
                continue

            confidence = max(
                0.35,
                confidence_to_score(
                    node.get(
                        "confidence_score",
                        node.get("confidence"),
                    )
                ),
            )

            source_boost = SOURCE_BOOSTS.get(
                source_type,
                1.0,
            )

            type_boost = TYPE_BOOSTS.get(
                node.get("node_type"),
                1.0,
            )

            auxiliary_penalty = self._auxiliary_penalty(
                node,
                query_tokens,
                normalized_query,
            )

            final_direct = (
                    base
                    * confidence
                    * source_boost
                    * type_boost
                    * auxiliary_penalty
            )

            direct_scores[node_id] = {
                "direct": final_direct,
                "bm25": bm25,
                "keyword": keyword,
                "title": title,
                "confidence": confidence,
                "source_boost": source_boost,
                "type_boost": type_boost,
                "auxiliary_penalty": auxiliary_penalty,
            }

        if not direct_scores:
            return []

        seeds = sorted(
            direct_scores,
            key=lambda node_id: direct_scores[node_id]["direct"],
            reverse=True,
        )[: self.config.seed_limit]

        expansion = self._expand_graph(
            seeds,
            direct_scores,
            hops=(
                self.config.graph_hops
                if graph_hops is None
                else graph_hops
            ),
        )

        candidate_ids = set(direct_scores) | set(expansion)
        results: list[dict[str, Any]] = []

        for node_id in candidate_ids:
            node = self.nodes[node_id]

            if node_types and node.get("node_type") not in node_types:
                continue

            if module_ids and not module_ids.intersection(
                    set(node.get("modules", []))
            ):
                continue

            source_type = node.get("source", {}).get(
                "source_type"
            )

            if source_types and source_type not in source_types:
                continue

            direct = direct_scores.get(
                node_id,
                {},
            ).get(
                "direct",
                0.0,
            )

            graph_score = expansion.get(
                node_id,
                {},
            ).get(
                "score",
                0.0,
            )

            auxiliary_penalty = direct_scores.get(
                node_id,
                {},
            ).get(
                "auxiliary_penalty",
            )

            if auxiliary_penalty is None:
                auxiliary_penalty = self._auxiliary_penalty(
                    node,
                    query_tokens,
                    normalized_query,
                )

            adjusted_graph_score = (
                    graph_score
                    * auxiliary_penalty
            )

            total = (
                    direct
                    + self.config.graph_weight
                    * adjusted_graph_score
            )

            if total <= 0:
                continue

            score_breakdown = dict(
                direct_scores.get(
                    node_id,
                    {},
                )
            )

            score_breakdown.setdefault(
                "auxiliary_penalty",
                auxiliary_penalty,
            )

            score_breakdown[
                "adjusted_graph_score"
            ] = adjusted_graph_score

            results.append(
                {
                    "id": node_id,
                    "score": round(total, 6),
                    "direct_score": round(direct, 6),
                    "graph_score": round(graph_score, 6),
                    "adjusted_graph_score": round(
                        adjusted_graph_score,
                        6,
                    ),
                    "node_type": node.get("node_type"),
                    "title": (
                            node.get("title")
                            or node.get("name")
                            or node_id
                    ),
                    "summary": self._summary(node),
                    "source": node.get("source", {}),
                    "sources": node.get("sources", []),
                    "modules": node.get("modules", []),
                    "keywords": node.get("keywords", []),
                    "score_breakdown": score_breakdown,
                    "graph_paths": expansion.get(
                        node_id,
                        {},
                    ).get(
                        "paths",
                        [],
                    ),
                    "node": node,
                }
            )

        results.sort(
            key=lambda row: (
                normalize_text(row["title"])
                != normalized_query,
                -row["score"],
                row["title"],
            )
        )

        return results[:limit]

    def _bm25(self, node_id: str, query_tokens: list[str]) -> float:
        frequency = self.term_frequencies[node_id]
        document_length = self.document_lengths[node_id]
        document_count = max(1, len(self.documents))
        score = 0.0
        for term in query_tokens:
            term_frequency = frequency.get(term, 0)
            if not term_frequency:
                continue
            df = self.document_frequency.get(term, 0)
            idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            denominator = term_frequency + self.config.k1 * (
                1 - self.config.b
                + self.config.b * document_length / max(self.average_document_length, 1.0)
            )
            score += idf * (term_frequency * (self.config.k1 + 1)) / denominator
        return score

    @staticmethod
    def _keyword_score(node: dict[str, Any], query_tokens: list[str], normalized_query: str) -> float:
        keyword_tokens = set(tokenize(" ".join(str(value) for value in node.get("keywords", []))))
        aliases = [
            node.get("name"),
            node.get("qualified_name"),
            node.get("entity_id"),
            *node.get("aliases", []),
        ]
        alias_texts = [normalize_text(alias) for alias in aliases if alias]
        overlap = sum(1 for token in set(query_tokens) if token in keyword_tokens)
        exact_alias = sum(1 for alias in alias_texts if alias and alias in normalized_query)
        inverse_alias = sum(1 for alias in alias_texts if normalized_query and normalized_query in alias)
        return overlap + 2.5 * exact_alias + 1.2 * inverse_alias

    @staticmethod
    def _title_score(
            node: dict[str, Any],
            query_tokens: list[str],
            normalized_query: str,
    ) -> float:
        title = normalize_text(
            node.get("title")
            or node.get("name")
            or ""
        )

        if not title:
            return 0.0

        if title == normalized_query:
            return 30.0

        qualified_name = normalize_text(
            node.get("qualified_name")
            or ""
        )

        if qualified_name and qualified_name == normalized_query:
            return 30.0

        name = normalize_text(
            node.get("name")
            or ""
        )

        if name and name == normalized_query:
            return 30.0

        if normalized_query and title.startswith(f"{normalized_query}."):
            return 2.5

        if normalized_query and title.startswith(f"{normalized_query} "):
            return 2.5

        if normalized_query in title:
            return 2.0

        if title in normalized_query:
            return 3.0

        title_tokens = set(tokenize(title))
        distinct_query_tokens = set(query_tokens)

        if not title_tokens or not distinct_query_tokens:
            return 0.0

        overlap = len(
            title_tokens.intersection(distinct_query_tokens)
        )

        return 1.5 * overlap / len(distinct_query_tokens)

    def _expand_graph(
        self,
        seeds: list[str],
        direct_scores: dict[str, dict[str, float]],
        *,
        hops: int,
    ) -> dict[str, dict[str, Any]]:
        expansion: dict[str, dict[str, Any]] = {}
        for seed in seeds:
            seed_score = direct_scores[seed]["direct"]
            queue: deque[tuple[str, int, float, list[dict[str, Any]]]] = deque(
                [(seed, 0, seed_score, [])]
            )
            best_seen: dict[tuple[str, int], float] = {(seed, 0): seed_score}
            while queue:
                current, depth, propagated, path = queue.popleft()
                if depth >= hops:
                    continue
                for neighbor, edge in self.adjacency.get(current, []):
                    edge_weight = float(edge.get("weight", 0.6))
                    next_score = propagated * self.config.graph_decay * edge_weight
                    next_depth = depth + 1
                    key = (neighbor, next_depth)
                    if next_score <= best_seen.get(key, 0.0):
                        continue
                    best_seen[key] = next_score
                    edge_path = path + [
                        {
                            "from": current,
                            "to": neighbor,
                            "type": edge.get("type"),
                            "weight": edge_weight,
                            "reverse": edge.get("reverse", False),
                        }
                    ]
                    entry = expansion.setdefault(neighbor, {"score": 0.0, "paths": []})
                    if next_score > entry["score"]:
                        entry["score"] = next_score
                    if len(entry["paths"]) < 3:
                        entry["paths"].append(
                            {
                                "seed": seed,
                                "score": round(next_score, 6),
                                "edges": edge_path,
                            }
                        )
                    queue.append((neighbor, next_depth, next_score, edge_path))
        return expansion

    @staticmethod
    def _context_summary_source(
            node: dict[str, Any],
    ) -> str:
        """
        Retorna somente conteúdo textual próprio do nó.

        Estruturas como business_rules, conditions, listas e dicionários
        permanecem na evidência estruturada e nunca são convertidas para
        texto de resumo. Isso evita que representações Python sejam
        enviadas ao SemanticTextSelector ou apareçam no prompt final.
        """
        textual_fields = (
            "description",
            "text",
            "question",
            "transactional_grain",
            "time_reporting",
            "purpose",
            "usage",
            "details",
            "content",
        )

        values: list[str] = []
        seen: set[str] = set()

        for field_name in textual_fields:
            value = node.get(field_name)

            if not isinstance(value, str):
                continue

            normalized_value = " ".join(value.split())

            if (
                    not normalized_value
                    or normalized_value in seen
            ):
                continue

            seen.add(normalized_value)
            values.append(normalized_value)

        return merge_text_fields(*values)

    @staticmethod
    def _summary(node: dict[str, Any], max_length: int = 500) -> str:
        text = HybridSearch._context_summary_source(node)
        if len(text) <= max_length:
            return text
        return text[: max_length - 1].rstrip() + "…"

    @staticmethod
    def _context_evidence_group(
            block: dict[str, Any],
    ) -> str:
        node_type = str(
            block.get("node_type") or ""
        )

        source_type = str(
            block.get(
                "source",
                {},
            ).get(
                "source_type"
            )
            or ""
        )

        if (
                node_type == "validated_rule"
                or source_type
                == "validated_environment_rule"
        ):
            return "validated_rules"

        if node_type == "physical_column":
            return "physical_columns"

        if node_type == "physical_table":
            return "physical_tables"

        if node_type in {
            "business_entity",
            "business_attribute",
        }:
            return "business_context"

        if (
                node_type.startswith("otbi_")
                or source_type
                == "oracle_otbi_documentation"
        ):
            return "otbi"

        if (
                node_type.startswith("rest_")
                or source_type
                == "oracle_rest_documentation"
        ):
            return "rest"

        if (
                node_type == "functional_section"
                or source_type
                == "oracle_functional_documentation"
        ):
            return "functional_documentation"

        return "exploratory"

    @staticmethod
    def _public_context_block(
            block: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Remove metadados internos usados apenas durante a montagem do
        contexto.

        Chaves iniciadas por ``_`` nunca são expostas no JSON final nem
        consideradas como evidência para a LLM.
        """
        return {
            key: value
            for key, value in block.items()
            if not str(key).startswith("_")
        }

    @staticmethod
    def _distribute_character_budget(
            items: list[dict[str, Any]],
            available: int,
            *,
            capacity_key: str,
            allocation_key: str,
    ) -> int:
        """
        Distribui caracteres proporcionalmente ao score dos itens.

        A distribuição respeita a capacidade individual informada e
        devolve a quantidade que não pôde ser utilizada. O método não
        altera ranking, score ou conteúdo; apenas incrementa a alocação
        numérica usada posteriormente pelo seletor semântico.
        """
        remaining = max(0, int(available))
        active = [
            item
            for item in items
            if int(item.get(capacity_key, 0)) > 0
        ]

        while remaining > 0 and active:
            active = [
                item
                for item in active
                if int(item.get(capacity_key, 0)) > 0
            ]

            if not active:
                break

            total_weight = sum(
                max(float(item.get("score") or 0.0), 0.000001)
                for item in active
            )
            round_budget = remaining
            increments: list[tuple[dict[str, Any], int]] = []
            allocated_in_round = 0

            for item in active:
                capacity = int(item.get(capacity_key, 0))
                weight = max(
                    float(item.get("score") or 0.0),
                    0.000001,
                )
                share = int(
                    round_budget * weight / total_weight
                )
                increment = min(share, capacity)

                if increment <= 0:
                    continue

                increments.append((item, increment))
                allocated_in_round += increment

            if allocated_in_round <= 0:
                item = max(
                    active,
                    key=lambda candidate: (
                        float(candidate.get("score") or 0.0),
                        int(candidate.get(capacity_key, 0)),
                    ),
                )
                increments = [(item, 1)]
                allocated_in_round = 1

            for item, increment in increments:
                item[allocation_key] = int(
                    item.get(allocation_key, 0)
                ) + increment
                item[capacity_key] = int(
                    item.get(capacity_key, 0)
                ) - increment

            remaining -= allocated_in_round

        return remaining

    @classmethod
    def _diversify_selection(
            cls,
            blocks: list[dict[str, Any]],
            *,
            max_items: int,
            max_characters: int,
            equal_budget_fraction: float = 0.40,
            min_relative_score_ratio: float = 0.05,
            min_group_score_ratio: float = 0.10,
            minimum_summary_characters: int = 96,
            maximum_summary_characters: int = 500,
    ) -> list[dict[str, Any]]:
        """
        Distribui o orçamento do contexto entre categorias relevantes.

        O orçamento é calculado em três partes:

        1. custo estrutural mínimo do melhor item de cada grupo;
        2. parcela igualitária entre os grupos representados;
        3. parcela proporcional ao maior score de cada grupo.

        Depois da representação mínima, candidatos adicionais competem
        dentro do orçamento do próprio grupo pela ordem global do
        ranking. Sobras são redistribuídas globalmente pelo mesmo
        ranking. O texto ainda não é resumido aqui; cada item recebe um
        orçamento dinâmico que será consumido por SemanticTextSelector
        em build_prompt_context().

        O método preserva a ordem relativa original dos resultados e
        nunca força categorias cujo melhor score esteja muito distante
        do melhor grupo recuperado.
        """
        if (
                not blocks
                or max_items <= 0
                or max_characters <= 0
        ):
            return []

        group_priority = (
            "validated_rules",
            "physical_columns",
            "physical_tables",
            "business_context",
            "otbi",
            "rest",
            "functional_documentation",
        )
        priority_index = {
            group: index
            for index, group in enumerate(group_priority)
        }

        equal_budget_fraction = min(
            max(float(equal_budget_fraction), 0.0),
            1.0,
        )
        minimum_summary_characters = max(
            0,
            int(minimum_summary_characters),
        )
        maximum_summary_characters = max(
            0,
            int(maximum_summary_characters),
        )

        prepared: list[dict[str, Any]] = []

        for rank_index, block in enumerate(blocks):
            public_block = cls._public_context_block(block)
            public_block["summary"] = ""
            summary_source = " ".join(
                str(
                    block.get("_summary_source")
                    or block.get("summary")
                    or ""
                ).split()
            )
            fixed_size = len(
                json.dumps(
                    public_block,
                    ensure_ascii=False,
                )
            )
            summary_cap = min(
                len(summary_source),
                maximum_summary_characters,
            )

            prepared.append(
                {
                    "rank_index": rank_index,
                    "block": block,
                    "group": cls._context_evidence_group(block),
                    "score": float(block.get("score") or 0.0),
                    "fixed_size": fixed_size,
                    "summary_cap": summary_cap,
                    "summary_allocation": 0,
                    "summary_capacity": summary_cap,
                }
            )

        top_score = max(
            (item["score"] for item in prepared),
            default=0.0,
        )
        relevance_floor = top_score * max(
            0.0,
            float(min_relative_score_ratio),
        )
        relevant = [
            item
            for item in prepared
            if item["score"] >= relevance_floor
        ]

        if not relevant:
            return []

        candidates_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in relevant:
            candidates_by_group[item["group"]].append(item)

        group_best_score = {
            group: max(item["score"] for item in items)
            for group, items in candidates_by_group.items()
            if group != "exploratory"
        }
        best_group_score = max(
            group_best_score.values(),
            default=0.0,
        )
        group_floor = best_group_score * max(
            0.0,
            float(min_group_score_ratio),
        )

        eligible_groups = [
            group
            for group, score in group_best_score.items()
            if (
                score >= group_floor
                or group == "validated_rules"
            )
        ]
        eligible_groups.sort(
            key=lambda group: (
                -group_best_score[group],
                priority_index.get(group, len(group_priority)),
            )
        )
        eligible_groups = eligible_groups[:max_items]

        selected_groups: list[str] = []
        minimum_group_cost: dict[str, int] = {}
        minimum_used = 0

        for group in eligible_groups:
            top_item = candidates_by_group[group][0]
            fixed_size = int(top_item["fixed_size"])

            if minimum_used + fixed_size > max_characters:
                continue

            selected_groups.append(group)
            minimum_group_cost[group] = fixed_size
            minimum_used += fixed_size

        if not selected_groups:
            fallback = relevant[0]
            if fallback["fixed_size"] > max_characters:
                return []
            selected_groups = [fallback["group"]]
            minimum_group_cost[fallback["group"]] = fallback["fixed_size"]
            minimum_used = fallback["fixed_size"]

        remaining_for_groups = max_characters - minimum_used
        equal_pool = int(
            remaining_for_groups * equal_budget_fraction
        )
        proportional_pool = remaining_for_groups - equal_pool
        group_budgets = dict(minimum_group_cost)

        equal_share, equal_remainder = divmod(
            equal_pool,
            len(selected_groups),
        )
        for index, group in enumerate(selected_groups):
            group_budgets[group] += equal_share
            if index < equal_remainder:
                group_budgets[group] += 1

        score_sum = sum(
            max(group_best_score.get(group, 0.0), 0.000001)
            for group in selected_groups
        )
        proportional_allocated = 0

        for group in selected_groups:
            weight = max(
                group_best_score.get(group, 0.0),
                0.000001,
            )
            share = int(
                proportional_pool * weight / score_sum
            )
            group_budgets[group] += share
            proportional_allocated += share

        unallocated = proportional_pool - proportional_allocated
        for group in selected_groups:
            if unallocated <= 0:
                break
            group_budgets[group] += 1
            unallocated -= 1

        selected_items: list[dict[str, Any]] = []
        selected_ranks: set[int] = set()
        group_used: Counter[str] = Counter()

        def add_item(
                item: dict[str, Any],
                *,
                summary_characters: int,
        ) -> bool:
            if (
                    len(selected_items) >= max_items
                    or item["rank_index"] in selected_ranks
            ):
                return False

            summary_characters = min(
                max(0, int(summary_characters)),
                int(item["summary_cap"]),
            )
            item["summary_allocation"] = summary_characters
            item["summary_capacity"] = (
                int(item["summary_cap"])
                - summary_characters
            )
            selected_items.append(item)
            selected_ranks.add(item["rank_index"])
            group_used[item["group"]] += (
                int(item["fixed_size"])
                + summary_characters
            )
            return True

        # Um representante estrutural por grupo relevante.
        for group in selected_groups:
            item = candidates_by_group[group][0]
            available = max(
                0,
                group_budgets[group] - int(item["fixed_size"]),
            )
            initial_summary = min(
                int(item["summary_cap"]),
                minimum_summary_characters,
                available,
            )
            add_item(
                item,
                summary_characters=initial_summary,
            )

        # Candidatos adicionais disputam apenas o orçamento do seu grupo
        # e seguem exatamente a ordem global do ranking.
        for item in relevant:
            if len(selected_items) >= max_items:
                break

            group = item["group"]
            if (
                    group not in group_budgets
                    or item["rank_index"] in selected_ranks
            ):
                continue

            minimum_summary = min(
                int(item["summary_cap"]),
                minimum_summary_characters,
            )
            required = int(item["fixed_size"]) + minimum_summary
            available = group_budgets[group] - group_used[group]

            if required > available:
                continue

            add_item(
                item,
                summary_characters=minimum_summary,
            )

        # Usa o saldo de cada categoria para enriquecer semanticamente
        # os itens já selecionados daquele grupo.
        for group in selected_groups:
            group_items = [
                item
                for item in selected_items
                if item["group"] == group
            ]
            available = max(
                0,
                group_budgets[group] - group_used[group],
            )
            unused = cls._distribute_character_budget(
                group_items,
                available,
                capacity_key="summary_capacity",
                allocation_key="summary_allocation",
            )
            group_used[group] += available - unused

        used = sum(
            int(item["fixed_size"])
            + int(item["summary_allocation"])
            for item in selected_items
        )
        remaining_global = max(0, max_characters - used)

        # Redistribuição global por ranking: primeiro enriquece ou inclui
        # as evidências mais bem pontuadas; somente depois alcança itens
        # exploratórios ou de menor score.
        for item in relevant:
            if remaining_global <= 0:
                break

            if item["rank_index"] in selected_ranks:
                capacity = int(item["summary_capacity"])
                increment = min(capacity, remaining_global)
                item["summary_allocation"] += increment
                item["summary_capacity"] -= increment
                remaining_global -= increment
                continue

            if len(selected_items) >= max_items:
                continue

            minimum_summary = min(
                int(item["summary_cap"]),
                minimum_summary_characters,
            )
            required = int(item["fixed_size"]) + minimum_summary

            if required > remaining_global:
                continue

            add_item(
                item,
                summary_characters=minimum_summary,
            )
            remaining_global -= required

        if remaining_global > 0:
            remaining_global = cls._distribute_character_budget(
                selected_items,
                remaining_global,
                capacity_key="summary_capacity",
                allocation_key="summary_allocation",
            )

        selected_items.sort(
            key=lambda item: item["rank_index"]
        )

        selected_blocks: list[dict[str, Any]] = []
        for item in selected_items:
            block = dict(item["block"])
            block["_allocated_characters"] = (
                int(item["fixed_size"])
                + int(item["summary_allocation"])
            )
            block["_summary_max_characters"] = int(
                item["summary_allocation"]
            )
            selected_blocks.append(block)

        return selected_blocks

    @staticmethod
    def _physical_table_context_evidence(
            node: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Gera uma evidência compacta para tabelas físicas.

        O contexto completo das tabelas pode conter dezenas de regras
        técnicas, flags, auditoria e bloqueio otimista. Esse volume
        tende a consumir uma parcela desproporcional do orçamento do
        prompt.

        Para geração de SQL, são preservados os elementos com maior
        valor estrutural:

        - chave primária;
        - grão documentado;
        - relacionamentos referenciais úteis para joins.

        A descrição funcional da tabela já permanece disponível no
        campo summary do bloco. Portanto, o documented_business_context
        não é repetido dentro da evidência estruturada.

        As regras completas continuam armazenadas no grafo e não são
        alteradas ou removidas da base.
        """
        relationships: list[dict[str, Any]] = []

        for rule in node.get(
                "business_rules",
                [],
        ):
            if (
                    rule.get("rule_type")
                    != "referential_integrity"
            ):
                continue

            relationship = {
                "columns": rule.get(
                    "columns",
                    [],
                ),
                "referenced_table": rule.get(
                    "referenced_table"
                ),
                "referenced_column": rule.get(
                    "referenced_column"
                ),
                "confidence": rule.get(
                    "confidence"
                ),
            }

            compact_relationship = {
                key: value
                for key, value
                in relationship.items()
                if value not in (
                    None,
                    "",
                    [],
                    {},
                )
            }

            if compact_relationship:
                relationships.append(
                    compact_relationship
                )

            if len(relationships) >= 6:
                break

        payload = {
            "primary_key": node.get(
                "primary_key"
            ),
            "result_grain": node.get(
                "result_grain"
            ),
            "relationships": relationships,
        }

        return {
            key: value
            for key, value
            in payload.items()
            if value not in (
                None,
                "",
                [],
                {},
            )
        }

    def build_prompt_context(
            self,
            query: str,
            *,
            limit: int = 16,
            max_characters: int = 14000,
            module_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """
        Monta o contexto da LLM a partir dos resultados já ranqueados.

        A busca recupera mais candidatos do que o limite final. Em
        seguida, o orçamento é distribuído entre as categorias
        relevantes e cada item recebe um limite textual próprio.
        SemanticTextSelector só é executado depois dessa distribuição,
        usando o espaço efetivamente disponível para o item.

        Busca, score, expansão do grafo e ranking permanecem
        inalterados. O parâmetro limit continua representando a
        quantidade máxima de evidências no contexto final.
        """
        candidate_limit = max(
            limit,
            limit * 3,
            limit + 12,
        )

        results = self.search(
            query,
            limit=candidate_limit,
            module_ids=module_ids,
        )

        blocks: list[dict[str, Any]] = []

        for result in results:
            node = result["node"]
            summary_source = (
                self._context_summary_source(node)
                or result["summary"]
            )

            if result["node_type"] == "physical_table":
                evidence = self._physical_table_context_evidence(node)
            else:
                evidence = self._evidence_payload(node)

            blocks.append(
                {
                    "id": result["id"],
                    "node_type": result["node_type"],
                    "title": result["title"],
                    "score": result["score"],
                    "summary": "",
                    "source": result["source"],
                    "sources": result.get("sources", []),
                    "modules": result.get("modules", []),
                    "evidence": evidence,
                    "_summary_source": summary_source,
                }
            )

        selected_with_budget = self._diversify_selection(
            blocks,
            max_items=limit,
            max_characters=max_characters,
            maximum_summary_characters=(
                self.semantic_text_selector.config
                .summary_max_characters
            ),
        )

        selected: list[dict[str, Any]] = []

        for budgeted_block in selected_with_budget:
            summary_source = str(
                budgeted_block.get("_summary_source")
                or ""
            )
            allocated_characters = int(
                budgeted_block.get("_allocated_characters")
                or 0
            )
            allocated_summary = int(
                budgeted_block.get("_summary_max_characters")
                or 0
            )

            public_block = self._public_context_block(
                budgeted_block
            )
            public_block["summary"] = ""
            fixed_size = len(
                json.dumps(
                    public_block,
                    ensure_ascii=False,
                )
            )
            available_summary = min(
                allocated_summary,
                max(0, allocated_characters - fixed_size),
            )

            semantic_summary = (
                self.semantic_text_selector.select_relevant_text(
                    query,
                    summary_source,
                    max_characters=available_summary,
                )
            )

            while semantic_summary:
                encoded_size = len(
                    json.dumps(
                        semantic_summary,
                        ensure_ascii=False,
                    )
                ) - 2

                if encoded_size <= available_summary:
                    break

                overflow = encoded_size - available_summary
                new_length = max(
                    0,
                    len(semantic_summary) - max(1, overflow),
                )
                semantic_summary = semantic_summary[:new_length].rstrip()

            public_block["summary"] = semantic_summary
            selected.append(public_block)

        used = sum(
            len(
                json.dumps(
                    block,
                    ensure_ascii=False,
                )
            )
            for block in selected
        )

        context_lines = [
            "OBJETIVO",
            query,
            "",
            "EVIDÊNCIAS RECUPERADAS",
        ]

        # A apresentação segue a ordem original do ranking.
        # A categoria é informada em cada linha, sem reagrupar ou
        # reposicionar os resultados por tipo ou fonte.
        for index, row in enumerate(
                selected,
                start=1,
        ):
            evidence_group = self._context_evidence_group(row)
            source_type = row.get(
                "source",
                {},
            ).get(
                "source_type",
                "unknown",
            )

            context_lines.append(
                "\n"
                f"{index}. {row['title']} "
                f"| grupo={evidence_group} "
                f"| fonte={source_type} "
                f"| tipo={row['node_type']} "
                f"| módulos="
                f"{','.join(row.get('modules', [])) or 'n/a'} "
                f"| score={row['score']}"
            )

            if row["summary"]:
                context_lines.append(
                    f"   {row['summary']}"
                )

            if row["evidence"]:
                context_lines.append(
                    "   Evidência estruturada: "
                    + json.dumps(
                        row["evidence"],
                        ensure_ascii=False,
                    )
                )

            url = row.get(
                "source",
                {},
            ).get("url")

            if url:
                context_lines.append(
                    f"   Fonte: {url}"
                )

        context_lines.extend(
            [
                "",
                "REGRAS DE RESPOSTA",
                "- Priorize regras validadas no ambiente sobre inferências automáticas.",
                "- Não invente filtros, joins, significados de códigos ou granularidade.",
                "- Diferencie fatos documentados de inferências.",
                "- Respeite o grão indicado e alerte sobre risco de duplicidade.",
                "- OBJECT_VERSION_NUMBER é controle otimista, salvo evidência funcional em contrário.",
            ]
        )

        return {
            "query": query,
            "context": "\n".join(context_lines),
            "results": selected,
            "characters": used,
        }

    @staticmethod
    def _evidence_payload(node: dict[str, Any]) -> dict[str, Any]:
        allowed = [
            "entity_id",
            "aliases",
            "primary_key",
            "result_grain",
            "ranking_rules",
            "business_rules",
            "conditions",
            "ranking",
            "tables",
            "columns",
            "sql_template",
            "transactional_grain",
            "time_reporting",
            "subject_areas",
            "endpoint_path",
            "method",
            "resource_hierarchy",
            "parameters",
            "attributes",
            "semantics",
            "qualified_name",
            "module_id",
            "module_name",
            "modules",
        ]
        return {key: node[key] for key in allowed if node.get(key) not in (None, [], {}, "")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Busca híbrida na base de conhecimento Oracle.")
    parser.add_argument("--graph", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--graph-hops", type=int, default=2)
    parser.add_argument("--context", action="store_true")
    parser.add_argument("--max-characters", type=int, default=14000)
    parser.add_argument("--module", action="append", default=[])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    search = HybridSearch.from_file(
        args.graph,
        config=SearchConfig(graph_hops=args.graph_hops),
    )
    if args.context:
        payload = search.build_prompt_context(
            args.query,
            limit=args.limit,
            max_characters=args.max_characters,
            module_ids=set(args.module) if args.module else None,
        )
    else:
        payload = search.search(
            args.query,
            limit=args.limit,
            module_ids=set(args.module) if args.module else None,
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
