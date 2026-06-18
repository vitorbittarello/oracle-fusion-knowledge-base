from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from oracle_knowledge.common import (
    CachedHttpClient,
    HttpSettings,
    extract_heading_sections,
    extract_keywords,
    extract_table_mentions,
    extract_title,
    iter_toc_links,
    merge_text_fields,
    normalize_space,
    read_json,
    read_jsonl,
    stable_id,
    unique_preserving_order,
    utc_now_iso,
    write_jsonl,
)


class FunctionalDocsCollector:
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

    def collect_guide(
        self,
        guide: dict[str, Any],
        *,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        guide_id = guide["guide_id"]
        guide_title = guide["title"]
        toc_url = guide["toc_url"]
        module_id = guide.get("module_id")
        module_name = guide.get("module_name")
        include_patterns = [value.lower() for value in guide.get("include_patterns", [])]
        exclude_patterns = [value.lower() for value in guide.get("exclude_patterns", [])]

        toc_soup, toc_metadata = self.client.get_soup(
            toc_url,
            force_refresh=self.force_refresh,
        )
        links = list(iter_toc_links(toc_soup, toc_url))
        filtered_links = []
        for item in links:
            candidate = f"{item['url']} {item['label']}".lower()
            if include_patterns and not any(pattern in candidate for pattern in include_patterns):
                continue
            if any(pattern in candidate for pattern in exclude_patterns):
                continue
            filtered_links.append(item)

        if max_pages is not None:
            filtered_links = filtered_links[:max_pages]

        fragments: list[dict[str, Any]] = []
        for page_number, item in enumerate(filtered_links, start=1):
            try:
                soup, metadata = self.client.get_soup(
                    item["url"],
                    force_refresh=self.force_refresh,
                )
                page_title = extract_title(soup)
                sections = extract_heading_sections(soup)
                for section_number, section in enumerate(sections, start=1):
                    text = normalize_space(section.get("text"))
                    tables = section.get("tables", [])
                    if not text and not tables:
                        continue
                    section_path = unique_preserving_order(
                        [*item.get("toc_path", []), *section.get("section_path", [])]
                    )
                    search_text = merge_text_fields(
                        guide_title,
                        page_title,
                        section_path,
                        text,
                        tables,
                    )
                    fragment_id = stable_id(
                        "functional",
                        guide_id,
                        item["url"],
                        "/".join(section_path),
                        str(section_number),
                    )
                    fragments.append(
                        {
                            "id": fragment_id,
                            "node_type": "functional_section",
                            "guide_id": guide_id,
                            "guide_title": guide_title,
                            "module_id": module_id,
                            "module_name": module_name,
                            "page_title": page_title,
                            "title": section["title"],
                            "section_path": section_path,
                            "text": text,
                            "tables": tables,
                            "keywords": extract_keywords(search_text),
                            "table_mentions": extract_table_mentions(search_text),
                            "business_domains": guide.get("business_domains", []),
                            "source": {
                                "source_type": "oracle_functional_documentation",
                                "module_id": module_id,
                                "module_name": module_name,
                                "guide_id": guide_id,
                                "release": guide.get("release"),
                                "url": item["url"],
                                "toc_url": toc_url,
                                "fetched_at": metadata.get("fetched_at"),
                                "last_modified": metadata.get("last_modified"),
                            },
                            "confidence": "high",
                            "search_text": search_text,
                            "collection": {
                                "page_number": page_number,
                                "section_number": section_number,
                                "toc_fetched_at": toc_metadata.get("fetched_at"),
                            },
                        }
                    )
            except Exception as exc:
                fragments.append(
                    {
                        "id": stable_id("collection_error", guide_id, item["url"]),
                        "node_type": "collection_error",
                        "guide_id": guide_id,
                        "module_id": module_id,
                        "module_name": module_name,
                        "title": item["label"],
                        "text": str(exc),
                        "source": {
                            "source_type": "oracle_functional_documentation",
                            "module_id": module_id,
                            "module_name": module_name,
                            "guide_id": guide_id,
                            "url": item["url"],
                            "release": guide.get("release"),
                        },
                        "confidence": "low",
                        "search_text": merge_text_fields(item["label"], exc),
                    }
                )
        return fragments

    def collect_all(
        self,
        guides: Iterable[dict[str, Any]],
        *,
        output_path: str,
        max_pages_per_guide: int | None = None,
        resume: bool = True,
    ) -> list[dict[str, Any]]:
        existing = read_jsonl(output_path) if resume else []
        existing_success_urls = {
            row.get("source", {}).get("url")
            for row in existing
            if row.get("node_type") != "collection_error"
        }
        new_rows: list[dict[str, Any]] = []
        for guide in guides:
            rows = self.collect_guide(guide, max_pages=max_pages_per_guide)
            for row in rows:
                url = row.get("source", {}).get("url")
                if resume and url in existing_success_urls and row.get("node_type") != "collection_error":
                    continue
                new_rows.append(row)

        combined_by_id = {row["id"]: row for row in existing}
        combined_by_id.update({row["id"]: row for row in new_rows})
        combined = list(combined_by_id.values())
        write_jsonl(output_path, combined)
        return combined


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coleta e fragmenta guias funcionais da documentação Oracle."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache-dir", default=".cache/oracle_docs")
    parser.add_argument("--max-pages-per-guide", type=int)
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = read_json(args.config, {})
    guides = config.get("functional_guides", [])
    if not guides:
        raise SystemExit("Nenhum item functional_guides encontrado no arquivo de configuração.")

    collector = FunctionalDocsCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    rows = collector.collect_all(
        guides,
        output_path=args.output,
        max_pages_per_guide=args.max_pages_per_guide,
        resume=not args.no_resume,
    )
    errors = sum(1 for row in rows if row.get("node_type") == "collection_error")
    print(
        f"[CONCLUÍDO] {len(rows)} fragmentos funcionais no total; "
        f"{errors} registros de erro. Gerado em {utc_now_iso()}."
    )


if __name__ == "__main__":
    main()
