from __future__ import annotations

import argparse
from typing import Any

from oracle_knowledge.common import (
    CachedHttpClient,
    HttpSettings,
    extract_heading_sections,
    extract_keywords,
    extract_title,
    iter_toc_links,
    merge_text_fields,
    normalize_text,
    read_json,
    stable_id,
    unique_preserving_order,
    utc_now_iso,
    write_json,
)


def _section_map(sections: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        result.setdefault(normalize_text(section.get("title")), []).append(section)
    return result


def _section_text(mapping: dict[str, list[dict[str, Any]]], *names: str) -> str | None:
    for name in names:
        matches = mapping.get(normalize_text(name), [])
        if matches:
            texts = [match.get("text", "") for match in matches if match.get("text")]
            return " ".join(texts) or None
    return None


def _extract_bullets_from_section(section: dict[str, Any] | None) -> list[str]:
    if not section:
        return []
    values: list[str] = list(section.get("list_items", []))
    for table_group in section.get("tables", []):
        for row in table_group:
            values.extend(value for value in row.values() if value)
    if not values:
        text = section.get("text") or ""
        for part in text.replace("•", "\n").split("\n"):
            part = part.strip(" -\t")
            if part:
                values.append(part)
    return unique_preserving_order(values)


def _find_section(mapping: dict[str, list[dict[str, Any]]], *names: str) -> dict[str, Any] | None:
    for name in names:
        matches = mapping.get(normalize_text(name), [])
        if matches:
            return matches[0]
    return None


class OtbiCollector:
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
        source_id = source.get("source_id") or source.get("module_id") or stable_id("otbi_source", toc_url)
        soup, toc_metadata = self.client.get_soup(
            toc_url,
            force_refresh=self.force_refresh,
        )
        links = list(iter_toc_links(soup, toc_url))
        if max_pages is not None:
            links = links[:max_pages]

        subject_areas: list[dict[str, Any]] = []
        business_questions: list[dict[str, Any]] = []
        other_pages: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for index, item in enumerate(links, start=1):
            try:
                page_soup, metadata = self.client.get_soup(
                    item["url"],
                    force_refresh=self.force_refresh,
                )
                title = extract_title(page_soup)
                sections = extract_heading_sections(page_soup)
                mapping = _section_map(sections)
                path_lower = item["url"].lower()
                section_titles = set(mapping)

                is_subject_area = (
                    "-sa-" in path_lower
                    or {
                        "description",
                        "business questions",
                        "transactional grain",
                    }.issubset(section_titles)
                )
                is_business_question = (
                    "-bq-" in path_lower
                    or (
                        "subject areas" in section_titles
                        and title.rstrip().endswith("?")
                    )
                )

                if is_subject_area:
                    record = self._parse_subject_area(
                        source,
                        item,
                        title,
                        mapping,
                        metadata,
                    )
                    subject_areas.append(record)
                elif is_business_question:
                    record = self._parse_business_question(
                        source,
                        item,
                        title,
                        mapping,
                        metadata,
                    )
                    business_questions.append(record)
                else:
                    text = merge_text_fields(title, [section.get("text") for section in sections])
                    other_pages.append(
                        {
                            "id": stable_id("otbi_page", source_id, item["url"]),
                            "node_type": "otbi_reference_page",
                            "title": title,
                            "toc_path": item.get("toc_path", []),
                            "text": text,
                            "keywords": extract_keywords(text),
                            "source": {
                                "source_type": "oracle_otbi_documentation",
                                "source_id": source_id,
                                "module_id": source.get("module_id"),
                                "module_name": source.get("module_name"),
                                "release": source.get("release"),
                                "url": item["url"],
                                "fetched_at": metadata.get("fetched_at"),
                            },
                            "confidence": "high",
                            "search_text": text,
                        }
                    )
            except Exception as exc:
                errors.append(
                    {
                        "id": stable_id("collection_error", source_id, item["url"]),
                        "node_type": "collection_error",
                        "title": item["label"],
                        "text": str(exc),
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "source_id": source_id,
                            "module_id": source.get("module_id"),
                            "module_name": source.get("module_name"),
                            "release": source.get("release"),
                            "url": item["url"],
                        },
                        "confidence": "low",
                    }
                )

        return {
            "version": "1.0.0",
            "generated_at": utc_now_iso(),
            "source": {
                "source_type": "oracle_otbi_documentation",
                "source_id": source_id,
                "module_id": source.get("module_id"),
                "module_name": source.get("module_name"),
                "release": source.get("release"),
                "toc_url": toc_url,
                "toc_fetched_at": toc_metadata.get("fetched_at"),
            },
            "subject_areas": subject_areas,
            "business_questions": business_questions,
            "other_pages": other_pages,
            "errors": errors,
            "stats": {
                "pages_considered": len(links),
                "subject_areas": len(subject_areas),
                "business_questions": len(business_questions),
                "other_pages": len(other_pages),
                "errors": len(errors),
            },
        }

    def _parse_subject_area(
        self,
        source: dict[str, Any],
        item: dict[str, Any],
        title: str,
        mapping: dict[str, list[dict[str, Any]]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        business_questions = _extract_bullets_from_section(
            _find_section(mapping, "Business Questions")
        )
        job_roles = _extract_bullets_from_section(_find_section(mapping, "Job Roles"))
        duty_roles = _extract_bullets_from_section(_find_section(mapping, "Duty Roles"))
        description = _section_text(mapping, "Description")
        primary_navigation = _section_text(mapping, "Primary Navigation")
        time_reporting = _section_text(mapping, "Time Reporting")
        transactional_grain = _section_text(mapping, "Transactional Grain")
        special_considerations = _section_text(mapping, "Special Considerations")
        search_text = merge_text_fields(
            title,
            description,
            business_questions,
            transactional_grain,
            time_reporting,
            special_considerations,
            job_roles,
            duty_roles,
        )
        return {
            "id": stable_id("subject_area", source.get("source_id") or source.get("module_id") or source.get("toc_url", "otbi"), title),
            "node_type": "otbi_subject_area",
            "name": title,
            "title": title,
            "description": description,
            "business_questions": business_questions,
            "job_roles": job_roles,
            "duty_roles": duty_roles,
            "primary_navigation": primary_navigation,
            "time_reporting": time_reporting,
            "transactional_grain": transactional_grain,
            "special_considerations": special_considerations,
            "toc_path": item.get("toc_path", []),
            "keywords": extract_keywords(search_text),
            "source": {
                "source_type": "oracle_otbi_documentation",
                "source_id": source.get("source_id") or source.get("module_id"),
                "module_id": source.get("module_id"),
                "module_name": source.get("module_name"),
                "release": source.get("release"),
                "url": item["url"],
                "fetched_at": metadata.get("fetched_at"),
                "last_modified": metadata.get("last_modified"),
            },
            "confidence": "high",
            "search_text": search_text,
        }

    def _parse_business_question(
        self,
        source: dict[str, Any],
        item: dict[str, Any],
        title: str,
        mapping: dict[str, list[dict[str, Any]]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        subject_areas = _extract_bullets_from_section(
            _find_section(mapping, "Subject Areas")
        )
        job_roles = _extract_bullets_from_section(_find_section(mapping, "Job Roles"))
        duty_roles = _extract_bullets_from_section(_find_section(mapping, "Duty Roles"))
        search_text = merge_text_fields(title, subject_areas, job_roles, duty_roles)
        return {
            "id": stable_id("business_question", source.get("source_id") or source.get("module_id") or source.get("toc_url", "otbi"), title),
            "node_type": "otbi_business_question",
            "question": title,
            "title": title,
            "subject_areas": subject_areas,
            "job_roles": job_roles,
            "duty_roles": duty_roles,
            "toc_path": item.get("toc_path", []),
            "keywords": extract_keywords(search_text),
            "source": {
                "source_type": "oracle_otbi_documentation",
                "source_id": source.get("source_id") or source.get("module_id"),
                "module_id": source.get("module_id"),
                "module_name": source.get("module_name"),
                "release": source.get("release"),
                "url": item["url"],
                "fetched_at": metadata.get("fetched_at"),
            },
            "confidence": "high",
            "search_text": search_text,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coleta metadados OTBI da Oracle.")
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
    source = config.get("otbi")
    if not source:
        raise SystemExit("Seção otbi não encontrada no arquivo de configuração.")
    collector = OtbiCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    payload = collector.collect(source, max_pages=args.max_pages)
    write_json(args.output, payload)
    print(
        "[CONCLUÍDO] "
        f"{payload['stats']['subject_areas']} subject areas, "
        f"{payload['stats']['business_questions']} perguntas de negócio e "
        f"{payload['stats']['errors']} erros."
    )


if __name__ == "__main__":
    main()
