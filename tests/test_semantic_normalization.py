from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from oracle_knowledge.common import write_json
from oracle_knowledge.semantic_normalization import (
    CURATED_AUDIT_COLUMN_TEXTS,
    normalize_semantic_corpus,
    normalize_semantic_text,
)


class SemanticNormalizationTest(unittest.TestCase):
    def _write_graphs(self, root: Path, *, status_description: str = "Document status") -> None:
        source = {"source_type": "oracle_data_dictionary"}
        physical_nodes = [
            {
                "id": "table:one",
                "node_type": "physical_table",
                "name": "TABLE_ONE",
                "title": "TABLE_ONE",
                "search_text": "first business table",
                "source": source,
            },
            {
                "id": "table:two",
                "node_type": "physical_table",
                "name": "TABLE_TWO",
                "title": "TABLE_TWO",
                "search_text": "second business table",
                "source": source,
            },
            {
                "id": "column:one:last-update-date",
                "node_type": "physical_column",
                "name": "LAST_UPDATE_DATE",
                "qualified_name": "TABLE_ONE.LAST_UPDATE_DATE",
                "title": "TABLE_ONE.LAST_UPDATE_DATE",
                "search_text": "TABLE_ONE LAST_UPDATE_DATE audit timestamp",
                "description": "Date and time when the row was last updated.",
                "source": source,
            },
            {
                "id": "column:two:last-update-date",
                "node_type": "physical_column",
                "name": "LAST_UPDATE_DATE",
                "qualified_name": "TABLE_TWO.LAST_UPDATE_DATE",
                "title": "TABLE_TWO.LAST_UPDATE_DATE",
                "search_text": "TABLE_TWO LAST_UPDATE_DATE audit timestamp",
                "description": "Date and time when the row was last updated.",
                "source": source,
            },
            {
                "id": "column:one:status",
                "node_type": "physical_column",
                "name": "STATUS",
                "qualified_name": "TABLE_ONE.STATUS",
                "title": "TABLE_ONE.STATUS",
                "search_text": f"TABLE_ONE STATUS {status_description}",
                "description": status_description,
                "source": source,
            },
            {
                "id": "column:two:status",
                "node_type": "physical_column",
                "name": "STATUS",
                "qualified_name": "TABLE_TWO.STATUS",
                "title": "TABLE_TWO.STATUS",
                "search_text": "TABLE_TWO STATUS processing status",
                "description": "Processing status",
                "source": source,
            },
        ]
        write_json(
            root / "physical.json",
            {
                "version": "3.0.0",
                "graph_layer": "physical",
                "nodes": physical_nodes,
                "edges": [],
                "stats": {"nodes": len(physical_nodes), "edges": 0},
            },
        )
        write_json(
            root / "rest.json",
            {
                "version": "3.0.0",
                "graph_layer": "rest",
                "nodes": [
                    {
                        "id": "adf:custom-object",
                        "node_type": "adf_resource",
                        "name": "CUSTOM_OBJECT_c",
                        "title": "Custom Object",
                        "search_text": "environment custom object",
                        "source": {"source_type": "fusion_adf_rest_metadata"},
                    }
                ],
                "edges": [],
                "stats": {"nodes": 1, "edges": 0},
            },
        )

    def test_text_normalization_is_conservative_and_idempotent(self) -> None:
        source = "  Café\u00a0de\r\nCompras\u200b   26B  "
        normalized = normalize_semantic_text(source)
        self.assertEqual(normalized, "Café de Compras 26B")
        self.assertEqual(normalize_semantic_text(normalized), normalized)

    def test_normalization_deduplicates_curated_audit_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)

            result = normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=2,
                checkpoint_percent=25,
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.total_nodes, 7)
            self.assertEqual(result.processed_nodes, 7)
            self.assertTrue(result.database_path.is_file())
            self.assertTrue(result.manifest_path.is_file())
            self.assertLess(result.unique_text_count, result.source_segment_count)

            with closing(sqlite3.connect(result.database_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT node_id, normalized_text_hash
                      FROM normalized_segments
                     WHERE node_id IN (
                         'column:one:last-update-date',
                         'column:two:last-update-date'
                     )
                  ORDER BY node_id
                    """
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][1], rows[1][1])
                text = connection.execute(
                    """
                    SELECT normalized_text
                      FROM normalized_texts
                     WHERE normalized_text_hash = ?
                    """,
                    (rows[0][1],),
                ).fetchone()[0]
                self.assertEqual(
                    text,
                    CURATED_AUDIT_COLUMN_TEXTS["LAST_UPDATE_DATE"],
                )

                status_hashes = connection.execute(
                    """
                    SELECT normalized_text_hash
                      FROM normalized_segments
                     WHERE node_id IN (
                         'column:one:status',
                         'column:two:status'
                     )
                  ORDER BY node_id
                    """
                ).fetchall()
                self.assertNotEqual(status_hashes[0][0], status_hashes[1][0])

    def test_completed_run_is_idempotent_and_incremental_run_reuses_unchanged_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)

            first = normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=3,
                checkpoint_percent=50,
            )
            second = normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=3,
                checkpoint_percent=50,
            )
            self.assertEqual(second.run_id, first.run_id)
            self.assertEqual(second.status, "completed")

            self._write_graphs(root, status_description="Approval document status")
            third = normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=3,
                checkpoint_percent=50,
            )
            self.assertNotEqual(third.run_id, first.run_id)
            self.assertEqual(third.reused_nodes, 6)
            self.assertEqual(third.normalized_nodes, 1)

    def test_interrupted_run_resumes_from_last_committed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            messages: list[str] = []

            def interrupt_after_checkpoint(message: str) -> None:
                messages.append(message)
                if "checkpoint persistido" in message:
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                normalize_semantic_corpus(
                    root,
                    layers=("rest", "physical"),
                    batch_size=1,
                    checkpoint_percent=10,
                    progress=interrupt_after_checkpoint,
                )

            resumed = normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=1,
                checkpoint_percent=10,
            )
            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(resumed.processed_nodes, resumed.total_nodes)
            self.assertGreater(resumed.normalized_nodes, 0)

    def test_progress_includes_percentage_eta_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            messages: list[str] = []

            normalize_semantic_corpus(
                root,
                layers=("rest", "physical"),
                batch_size=2,
                checkpoint_percent=20,
                progress=messages.append,
            )

            checkpoint_messages = [
                message for message in messages if "checkpoint persistido" in message
            ]
            self.assertTrue(checkpoint_messages)
            self.assertTrue(any("%" in message for message in checkpoint_messages))
            self.assertTrue(any("ETA" in message for message in checkpoint_messages))


if __name__ == "__main__":
    unittest.main()
