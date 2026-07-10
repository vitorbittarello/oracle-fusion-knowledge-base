from __future__ import annotations

import json
from pathlib import Path

from oracle_knowledge.query_planning import decompose_query, diagnose_query_encoding
from oracle_knowledge.topology import TopologyNavigator, build_topology_catalog, classify_provenance


def _write_graph(path: Path, nodes, edges):
    path.write_text(json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False), encoding="utf-8")


def _bundle(tmp_path: Path) -> Path:
    nodes = [
        {"id": "entity:purchase_agreement", "node_type": "business_entity", "title": "Purchase Agreement", "module_id": "procurement"},
        {"id": "attribute:agreement_number", "node_type": "business_attribute", "title": "Agreement Number", "module_id": "procurement"},
        {"id": "table:PO_HEADERS_ALL", "node_type": "physical_table", "title": "PO_HEADERS_ALL", "module_id": "procurement"},
        {"id": "column:PO_HEADERS_ALL.SEGMENT1", "node_type": "physical_column", "title": "PO_HEADERS_ALL.SEGMENT1", "name": "Agreement Number", "module_id": "procurement"},
        {"id": "rule:ppm_budget", "node_type": "validated_rule", "title": "Approved Budget", "module_id": "ppm"},
        {"id": "rest:notifications", "node_type": "rest_resource", "title": "Notifications", "module_id": "common"},
    ]
    edges = [
        {"source": "entity:purchase_agreement", "target": "attribute:agreement_number", "type": "has_attribute", "provenance": "VALIDATED"},
        {"source": "entity:purchase_agreement", "target": "table:PO_HEADERS_ALL", "type": "mapped_to_table", "provenance": "EXTRACTED"},
        {"source": "table:PO_HEADERS_ALL", "target": "column:PO_HEADERS_ALL.SEGMENT1", "type": "contains_column", "provenance": "EXTRACTED"},
    ]
    _write_graph(tmp_path / "business.json", nodes[:2] + nodes[4:5], edges[:1])
    _write_graph(tmp_path / "physical.json", nodes[2:4], edges[1:])
    _write_graph(tmp_path / "rest.json", nodes[5:], [])
    _write_graph(tmp_path / "otbi_analytics.json", [], [])
    _write_graph(tmp_path / "otbi_security.json", [], [])
    _write_graph(tmp_path / "master_graph.json", nodes, edges)
    return tmp_path


def test_mojibake_high_confidence_and_utf8_preservation():
    fixed = diagnose_query_encoding("AquisiþÒo e DescriþÒo; PO_HEADERS_ALL.SEGMENT1")
    assert fixed["encoding_status"] == "corrected"
    assert fixed["normalized_query"] == "Aquisição e Descrição; PO_HEADERS_ALL.SEGMENT1"
    valid = diagnose_query_encoding("Aquisição e descrição")
    assert valid["encoding_status"] == "valid"
    assert valid["normalized_query"] == "Aquisição e descrição"


def test_query_decomposition_keeps_entity_attributes_and_grain():
    nodes = [
        {"id": "e", "node_type": "business_entity", "title": "Purchase Agreement"},
        {"id": "a", "node_type": "business_attribute", "title": "Agreement Number", "name": "agreement_number"},
    ]
    plan = decompose_query("Purchase Agreement com Agreement Number, uma linha por acordo", nodes)
    assert plan["primary_entity_id"] == "e"
    assert "agreement_number" in plan["requested_attributes"]
    assert plan["requested_grain"].startswith("one_row_per_")


def test_communities_are_deterministic_idempotent_and_separate_modules(tmp_path):
    graph_dir = _bundle(tmp_path)
    first = build_topology_catalog(graph_dir)
    second = build_topology_catalog(graph_dir)
    assert first["graph_signature"] == second["graph_signature"]
    assert second["status"] == "unchanged"
    assert first["memberships"]["entity:purchase_agreement"]
    assert "rule:ppm_budget" not in first["memberships"]
    assert first["unassigned_node_count"] >= 2


def test_god_nodes_are_local_and_structural_paths_have_provenance(tmp_path):
    navigator = TopologyNavigator(_bundle(tmp_path))
    community = navigator.community("entity:purchase_agreement")
    assert community and community["god_nodes"]
    path = navigator.path("entity:purchase_agreement", "column:PO_HEADERS_ALL.SEGMENT1", allowed_community=community["community_id"])
    assert path and path["complete"]
    assert [classify_provenance(edge) for edge in path["edges"]] == ["EXTRACTED", "EXTRACTED"]


def test_disconnected_rest_and_ppm_candidates_have_no_path(tmp_path):
    navigator = TopologyNavigator(_bundle(tmp_path))
    community = navigator.community("entity:purchase_agreement")
    assert navigator.path("entity:purchase_agreement", "rest:notifications", allowed_community=community["community_id"]) is None
    assert navigator.path("entity:purchase_agreement", "rule:ppm_budget", allowed_community=community["community_id"]) is None


def test_validated_priority_precedes_extracted_and_inferred():
    edges = [
        {"provenance": "INFERRED"},
        {"provenance": "EXTRACTED"},
        {"provenance": "VALIDATED"},
    ]
    priority = {"VALIDATED": 0, "EXTRACTED": 1, "INFERRED": 2, "AMBIGUOUS": 3}
    assert [classify_provenance(edge) for edge in sorted(edges, key=lambda edge: priority[classify_provenance(edge)])] == ["VALIDATED", "EXTRACTED", "INFERRED"]


