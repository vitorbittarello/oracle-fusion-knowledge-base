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


def test_structural_gating_filters_disconnected_business_rule_before_context_rendering():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        entity = {
            "id": "entity:purchase-agreement",
            "node_type": "business_entity",
            "entity_id": "purchase_agreement",
            "title": "Purchase Agreement",
            "aliases": ["gerenciar acordo", "acordo de compra"],
            "search_text": "purchase agreement gerenciar acordo acordo de compra",
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "curated_entity_map"},
        }
        attribute = {
            "id": "attribute:agreement-number",
            "node_type": "business_attribute",
            "entity_id": "purchase_agreement",
            "attribute_id": "agreement_number",
            "title": "Agreement Number",
            "name": "Agreement Number",
            "aliases": ["acordo"],
            "search_text": "agreement number acordo",
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "curated_entity_map"},
        }
        disconnected_rule = {
            "id": "rule:ppm-budget",
            "node_type": "validated_rule",
            "title": "Approved Budget PPM_RULE_SENTINEL",
            "search_text": "approved budget project version",
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "ppm",
            "source": {"source_type": "validated_environment_rule"},
        }
        table = {
            "id": "table:po-headers",
            "node_type": "physical_table",
            "title": "PO_HEADERS_ALL",
            "name": "PO_HEADERS_ALL",
            "search_text": "purchase agreement header",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "oracle_data_dictionary"},
        }
        column = {
            "id": "column:segment1",
            "node_type": "physical_column",
            "title": "PO_HEADERS_ALL.SEGMENT1",
            "name": "SEGMENT1",
            "qualified_name": "PO_HEADERS_ALL.SEGMENT1",
            "table_name": "PO_HEADERS_ALL",
            "search_text": "agreement number",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "oracle_data_dictionary"},
        }
        business_edges = [
            {
                "source": entity["id"],
                "target": attribute["id"],
                "type": "has_attribute",
                "provenance": "VALIDATED",
            }
        ]
        physical_edges = [
            {
                "source": table["id"],
                "target": column["id"],
                "type": "contains_column",
                "provenance": "EXTRACTED",
            }
        ]
        master_edges = business_edges + physical_edges + [
            {
                "source": entity["id"],
                "target": table["id"],
                "type": "mapped_to_entity",
                "provenance": "EXTRACTED",
            },
            {
                "source": attribute["id"],
                "target": column["id"],
                "type": "mapped_to_attribute",
                "provenance": "EXTRACTED",
            },
        ]
        write_graph(root / "business.json", "business", [entity, attribute, disconnected_rule], business_edges)
        write_graph(root / "physical.json", "physical", [table, column], physical_edges)
        write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
        write_graph(root / "otbi_security.json", "otbi_security", [], [])
        write_graph(root / "rest.json", "rest", [], [])
        write_graph(
            root / "master_graph.json",
            "master",
            [entity, attribute, disconnected_rule, table, column],
            master_edges,
            {
                "business": "business.json",
                "physical": "physical.json",
                "otbi_analytics": "otbi_analytics.json",
                "rest": "rest.json",
            },
        )

        selector = SemanticTextSelector(
            SemanticContextConfig(summary_max_characters=300),
            model=FederatedEmbeddingModel(),
        )
        with FederatedGraphSearch(
            root,
            semantic_text_selector=selector,
            use_index=False,
        ) as search:
            payload = search.build_prompt_context(
                "Gerenciar Acordo e approved budget, com uma linha por acordo e "
                "os campos: Acordo. NÒo invente tabelas.",
                limit=10,
                max_characters=8000,
            )

        result_ids = {row["id"] for row in payload["results"]}
        assert "entity:purchase-agreement" in result_ids
        assert "column:segment1" in result_ids
        assert "rule:ppm-budget" not in result_ids
        assert "PPM_RULE_SENTINEL" not in payload["context"]
        assert any(
            row["node_id"] == "rule:ppm-budget"
            and row["reason"] == "no_structural_path"
            for row in payload["rejected_candidates"]
        )
        assert payload["query_diagnostics"]["encoding_status"] == "corrected"
        assert payload["query_plan"]["requested_attributes"] == ["agreement_number"]
        assert payload["routing"]["semantic_scope"] == "active_community"
        assert payload["routing"]["embedding_recalculated"] is False
        assert "members" not in payload["community"]


