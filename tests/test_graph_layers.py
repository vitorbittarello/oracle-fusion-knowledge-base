from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oracle_knowledge.common import read_json, write_json
from oracle_knowledge.linker.graph_layers import (
    GRAPH_FILENAMES,
    classify_otbi_reference_page,
    write_graph_bundle,
)
from oracle_knowledge.linker.knowledge_linker import (
    build_graph,
    build_graph_bundle,
)


class GraphLayerClassificationTest(unittest.TestCase):
    def test_classifies_otbi_security_and_analytics_pages(self):
        security = {
            "node_type": "otbi_reference_page",
            "title": "Buyer",
            "text": (
                "Buyer ORA_PO_BUYER_JOB This job role is related to "
                "Agreement Transaction Analysis Duty and secures access."
            ),
        }
        analytics = {
            "node_type": "otbi_reference_page",
            "title": "Purchase Analysis",
            "text": "Purchase Analysis lists subject areas and analytical content.",
        }
        generic = {
            "node_type": "otbi_reference_page",
            "title": "Overview",
            "text": "Overview",
        }

        self.assertEqual(classify_otbi_reference_page(security), "security")
        self.assertEqual(classify_otbi_reference_page(analytics), "analytics")
        self.assertEqual(classify_otbi_reference_page(generic), "excluded")


