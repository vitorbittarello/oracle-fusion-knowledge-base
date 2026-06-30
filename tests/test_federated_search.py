from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from oracle_knowledge.indexing import build_index_bundle, build_search_index
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.search.federated_search import (
    FederatedGraphSearch,
    FederatedSearchConfig,
)
from oracle_knowledge.search.semantic_context import SemanticContextConfig, SemanticTextSelector


class FederatedEmbeddingModel:
    def __init__(self):
        self.query_calls = 0
        self.document_calls = 0
        self.document_text_count = 0

    def encode(self, texts, **kwargs):
        if texts and texts[0].startswith("Instruct:"):
            self.query_calls += 1
            return np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32)
        self.document_calls += 1
        self.document_text_count += len(texts)
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


def write_bundle_manifest(root: Path):
    stats = {}
    for layer, filename in GRAPH_FILENAMES.items():
        payload = json.loads((root / filename).read_text(encoding="utf-8"))
        stats[layer] = payload["stats"]
    (root / "graph_bundle.json").write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "generated_at": "2026-06-27T12:00:00+00:00",
                "graphs": {
                    layer: filename
                    for layer, filename in GRAPH_FILENAMES.items()
                },
                "stats": stats,
            }
        ),
        encoding="utf-8",
    )


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

    def test_uses_sqlite_index_for_semantic_roots_and_local_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            layers = {
                "physical": "physical.json",
                "otbi_analytics": "otbi_analytics.json",
                "rest": "rest.json",
                "business": "business.json",
            }
            write_graph(root / "master_graph.json", "master", [], [], layers)
            write_graph(
                root / "physical.json",
                "physical",
                [work_order, work_order_number, noise],
                [
                    {
                        "source": work_order["id"],
                        "target": work_order_number["id"],
                        "type": "contains_column",
                        "weight": 1.0,
                    }
                ],
            )
            write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
            write_graph(root / "otbi_security.json", "otbi_security", [], [])
            write_graph(root / "rest.json", "rest", [], [])
            write_graph(root / "business.json", "business", [], [])
            write_bundle_manifest(root)

            selector = self.selector()
            build_index_bundle(
                root,
                include_semantic_embeddings=True,
                semantic_text_selector=selector,
                semantic_batch_size=2,
            )
            model = selector._model
            model.query_calls = 0
            model.document_calls = 0

            progress_messages = []

            with FederatedGraphSearch(
                root,
                config=FederatedSearchConfig(fallback_roots_per_layer=1),
                semantic_text_selector=selector,
                require_index=True,
                progress=progress_messages.append,
            ) as search:
                payload = search.build_prompt_context(
                    "ordem de produção",
                    module_ids={"scm"},
                    limit=10,
                    max_characters=8000,
                )

            identifiers = {row["id"] for row in payload["results"]}
            self.assertEqual(payload["routing"]["backend"], "sqlite_bundle")
            self.assertIn("physical", payload["routing"]["index_paths"])
            self.assertIn("table:work-orders", identifiers)
            self.assertIn("column:work-order-number", identifiers)
            self.assertNotIn("table:noise", identifiers)
            self.assertTrue(payload["routing"]["semantic_fallback_roots"])
            self.assertEqual(model.query_calls, 1)
            self.assertEqual(model.document_calls, 0)
            physical_diagnostics = payload["routing"][
                "semantic_inference_diagnostics"
            ]["physical"]
            self.assertEqual(
                physical_diagnostics["persisted_candidates"],
                1,
            )
            self.assertEqual(physical_diagnostics["live_candidates"], 0)
            self.assertIn("timings_seconds", payload["routing"])
            self.assertIn("total", payload["routing"]["timings_seconds"])
            self.assertTrue(
                any(message.startswith("[SEARCH]") for message in progress_messages)
            )

    def test_rest_expansion_prefilters_large_operation_fanout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = {
                "id": "rest-resource:inventory-balances",
                "node_type": "rest_resource",
                "title": "Inventory On-Hand Balances",
                "name": "Inventory On-Hand Balances",
                "search_text": "inventory on hand balances subinventory quantities",
                "graph_layer": "rest",
                "modules": ["scm"],
                "source": {"source_type": "oracle_rest_documentation"},
            }
            operations = []
            edges = []
            for index in range(200):
                relevant = index == 173
                operation = {
                    "id": f"rest-operation:{index}",
                    "node_type": "rest_operation",
                    "title": (
                        "Get inventory on-hand balances by subinventory"
                        if relevant
                        else f"Unrelated operation {index:03d}"
                    ),
                    "method": "GET",
                    "endpoint_path": (
                        "/fscmRestApi/resources/inventoryOnhandBalances"
                        if relevant
                        else f"/fscmRestApi/resources/unrelated/{index}"
                    ),
                    "description": (
                        "Returns inventory quantities by subinventory."
                        if relevant
                        else "Technical unrelated operation."
                    ),
                    "resource_hierarchy": ["Inventory On-Hand Balances"],
                    "search_text": (
                        "inventory on hand balances subinventory quantities"
                        if relevant
                        else f"technical unrelated operation {index}"
                    ),
                    "graph_layer": "rest",
                    "modules": ["scm"],
                    "source": {"source_type": "oracle_rest_documentation"},
                }
                operations.append(operation)
                edges.append(
                    {
                        "source": resource["id"],
                        "target": operation["id"],
                        "type": "has_operation",
                        "weight": 0.95,
                    }
                )

            layers = {
                "physical": "physical.json",
                "otbi_analytics": "otbi_analytics.json",
                "rest": "rest.json",
                "business": "business.json",
            }
            write_graph(root / "master_graph.json", "master", [], [], layers)
            write_graph(root / "physical.json", "physical", [], [])
            write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
            write_graph(root / "otbi_security.json", "otbi_security", [], [])
            write_graph(root / "rest.json", "rest", [resource, *operations], edges)
            write_graph(root / "business.json", "business", [], [])
            write_bundle_manifest(root)

            selector = self.selector()
            build_search_index(
                root,
                include_semantic_embeddings=True,
                semantic_text_selector=selector,
                semantic_batch_size=8,
            )
            model = selector._model
            model.query_calls = 0
            model.document_calls = 0
            model.document_text_count = 0

            with FederatedGraphSearch(
                root,
                config=FederatedSearchConfig(
                    fallback_roots_per_layer=1,
                    local_operations_per_resource=3,
                    local_operation_candidates_per_resource=16,
                ),
                semantic_text_selector=selector,
                require_index=True,
            ) as search:
                payload = search.build_prompt_context(
                    "inventory subinventory quantities",
                    module_ids={"scm"},
                    limit=10,
                    max_characters=8000,
                )

            identifiers = {row["id"] for row in payload["results"]}
            diagnostics = payload["routing"]["rest_operation_diagnostics"]
            self.assertIn("rest-operation:173", identifiers)
            self.assertEqual(diagnostics["linked_operation_count"], 200)
            self.assertLessEqual(diagnostics["semantic_candidate_count"], 16)
            self.assertGreater(diagnostics["fts_candidate_count"], 0)
            self.assertEqual(model.document_text_count, 0)
            semantic_diagnostics = payload["routing"][
                "semantic_inference_diagnostics"
            ]["rest"]
            self.assertEqual(semantic_diagnostics["live_candidates"], 0)
            self.assertEqual(
                semantic_diagnostics["persisted_candidates"],
                diagnostics["semantic_candidate_count"],
            )



if __name__ == "__main__":
    unittest.main()
