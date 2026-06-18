from __future__ import annotations

import argparse
import re
from collections import defaultdict
from typing import Any

from oracle_knowledge.common import (
    CachedHttpClient,
    HttpSettings,
    extract_heading_sections,
    extract_keywords,
    extract_title,
    iter_toc_links,
    merge_text_fields,
    normalize_space,
    normalize_text,
    read_json,
    stable_id,
    unique_preserving_order,
    utc_now_iso,
    write_json,
)

HTTP_METHOD_RE = re.compile(r"\b(GET|POST|PATCH|PUT|DELETE)\b", re.IGNORECASE)
ENDPOINT_RE = re.compile(r"(/fscmRestApi/resources/[^\s<]+)", re.IGNORECASE)


def _flatten_tables(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for section in sections:
        for table_group in section.get("tables", []):
            flattened.append(
                {
                    "section_title": section.get("title"),
                    "section_path": section.get("section_path", []),
                    "rows": table_group,
                }
            )
    return flattened


def _infer_method_and_path(title: str, page_text: str) -> tuple[str | None, str | None]:
    method_match = HTTP_METHOD_RE.search(page_text)
    path_match = ENDPOINT_RE.search(page_text)
    method = method_match.group(1).upper() if method_match else None
    path = path_match.group(1).rstrip(".,;") if path_match else None

    if not method:
        normalized_title = normalize_text(title)
        prefixes = {
            "get ": "GET",
            "create ": "POST",
            "update ": "PATCH",
            "delete ": "DELETE",
            "replace ": "PUT",
        }
        for prefix, candidate in prefixes.items():
            if normalized_title.startswith(prefix):
                method = candidate
                break
    return method, path


def _first_meaningful_text(sections: list[dict[str, Any]], title: str) -> str | None:
    for section in sections:
        text = normalize_space(section.get("text"))
        if not text:
            continue
        if normalize_text(section.get("title")) == normalize_text(title):
            return text
        if normalize_text(section.get("title")) not in {
            "request",
            "response",
            "examples",
            "jump to",
        }:
            return text
    return None


def _extract_parameters(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for table in tables:
        section_name = normalize_text(table.get("section_title"))
        if "parameter" not in section_name and "request" not in " ".join(
            normalize_text(part) for part in table.get("section_path", [])
        ):
            continue
        for row in table.get("rows", []):
            normalized = {normalize_text(key).replace(" ", "_"): value for key, value in row.items()}
            name = (
                normalized.get("name")
                or normalized.get("parameter")
                or normalized.get("column_1")
            )
            if not name:
                continue
            location = "query"
            if "path" in section_name:
                location = "path"
            elif "header" in section_name:
                location = "header"
            key = (location, normalize_text(name))
            if key in seen:
                continue
            seen.add(key)
            parameters.append(
                {
                    "name": name,
                    "in": location,
                    "type": normalized.get("type") or normalized.get("column_2"),
                    "required": normalized.get("required"),
                    "description": normalized.get("description") or normalized.get("column_3"),
                    "raw": row,
                }
            )
    return parameters


def _extract_attributes(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for table in tables:
        path = " ".join(normalize_text(part) for part in table.get("section_path", []))
        section_name = normalize_text(table.get("section_title"))
        if not any(marker in f"{path} {section_name}" for marker in ("schema", "response", "request body", "properties")):
            continue
        for row in table.get("rows", []):
            normalized = {normalize_text(key).replace(" ", "_"): value for key, value in row.items()}
            name = normalized.get("name") or normalized.get("property") or normalized.get("column_1")
            if not name:
                continue
            name_key = normalize_text(name)
            if name_key in seen:
                continue
            seen.add(name_key)
            attributes.append(
                {
                    "name": name,
                    "type": normalized.get("type") or normalized.get("column_2"),
                    "title": normalized.get("title"),
                    "description": normalized.get("description") or normalized.get("column_3"),
                    "read_only": normalized.get("read_only") or normalized.get("readonly"),
                    "required": normalized.get("required"),
                    "raw": row,
                }
            )
    return attributes


class RestCollector:
    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        delay_seconds: float = 0.15,
        force_refresh: bool = False,
    ):
        self.client = CachedHttpClient(
            HttpSettings(cache_dir=cache_dir, delay_seconds=delay_seconds)
        )
        self.force_refresh = force_refresh

    def collect(
        self,
        source: dict[str, Any],
        *,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        toc_url = source["toc_url"]
        source_id = source.get("source_id") or source.get("module_id") or stable_id("rest_source", toc_url)
        toc_soup, toc_metadata = self.client.get_soup(
            toc_url,
            force_refresh=self.force_refresh,
        )
        links = list(iter_toc_links(toc_soup, toc_url))
        operation_candidates = [
            item
            for item in links
            if any(
                normalize_text(item["label"]).startswith(prefix)
                for prefix in (
                    "get ",
                    "create ",
                    "update ",
                    "delete ",
                    "replace ",
                    "adjust ",
                    "refresh ",
                    "reprocess ",
                    "perform ",
                )
            )
            or "/op-" in item["url"].lower()
        ]
        if max_pages is not None:
            operation_candidates = operation_candidates[:max_pages]

        operations: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        resources: dict[str, dict[str, Any]] = {}

        for index, item in enumerate(operation_candidates, start=1):
            try:
                page_soup, metadata = self.client.get_soup(
                    item["url"],
                    force_refresh=self.force_refresh,
                )
                title = extract_title(page_soup)
                sections = extract_heading_sections(page_soup)
                page_text = merge_text_fields(
                    title,
                    [section.get("title") for section in sections],
                    [section.get("text") for section in sections],
                    [section.get("tables") for section in sections],
                )
                method, endpoint_path = _infer_method_and_path(title, page_text)
                if not method and not endpoint_path:
                    continue

                toc_path = item.get("toc_path", [])
                resource_hierarchy = self._resource_hierarchy(toc_path, title)
                resource_name = resource_hierarchy[-1] if resource_hierarchy else self._resource_from_endpoint(endpoint_path)
                tables = _flatten_tables(sections)
                parameters = _extract_parameters(tables)
                attributes = _extract_attributes(tables)
                description = _first_meaningful_text(sections, title)
                search_text = merge_text_fields(
                    title,
                    description,
                    method,
                    endpoint_path,
                    resource_hierarchy,
                    parameters,
                    attributes,
                )
                operation = {
                    "id": stable_id("rest_operation", source_id, method or "UNKNOWN", endpoint_path or item["url"]),
                    "node_type": "rest_operation",
                    "title": title,
                    "method": method,
                    "endpoint_path": endpoint_path,
                    "description": description,
                    "resource_name": resource_name,
                    "resource_hierarchy": resource_hierarchy,
                    "parameters": parameters,
                    "attributes": attributes,
                    "tables": tables,
                    "keywords": extract_keywords(search_text),
                    "source": {
                        "source_type": "oracle_rest_documentation",
                        "source_id": source_id,
                        "module_id": source.get("module_id"),
                        "module_name": source.get("module_name"),
                        "release": source.get("release"),
                        "url": item["url"],
                        "fetched_at": metadata.get("fetched_at"),
                        "last_modified": metadata.get("last_modified"),
                    },
                    "confidence": "high",
                    "search_text": search_text,
                    "collection": {"page_number": index},
                }
                operations.append(operation)

                for depth, name in enumerate(resource_hierarchy, start=1):
                    resource_id = stable_id("rest_resource", source_id, name)
                    resource = resources.setdefault(
                        resource_id,
                        {
                            "id": resource_id,
                            "node_type": "rest_resource",
                            "name": name,
                            "title": name,
                            "parent_resource": resource_hierarchy[depth - 2] if depth > 1 else None,
                            "child_resources": [],
                            "operation_ids": [],
                            "endpoint_paths": [],
                            "keywords": [],
                            "source": {
                                "source_type": "oracle_rest_documentation",
                                "source_id": source_id,
                                "module_id": source.get("module_id"),
                                "module_name": source.get("module_name"),
                                "release": source.get("release"),
                                "toc_url": toc_url,
                            },
                            "confidence": "high",
                        },
                    )
                    resource["operation_ids"].append(operation["id"])
                    if endpoint_path:
                        resource["endpoint_paths"].append(endpoint_path)
                    if depth < len(resource_hierarchy):
                        resource["child_resources"].append(resource_hierarchy[depth])
            except Exception as exc:
                errors.append(
                    {
                        "id": stable_id("collection_error", source_id, item["url"]),
                        "node_type": "collection_error",
                        "title": item["label"],
                        "text": str(exc),
                        "source": {
                            "source_type": "oracle_rest_documentation",
                            "source_id": source_id,
                            "module_id": source.get("module_id"),
                            "module_name": source.get("module_name"),
                            "release": source.get("release"),
                            "url": item["url"],
                        },
                        "confidence": "low",
                    }
                )

        for resource in resources.values():
            resource["child_resources"] = unique_preserving_order(resource["child_resources"])
            resource["operation_ids"] = unique_preserving_order(resource["operation_ids"])
            resource["endpoint_paths"] = unique_preserving_order(resource["endpoint_paths"])
            resource["search_text"] = merge_text_fields(
                resource["title"],
                resource["parent_resource"],
                resource["child_resources"],
                resource["endpoint_paths"],
            )
            resource["keywords"] = extract_keywords(resource["search_text"])

        return {
            "version": "1.0.0",
            "generated_at": utc_now_iso(),
            "source": {
                "source_type": "oracle_rest_documentation",
                "source_id": source_id,
                "module_id": source.get("module_id"),
                "module_name": source.get("module_name"),
                "release": source.get("release"),
                "toc_url": toc_url,
                "toc_fetched_at": toc_metadata.get("fetched_at"),
            },
            "resources": sorted(resources.values(), key=lambda item: item["name"]),
            "operations": operations,
            "errors": errors,
            "stats": {
                "toc_links": len(links),
                "operation_candidates": len(operation_candidates),
                "resources": len(resources),
                "operations": len(operations),
                "errors": len(errors),
            },
        }

    @staticmethod
    def _resource_hierarchy(toc_path: list[str], title: str) -> list[str]:
        cleaned = []
        ignored = {
            "tasks",
            "get started",
            "learn more",
            "reference",
            "about the rest apis",
            "all rest endpoints",
        }
        for label in toc_path[:-1]:
            if normalize_text(label) in ignored:
                continue
            if normalize_text(label).startswith(("get ", "create ", "update ", "delete ", "replace ")):
                continue
            cleaned.append(label)
        return unique_preserving_order(cleaned)

    @staticmethod
    def _resource_from_endpoint(endpoint_path: str | None) -> str:
        if not endpoint_path:
            return "Unknown Resource"
        parts = [part for part in endpoint_path.split("/") if part]
        for index, part in enumerate(parts):
            if part == "resources" and index + 2 < len(parts):
                return parts[index + 2].split("?")[0]
        return parts[-1] if parts else "Unknown Resource"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coleta operações e recursos REST da Oracle.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache-dir", default=".cache/oracle_docs")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    parser.add_argument("--force-refresh", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = read_json(args.config, {})
    source = config.get("rest")
    if not source:
        raise SystemExit("Seção rest não encontrada no arquivo de configuração.")
    collector = RestCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    payload = collector.collect(source, max_pages=args.max_pages)
    write_json(args.output, payload)
    print(
        "[CONCLUÍDO] "
        f"{payload['stats']['resources']} recursos, "
        f"{payload['stats']['operations']} operações e "
        f"{payload['stats']['errors']} erros."
    )


if __name__ == "__main__":
    main()