class AttributeEmbeddingModel:
    """Deterministic multilingual vectors for local attribute-routing tests."""

    dimensions = 6

    @staticmethod
    def _vector(text: str):
        normalized = text.casefold()
        vector = np.zeros(AttributeEmbeddingModel.dimensions, dtype=np.float32)
        mappings = (
            (("descrição", "description", "comments"), 0),
            (("moeda", "currency"), 1),
            (("status", "document status"), 2),
            (("valor de acordo", "blanket total amount"), 3),
            (("data inicial", "start date"), 4),
            (("data final", "end date"), 5),
        )
        for terms, index in mappings:
            if any(term in normalized for term in terms):
                vector[index] = 1.0
                return vector
        vector[0] = 0.1
        vector[1] = 0.1
        return vector

    def encode(self, texts, **kwargs):
        return np.asarray([self._vector(text) for text in texts], dtype=np.float32)


def test_local_persisted_semantic_fallback_returns_suggestions_without_false_resolution():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        entity = {
            "id": "entity:purchase-agreement",
            "node_type": "business_entity",
            "entity_id": "purchase_agreement",
            "title": "Purchase Agreement",
            "aliases": ["gerenciar acordo", "acordo de compra"],
            "search_text": "purchase agreement gerenciar acordo",
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "curated_entity_map"},
        }
        header = {
            "id": "table:po-headers",
            "node_type": "physical_table",
            "title": "PO_HEADERS_ALL",
            "name": "PO_HEADERS_ALL",
            "result_grain": {"grain_columns": ["PO_HEADER_ID"]},
            "search_text": "purchase agreement header",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "oracle_data_dictionary"},
        }
        line = {
            "id": "table:po-lines",
            "node_type": "physical_table",
            "title": "PO_LINES_ALL",
            "name": "PO_LINES_ALL",
            "result_grain": {"grain_columns": ["PO_LINE_ID"]},
            "search_text": "purchase agreement lines",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
            "source": {"source_type": "oracle_data_dictionary"},
        }
        columns = [
            {
                "id": "column:comments",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.COMMENTS",
                "name": "COMMENTS",
                "qualified_name": "PO_HEADERS_ALL.COMMENTS",
                "table_name": "PO_HEADERS_ALL",
                "description": "Description and comments for the agreement.",
                "search_text": "description comments agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "source": {"source_type": "oracle_data_dictionary"},
            },
            {
                "id": "column:currency",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.CURRENCY_CODE",
                "name": "CURRENCY_CODE",
                "qualified_name": "PO_HEADERS_ALL.CURRENCY_CODE",
                "table_name": "PO_HEADERS_ALL",
                "description": "Currency code of the agreement.",
                "search_text": "currency code agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "source": {"source_type": "oracle_data_dictionary"},
            },
            {
                "id": "column:status",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.DOCUMENT_STATUS",
                "name": "DOCUMENT_STATUS",
                "qualified_name": "PO_HEADERS_ALL.DOCUMENT_STATUS",
                "table_name": "PO_HEADERS_ALL",
                "description": "Document status of the agreement.",
                "search_text": "document status agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "source": {"source_type": "oracle_data_dictionary"},
            },
            {
                "id": "column:line-status",
                "node_type": "physical_column",
                "title": "PO_LINES_ALL.LINE_STATUS",
                "name": "LINE_STATUS",
                "qualified_name": "PO_LINES_ALL.LINE_STATUS",
                "table_name": "PO_LINES_ALL",
                "description": "Status of an agreement line.",
                "search_text": "line status agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "source": {"source_type": "oracle_data_dictionary"},
            },
        ]
        physical_edges = [
            {
                "source": header["id"],
                "target": column["id"],
                "type": "contains_column",
                "provenance": "EXTRACTED",
            }
            for column in columns[:3]
        ] + [
            {
                "source": line["id"],
                "target": columns[3]["id"],
                "type": "contains_column",
                "provenance": "EXTRACTED",
            }
        ]
        master_edges = [
            {
                "source": entity["id"],
                "target": header["id"],
                "type": "mapped_to_entity",
                "provenance": "EXTRACTED",
            },
            {
                "source": entity["id"],
                "target": line["id"],
                "type": "mapped_to_entity",
                "provenance": "EXTRACTED",
            },
            *physical_edges,
        ]
        layers = {
            "business": "business.json",
            "physical": "physical.json",
            "otbi_analytics": "otbi_analytics.json",
            "otbi_security": "otbi_security.json",
            "rest": "rest.json",
        }
        write_graph(root / "business.json", "business", [entity], [])
        write_graph(root / "physical.json", "physical", [header, line, *columns], physical_edges)
        write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
        write_graph(root / "otbi_security.json", "otbi_security", [], [])
        write_graph(root / "rest.json", "rest", [], [])
        write_graph(
            root / "master_graph.json",
            "master",
            [entity, header, line, *columns],
            master_edges,
            layers,
        )
        write_bundle_manifest(root)

        selector = SemanticTextSelector(
            SemanticContextConfig(
                model_name="intfloat/multilingual-e5-base",
                query_mode="prefix",
                query_prefix="query: ",
                document_prefix="passage: ",
                expected_dimensions=AttributeEmbeddingModel.dimensions,
            ),
            model=AttributeEmbeddingModel(),
        )
        build_index_bundle(
            root,
            include_semantic_embeddings=True,
            semantic_text_selector=selector,
            semantic_batch_size=8,
        )

        with FederatedGraphSearch(
            root,
            semantic_text_selector=selector,
            require_index=True,
        ) as search:
            payload = search.build_prompt_context(
                "Gerenciar Acordo, com uma linha por acordo e os campos: "
                "Descrição, Moeda e Status. Não invente tabelas.",
                limit=20,
                max_characters=10000,
            )

        evidence = {row["attribute"]: row for row in payload["attribute_evidence"]}
        assert evidence["descricao"]["status"] == "ambiguous"
        assert evidence["moeda"]["status"] == "ambiguous"
        assert evidence["status"]["status"] == "resolved"
        assert evidence["descricao"]["selected_path"] is None
        assert evidence["moeda"]["selected_path"] is None
        assert evidence["descricao"]["evidence"] == []
        assert evidence["moeda"]["evidence"] == []
        assert evidence["descricao"]["alternative_paths"][0]["nodes"][-1]["title"] == "PO_HEADERS_ALL.COMMENTS"
        assert evidence["moeda"]["alternative_paths"][0]["nodes"][-1]["title"] == "PO_HEADERS_ALL.CURRENCY_CODE"
        assert evidence["status"]["evidence"][0]["title"] == "PO_HEADERS_ALL.DOCUMENT_STATUS"
        assert evidence["descricao"]["selection_basis"] == "semantic_suggestion"
        assert evidence["moeda"]["selection_basis"] == "semantic_suggestion"
        assert evidence["status"]["selection_basis"] == "strong_lexical_match"
        assert evidence["descricao"]["resolution_confidence"] == "low"
        assert evidence["moeda"]["resolution_confidence"] == "low"
        assert evidence["status"]["resolution_confidence"] == "medium"
        assert set(payload["gaps"]) == {"descricao", "moeda"}
        assert payload["routing"]["document_embeddings_recalculated"] is False
        assert payload["routing"]["attribute_query_vector_count"] == 3


