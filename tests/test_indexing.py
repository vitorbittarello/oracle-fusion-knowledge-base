from __future__ import annotations

import sqlite3
import time
from contextlib import closing
import tempfile
import unittest

import numpy as np
from pathlib import Path

from oracle_knowledge.common import write_json
from oracle_knowledge.indexing import (
    build_index_bundle,
    build_search_index,
    default_index_bundle_path,
    default_index_path,
    default_layer_index_path,
)
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.search.semantic_context import SemanticContextConfig, SemanticTextSelector
from oracle_knowledge.validation import validate_index_bundle, validate_index_database


class CountingEmbeddingModel:
    def __init__(self):
        self.document_calls = 0
        self.document_text_count = 0

    def encode(self, texts, **kwargs):
        self.document_calls += 1
        self.document_text_count += len(texts)
        return np.asarray(
            [[1.0, 0.0, 0.0] for _ in texts],
            dtype=np.float32,
        )


def semantic_selector(model: CountingEmbeddingModel) -> SemanticTextSelector:
    return SemanticTextSelector(
        SemanticContextConfig(model_name="test/model", batch_size=8),
        model=model,
    )


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


    def _write_graph_bundle_manifest_only(self, root: Path) -> None:
        stats_by_layer = {}
        for layer, filename in GRAPH_FILENAMES.items():
            payload = __import__("json").loads(
                (root / filename).read_text(encoding="utf-8")
            )
            stats_by_layer[layer] = payload["stats"]
        write_json(
            root / "graph_bundle.json",
            {
                "version": "1.0.0",
                "generated_at": "2026-06-27T12:00:01+00:00",
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


    def test_builds_layer_bundle_and_reuses_legacy_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graph_bundle(root)

            legacy_model = CountingEmbeddingModel()
            build_search_index(
                root,
                include_semantic_embeddings=True,
                semantic_text_selector=semantic_selector(legacy_model),
                semantic_batch_size=8,
            )
            self.assertGreater(legacy_model.document_text_count, 0)

            legacy_index = default_index_path(root)
            with closing(sqlite3.connect(legacy_index)) as connection:
                connection.execute("PRAGMA user_version = 2")
                connection.execute(
                    "UPDATE index_metadata SET value_json = ? WHERE key = 'schema_version'",
                    ('"2.0.0"',),
                )
                connection.execute(
                    """
                    DELETE FROM semantic_roots
                     WHERE node_pk IN (
                           SELECT node_pk
                             FROM nodes
                            WHERE graph_layer = 'master'
                     )
                    """
                )
                connection.commit()

            migration_model = CountingEmbeddingModel()
            result = build_index_bundle(
                root,
                semantic_text_selector=semantic_selector(migration_model),
                semantic_batch_size=8,
            )

            self.assertEqual(set(result.built_layers), set(GRAPH_FILENAMES))
            self.assertEqual(result.skipped_layers, ())
            self.assertTrue(default_index_bundle_path(root).is_file())
            for layer in GRAPH_FILENAMES:
                self.assertTrue(default_layer_index_path(root, layer).is_file())

            total_reused = sum(
                int(entry.get("reused_semantic_root_count") or 0)
                for entry in result.indexes.values()
            )
            total_generated = sum(
                int(entry.get("generated_semantic_root_count") or 0)
                for entry in result.indexes.values()
            )
            self.assertEqual(total_reused, 1)
            self.assertEqual(total_generated, 1)
            self.assertEqual(migration_model.document_text_count, 1)

            report = validate_index_bundle(
                default_index_bundle_path(root),
                graph_dir=root,
                full_hash=True,
            )
            self.assertEqual(report.error_count, 0)

            skip_model = CountingEmbeddingModel()
            skipped = build_index_bundle(
                root,
                semantic_text_selector=semantic_selector(skip_model),
                semantic_batch_size=8,
            )
            self.assertEqual(skipped.built_layers, ())
            self.assertEqual(set(skipped.skipped_layers), set(GRAPH_FILENAMES))
            self.assertEqual(skip_model.document_text_count, 0)

    def test_rebuilds_only_changed_master_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graph_bundle(root)
            initial_model = CountingEmbeddingModel()
            build_index_bundle(
                root,
                semantic_text_selector=semantic_selector(initial_model),
                semantic_batch_size=8,
            )

            physical_index = default_layer_index_path(root, "physical")
            physical_modified_ns = physical_index.stat().st_mtime_ns
            master_path = root / GRAPH_FILENAMES["master"]
            master_payload = __import__("json").loads(
                master_path.read_text(encoding="utf-8")
            )
            master_payload["nodes"][0]["search_text"] = (
                "ordem de produção work order ordem fabril"
            )
            write_json(master_path, master_payload)
            time.sleep(0.002)
            self._write_graph_bundle_manifest_only(root)

            rebuild_model = CountingEmbeddingModel()
            result = build_index_bundle(
                root,
                layers=["master"],
                semantic_text_selector=semantic_selector(rebuild_model),
                semantic_batch_size=8,
            )

            self.assertEqual(result.built_layers, ("master",))
            self.assertEqual(result.skipped_layers, ())
            self.assertEqual(
                physical_index.stat().st_mtime_ns,
                physical_modified_ns,
            )
            master_entry = result.indexes["master"]
            self.assertEqual(master_entry["generated_semantic_root_count"], 1)
            self.assertEqual(master_entry["reused_semantic_root_count"], 0)
            self.assertEqual(rebuild_model.document_text_count, 1)

            report = validate_index_bundle(
                default_index_bundle_path(root),
                graph_dir=root,
                full_hash=True,
            )
            self.assertEqual(report.error_count, 0)


if __name__ == "__main__":
    unittest.main()
