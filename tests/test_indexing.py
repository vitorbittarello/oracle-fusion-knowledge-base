from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from oracle_knowledge.common import write_json
from oracle_knowledge.indexing import build_search_index, default_index_path
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.validation import validate_index_database


class SearchIndexTest(unittest.TestCase):
    def _write_graph_bundle(self, root: Path) -> None:
        stats_by_layer: dict[str, dict[str, int]] = {}

        for layer, filename in GRAPH_FILENAMES.items():
            nodes: list[dict[str, object]] = []
            edges: list[dict[str, object]] = []

            if layer == "business":
                nodes = [
                    {
                        "id": "entity:work-order",
                        "node_type": "business_entity",
                        "title": "Work Order",
                        "graph_layer": "business",
                        "search_text": "ordem de produção work order",
                        "modules": ["scm"],
                        "source": {"source_type": "curated_entity_map"},
                    }
                ]
            elif layer == "physical":
                nodes = [
                    {
                        "id": "table:wie-work-orders-b",
                        "node_type": "physical_table",
                        "title": "WIE_WORK_ORDERS_B",
                        "name": "WIE_WORK_ORDERS_B",
                        "graph_layer": "physical",
                        "search_text": "manufacturing work orders production orders",
                        "modules": ["scm"],
                        "source": {"source_type": "oracle_data_dictionary"},
                    },
                    {
                        "id": "column:wie-work-orders-b-work-order-number",
                        "node_type": "physical_column",
                        "title": "WIE_WORK_ORDERS_B.WORK_ORDER_NUMBER",
                        "qualified_name": "WIE_WORK_ORDERS_B.WORK_ORDER_NUMBER",
                        "graph_layer": "physical",
                        "search_text": "work order number production order identifier",
                        "modules": ["scm"],
                        "source": {"source_type": "oracle_data_dictionary"},
                    },
                ]
                edges = [
                    {
                        "source": "table:wie-work-orders-b",
                        "target": "column:wie-work-orders-b-work-order-number",
                        "type": "contains_column",
                        "weight": 1.0,
                    }
                ]
            elif layer == "master":
                nodes = [
                    {
                        "id": "entity:work-order",
                        "node_type": "business_entity",
                        "title": "Work Order",
                        "graph_layer": "business",
                        "search_text": "ordem de produção work order",
                        "modules": ["scm"],
                        "source": {"source_type": "curated_entity_map"},
                    },
                    {
                        "id": "table:wie-work-orders-b",
                        "node_type": "physical_table",
                        "title": "WIE_WORK_ORDERS_B",
                        "graph_layer": "physical",
                        "search_text": "manufacturing work orders production orders",
                        "modules": ["scm"],
                        "source": {"source_type": "oracle_data_dictionary"},
                    },
                ]
                edges = [
                    {
                        "source": "entity:work-order",
                        "target": "table:wie-work-orders-b",
                        "type": "mapped_to_entity",
                        "weight": 1.0,
                        "source_layer": "business",
                        "target_layer": "physical",
                    }
                ]

            payload = {
                "version": "3.0.0",
                "generated_at": "2026-06-27T12:00:00+00:00",
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
                "generated_at": "2026-06-27T12:00:00+00:00",
                "graphs": {
                    layer: str(root / filename)
                    for layer, filename in GRAPH_FILENAMES.items()
                },
                "stats": stats_by_layer,
            },
        )

    def test_builds_sqlite_fts_index_and_validates_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graph_bundle(root)

            result = build_search_index(root, batch_size=2)
            index_path = default_index_path(root)

            self.assertEqual(result.index_path, index_path)
            self.assertEqual(result.node_count, 5)
            self.assertEqual(result.edge_count, 2)
            self.assertTrue(index_path.is_file())

            with closing(sqlite3.connect(index_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT n.graph_layer, n.node_id
                      FROM nodes_fts f
                      JOIN nodes n ON n.node_pk = f.rowid
                     WHERE nodes_fts MATCH ?
                  ORDER BY bm25(nodes_fts), n.graph_layer
                    """,
                    ('"production"',),
                ).fetchall()
                self.assertTrue(rows)
                self.assertIn(
                    ("physical", "table:wie-work-orders-b"),
                    rows,
                )

            report = validate_index_database(index_path, graph_dir=root, full_hash=True)
            self.assertEqual(report.error_count, 0)

    def test_validation_detects_changed_graph_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graph_bundle(root)
            result = build_search_index(root)

            physical_path = root / GRAPH_FILENAMES["physical"]
            payload = {
                "version": "3.0.0",
                "graph_layer": "physical",
                "nodes": [],
                "edges": [],
                "stats": {"nodes": 0, "edges": 0},
            }
            write_json(physical_path, payload)

            report = validate_index_database(
                result.index_path,
                graph_dir=root,
            )

            self.assertGreater(report.error_count, 0)
            self.assertTrue(
                any(check.code == "INDEX_STALE" for check in report.checks)
            )

    def test_builder_rejects_orphan_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graph_bundle(root)
            physical_path = root / GRAPH_FILENAMES["physical"]
            write_json(
                physical_path,
                {
                    "version": "3.0.0",
                    "graph_layer": "physical",
                    "nodes": [],
                    "edges": [
                        {
                            "source": "missing:source",
                            "target": "missing:target",
                            "type": "contains_column",
                        }
                    ],
                    "stats": {"nodes": 0, "edges": 1},
                },
            )

            with self.assertRaisesRegex(ValueError, "Aresta órfã"):
                build_search_index(root)


if __name__ == "__main__":
    unittest.main()
