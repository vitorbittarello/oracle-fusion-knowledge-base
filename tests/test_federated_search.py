from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from oracle_knowledge.search.federated_search import FederatedGraphSearch
from oracle_knowledge.search.semantic_context import SemanticContextConfig, SemanticTextSelector


class FederatedEmbeddingModel:
    def encode(self, texts, **kwargs):
        if texts and texts[0].startswith("Instruct:"):
            return np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32)
        vectors = []
        for text in texts:
            normalized = text.casefold()
            if any(term in normalized for term in ("item number", "inventory item", "work order")):
                vectors.append([1.0, 0.0, 0.0])
            elif "description" in normalized:
                vectors.append([0.8, 0.2, 0.0])
            else:
                vectors.append([0.05, 0.95, 0.0])
        return np.asarray(vectors, dtype=np.float32)


def write_graph(path: Path, layer: str, nodes, edges, layers=None):
    payload = {
        "version": "3.0.0",
        "graph_layer": layer,
        "nodes": nodes,
        "edges": edges,
        "sources": [],
        "stats": {"nodes": len(nodes), "edges": len(edges)},
    }
    if layers:
        payload["layers"] = layers
    path.write_text(json.dumps(payload), encoding="utf-8")


class FederatedGraphSearchTest(unittest.TestCase):
    def selector(self):
        return SemanticTextSelector(
            SemanticContextConfig(summary_max_characters=300),
            model=FederatedEmbeddingModel(),
        )

    def test_master_routes_explicit_targets_without_layer_lexical_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entity = {
                "id": "entity:item",
                "node_type": "business_entity",
                "title": "Inventory Item",
                "aliases": ["item de estoque"],
                "search_text": "inventory item item de estoque",
                "graph_layer": "business",
                "modules": ["scm"],
                "source": {"source_type": "curated_entity_map"},
            }
            attribute = {
                "id": "attribute:item-number",
                "node_type": "business_attribute",
                "title": "Item Number",
                "aliases": ["número do item"],
                "search_text": "item number número do item",
                "graph_layer": "business",
                "modules": ["scm"],
                "source": {"source_type": "curated_entity_map"},
            }
            table = {
                "id": "table:items",
                "node_type": "physical_table",
                "title": "EGP_SYSTEM_ITEMS_B",
                "name": "EGP_SYSTEM_ITEMS_B",
                "description": "Stores inventory items.",
                "search_text": "inventory items",
                "graph_layer": "physical",
                "modules": ["scm"],
                "source": {"source_type": "oracle_data_dictionary"},
            }
            column = {
                "id": "column:item-number",
                "node_type": "physical_column",
                "title": "EGP_SYSTEM_ITEMS_B.ITEM_NUMBER",
                "name": "ITEM_NUMBER",
                "table_name": "EGP_SYSTEM_ITEMS_B",
                "qualified_name": "EGP_SYSTEM_ITEMS_B.ITEM_NUMBER",
                "description": "Item number.",
                "search_text": "item number",
                "graph_layer": "physical",
                "modules": ["scm"],
                "source": {"source_type": "oracle_data_dictionary"},
            }
            subject = {
                "id": "subject:item",
                "node_type": "otbi_subject_area",
                "title": "Product Management - Item Real Time",
                "description": "Real-time information about inventory items.",
                "search_text": "inventory item real time",
                "graph_layer": "otbi_analytics",
                "modules": ["scm"],
                "source": {"source_type": "oracle_otbi_documentation"},
            }
            master_edges = [
                {"source": entity["id"], "target": attribute["id"], "type": "has_attribute", "weight": 1.0},
                {"source": entity["id"], "target": table["id"], "type": "mapped_to_entity", "weight": 1.0},
                {"source": entity["id"], "target": subject["id"], "type": "mapped_to_entity", "weight": 1.0},
                {"source": attribute["id"], "target": column["id"], "type": "mapped_to_attribute", "weight": 1.0},
            ]
            layers = {"physical": "physical.json", "otbi_analytics": "otbi_analytics.json", "rest": "rest.json", "business": "business.json"}
            write_graph(root / "master_graph.json", "master", [entity, attribute, table, column, subject], master_edges, layers)
            write_graph(root / "physical.json", "physical", [table, column], [{"source": table["id"], "target": column["id"], "type": "contains_column", "weight": 1.0}])
            write_graph(root / "otbi_analytics.json", "otbi_analytics", [subject], [])
            write_graph(root / "rest.json", "rest", [], [])
            write_graph(root / "business.json", "business", [entity, attribute], [{"source": entity["id"], "target": attribute["id"], "type": "has_attribute", "weight": 1.0}])

            search = FederatedGraphSearch(root, semantic_text_selector=self.selector())
            payload = search.build_prompt_context(
                "item de estoque número do item",
                module_ids={"scm"},
                limit=20,
                max_characters=14000,
            )
            identifiers = {row["id"] for row in payload["results"]}
            self.assertIn("entity:item", identifiers)
            self.assertIn("attribute:item-number", identifiers)
            self.assertIn("table:items", identifiers)
            self.assertIn("column:item-number", identifiers)
            self.assertIn("subject:item", identifiers)
            self.assertEqual(payload["routing"]["semantic_fallback_roots"], [])

    def test_semantic_fallback_routes_uncurated_module_by_layer_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = {
                "id": "entity:unrelated",
                "node_type": "business_entity",
                "title": "Unrelated",
                "search_text": "unrelated concept",
                "graph_layer": "business",
                "modules": ["scm"],
                "source": {"source_type": "curated_entity_map"},
            }
            work_order = {
                "id": "table:work-orders",
                "node_type": "physical_table",
                "title": "WIE_WORK_ORDERS_B",
                "name": "WIE_WORK_ORDERS_B",
                "description": "Stores manufacturing work orders.",
                "search_text": "manufacturing work order",
                "graph_layer": "physical",
                "modules": ["scm"],
                "source": {"source_type": "oracle_data_dictionary"},
            }
            work_order_number = {
                "id": "column:work-order-number",
                "node_type": "physical_column",
                "title": "WIE_WORK_ORDERS_B.WORK_ORDER_NUMBER",
                "name": "WORK_ORDER_NUMBER",
                "table_name": "WIE_WORK_ORDERS_B",
                "description": "Work order number.",
                "search_text": "work order number",
                "graph_layer": "physical",
                "modules": ["scm"],
                "source": {"source_type": "oracle_data_dictionary"},
            }
            noise = {
                "id": "table:noise",
                "node_type": "physical_table",
                "title": "CST_AUDIT_LOG",
                "name": "CST_AUDIT_LOG",
                "description": "Technical audit information.",
                "search_text": "technical audit",
                "graph_layer": "physical",
                "modules": ["scm"],
                "source": {"source_type": "oracle_data_dictionary"},
            }
            layers = {"physical": "physical.json", "otbi_analytics": "otbi_analytics.json", "rest": "rest.json", "business": "business.json"}
            write_graph(root / "master_graph.json", "master", [], [], layers)
            write_graph(root / "physical.json", "physical", [work_order, work_order_number, noise], [{"source": work_order["id"], "target": work_order_number["id"], "type": "contains_column", "weight": 1.0}])
            write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
            write_graph(root / "rest.json", "rest", [], [])
            write_graph(root / "business.json", "business", [unrelated], [])

            search = FederatedGraphSearch(root, semantic_text_selector=self.selector())
            payload = search.build_prompt_context(
                "ordem de produção",
                module_ids={"scm"},
                limit=10,
                max_characters=8000,
            )
            identifiers = {row["id"] for row in payload["results"]}
            self.assertIn("table:work-orders", identifiers)
            self.assertIn("column:work-order-number", identifiers)
            self.assertTrue(payload["routing"]["semantic_fallback_roots"])


if __name__ == "__main__":
    unittest.main()