def test_identifier_only_attribute_is_partial_and_path_contains_owner_table():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        entity = {
            "id": "entity:purchase-agreement",
            "node_type": "business_entity",
            "entity_id": "purchase_agreement",
            "title": "Purchase Agreement",
            "aliases": ["gerenciar acordo"],
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        attribute = {
            "id": "attribute:supplier",
            "node_type": "business_attribute",
            "entity_id": "purchase_agreement",
            "attribute_id": "supplier",
            "title": "Supplier",
            "aliases": ["fornecedor"],
            "columns": ["PO_HEADERS_ALL.VENDOR_ID"],
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        table = {
            "id": "table:po-headers",
            "node_type": "physical_table",
            "title": "PO_HEADERS_ALL",
            "name": "PO_HEADERS_ALL",
            "result_grain": {"grain_columns": ["PO_HEADER_ID"]},
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        column = {
            "id": "column:vendor-id",
            "node_type": "physical_column",
            "title": "PO_HEADERS_ALL.VENDOR_ID",
            "name": "VENDOR_ID",
            "qualified_name": "PO_HEADERS_ALL.VENDOR_ID",
            "table_name": "PO_HEADERS_ALL",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        distractor = {
            "id": "column:multiple-sites",
            "node_type": "physical_column",
            "title": "PO_HEADERS_ALL.ALLOW_MULTIPLE_SITES_FLAG",
            "name": "ALLOW_MULTIPLE_SITES_FLAG",
            "qualified_name": "PO_HEADERS_ALL.ALLOW_MULTIPLE_SITES_FLAG",
            "table_name": "PO_HEADERS_ALL",
            "description": "Allows multiple supplier sites for the agreement.",
            "search_text": "supplier vendor multiple sites",
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        business_edges = [{"source": entity["id"], "target": attribute["id"], "type": "has_attribute", "provenance": "EXTRACTED"}]
        physical_edges = [
            {"source": table["id"], "target": column["id"], "type": "contains_column", "provenance": "EXTRACTED"},
            {"source": table["id"], "target": distractor["id"], "type": "contains_column", "provenance": "EXTRACTED"},
        ]
        master_edges = [
            *business_edges,
            *physical_edges,
            {"source": entity["id"], "target": table["id"], "type": "mapped_to_entity", "provenance": "EXTRACTED"},
            {"source": attribute["id"], "target": column["id"], "type": "mapped_to_attribute", "provenance": "EXTRACTED"},
        ]
        layers = {"business": "business.json", "physical": "physical.json", "otbi_analytics": "otbi_analytics.json", "rest": "rest.json"}
        write_graph(root / "business.json", "business", [entity, attribute], business_edges)
        write_graph(root / "physical.json", "physical", [table, column, distractor], physical_edges)
        write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
        write_graph(root / "otbi_security.json", "otbi_security", [], [])
        write_graph(root / "rest.json", "rest", [], [])
        write_graph(root / "master_graph.json", "master", [entity, attribute, table, column, distractor], master_edges, layers)
        write_bundle_manifest(root)
        selector = SemanticTextSelector(
            SemanticContextConfig(
                model_name="intfloat/multilingual-e5-base",
                query_mode="prefix",
                query_prefix="query: ",
                document_prefix="passage: ",
                expected_dimensions=3,
                summary_max_characters=300,
            ),
            model=FederatedEmbeddingModel(),
        )
        bundle = build_index_bundle(
            root,
            include_semantic_embeddings=False,
            semantic_text_selector=selector,
        )
        with FederatedGraphSearch(
            root,
            semantic_text_selector=selector,
            index_path=bundle.bundle_path,
            require_index=True,
        ) as search:
            payload = search.build_prompt_context(
                "Gerenciar Acordo, com uma linha por acordo e os campos: Fornecedor.",
                limit=10,
                max_characters=8000,
            )

        row = payload["attribute_evidence"][0]
        assert row["status"] == "partial"
        assert row["evidence"][0]["role"] == "identifier_only"
        assert row["grain_compatibility"] == "compatible"
        assert row["selection_basis"] == "curated_mapping"
        assert row["resolution_confidence"] == "high"
        assert row["candidate_diagnostics"][0]["node_id"] == "column:vendor-id"
        assert row["candidate_diagnostics"][0]["evidence_tier"] == 5
        assert [node["id"] for node in row["selected_path"]["nodes"]] == [
            "entity:purchase-agreement",
            "table:po-headers",
            "column:vendor-id",
        ]


def test_generic_status_candidates_remain_ambiguous_without_curated_mapping():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        entity = {
            "id": "entity:purchase-agreement",
            "node_type": "business_entity",
            "entity_id": "purchase_agreement",
            "title": "Purchase Agreement",
            "aliases": ["gerenciar acordo"],
            "graph_layer": "business",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        table = {
            "id": "table:po-headers",
            "node_type": "physical_table",
            "title": "PO_HEADERS_ALL",
            "name": "PO_HEADERS_ALL",
            "result_grain": {"grain_columns": ["PO_HEADER_ID"]},
            "graph_layer": "physical",
            "modules": ["procurement"],
            "module_id": "procurement",
        }
        columns = [
            {
                "id": "column:document-status",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.DOCUMENT_STATUS",
                "name": "DOCUMENT_STATUS",
                "qualified_name": "PO_HEADERS_ALL.DOCUMENT_STATUS",
                "description": "Document status of the agreement.",
                "search_text": "document status agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "module_id": "procurement",
            },
            {
                "id": "column:funds-status",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.FUNDS_STATUS",
                "name": "FUNDS_STATUS",
                "qualified_name": "PO_HEADERS_ALL.FUNDS_STATUS",
                "description": "Funds status of the agreement.",
                "search_text": "funds status agreement",
                "graph_layer": "physical",
                "modules": ["procurement"],
                "module_id": "procurement",
            },
        ]
        physical_edges = [
            {
                "source": table["id"],
                "target": column["id"],
                "type": "contains_column",
                "provenance": "EXTRACTED",
            }
            for column in columns
        ]
        master_edges = [
            {
                "source": entity["id"],
                "target": table["id"],
                "type": "mapped_to_entity",
                "provenance": "EXTRACTED",
            },
            *physical_edges,
        ]
        layers = {
            "business": "business.json",
            "physical": "physical.json",
            "otbi_analytics": "otbi_analytics.json",
            "otbi_security": "otbi_security.json",
            "rest": "rest.json",
        }
        write_graph(root / "business.json", "business", [entity], [])
        write_graph(root / "physical.json", "physical", [table, *columns], physical_edges)
        write_graph(root / "otbi_analytics.json", "otbi_analytics", [], [])
        write_graph(root / "otbi_security.json", "otbi_security", [], [])
        write_graph(root / "rest.json", "rest", [], [])
        write_graph(
            root / "master_graph.json",
            "master",
            [entity, table, *columns],
            master_edges,
            layers,
        )
        write_bundle_manifest(root)
        selector = SemanticTextSelector(
            SemanticContextConfig(
                model_name="intfloat/multilingual-e5-base",
                query_mode="prefix",
                query_prefix="query: ",
                document_prefix="passage: ",
                expected_dimensions=3,
                summary_max_characters=300,
            ),
            model=FederatedEmbeddingModel(),
        )
        bundle = build_index_bundle(
            root,
            include_semantic_embeddings=False,
            semantic_text_selector=selector,
        )
        with FederatedGraphSearch(
            root,
            semantic_text_selector=selector,
            index_path=bundle.bundle_path,
            require_index=True,
        ) as search:
            payload = search.build_prompt_context(
                "Gerenciar Acordo, com uma linha por acordo e os campos: Status.",
                limit=10,
                max_characters=8000,
            )

        row = payload["attribute_evidence"][0]
        assert row["status"] == "ambiguous"
        assert row["selected_path"] is None
        assert row["evidence"] == []
        assert row["selection_basis"] == "strong_lexical_match"
        assert row["resolution_confidence"] == "low"
        assert row["top_candidate_id"] in {"column:document-status", "column:funds-status"}
        assert len(row["alternative_paths"]) == 2
        assert payload["gaps"] == ["status"]