class GraphBundleTest(unittest.TestCase):
    def _write_sources(self, root: Path) -> dict[str, str]:
        physical = root / "physical.json"
        otbi = root / "otbi.json"
        rest = root / "rest.json"
        entities = root / "entities.json"
        rules = root / "rules.json"

        write_json(
            physical,
            {
                "metadata": {
                    "module_id": "procurement",
                    "module_name": "Procurement",
                    "release_version": "26B",
                },
                "skills_catalog": [
                    {
                        "module_id": "procurement",
                        "module_name": "Procurement",
                        "sub_module": "Purchasing",
                        "components": [
                            {
                                "table_name": "PO_HEADERS_ALL",
                                "description": "Purchase agreement headers.",
                                "primary_key": ["PO_HEADER_ID"],
                                "fields_to_extract": [
                                    "PO_HEADER_ID",
                                    "TERMS_ID",
                                ],
                                "columns": [
                                    {
                                        "name": "PO_HEADER_ID",
                                        "datatype": "NUMBER",
                                        "nullable": False,
                                        "description": "Header identifier.",
                                    },
                                    {
                                        "name": "TERMS_ID",
                                        "datatype": "NUMBER",
                                        "nullable": True,
                                        "description": (
                                            "Payment terms unique identifier "
                                            "(References to AP_TERMS_TL.TERM_ID)"
                                        ),
                                    },
                                ],
                                "column_semantics": [
                                    {
                                        "column": "TERMS_ID",
                                        "semantic_role": "identifier",
                                        "description": "Payment terms identifier.",
                                    }
                                ],
                                "business_rules": [
                                    {
                                        "rule_type": "optimistic_locking",
                                        "columns": ["OBJECT_VERSION_NUMBER"],
                                        "rule": "Used to implement optimistic locking.",
                                        "source": "oracle_documentation",
                                        "confidence": "high",
                                    }
                                ],
                                "relationships": {
                                    "outgoing": [],
                                    "incoming": [],
                                },
                                "source_url": "https://example.test/po_headers_all",
                            }
                        ],
                    }
                ],
            },
        )

        write_json(
            otbi,
            {
                "subject_areas": [
                    {
                        "id": "subject:agreements",
                        "node_type": "otbi_subject_area",
                        "name": "Procurement - Purchasing Agreements Real Time",
                        "title": "Procurement - Purchasing Agreements Real Time",
                        "description": "Purchasing blanket and contract agreements.",
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    }
                ],
                "business_questions": [
                    {
                        "id": "question:expiring",
                        "node_type": "otbi_business_question",
                        "title": "How many agreements expire?",
                        "subject_areas": [
                            "Procurement - Purchasing Agreements Real Time"
                        ],
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    }
                ],
                "other_pages": [
                    {
                        "id": "page:buyer",
                        "node_type": "otbi_reference_page",
                        "title": "Buyer",
                        "text": (
                            "Buyer ORA_PO_BUYER_JOB This job role secures access "
                            "to subject areas through duty roles."
                        ),
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    },
                    {
                        "id": "page:analysis",
                        "node_type": "otbi_reference_page",
                        "title": "Purchase Analysis",
                        "text": "Purchase Analysis describes analytical subject areas.",
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    },
                    {
                        "id": "page:overview",
                        "node_type": "otbi_reference_page",
                        "title": "Overview",
                        "text": "Overview",
                        "source": {
                            "source_type": "oracle_otbi_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    },
                ],
            },
        )

        write_json(
            rest,
            {
                "resources": [
                    {
                        "id": "resource:purchase-agreements",
                        "node_type": "rest_resource",
                        "name": "purchaseAgreements",
                        "title": "Purchase Agreements",
                        "description": "Purchase agreement REST resource.",
                        "source": {
                            "source_type": "oracle_rest_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    }
                ],
                "operations": [
                    {
                        "id": "operation:purchase-agreements:get",
                        "node_type": "rest_operation",
                        "title": "Get purchase agreements",
                        "resource_name": "purchaseAgreements",
                        "method": "GET",
                        "endpoint_path": "/purchaseAgreements",
                        "source": {
                            "source_type": "oracle_rest_documentation",
                            "module_id": "procurement",
                        },
                        "module_id": "procurement",
                        "confidence": "high",
                    }
                ],
            },
        )

        write_json(
            rules,
            {
                "rules": [
                    {
                        "id": "procurement.purchase_agreement",
                        "name": "Purchase agreement",
                        "description": "Use the purchase agreement header.",
                        "tables": ["PO_HEADERS_ALL"],
                        "columns": ["PO_HEADERS_ALL.TERMS_ID"],
                        "conditions": [
                            {
                                "column": "TYPE_LOOKUP_CODE",
                                "operator": "IN",
                                "value": "BLANKET, CONTRACT",
                            }
                        ],
                        "confidence": "very_high",
                    }
                ]
            },
        )

        write_json(
            entities,
            {
                "entities": [
                    {
                        "entity_id": "purchase_agreement",
                        "name": "Purchase Agreement",
                        "aliases": ["acordo de compra", "purchase agreement"],
                        "module_id": "procurement",
                        "tables": ["PO_HEADERS_ALL"],
                        "subject_areas": [
                            "Procurement - Purchasing Agreements Real Time"
                        ],
                        "rest_resources": ["purchaseAgreements"],
                        "validated_rules": ["procurement.purchase_agreement"],
                        "attributes": [
                            {
                                "attribute_id": "payment_terms",
                                "name": "Payment Terms",
                                "aliases": ["condições de pagamento"],
                                "columns": ["PO_HEADERS_ALL.TERMS_ID"],
                            }
                        ],
                    }
                ]
            },
        )

        return {
            "physical_manifest": str(physical),
            "otbi_catalog": str(otbi),
            "rest_catalog": str(rest),
            "validated_rules": str(rules),
            "entity_aliases": str(entities),
        }

    def test_builds_separate_graphs_and_clean_master_bridges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self._write_sources(root)
            bundle = build_graph_bundle(**sources)

            self.assertEqual(
                set(bundle),
                {
                    "business",
                    "physical",
                    "otbi_analytics",
                    "otbi_security",
                    "rest",
                    "master",
                },
            )

            self.assertIn(
                "page:buyer",
                {node["id"] for node in bundle["otbi_security"]["nodes"]},
            )
            self.assertIn(
                "page:analysis",
                {node["id"] for node in bundle["otbi_analytics"]["nodes"]},
            )
            self.assertNotIn(
                "page:overview",
                {
                    node["id"]
                    for graph in bundle.values()
                    for node in graph["nodes"]
                },
            )

            all_edges = [
                edge
                for graph in bundle.values()
                for edge in graph["edges"]
            ]
            self.assertFalse(
                any(edge["type"] == "mentions_entity" for edge in all_edges)
            )
            self.assertFalse(
                any(
                    edge["type"] == "incoming_foreign_key_from"
                    for edge in all_edges
                )
            )

            master_edge_types = {
                edge["type"] for edge in bundle["master"]["edges"]
            }
            self.assertTrue(
                master_edge_types.issubset(
                    {
                        "has_attribute",
                        "mapped_to_entity",
                        "mapped_to_attribute",
                        "uses_table",
                        "uses_column",
                    }
                )
            )

            physical_nodes = {
                node["title"]: node
                for node in bundle["physical"]["nodes"]
            }
            self.assertIn("AP_TERMS_TL", physical_nodes)
            self.assertEqual(
                physical_nodes["AP_TERMS_TL"]["node_type"],
                "physical_table_stub",
            )

            header = physical_nodes["PO_HEADERS_ALL"]
            self.assertNotIn("{'rule_type'", header["search_text"])
            self.assertNotIn("'confidence':", header["search_text"])
            self.assertIn("optimistic locking", header["search_text"].lower())

            output_dir = root / "graphs"
            outputs = write_graph_bundle(output_dir, bundle)
            for layer, filename in GRAPH_FILENAMES.items():
                self.assertEqual(Path(outputs[layer]).name, filename)
                self.assertTrue(Path(outputs[layer]).exists())
            self.assertTrue(Path(outputs["manifest"]).exists())
            master_from_disk = read_json(outputs["master"], {})
            self.assertEqual(master_from_disk["graph_layer"], "master")

    def test_legacy_combined_graph_is_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = self._write_sources(Path(directory))
            graph = build_graph(**sources)
            edge_types = {edge["type"] for edge in graph["edges"]}
            self.assertNotIn("mentions_entity", edge_types)
            self.assertNotIn("incoming_foreign_key_from", edge_types)
            self.assertIn("nodes_by_layer", graph["stats"])


if __name__ == "__main__":
    unittest.main()
