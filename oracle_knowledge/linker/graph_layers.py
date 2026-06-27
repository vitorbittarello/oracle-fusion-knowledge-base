from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from oracle_knowledge.common import (
    extract_keywords,
    merge_text_fields,
    normalize_text,
    utc_now_iso,
    write_json,
)

GRAPH_FILENAMES = {
    "business": "business.json",
    "physical": "physical.json",
    "otbi_analytics": "otbi_analytics.json",
    "otbi_security": "otbi_security.json",
    "rest": "rest.json",
    "master": "master_graph.json",
}

BUSINESS_NODE_TYPES = {
    "business_entity",
    "business_attribute",
    "validated_rule",
    "functional_section",
}

BUSINESS_CORE_NODE_TYPES = {
    "business_entity",
    "business_attribute",
    "validated_rule",
}

PHYSICAL_NODE_TYPES = {
    "physical_table",
    "physical_table_stub",
    "physical_column",
}

REST_NODE_TYPES = {
    "rest_resource",
    "rest_operation",
}

OTBI_ANALYTICS_NODE_TYPES = {
    "otbi_subject_area",
    "otbi_business_question",
}

MASTER_EDGE_TYPES = {
    "has_attribute",
    "mapped_to_entity",
    "mapped_to_attribute",
    "uses_table",
    "uses_column",
}

EXCLUDED_EDGE_TYPES = {
    "mentions_entity",
    "related_by_alias",
    "incoming_foreign_key_from",
}


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            nested = _unique_strings(value)
            for item in nested:
                if item not in seen:
                    seen.add(item)
                    result.append(item)
            continue
        text = " ".join(str(value).split())
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dict_texts(
    values: Iterable[Any],
    *,
    keys: tuple[str, ...],
) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in keys:
            field_value = value.get(key)
            if isinstance(field_value, str):
                result.append(field_value)
            elif isinstance(field_value, (list, tuple, set)):
                result.extend(_unique_strings(field_value))
    return result


def classify_otbi_reference_page(node: dict[str, Any]) -> str:
    """
    Classifica páginas OTBI que não são subject areas nem business questions.

    O objetivo não é reproduzir toda a taxonomia da documentação. A separação
    existe apenas para impedir que páginas de papéis e duties concorram com
    conteúdo analítico durante a recuperação de contexto.
    """
    title = str(node.get("title") or node.get("name") or "")
    source_url = str((node.get("source") or {}).get("url") or "")
    raw_text = merge_text_fields(
        title,
        node.get("text"),
        node.get("description"),
        node.get("toc_path"),
        source_url,
    )
    normalized = normalize_text(raw_text)
    upper_text = raw_text.upper()

    security_markers = (
        " job role",
        " duty role",
        " data role",
        " abstract role",
        " secures access",
        " privilege",
        " security role",
    )

    if (
        any(marker in f" {normalized}" for marker in security_markers)
        or "_JOB" in upper_text
        or "_DUTY" in upper_text
        or normalize_text(title).endswith(" duty")
        or "transaction analysis duty" in normalized
    ):
        return "security"

    normalized_title = normalize_text(title)
    normalized_url = normalize_text(source_url).replace("-", " ").replace("_", " ")

    if normalized_title == "get help":
        return "excluded"

    if normalized_title == "overview":
        if (
            "subject areas chap" in normalized_url
            or "business questions chap" in normalized_url
        ):
            return "analytics"
        if (
            "job roles chap" in normalized_url
            or "duty roles chap" in normalized_url
        ):
            return "security"
        return "excluded"

    analytics_markers = (
        "subject area",
        "analysis",
        "analytics",
        "business question",
        "metric",
        "dimension",
        "fact folder",
        "presentation folder",
        "report",
        "dashboard",
    )

    if any(marker in normalized for marker in analytics_markers):
        return "analytics"

    # Páginas OTBI não relacionadas a segurança continuam na camada analítica
    # apenas quando possuem conteúdo útil. A coleta original permanece intacta;
    # páginas genéricas ficam fora do grafo de busca.
    if len(normalized) >= 80:
        return "analytics"

    return "excluded"


def node_graph_layer(node: dict[str, Any]) -> str | None:
    node_type = str(node.get("node_type") or "")

    if node_type in BUSINESS_NODE_TYPES:
        return "business"

    if node_type in PHYSICAL_NODE_TYPES:
        return "physical"

    if node_type in REST_NODE_TYPES:
        return "rest"

    if node_type in OTBI_ANALYTICS_NODE_TYPES:
        return "otbi_analytics"

    if node_type == "otbi_reference_page":
        category = classify_otbi_reference_page(node)
        if category == "security":
            return "otbi_security"
        if category == "analytics":
            return "otbi_analytics"
        return None

    return None


