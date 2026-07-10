from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Iterable

from oracle_knowledge.common import read_json, utc_now_iso

ALGORITHM_VERSION = "topology-2.0.0"
PROVENANCE_PRIORITY = {"VALIDATED": 0, "EXTRACTED": 1, "INFERRED": 2, "AMBIGUOUS": 3}

# Only explicit, typed bridges may define the entity-centered structural
# neighborhood.  Generic module membership and lexical relationships are
# intentionally excluded because they collapse most of the graph into a single
# component without adding business meaning.
CORE_COMMUNITY_EDGE_TYPES = {
    "has_attribute",
    "mapped_to_entity",
    "mapped_to_attribute",
    "uses_table",
    "uses_column",
    "mapped_to_table",
    "environment_variant_of",
    "validated_by",
}
LOCAL_COMMUNITY_EDGE_TYPES = {
    "contains_column",
    "foreign_key_to",
    "foreign_key",
    "references",
    "has_operation",
    "answered_by",
}
PATH_EDGE_TYPES = CORE_COMMUNITY_EDGE_TYPES | LOCAL_COMMUNITY_EDGE_TYPES
STRUCTURAL_EDGE_TYPES = PATH_EDGE_TYPES | {"belongs_to_module"}
GENERIC_NODE_TYPES = {"physical_column"}
AUDIT_MARKERS = {
    "created_by",
    "creation_date",
    "last_updated_by",
    "last_update_date",
    "last_update_login",
    "object_version_number",
}
DEFAULT_MAX_COMMUNITY_MEMBERS = 5000


def classify_provenance(edge: dict[str, Any]) -> str:
    raw = str(edge.get("provenance") or "").upper()
    if raw in PROVENANCE_PRIORITY:
        return raw
    if edge.get("validated") is True:
        return "VALIDATED"
    if edge.get("inferred") is True:
        return "INFERRED"
    source = edge.get("source_reference") or edge.get("evidence") or edge.get("sources")
    if isinstance(edge.get("source"), dict):
        source = source or edge.get("source")
    if source or str(edge.get("type") or "") in STRUCTURAL_EDGE_TYPES:
        return "EXTRACTED"
    return "AMBIGUOUS"


def _node_layer(node: dict[str, Any]) -> str:
    explicit = str(node.get("graph_layer") or "")
    if explicit:
        return explicit
    node_type = str(node.get("node_type") or "")
    if node_type.startswith("business_") or node_type in {"validated_rule", "functional_section"}:
        return "business"
    if node_type.startswith("physical_"):
        return "physical"
    if node_type.startswith("otbi_"):
        return "otbi_analytics"
    if node_type.startswith("rest_") or node_type == "adf_resource":
        return "rest"
    return "unknown"


def _node_modules(node: dict[str, Any]) -> set[str]:
    result = {str(value) for value in node.get("modules") or [] if value}
    if node.get("module_id"):
        result.add(str(node["module_id"]))
    return result


def _modules_compatible(anchor_modules: set[str], node: dict[str, Any]) -> bool:
    node_modules = _node_modules(node)
    return not anchor_modules or not node_modules or bool(anchor_modules & node_modules)


