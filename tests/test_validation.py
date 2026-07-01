from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from oracle_knowledge.common import write_json
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.validation import (
    validate_adf_environment,
    validate_graph_directory,
    validate_module_directory,
    validate_search_result,
)


class ValidationCommandsTest(unittest.TestCase):
    def test_validate_module_uses_module_metadata_to_determine_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            write_json(
                root / "module.json",
                {
                    "module_id": "scm",
                    "module_name": "Supply Chain Management",
                    "release": "26B",
                    "source_urls": {
                        "physical": "https://example.test/physical",
                        "functional": [],
                        "otbi": "https://example.test/otbi",
                        "rest": None,
                    },
                    "outputs": {
                        "physical_manifest": str(root / "physical/manifest.json"),
                        "otbi_catalog": str(root / "otbi/catalog.json"),
                    },
                },
            )
            write_json(root / "physical/manifest.json", {"tables": []})
            write_json(root / "otbi/catalog.json", {"stats": {"pages": 1}})
            write_json(root / "rules/validated_rules.json", {"rules": []})
            write_json(root / "config/entity_aliases.json", {"entities": []})

            report = validate_module_directory(root)

            self.assertEqual(report.error_count, 0)
            self.assertFalse((root / "rest/catalog.json").exists())
            self.assertEqual(
                report.metadata["sources"]["rest"]["expected"],
                False,
            )

    def test_validate_adf_environment_accepts_global_catalog_and_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir) / "data/environment/adf"
            write_json(
                root / "catalog.json",
                {
                    "source": {"environment_host": "fusion.example.test"},
                    "resources": [
                        {"name": "purchaseOrders"},
                        {"name": "APCUSTOMBM_c"},
                    ],
                    "stats": {"catalog_resources": 2},
                },
            )
            write_json(
                root / "manifest.json",
                {"stats": {"catalog_resources": 2}},
            )
            write_json(
                root / "modules/procurement.json",
                {
                    "module_id": "procurement",
                    "resources": ["purchaseOrders", "APCUSTOMBM_c"],
                },
            )
            write_json(
                root / "modules/unclassified.json",
                {"module_id": "unclassified", "resources": []},
            )

            report = validate_adf_environment(root)

            self.assertEqual(report.error_count, 0)
            self.assertEqual(report.metadata["resources"], 2)
            self.assertEqual(
                report.metadata["environment_host"],
                "fusion.example.test",
            )


    def test_validate_module_reports_missing_expected_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            write_json(
                root / "module.json",
                {
                    "module_id": "scm",
                    "module_name": "Supply Chain Management",
                    "release": "26B",
                    "source_urls": {
                        "physical": "https://example.test/physical",
                        "functional": [],
                        "otbi": None,
                        "rest": "https://example.test/rest",
                    },
                    "outputs": {},
                },
            )
            write_json(root / "physical/manifest.json", {"tables": []})
            write_json(root / "rules/validated_rules.json", {"rules": []})
            write_json(root / "config/entity_aliases.json", {"entities": []})

            report = validate_module_directory(root)

            self.assertGreater(report.error_count, 0)
            self.assertTrue(
                any(
                    check.code == "MODULE_REST_MISSING"
                    for check in report.checks
                )
            )

    def test_validate_graph_accepts_consistent_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            stats_by_layer = {}

            for layer, filename in GRAPH_FILENAMES.items():
                if layer == "business":
                    nodes = [
                        {
                            "id": "entity:test",
                            "node_type": "business_entity",
                            "graph_layer": "business",
                        }
                    ]
                elif layer == "master":
                    nodes = [
                        {
                            "id": "entity:test",
                            "node_type": "business_entity",
                            "graph_layer": "business",
                        }
                    ]
                else:
                    nodes = []

                payload = {
                    "version": "3.0.0",
                    "graph_layer": layer,
                    "nodes": nodes,
                    "edges": [],
                    "stats": {
                        "nodes": len(nodes),
                        "edges": 0,
                    },
                }
                write_json(root / filename, payload)
                stats_by_layer[layer] = payload["stats"]

            write_json(
                root / "graph_bundle.json",
                {
                    "version": "1.0.0",
                    "graphs": {
                        layer: str(root / filename)
                        for layer, filename in GRAPH_FILENAMES.items()
                    },
                    "stats": stats_by_layer,
                },
            )

            report = validate_graph_directory(root)

            self.assertEqual(report.error_count, 0)

    def test_validate_graph_rejects_forbidden_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            stats_by_layer = {}

            for layer, filename in GRAPH_FILENAMES.items():
                nodes = []
                edges = []
                if layer == "business":
                    nodes = [
                        {
                            "id": "entity:test",
                            "node_type": "business_entity",
                            "graph_layer": "business",
                        },
                        {
                            "id": "attribute:test",
                            "node_type": "business_attribute",
                            "graph_layer": "business",
                        },
                    ]
                    edges = [
                        {
                            "source": "entity:test",
                            "target": "attribute:test",
                            "type": "mentions_entity",
                        }
                    ]

                payload = {
                    "version": "3.0.0",
                    "graph_layer": layer,
                    "nodes": nodes,
                    "edges": edges,
                    "stats": {
                        "nodes": len(nodes),
                        "edges": len(edges),
                    },
                }
                write_json(root / filename, payload)
                stats_by_layer[layer] = payload["stats"]

            write_json(
                root / "graph_bundle.json",
                {
                    "version": "1.0.0",
                    "stats": stats_by_layer,
                },
            )

            report = validate_graph_directory(root)

            self.assertGreater(report.error_count, 0)
            self.assertTrue(
                any(
                    check.code == "GRAPH_FORBIDDEN_EDGES"
                    for check in report.checks
                )
            )

    def test_validate_search_result_checks_rendered_character_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "result.json"
            context = "OBJETIVO\nTeste"
            write_json(
                path,
                {
                    "query": "teste",
                    "context": context,
                    "results": [
                        {
                            "id": "table:test",
                            "title": "TEST",
                            "source": {"source_type": "test"},
                        }
                    ],
                    "characters": len(context),
                    "routing": {
                        "master_business_seeds": [],
                        "master_routes": [],
                        "semantic_fallback_roots": [],
                        "candidate_count": 1,
                    },
                },
            )

            report = validate_search_result(path, max_characters=100)

            self.assertEqual(report.error_count, 0)

    def test_validate_search_result_detects_mojibake_and_wrong_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "result.json"
            write_json(
                path,
                {
                    "query": "condi├º├Áes",
                    "context": "contexto",
                    "results": [],
                    "characters": 999,
                },
            )

            report = validate_search_result(path, max_characters=100)

            self.assertGreater(report.error_count, 0)
            self.assertTrue(
                any(
                    check.code == "SEARCH_RESULT_ENCODING"
                    and check.status == "WARNING"
                    for check in report.checks
                )
            )


if __name__ == "__main__":
    unittest.main()