def clean_node_search_text(node: dict[str, Any]) -> str:
    """
    Reconstrói search_text sem serializar listas de dicionários.

    O texto inclui apenas campos com valor semântico real para o tipo de nó.
    Metadados como source, confidence, datatype keys e representações Python
    não são incorporados ao índice.
    """
    node_type = str(node.get("node_type") or "")
    values: list[Any] = [
        node.get("title"),
        node.get("name"),
        node.get("qualified_name"),
    ]

    if node_type in {"physical_table", "physical_table_stub"}:
        values.extend(
            [
                node.get("description"),
                node.get("primary_key"),
                node.get("sub_module"),
                node.get("module_name"),
            ]
        )
        result_grain = node.get("result_grain")
        if isinstance(result_grain, dict):
            values.extend(
                [
                    result_grain.get("description"),
                    result_grain.get("grain_columns"),
                ]
            )
        values.extend(
            _dict_texts(
                node.get("business_rules") or [],
                keys=(
                    "rule",
                    "description",
                    "columns",
                    "referenced_table",
                    "referenced_column",
                ),
            )
        )
        values.extend(
            _dict_texts(
                node.get("ranking_rules") or [],
                keys=("rule", "description", "columns", "order_by"),
            )
        )

    elif node_type == "physical_column":
        values.extend(
            [
                node.get("description"),
                node.get("datatype"),
                node.get("table_name"),
                node.get("module_name"),
            ]
        )
        values.extend(
            _dict_texts(
                node.get("semantics") or [],
                keys=(
                    "description",
                    "semantic_role",
                    "semantic_roles",
                    "referenced_table",
                    "referenced_column",
                ),
            )
        )
        for semantic in node.get("semantics") or []:
            if not isinstance(semantic, dict):
                continue
            values.extend(
                _dict_texts(
                    semantic.get("references") or [],
                    keys=("table", "column"),
                )
            )

    elif node_type == "validated_rule":
        values.extend(
            [
                node.get("description"),
                node.get("business_entity"),
                node.get("tables"),
                node.get("columns"),
                node.get("sql_template"),
            ]
        )
        values.extend(
            _dict_texts(
                node.get("conditions") or [],
                keys=("column", "operator", "value", "description"),
            )
        )
        ranking = node.get("ranking")
        if isinstance(ranking, dict):
            values.extend(
                [
                    ranking.get("partition_by"),
                    ranking.get("order_by"),
                ]
            )

    elif node_type in {"business_entity", "business_attribute"}:
        values.extend(
            [
                node.get("entity_id"),
                node.get("attribute_id"),
                node.get("aliases"),
                node.get("description"),
                node.get("business_domains"),
                node.get("tables"),
                node.get("columns"),
            ]
        )

    elif node_type == "functional_section":
        values.extend(
            [
                node.get("description"),
                node.get("text"),
                node.get("section_path"),
                node.get("table_mentions"),
            ]
        )

    elif node_type.startswith("otbi_"):
        values.extend(
            [
                node.get("description"),
                node.get("text"),
                node.get("question"),
                node.get("transactional_grain"),
                node.get("time_reporting"),
                node.get("subject_areas"),
            ]
        )

    elif node_type.startswith("rest_"):
        values.extend(
            [
                node.get("description"),
                node.get("text"),
                node.get("resource_name"),
                node.get("parent_resource"),
                node.get("endpoint_path"),
                node.get("method"),
            ]
        )
        values.extend(
            _dict_texts(
                node.get("parameters") or [],
                keys=("name", "description", "type"),
            )
        )
        values.extend(
            _dict_texts(
                node.get("attributes") or [],
                keys=("name", "description", "type"),
            )
        )

    else:
        values.extend(
            [
                node.get("description"),
                node.get("text"),
            ]
        )

    return merge_text_fields(*_unique_strings(values))


def normalize_graph_node(node: dict[str, Any]) -> dict[str, Any] | None:
    layer = node_graph_layer(node)
    if not layer:
        return None

    normalized = dict(node)
    normalized["graph_layer"] = layer

    if normalized.get("node_type") == "otbi_reference_page":
        normalized["otbi_page_category"] = (
            "security" if layer == "otbi_security" else "analytics"
        )

    search_text = clean_node_search_text(normalized)
    normalized["search_text"] = search_text
    normalized["keywords"] = extract_keywords(search_text)
    return normalized


