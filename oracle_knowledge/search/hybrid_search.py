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
    def __init__(self, graph: dict[str, Any], config: SearchConfig | None = None):
        self.graph = graph
        self.config = config or SearchConfig()
        self.nodes = {node["id"]: node for node in graph.get("nodes", [])}
        self.documents: dict[str, list[str]] = {}
        self.term_frequencies: dict[str, Counter[str]] = {}
        self.document_frequency: Counter[str] = Counter()
        self.document_lengths: dict[str, int] = {}
        self.adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._build_index()

    @classmethod
    def from_file(cls, path: str, config: SearchConfig | None = None) -> "HybridSearch":
        return cls(read_json(path, {}), config=config)

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
    def _summary(node: dict[str, Any], max_length: int = 500) -> str:
        text = merge_text_fields(
            node.get("description"),
            node.get("text"),
            node.get("question"),
            node.get("transactional_grain"),
            node.get("business_rules"),
            node.get("conditions"),
        )
        if len(text) <= max_length:
            return text
        return text[: max_length - 1].rstrip() + "…"

    def build_prompt_context(
        self,
        query: str,
        *,
        limit: int = 16,
        max_characters: int = 14000,
        module_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        results = self.search(query, limit=limit, module_ids=module_ids)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        used = 0
        selected: list[dict[str, Any]] = []
        for result in results:
            node = result["node"]
            source_type = node.get("source", {}).get("source_type", "unknown")
            block = {
                "id": result["id"],
                "node_type": result["node_type"],
                "title": result["title"],
                "score": result["score"],
                "summary": result["summary"],
                "source": result["source"],
                "sources": result.get("sources", []),
                "modules": result.get("modules", []),
                "evidence": self._evidence_payload(node),
            }
            estimated = len(json.dumps(block, ensure_ascii=False))
            if selected and used + estimated > max_characters:
                break
            used += estimated
            selected.append(block)
            groups[source_type].append(block)

        priority = [
            "validated_environment_rule",
            "oracle_functional_documentation",
            "oracle_otbi_documentation",
            "oracle_rest_documentation",
            "oracle_data_dictionary",
            "curated_entity_map",
            "unknown",
        ]
        context_lines = [
            "OBJETIVO",
            query,
            "",
            "EVIDÊNCIAS RECUPERADAS",
        ]
        for source_type in priority:
            rows = groups.get(source_type, [])
            if not rows:
                continue
            context_lines.append(f"\n[{source_type}]")
            for index, row in enumerate(rows, start=1):
                context_lines.append(
                    f"{index}. {row['title']} | tipo={row['node_type']} | módulos={','.join(row.get('modules', [])) or 'n/a'} | score={row['score']}"
                )
                if row["summary"]:
                    context_lines.append(f"   {row['summary']}")
                if row["evidence"]:
                    context_lines.append(
                        "   Evidência estruturada: "
                        + json.dumps(row["evidence"], ensure_ascii=False)
                    )
                url = row.get("source", {}).get("url")
                if url:
                    context_lines.append(f"   Fonte: {url}")

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
