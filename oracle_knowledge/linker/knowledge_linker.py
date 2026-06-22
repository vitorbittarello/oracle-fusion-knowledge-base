from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from oracle_knowledge.common import (
    confidence_to_score,
    extract_keywords,
    merge_text_fields,
    normalize_text,
    read_json,
    read_jsonl,
    stable_id,
    utc_now_iso,
    write_json,
)

EDGE_WEIGHTS = {
    "contains_column": 1.0,
    "foreign_key_to": 1.0,
    "incoming_foreign_key_from": 0.9,
    "uses_table": 1.0,
    "uses_column": 1.0,
    "answered_by": 1.0,
    "has_operation": 0.95,
    "parent_of": 0.85,
    "mapped_to_entity": 1.0,
    "mentions_table": 0.8,
    "mentions_entity": 0.7,
    "related_by_alias": 0.75,
}


def _path_list(value: str | Path | Iterable[str | Path] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    result: list[str] = []
    for item in value:
        if item:
            result.append(str(item))
    return result


def _unique_objects(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


class GraphBuilder:
    def __init__(self):
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.table_by_name: dict[str, str] = {}
        self.column_by_qualified_name: dict[str, str] = {}
        self.subject_area_by_name: dict[str, list[str]] = defaultdict(list)
        self.rest_resource_by_name: dict[str, list[str]] = defaultdict(list)
        self.entity_by_id: dict[str, str] = {}
        self.pending_relationships: list[tuple[str, dict[str, Any]]] = []
        self.loaded_sources: list[dict[str, Any]] = []

    def add_node(self, node: dict[str, Any]) -> str:
        node = dict(node)
        node_id = node["id"]
        node.setdefault(
            "keywords",
            extract_keywords(node.get("search_text") or node.get("title") or ""),
        )
        node.setdefault("confidence_score", confidence_to_score(node.get("confidence")))
        node.setdefault(
            "search_text",
            merge_text_fields(
                node.get("title"),
                node.get("text"),
                node.get("description"),
                node.get("keywords"),
            ),
        )

        source = node.get("source") or {}
        sources = list(node.get("sources") or [])
        if source:
            sources.append(source)
        node["sources"] = _unique_objects(sources)

        modules = list(node.get("modules") or [])
        module_id = node.get("module_id") or source.get("module_id")
        if module_id:
            modules.append(module_id)
        node["modules"] = list(dict.fromkeys(str(value) for value in modules if value))

        existing = self.nodes.get(node_id)
        if existing is None:
            self.nodes[node_id] = node
            return node_id

        merged = dict(existing)
        for key, value in node.items():
            if value in (None, "", [], {}):
                continue
            if key in {"sources", "business_rules", "ranking_rules", "keywords", "aliases", "modules"}:
                old_values = merged.get(key) or []
                if not isinstance(old_values, list):
                    old_values = [old_values]
                new_values = value if isinstance(value, list) else [value]
                merged[key] = _unique_objects([*old_values, *new_values])
            elif key == "search_text":
                merged[key] = merge_text_fields(merged.get(key), value)
            elif key == "confidence_score":
                merged[key] = max(float(merged.get(key, 0.0)), float(value))
            elif key not in merged or merged.get(key) in (None, "", [], {}):
                merged[key] = value

        merged["sources"] = _unique_objects(
            [*(existing.get("sources") or []), *(node.get("sources") or [])]
        )
        merged["modules"] = list(
            dict.fromkeys([*(existing.get("modules") or []), *(node.get("modules") or [])])
        )
        self.nodes[node_id] = merged
        return node_id

    def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        *,
        weight: float | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        if source not in self.nodes or target not in self.nodes:
            return
        key = (source, target, edge_type)
        self.edges[key] = {
            "source": source,
            "target": target,
            "type": edge_type,
            "weight": weight if weight is not None else EDGE_WEIGHTS.get(edge_type, 0.6),
            "evidence": evidence or {},
        }

    def load_physical_manifest(self, path: str | None) -> None:
        if not path:
            return
        payload = read_json(path, {})
        metadata = payload.get("metadata", {})
        default_module_id = metadata.get("module_id")
        default_module_name = metadata.get("module_name") or metadata.get("module")
        release = metadata.get("release_version")
        self.loaded_sources.append(
            {
                "kind": "physical",
                "path": str(path),
                "module_id": default_module_id,
                "module_name": default_module_name,
            }
        )

        for catalog in payload.get("skills_catalog", []):
            module_id = catalog.get("module_id") or default_module_id
            module_name = catalog.get("module_name") or default_module_name
            sub_module = catalog.get("sub_module") or module_name
            for component in catalog.get("components", []):
                table_name = (component.get("table_name") or "").upper()
                if not table_name:
                    continue
                table_id = stable_id("table", table_name)
                source = {
                    "source_type": "oracle_data_dictionary",
                    "module_id": module_id,
                    "module_name": module_name,
                    "url": component.get("source_url"),
                    "manifest_path": str(path),
                    "release": release,
                }
                search_text = merge_text_fields(
                    table_name,
                    component.get("description"),
                    component.get("primary_key"),
                    component.get("column_semantics"),
                    component.get("business_rules"),
                    component.get("result_grain"),
                    component.get("ranking_rules"),
                    sub_module,
                    module_name,
                )
                self.add_node(
                    {
                        "id": table_id,
                        "node_type": "physical_table",
                        "name": table_name,
                        "title": table_name,
                        "description": component.get("description"),
                        "primary_key": component.get("primary_key", []),
                        "result_grain": component.get("result_grain"),
                        "ranking_rules": component.get("ranking_rules", []),
                        "business_rules": component.get("business_rules", []),
                        "sub_module": sub_module,
                        "module_id": module_id,
                        "module_name": module_name,
                        "source": source,
                        "confidence": "high",
                        "search_text": search_text,
                    }
                )
                self.table_by_name[table_name] = table_id

                metadata_by_name = {
                    (column.get("name") or "").upper(): column
                    for column in component.get("columns", [])
                    if column.get("name")
                }
                semantics_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for semantic in component.get("column_semantics", []):
                    semantics_by_name[(semantic.get("column") or "").upper()].append(semantic)

                for column_name in component.get("fields_to_extract", []):
                    column_name = str(column_name).upper()
                    column_metadata = metadata_by_name.get(column_name, {})
                    semantics = semantics_by_name.get(column_name, [])
                    qualified_name = f"{table_name}.{column_name}"
                    column_id = stable_id("column", qualified_name)
                    self.add_node(
                        {
                            "id": column_id,
                            "node_type": "physical_column",
                            "name": column_name,
                            "qualified_name": qualified_name,
                            "title": qualified_name,
                            "table_name": table_name,
                            "module_id": module_id,
                            "module_name": module_name,
                            "datatype": column_metadata.get("datatype"),
                            "nullable": column_metadata.get("nullable"),
                            "description": column_metadata.get("description"),
                            "semantics": semantics,
                            "source": source,
                            "confidence": "high",
                            "search_text": merge_text_fields(
                                qualified_name,
                                column_metadata,
                                semantics,
                                module_name,
                            ),
                        }
                    )
                    self.column_by_qualified_name[qualified_name] = column_id
                    self.add_edge(table_id, column_id, "contains_column")

                for relation in component.get("relationships", {}).get("outgoing", []):
                    self.pending_relationships.append((table_name, dict(relation)))

    def resolve_physical_relationships(self) -> None:
        for source_name, relation in self.pending_relationships:
            source_id = self.table_by_name.get(source_name.upper())
            target_name = (relation.get("target_table") or "").upper()
            target_id = self.table_by_name.get(target_name)
            if source_id and target_id:
                self.add_edge(source_id, target_id, "foreign_key_to", evidence=relation)
                self.add_edge(
                    target_id,
                    source_id,
                    "incoming_foreign_key_from",
                    evidence=relation,
                )

    def load_functional(self, path: str | None) -> None:
        if not path:
            return
        self.loaded_sources.append({"kind": "functional", "path": str(path)})
        for row in read_jsonl(path):
            if row.get("node_type") == "collection_error":
                continue
            node_id = self.add_node(dict(row))
            for table_name in row.get("table_mentions", []):
                table_id = self.table_by_name.get(table_name.upper())
                if table_id:
                    self.add_edge(
                        node_id,
                        table_id,
                        "mentions_table",
                        evidence={"table_name": table_name},
                    )

    def _matching_ids(
        self,
        index: dict[str, list[str]],
        name: str | None,
        module_id: str | None,
    ) -> list[str]:
        candidates = index.get(normalize_text(name), [])
        if not module_id:
            return list(candidates)
        same_module = [
            node_id
            for node_id in candidates
            if module_id in (self.nodes.get(node_id, {}).get("modules") or [])
        ]
        return same_module or list(candidates)

    def load_otbi(self, path: str | None) -> None:
        if not path:
            return
        self.loaded_sources.append({"kind": "otbi", "path": str(path)})
        payload = read_json(path, {})
        for subject_area in payload.get("subject_areas", []):
            node_id = self.add_node(dict(subject_area))
            key = normalize_text(subject_area.get("name"))
            if node_id not in self.subject_area_by_name[key]:
                self.subject_area_by_name[key].append(node_id)
        for question in payload.get("business_questions", []):
            question_id = self.add_node(dict(question))
            module_id = question.get("module_id") or question.get("source", {}).get("module_id")
            for name in question.get("subject_areas", []):
                for subject_id in self._matching_ids(
                    self.subject_area_by_name,
                    name,
                    module_id,
                ):
                    self.add_edge(question_id, subject_id, "answered_by")
        for page in payload.get("other_pages", []):
            self.add_node(dict(page))

    def load_rest(self, path: str | None) -> None:
        if not path:
            return
        self.loaded_sources.append({"kind": "rest", "path": str(path)})
        payload = read_json(path, {})
        for resource in payload.get("resources", []):
            node_id = self.add_node(dict(resource))
            key = normalize_text(resource.get("name"))
            if node_id not in self.rest_resource_by_name[key]:
                self.rest_resource_by_name[key].append(node_id)
        for operation in payload.get("operations", []):
            operation_id = self.add_node(dict(operation))
            module_id = operation.get("module_id") or operation.get("source", {}).get("module_id")
            for resource_id in self._matching_ids(
                self.rest_resource_by_name,
                operation.get("resource_name"),
                module_id,
            ):
                self.add_edge(resource_id, operation_id, "has_operation")
        for resource in payload.get("resources", []):
            parent_name = resource.get("parent_resource")
            if not parent_name:
                continue
            module_id = resource.get("module_id") or resource.get("source", {}).get("module_id")
            parent_ids = self._matching_ids(self.rest_resource_by_name, parent_name, module_id)
            child_ids = self._matching_ids(
                self.rest_resource_by_name,
                resource.get("name"),
                module_id,
            )
            for parent_id in parent_ids:
                for child_id in child_ids:
                    self.add_edge(parent_id, child_id, "parent_of")

    def load_rules(self, path: str | None) -> None:
        if not path:
            return
        self.loaded_sources.append({"kind": "rules", "path": str(path)})
        payload = read_json(path, {})
        for rule in payload.get("rules", []):
            rule_id = rule.get("id") or stable_id("validated_rule", rule.get("name", "rule"))
            node = {
                **rule,
                "id": rule_id,
                "node_type": "validated_rule",
                "title": rule.get("name") or rule_id,
                "source": {
                    "source_type": "validated_environment_rule",
                    "rules_path": str(path),
                    **rule.get("source", {}),
                },
                "confidence": rule.get("confidence", "very_high"),
                "search_text": merge_text_fields(
                    rule.get("name"),
                    rule.get("description"),
                    rule.get("business_entity"),
                    rule.get("conditions"),
                    rule.get("ranking"),
                    rule.get("tables"),
                    rule.get("columns"),
                    rule.get("sql_template"),
                ),
            }
            self.add_node(node)
            for table_name in rule.get("tables", []):
                table_id = self.table_by_name.get(str(table_name).upper())
                if table_id:
                    self.add_edge(rule_id, table_id, "uses_table")
            for qualified_name in rule.get("columns", []):
                column_id = self.column_by_qualified_name.get(str(qualified_name).upper())
                if column_id:
                    self.add_edge(rule_id, column_id, "uses_column")

    def load_entities(self, path: str | None) -> None:
        if not path:
            return

        self.loaded_sources.append(
            {
                "kind": "entities",
                "path": str(path),
            }
        )

        payload = read_json(path, {})

        for entity in payload.get("entities", []):
            entity_key = entity["entity_id"]
            entity_node_id = stable_id(
                "entity",
                entity_key,
            )

            node = {
                "id": entity_node_id,
                "node_type": "business_entity",
                "entity_id": entity_key,
                "name": entity.get(
                    "name",
                    entity_key,
                ),
                "title": entity.get(
                    "name",
                    entity_key,
                ),
                "aliases": entity.get(
                    "aliases",
                    [],
                ),
                "description": entity.get(
                    "description"
                ),
                "business_domains": entity.get(
                    "business_domains",
                    [],
                ),
                "module_id": entity.get(
                    "module_id"
                ),
                "confidence": "high",
                "source": {
                    "source_type": "curated_entity_map",
                    "entity_aliases_path": str(path),
                    "module_id": entity.get(
                        "module_id"
                    ),
                },
                "search_text": merge_text_fields(
                    entity_key,
                    entity.get("name"),
                    entity.get("aliases"),
                    entity.get("description"),
                    entity.get("business_domains"),
                ),
            }

            self.add_node(node)
            self.entity_by_id[entity_key] = entity_node_id

            module_id = entity.get("module_id")

            for table_name in entity.get(
                    "tables",
                    [],
            ):
                table_id = self.table_by_name.get(
                    str(table_name).upper()
                )

                if table_id:
                    self.add_edge(
                        entity_node_id,
                        table_id,
                        "mapped_to_entity",
                    )

            for subject_area_name in entity.get(
                    "subject_areas",
                    [],
            ):
                for subject_id in self._matching_ids(
                        self.subject_area_by_name,
                        subject_area_name,
                        module_id,
                ):
                    self.add_edge(
                        entity_node_id,
                        subject_id,
                        "mapped_to_entity",
                    )

            for resource_name in entity.get(
                    "rest_resources",
                    [],
            ):
                for resource_id in self._matching_ids(
                        self.rest_resource_by_name,
                        resource_name,
                        module_id,
                ):
                    self.add_edge(
                        entity_node_id,
                        resource_id,
                        "mapped_to_entity",
                    )

            for rule_id in entity.get(
                    "validated_rules",
                    [],
            ):
                if rule_id in self.nodes:
                    self.add_edge(
                        entity_node_id,
                        rule_id,
                        "mapped_to_entity",
                    )

            for attribute in entity.get(
                    "attributes",
                    [],
            ):
                attribute_key = attribute.get(
                    "attribute_id"
                )

                if not attribute_key:
                    continue

                attribute_node_id = stable_id(
                    "attribute",
                    f"{entity_key}.{attribute_key}",
                )

                attribute_node = {
                    "id": attribute_node_id,
                    "node_type": "business_attribute",
                    "attribute_id": attribute_key,
                    "entity_id": entity_key,
                    "name": attribute.get(
                        "name",
                        attribute_key,
                    ),
                    "title": attribute.get(
                        "name",
                        attribute_key,
                    ),
                    "aliases": attribute.get(
                        "aliases",
                        [],
                    ),
                    "description": attribute.get(
                        "description"
                    ),
                    "columns": attribute.get(
                        "columns",
                        [],
                    ),
                    "module_id": module_id,
                    "confidence": attribute.get(
                        "confidence",
                        "high",
                    ),
                    "source": {
                        "source_type": "curated_entity_map",
                        "entity_aliases_path": str(path),
                        "module_id": module_id,
                    },
                    "search_text": merge_text_fields(
                        attribute_key,
                        attribute.get("name"),
                        attribute.get("aliases"),
                        attribute.get("description"),
                        attribute.get("columns"),
                        entity_key,
                        entity.get("name"),
                        entity.get("aliases"),
                    ),
                }

                self.add_node(attribute_node)

                self.add_edge(
                    entity_node_id,
                    attribute_node_id,
                    "has_attribute",
                    weight=1.0,
                    evidence={
                        "entity_id": entity_key,
                        "attribute_id": attribute_key,
                    },
                )

                for qualified_name in attribute.get(
                        "columns",
                        [],
                ):
                    normalized_qualified_name = str(
                        qualified_name
                    ).upper()

                    column_id = (
                        self.column_by_qualified_name.get(
                            normalized_qualified_name
                        )
                    )

                    if not column_id:
                        continue

                    self.add_edge(
                        attribute_node_id,
                        column_id,
                        "mapped_to_attribute",
                        weight=1.0,
                        evidence={
                            "entity_id": entity_key,
                            "attribute_id": attribute_key,
                            "qualified_name": (
                                normalized_qualified_name
                            ),
                        },
                    )

        self._link_entities_by_alias()

    def _link_entities_by_alias(self) -> None:
        for entity_key, entity_node_id in self.entity_by_id.items():
            entity = self.nodes[entity_node_id]
            aliases = [entity_key, entity.get("name", ""), *entity.get("aliases", [])]
            normalized_aliases = [
                normalize_text(alias)
                for alias in aliases
                if normalize_text(alias)
            ]
            for node_id, node in list(self.nodes.items()):
                if node_id == entity_node_id or node.get("node_type") == "business_entity":
                    continue
                haystack = normalize_text(node.get("search_text"))
                matches = [
                    alias
                    for alias in normalized_aliases
                    if len(alias) >= 4 and alias in haystack
                ]
                if matches:
                    self.add_edge(
                        entity_node_id,
                        node_id,
                        "mentions_entity",
                        weight=min(0.9, 0.55 + 0.08 * len(matches)),
                        evidence={"aliases": matches[:5]},
                    )

    def build(self) -> dict[str, Any]:
        nodes = list(self.nodes.values())
        edges = list(self.edges.values())
        type_counts: dict[str, int] = defaultdict(int)
        edge_counts: dict[str, int] = defaultdict(int)
        module_counts: dict[str, int] = defaultdict(int)
        for node in nodes:
            type_counts[node.get("node_type", "unknown")] += 1
            for module_id in node.get("modules", []):
                module_counts[module_id] += 1
        for edge in edges:
            edge_counts[edge["type"]] += 1
        return {
            "version": "2.0.0",
            "generated_at": utc_now_iso(),
            "sources": self.loaded_sources,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "nodes_by_type": dict(sorted(type_counts.items())),
                "edges_by_type": dict(sorted(edge_counts.items())),
                "nodes_by_module": dict(sorted(module_counts.items())),
                "source_files": len(self.loaded_sources),
            },
        }


def build_graph(
    *,
    physical_manifest: str | Path | Iterable[str | Path] | None = None,
    functional_fragments: str | Path | Iterable[str | Path] | None = None,
    otbi_catalog: str | Path | Iterable[str | Path] | None = None,
    rest_catalog: str | Path | Iterable[str | Path] | None = None,
    validated_rules: str | Path | Iterable[str | Path] | None = None,
    entity_aliases: str | Path | Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    builder = GraphBuilder()
    for path in _path_list(physical_manifest):
        builder.load_physical_manifest(path)
    builder.resolve_physical_relationships()
    for path in _path_list(functional_fragments):
        builder.load_functional(path)
    for path in _path_list(otbi_catalog):
        builder.load_otbi(path)
    for path in _path_list(rest_catalog):
        builder.load_rest(path)
    for path in _path_list(validated_rules):
        builder.load_rules(path)
    for path in _path_list(entity_aliases):
        builder.load_entities(path)
    return builder.build()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Liga múltiplos módulos em um único grafo de conhecimento."
    )
    parser.add_argument("--physical-manifest", action="append")
    parser.add_argument("--functional-fragments", action="append")
    parser.add_argument("--otbi-catalog", action="append")
    parser.add_argument("--rest-catalog", action="append")
    parser.add_argument("--validated-rules", action="append")
    parser.add_argument("--entity-aliases", action="append")
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    graph = build_graph(
        physical_manifest=args.physical_manifest,
        functional_fragments=args.functional_fragments,
        otbi_catalog=args.otbi_catalog,
        rest_catalog=args.rest_catalog,
        validated_rules=args.validated_rules,
        entity_aliases=args.entity_aliases,
    )
    write_json(args.output, graph)
    print(
        f"[CONCLUÍDO] {graph['stats']['nodes']} nós, "
        f"{graph['stats']['edges']} arestas e "
        f"{graph['stats']['source_files']} arquivos-fonte."
    )


if __name__ == "__main__":
    main()
