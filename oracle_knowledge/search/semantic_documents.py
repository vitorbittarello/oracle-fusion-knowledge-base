from __future__ import annotations

from typing import Any


TEXTUAL_SUMMARY_FIELDS = (
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


def context_summary_source(node: dict[str, Any]) -> str:
    """Retorna apenas o conteúdo textual próprio do nó, sem serializar estruturas."""
    values: list[str] = []
    seen: set[str] = set()

    for field_name in TEXTUAL_SUMMARY_FIELDS:
        value = node.get(field_name)
        if not isinstance(value, str):
            continue
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        values.append(normalized)

    return " ".join(values)


def default_semantic_document_text(node: dict[str, Any]) -> str:
    """Representação estável usada para nós físicos e OTBI."""
    return "\n".join(
        value
        for value in (
            str(node.get("title") or node.get("name") or "").strip(),
            str(node.get("qualified_name") or "").strip(),
            str(node.get("search_text") or "").strip(),
            context_summary_source(node),
        )
        if value
    )


def rest_operation_semantic_document_text(node: dict[str, Any]) -> str:
    """Representação estável usada para operações REST durante o reranking local."""
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


def semantic_document_text(node: dict[str, Any]) -> str:
    """Escolhe a representação semântica persistível conforme o tipo do nó."""
    if node.get("node_type") == "rest_operation":
        return rest_operation_semantic_document_text(node)
    return default_semantic_document_text(node)