def _load_graphs(graph_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edge_rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for filename in (
        "business.json",
        "physical.json",
        "otbi_analytics.json",
        "otbi_security.json",
        "rest.json",
        "master_graph.json",
    ):
        graph = read_json(graph_dir / filename, {})
        for node in graph.get("nodes", []):
            node_id = str(node.get("id") or "")
            if node_id:
                nodes.setdefault(node_id, node)
        for edge in graph.get("edges", []):
            item = dict(edge)
            source = str(item.get("source") or "")
            target = str(item.get("target") or "")
            edge_type = str(item.get("type") or "")
            if not source or not target or not edge_type:
                continue
            item["provenance"] = classify_provenance(item)
            item.setdefault(
                "confidence",
                1.0 if item["provenance"] == "VALIDATED" else 0.9 if item["provenance"] == "EXTRACTED" else 0.55,
            )
            item.setdefault("source_type", "graph_edge")
            if "source_reference" not in item:
                raw_source = item.get("source_metadata")
                if isinstance(raw_source, dict):
                    item["source_reference"] = raw_source.get("url") or raw_source.get("path")
                else:
                    item["source_reference"] = None
            item.setdefault("explanation", f"Aresta {edge_type} presente no grafo.")
            key = (source, target, edge_type, item["provenance"])
            edge_rows.setdefault(key, item)
    return nodes, list(edge_rows.values())


def graph_signature(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    payload = {
        "nodes": sorted(nodes),
        "edges": sorted(
            (
                str(edge.get("source")),
                str(edge.get("target")),
                str(edge.get("type")),
                str(edge.get("provenance")),
            )
            for edge in edges
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _edge_indexes(
    edges: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[tuple[dict[str, Any], str]]], dict[str, list[tuple[dict[str, Any], str]]]]:
    outgoing: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    incoming: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        outgoing[source].append((edge, target))
        incoming[target].append((edge, source))
    return outgoing, incoming


def _community_id(anchor_id: str) -> str:
    digest = hashlib.sha1(anchor_id.encode("utf-8")).hexdigest()[:12]
    return f"community:entity:{digest}"


def _entity_neighborhood(
    anchor_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_members: int = DEFAULT_MAX_COMMUNITY_MEMBERS,
) -> tuple[set[str], bool]:
    anchor = nodes[anchor_id]
    anchor_modules = _node_modules(anchor)
    outgoing, incoming = _edge_indexes(edges)
    members: set[str] = {anchor_id}
    queue = deque([(anchor_id, 0)])
    direct_physical: set[str] = set()
    truncated = False

    def add(node_id: str, depth: int) -> bool:
        nonlocal truncated
        if node_id in members:
            return False
        node = nodes.get(node_id)
        if not node or not _modules_compatible(anchor_modules, node):
            return False
        if node.get("node_type") == "business_entity":
            return False
        if len(members) >= max_members:
            truncated = True
            return False
        members.add(node_id)
        queue.append((node_id, depth))
        return True

    while queue:
        current, depth = queue.popleft()
        if depth >= 4:
            continue
        for edge, neighbor in outgoing.get(current, []) + incoming.get(current, []):
            if classify_provenance(edge) not in {"VALIDATED", "EXTRACTED"}:
                continue
            if str(edge.get("type") or "") not in CORE_COMMUNITY_EDGE_TYPES:
                continue
            if add(neighbor, depth + 1):
                if _node_layer(nodes[neighbor]) == "physical":
                    direct_physical.add(neighbor)

    # Add local evidence around nodes reached by explicit business bridges.  The
    # expansion is deliberately non-recursive for foreign keys, preventing a
    # single physical schema from becoming one giant community.
    physical_seed_ids = {
        node_id for node_id in members if _node_layer(nodes[node_id]) == "physical"
    }
    physical_seed_ids.update(direct_physical)
    mapped_columns = {
        node_id
        for node_id in physical_seed_ids
        if nodes[node_id].get("node_type") == "physical_column"
    }
    parent_tables: set[str] = set()

    for node_id in list(physical_seed_ids):
        node_type = str(nodes[node_id].get("node_type") or "")
        if node_type in {"physical_table", "physical_table_stub"}:
            parent_tables.add(node_id)
        if node_type == "physical_column":
            for edge, neighbor in incoming.get(node_id, []):
                if edge.get("type") == "contains_column" and neighbor in nodes:
                    if _modules_compatible(anchor_modules, nodes[neighbor]):
                        members.add(neighbor)
                        parent_tables.add(neighbor)

    for table_id in list(parent_tables):
        for edge, child_id in outgoing.get(table_id, []):
            if edge.get("type") != "contains_column" or child_id not in nodes:
                continue
            if not _modules_compatible(anchor_modules, nodes[child_id]):
                continue
            if len(members) >= max_members:
                truncated = True
                break
            members.add(child_id)

    relationship_targets: set[str] = set()
    relationship_types = {"foreign_key_to", "foreign_key", "references"}
    for column_id in mapped_columns:
        column_name = str(nodes[column_id].get("name") or nodes[column_id].get("title") or "").split(".")[-1].upper()
        for edge, neighbor in outgoing.get(column_id, []) + incoming.get(column_id, []):
            if edge.get("type") in relationship_types and neighbor in nodes:
                relationship_targets.add(neighbor)
        for edge, table_id in incoming.get(column_id, []):
            if edge.get("type") != "contains_column":
                continue
            for relation, target_id in outgoing.get(table_id, []):
                if relation.get("type") not in relationship_types or target_id not in nodes:
                    continue
                evidence = relation.get("evidence") or {}
                source_column = str(evidence.get("source_column") or "").upper()
                if source_column and source_column == column_name:
                    relationship_targets.add(target_id)

    target_tables: set[str] = set()
    for target_id in sorted(relationship_targets):
        target = nodes.get(target_id)
        if not target or not _modules_compatible(anchor_modules, target):
            continue
        if len(members) >= max_members:
            truncated = True
            break
        members.add(target_id)
        if target.get("node_type") in {"physical_table", "physical_table_stub"}:
            target_tables.add(target_id)
        elif target.get("node_type") == "physical_column":
            for edge, table_id in incoming.get(target_id, []):
                if edge.get("type") == "contains_column" and table_id in nodes:
                    members.add(table_id)
                    target_tables.add(table_id)

    for table_id in sorted(target_tables):
        for edge, child_id in outgoing.get(table_id, []):
            if edge.get("type") != "contains_column" or child_id not in nodes:
                continue
            if not _modules_compatible(anchor_modules, nodes[child_id]):
                continue
            if len(members) >= max_members:
                truncated = True
                break
            members.add(child_id)

    for node_id in list(members):
        node_type = str(nodes[node_id].get("node_type") or "")
        local_type = "has_operation" if node_type in {"rest_resource", "adf_resource"} else "answered_by" if node_type == "otbi_subject_area" else None
        if not local_type:
            continue
        pairs = outgoing.get(node_id, [])
        if local_type == "answered_by":
            pairs = pairs + incoming.get(node_id, [])
        for edge, child_id in pairs:
            if edge.get("type") != local_type or child_id not in nodes:
                continue
            if not _modules_compatible(anchor_modules, nodes[child_id]):
                continue
            if len(members) >= max_members:
                truncated = True
                break
            members.add(child_id)

    return members, truncated


def _god_nodes(
    anchor_id: str,
    members: set[str],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in members or target not in members:
            continue
        if str(edge.get("type") or "") not in PATH_EDGE_TYPES:
            continue
        provenance = classify_provenance(edge)
        if provenance not in {"VALIDATED", "EXTRACTED"}:
            continue
        weight = 3.0 if provenance == "VALIDATED" else 1.5
        scores[source] += weight
        scores[target] += weight

    rows: list[tuple[float, str]] = []
    for node_id in members:
        node = nodes[node_id]
        score = scores.get(node_id, 0.0)
        node_type = str(node.get("node_type") or "")
        if node_id == anchor_id:
            score += 10.0
        elif node_type == "business_attribute":
            score += 4.0
        elif node_type in {"physical_table", "adf_resource", "rest_resource", "otbi_subject_area"}:
            score += 2.0
        normalized_name = str(node.get("name") or node.get("title") or "").casefold()
        if node_type in GENERIC_NODE_TYPES:
            score *= 0.35
        if any(marker in normalized_name for marker in AUDIT_MARKERS):
            score *= 0.05
        rows.append((round(score, 6), node_id))
    rows.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"node_id": node_id, "score": score, "metric": "weighted_local_degree_v2"}
        for score, node_id in rows[:5]
    ]


def build_topology_catalog(
    graph_dir: str | Path,
    output: str | Path | None = None,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    graph_dir = Path(graph_dir)
    output_path = Path(output) if output else graph_dir / "topology_catalog.json"
    nodes, edges = _load_graphs(graph_dir)
    signature = graph_signature(nodes, edges)
    current = read_json(output_path, {})
    if (
        not force
        and current.get("graph_signature") == signature
        and current.get("algorithm_version") == ALGORITHM_VERSION
    ):
        return {**current, "status": "unchanged"}

    anchors = sorted(
        node_id
        for node_id, node in nodes.items()
        if node.get("node_type") == "business_entity"
    )
    communities: list[dict[str, Any]] = []
    memberships_all: dict[str, list[str]] = defaultdict(list)
    for anchor_id in anchors:
        members, truncated = _entity_neighborhood(anchor_id, nodes, edges)
        community_id = _community_id(anchor_id)
        for node_id in sorted(members):
            memberships_all[node_id].append(community_id)
        node_type_counts = Counter(str(nodes[node_id].get("node_type") or "unknown") for node_id in members)
        layer_counts = Counter(_node_layer(nodes[node_id]) for node_id in members)
        modules = sorted({module for node_id in members for module in _node_modules(nodes[node_id])})
        communities.append(
            {
                "community_id": community_id,
                "anchor_node_id": anchor_id,
                "members": sorted(members),
                "member_count": len(members),
                "modules": modules,
                "node_type_counts": dict(sorted(node_type_counts.items())),
                "layer_counts": dict(sorted(layer_counts.items())),
                "truncated": truncated,
                "god_nodes": _god_nodes(anchor_id, members, nodes, edges),
            }
        )

    sizes = {row["community_id"]: int(row["member_count"]) for row in communities}
    anchor_communities = {row["anchor_node_id"]: row["community_id"] for row in communities}
    memberships: dict[str, str] = {}
    for node_id, community_ids in memberships_all.items():
        if node_id in anchor_communities:
            memberships[node_id] = anchor_communities[node_id]
        else:
            memberships[node_id] = min(community_ids, key=lambda value: (sizes[value], value))

    payload = {
        "schema_version": "2.0.0",
        "algorithm_version": ALGORITHM_VERSION,
        "graph_signature": signature,
        "generated_at": utc_now_iso(),
        "community_strategy": "entity_centered_structural_neighborhood",
        "community_count": len(communities),
        "communities": communities,
        "memberships": memberships,
        "memberships_all": {key: sorted(value) for key, value in sorted(memberships_all.items())},
        "anchor_communities": anchor_communities,
        "edge_count": len(edges),
        "structural_edge_count": sum(
            1
            for edge in edges
            if edge.get("type") in PATH_EDGE_TYPES
            and classify_provenance(edge) in {"VALIDATED", "EXTRACTED"}
        ),
        "unassigned_node_count": len(nodes) - len(memberships_all),
        "status": "rebuilt",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=output_path.name,
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    if progress:
        progress(
            f"[TOPOLOGY] {len(communities)} comunidades centradas em entidades persistidas em {output_path}."
        )
    return payload


class TopologyNavigator:
    def __init__(self, graph_dir: str | Path) -> None:
        self.graph_dir = Path(graph_dir)
        self.nodes, self.edges = _load_graphs(self.graph_dir)
        self.catalog = build_topology_catalog(self.graph_dir)
        self.memberships: dict[str, str] = self.catalog.get("memberships", {})
        self.memberships_all: dict[str, list[str]] = self.catalog.get("memberships_all", {})
        self._communities = {
            row["community_id"]: row for row in self.catalog.get("communities", [])
        }
        self._anchor_communities = self.catalog.get("anchor_communities", {})
        self.adjacency: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
        for edge in self.edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source in self.nodes and target in self.nodes:
                self.adjacency[source].append((edge, target))
                self.adjacency[target].append((edge, source))

    def community(self, node_id: str | None) -> dict[str, Any] | None:
        identifier = str(node_id or "")
        community_id = self._anchor_communities.get(identifier) or self.memberships.get(identifier)
        return self._communities.get(community_id)

    def community_by_id(self, community_id: str | None) -> dict[str, Any] | None:
        return self._communities.get(str(community_id or ""))

    def contains(self, community_id: str | None, node_id: str | None) -> bool:
        if not community_id or not node_id:
            return False
        return str(community_id) in set(self.memberships_all.get(str(node_id), []))

    def community_members(self, community_id: str | None) -> set[str]:
        community = self.community_by_id(community_id)
        return set(community.get("members", [])) if community else set()

    def compact_community(self, community: dict[str, Any] | None) -> dict[str, Any]:
        if not community:
            return {}
        return {
            "community_id": community.get("community_id"),
            "strategy": self.catalog.get("community_strategy"),
            "anchor_node_id": community.get("anchor_node_id"),
            "member_count": community.get("member_count"),
            "modules": community.get("modules", []),
            "node_type_counts": community.get("node_type_counts", {}),
            "layer_counts": community.get("layer_counts", {}),
            "truncated": bool(community.get("truncated")),
        }

    def linked_targets(
        self,
        node_id: str,
        edge_types: set[str],
        *,
        community_id: str | None = None,
    ) -> list[str]:
        result: list[str] = []
        for edge, neighbor in self.adjacency.get(node_id, []):
            if str(edge.get("type") or "") not in edge_types:
                continue
            if classify_provenance(edge) not in {"VALIDATED", "EXTRACTED"}:
                continue
            if community_id and not self.contains(community_id, neighbor):
                continue
            result.append(neighbor)
        return sorted(set(result))

    def edge_between(
        self,
        source: str,
        target: str,
        edge_types: set[str] | None = None,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for edge, neighbor in self.adjacency.get(source, []):
            if neighbor != target:
                continue
            if edge_types and str(edge.get("type") or "") not in edge_types:
                continue
            if classify_provenance(edge) not in {"VALIDATED", "EXTRACTED"}:
                continue
            candidates.append(edge)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda edge: (
                PROVENANCE_PRIORITY[classify_provenance(edge)],
                str(edge.get("type") or ""),
            ),
        )

    def physical_owner_table(
        self,
        column_id: str,
        *,
        community_id: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        column = self.nodes.get(column_id, {})
        if str(column.get("node_type") or "") != "physical_column":
            return None
        qualified = str(
            column.get("qualified_name")
            or column.get("title")
            or column.get("name")
            or ""
        )
        table_name = str(column.get("table_name") or "")
        if not table_name and "." in qualified:
            table_name = qualified.rsplit(".", 1)[0]
        table_name = table_name.upper()
        if not table_name:
            return None

        candidates: list[tuple[str, dict[str, Any]]] = []
        for node_id, node in self.nodes.items():
            if str(node.get("node_type") or "") not in {
                "physical_table",
                "physical_table_stub",
            }:
                continue
            if community_id and not self.contains(community_id, node_id):
                continue
            identifiers = {
                str(node.get("title") or "").upper(),
                str(node.get("name") or "").upper(),
                str(node.get("qualified_name") or "").upper(),
            }
            if table_name not in identifiers:
                continue
            edge = self.edge_between(
                node_id,
                column_id,
                {"contains_column", "has_column"},
            )
            if edge is not None:
                candidates.append((node_id, edge))
        return min(candidates, key=lambda item: item[0]) if candidates else None

    def path(
        self,
        start: str,
        target: str,
        *,
        max_depth: int = 6,
        allowed_community: str | None = None,
        allowed_provenance: set[str] | None = None,
    ) -> dict[str, Any] | None:
        if start == target:
            return {"nodes": [start], "edges": [], "complete": True}
        allowed_provenance = allowed_provenance or {"VALIDATED", "EXTRACTED"}
        if allowed_community and (
            not self.contains(allowed_community, start)
            or not self.contains(allowed_community, target)
        ):
            return None
        queue = deque([(start, [start], [])])
        visited = {start}
        while queue:
            current, node_path, edge_path = queue.popleft()
            if len(edge_path) >= max_depth:
                continue
            ranked = sorted(
                self.adjacency.get(current, []),
                key=lambda item: (
                    PROVENANCE_PRIORITY[classify_provenance(item[0])],
                    str(item[0].get("type")),
                    item[1],
                ),
            )
            for edge, neighbor in ranked:
                provenance = classify_provenance(edge)
                if provenance not in allowed_provenance:
                    continue
                if str(edge.get("type") or "") not in PATH_EDGE_TYPES:
                    continue
                if allowed_community and not self.contains(allowed_community, neighbor):
                    continue
                if neighbor in visited:
                    continue
                next_nodes = node_path + [neighbor]
                next_edges = edge_path + [edge]
                if neighbor == target:
                    return {"nodes": next_nodes, "edges": next_edges, "complete": True}
                visited.add(neighbor)
                queue.append((neighbor, next_nodes, next_edges))
        return None