def _graph_payload(
    *,
    layer: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    type_counts: dict[str, int] = defaultdict(int)
    edge_counts: dict[str, int] = defaultdict(int)
    module_counts: dict[str, int] = defaultdict(int)

    for node in nodes:
        type_counts[str(node.get("node_type") or "unknown")] += 1
        for module_id in node.get("modules") or []:
            module_counts[str(module_id)] += 1

    for edge in edges:
        edge_counts[str(edge.get("type") or "unknown")] += 1

    payload: dict[str, Any] = {
        "version": "3.0.0",
        "generated_at": utc_now_iso(),
        "graph_layer": layer,
        "sources": sources,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "nodes_by_type": dict(sorted(type_counts.items())),
            "edges_by_type": dict(sorted(edge_counts.items())),
            "nodes_by_module": dict(sorted(module_counts.items())),
            "source_files": len(sources),
        },
    }

    if extra:
        payload.update(extra)

    return payload


def build_graph_bundle_from_graph(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Separa um grafo carregado em camadas independentes e um master graph.

    A função também pode migrar grafos antigos: mentions_entity,
    related_by_alias e incoming_foreign_key_from são descartadas, e o
    search_text é reconstruído por tipo.
    """
    normalized_nodes: dict[str, dict[str, Any]] = {}
    excluded_nodes = 0

    for node in graph.get("nodes") or []:
        normalized = normalize_graph_node(node)
        if normalized is None:
            excluded_nodes += 1
            continue
        normalized_nodes[normalized["id"]] = normalized

    clean_edges: list[dict[str, Any]] = []
    for edge in graph.get("edges") or []:
        if edge.get("type") in EXCLUDED_EDGE_TYPES:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if source not in normalized_nodes or target not in normalized_nodes:
            continue
        normalized_edge = dict(edge)
        normalized_edge["source_layer"] = normalized_nodes[source]["graph_layer"]
        normalized_edge["target_layer"] = normalized_nodes[target]["graph_layer"]
        clean_edges.append(normalized_edge)

    layers = (
        "business",
        "physical",
        "otbi_analytics",
        "otbi_security",
        "rest",
    )

    sources = list(graph.get("sources") or [])
    bundle: dict[str, dict[str, Any]] = {}

    for layer in layers:
        layer_nodes = [
            node
            for node in normalized_nodes.values()
            if node.get("graph_layer") == layer
        ]
        layer_ids = {node["id"] for node in layer_nodes}
        layer_edges = [
            edge
            for edge in clean_edges
            if edge["source"] in layer_ids and edge["target"] in layer_ids
        ]
        bundle[layer] = _graph_payload(
            layer=layer,
            nodes=layer_nodes,
            edges=layer_edges,
            sources=sources,
        )

    master_seed_ids = {
        node_id
        for node_id, node in normalized_nodes.items()
        if node.get("node_type") in BUSINESS_CORE_NODE_TYPES
    }

    master_edges: list[dict[str, Any]] = []
    master_ids = set(master_seed_ids)

    for edge in clean_edges:
        if edge.get("type") not in MASTER_EDGE_TYPES:
            continue
        source = edge["source"]
        target = edge["target"]
        if source not in master_seed_ids and target not in master_seed_ids:
            continue
        master_edges.append(edge)
        master_ids.add(source)
        master_ids.add(target)

    master_nodes = [
        node
        for node_id, node in normalized_nodes.items()
        if node_id in master_ids
    ]

    layer_files = {
        layer: GRAPH_FILENAMES[layer]
        for layer in layers
    }

    bundle["master"] = _graph_payload(
        layer="master",
        nodes=master_nodes,
        edges=master_edges,
        sources=sources,
        extra={
            "layers": layer_files,
            "excluded_nodes": excluded_nodes,
            "bridge_edge_types": sorted(MASTER_EDGE_TYPES),
        },
    )

    return bundle


def write_graph_bundle(
    output_dir: str | Path,
    bundle: dict[str, dict[str, Any]],
) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}

    for layer, filename in GRAPH_FILENAMES.items():
        graph = bundle.get(layer)
        if graph is None:
            continue
        path = root / filename
        write_json(path, graph)
        outputs[layer] = str(path)

    manifest = {
        "version": "1.0.0",
        "generated_at": utc_now_iso(),
        "graphs": outputs,
        "stats": {
            layer: graph.get("stats", {})
            for layer, graph in bundle.items()
        },
    }
    manifest_path = root / "graph_bundle.json"
    write_json(manifest_path, manifest)
    outputs["manifest"] = str(manifest_path)
    return outputs