def test_incremental_change_updates_signature(tmp_path):
    graph_dir = _bundle(tmp_path)
    first = build_topology_catalog(graph_dir)
    graph = json.loads((graph_dir / "rest.json").read_text(encoding="utf-8"))
    graph["nodes"].append({"id": "rest:purchase", "node_type": "rest_resource", "title": "Purchase Agreements"})
    (graph_dir / "rest.json").write_text(json.dumps(graph), encoding="utf-8")
    second = build_topology_catalog(graph_dir)
    assert second["status"] == "rebuilt"
    assert second["graph_signature"] != first["graph_signature"]


def test_cp850_windows_corruption_is_repaired_as_a_single_high_confidence_step():
    diagnostics = diagnose_query_encoding(
        "AquisiþÒo, DescriþÒo, Condiþ§es de Pagamento, NÒo e inferÛncia"
    )
    assert diagnostics["encoding_status"] == "corrected"
    assert diagnostics["normalized_query"] == (
        "Aquisição, Descrição, Condições de Pagamento, Não e inferência"
    )
    assert diagnostics["corrections"][0]["method"] == "cp850_bytes_to_cp1252"


def test_explicit_field_list_does_not_promote_unrequested_physical_columns():
    nodes = [
        {
            "id": "entity:purchase_agreement",
            "node_type": "business_entity",
            "entity_id": "purchase_agreement",
            "title": "Purchase Agreement",
            "aliases": ["acordo de compra", "gerenciar acordo"],
            "module_id": "procurement",
        },
        {
            "id": "attribute:agreement_number",
            "node_type": "business_attribute",
            "entity_id": "purchase_agreement",
            "attribute_id": "agreement_number",
            "name": "Agreement Number",
            "aliases": ["acordo"],
        },
        {
            "id": "attribute:supplier",
            "node_type": "business_attribute",
            "entity_id": "purchase_agreement",
            "attribute_id": "supplier",
            "name": "Supplier",
            "aliases": ["fornecedor"],
        },
        {
            "id": "column:noise",
            "node_type": "physical_column",
            "title": "CMR_AP_INVOICE_DTLS.ACCOUNTING_DATE",
            "name": "ACCOUNTING_DATE",
        },
    ]
    plan = decompose_query(
        "Gerenciar Acordo, com uma linha por acordo e os campos: "
        "Acordo, Fornecedor, Valor de Acordo, Status e Data Final.",
        nodes,
    )
    assert plan["primary_entity"] == "purchase_agreement"
    assert plan["requested_grain"] == "one_row_per_agreement"
    assert plan["requested_attributes"] == [
        "agreement_number",
        "supplier",
        "valor_de_acordo",
        "status",
        "data_final",
    ]
    assert "ACCOUNTING_DATE" not in plan["requested_attributes"]


def test_module_membership_edges_do_not_create_a_giant_cross_domain_community(tmp_path):
    graph_dir = _bundle(tmp_path)
    business = json.loads((graph_dir / "business.json").read_text(encoding="utf-8"))
    business["nodes"].append(
        {
            "id": "module:shared",
            "node_type": "functional_section",
            "title": "Shared Module Hub",
        }
    )
    business["edges"].extend(
        [
            {
                "source": "entity:purchase_agreement",
                "target": "module:shared",
                "type": "belongs_to_module",
                "provenance": "EXTRACTED",
            },
            {
                "source": "rule:ppm_budget",
                "target": "module:shared",
                "type": "belongs_to_module",
                "provenance": "EXTRACTED",
            },
        ]
    )
    (graph_dir / "business.json").write_text(
        json.dumps(business, ensure_ascii=False), encoding="utf-8"
    )
    master = json.loads((graph_dir / "master_graph.json").read_text(encoding="utf-8"))
    master["nodes"].append(business["nodes"][-1])
    master["edges"].extend(business["edges"][-2:])
    (graph_dir / "master_graph.json").write_text(
        json.dumps(master, ensure_ascii=False), encoding="utf-8"
    )

    navigator = TopologyNavigator(graph_dir)
    community = navigator.community("entity:purchase_agreement")
    assert community
    assert "rule:ppm_budget" not in community["members"]
    assert community["member_count"] < 20
    compact = navigator.compact_community(community)
    assert "members" not in compact
    assert compact["strategy"] == "entity_centered_structural_neighborhood"


def test_mojibake_repair_survives_unrelated_unicode_characters():
    diagnostics = diagnose_query_encoding(
        "🔎 AquisiþÒo, DescriþÒo, Condiþ§es de Pagamento e NÒo invente"
    )
    assert diagnostics["encoding_status"] == "corrected"
    assert diagnostics["normalized_query"] == (
        "🔎 Aquisição, Descrição, Condições de Pagamento e Não invente"
    )


def test_physical_owner_table_is_explicit_and_provenanced(tmp_path):
    navigator = TopologyNavigator(_bundle(tmp_path))
    community = navigator.community("entity:purchase_agreement")
    owner = navigator.physical_owner_table(
        "column:PO_HEADERS_ALL.SEGMENT1",
        community_id=community["community_id"],
    )
    assert owner is not None
    table_id, edge = owner
    assert table_id == "table:PO_HEADERS_ALL"
    assert edge["type"] == "contains_column"
    assert classify_provenance(edge) == "EXTRACTED"
