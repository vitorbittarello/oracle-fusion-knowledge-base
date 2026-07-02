from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np

from oracle_knowledge.common import read_json, write_json
from oracle_knowledge.search.semantic_context import (
    SemanticContextConfig,
    SemanticTextSelector,
)
from oracle_knowledge.semantic_normalization import normalize_semantic_corpus
from oracle_knowledge.semantic_vectorization import vectorize_semantic_corpus


class _DeterministicEmbeddingModel:
    def __init__(self) -> None:
        self.encoded_documents: list[str] = []

    def encode(self, documents, **kwargs):
        self.encoded_documents.extend(str(value) for value in documents)
        vectors: list[list[float]] = []
        for value in documents:
            digest = hashlib.sha256(str(value).encode("utf-8")).digest()
            vectors.append(
                [
                    float(digest[0] + 1),
                    float(digest[1] + 1),
                    float(digest[2] + 1),
                    float(digest[3] + 1),
                ]
            )
        return np.asarray(vectors, dtype=np.float32)


class SemanticVectorizationTest(unittest.TestCase):
    def _write_graphs(self, root: Path, *, extra_description: str = "") -> None:
        source = {"source_type": "oracle_data_dictionary"}
        nodes = [
            {
                "id": "table:one",
                "node_type": "physical_table",
                "name": "TABLE_ONE",
                "title": "TABLE_ONE",
                "search_text": "purchase agreement header",
                "source": source,
            },
            {
                "id": "column:one:last-update-date",
                "node_type": "physical_column",
                "name": "LAST_UPDATE_DATE",
                "qualified_name": "TABLE_ONE.LAST_UPDATE_DATE",
                "title": "TABLE_ONE.LAST_UPDATE_DATE",
                "search_text": "audit timestamp",
                "description": "Date and time when the row was last updated.",
                "source": source,
            },
            {
                "id": "column:one:status",
                "node_type": "physical_column",
                "name": "STATUS",
                "qualified_name": "TABLE_ONE.STATUS",
                "title": "TABLE_ONE.STATUS",
                "search_text": f"agreement status {extra_description}".strip(),
                "description": f"Agreement status {extra_description}".strip(),
                "source": source,
            },
        ]
        write_json(
            root / "physical.json",
            {
                "version": "3.0.0",
                "graph_layer": "physical",
                "nodes": nodes,
                "edges": [],
                "stats": {"nodes": len(nodes), "edges": 0},
            },
        )

    @staticmethod
    def _selector(model: _DeterministicEmbeddingModel) -> SemanticTextSelector:
        return SemanticTextSelector(
            SemanticContextConfig(
                model_name="test/model",
                batch_size=2,
            ),
            model=model,
        )

    def test_vectorization_persists_l2_normalized_float32_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            normalized = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=2,
                checkpoint_percent=50,
            )
            model = _DeterministicEmbeddingModel()
            result = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized.database_path,
                model_name="test/model",
                batch_size=2,
                checkpoint_percent=50,
                expected_dimensions=4,
                semantic_text_selector=self._selector(model),
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.dimensions, 4)
            self.assertEqual(result.processed_texts, result.total_texts)
            self.assertEqual(result.generated_embeddings, result.total_texts)
            self.assertEqual(len(model.encoded_documents), result.total_texts)

            with closing(sqlite3.connect(result.database_path)) as connection:
                row = connection.execute(
                    """
                    SELECT dimensions, storage_dtype, vector_normalization,
                           similarity_metric, embedding
                      FROM semantic_embeddings
                     LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(row[0], 4)
                self.assertEqual(row[1], "float32")
                self.assertEqual(row[2], "l2")
                self.assertEqual(row[3], "cosine")
                self.assertEqual(len(row[4]), 4 * 4)
                vector = np.frombuffer(row[4], dtype="<f4")
                self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)

    def test_completed_run_is_idempotent_and_new_corpus_reuses_existing_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            normalized = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=2,
                checkpoint_percent=50,
            )
            first_model = _DeterministicEmbeddingModel()
            first = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized.database_path,
                model_name="test/model",
                batch_size=2,
                checkpoint_percent=50,
                expected_dimensions=4,
                semantic_text_selector=self._selector(first_model),
            )

            second_model = _DeterministicEmbeddingModel()
            second = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized.database_path,
                model_name="test/model",
                batch_size=2,
                checkpoint_percent=50,
                expected_dimensions=4,
                semantic_text_selector=self._selector(second_model),
            )
            self.assertEqual(second.run_id, first.run_id)
            self.assertEqual(second_model.encoded_documents, [])

            unchanged_graph = read_json(root / "physical.json", {})
            unchanged_graph["generated_at"] = "2026-07-01T00:00:00Z"
            write_json(root / "physical.json", unchanged_graph)
            normalized_same_texts = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=2,
                checkpoint_percent=50,
            )
            reused_model = _DeterministicEmbeddingModel()
            reused = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized_same_texts.database_path,
                model_name="test/model",
                batch_size=2,
                checkpoint_percent=50,
                expected_dimensions=4,
                semantic_text_selector=self._selector(reused_model),
            )
            self.assertNotEqual(reused.run_id, first.run_id)
            self.assertEqual(reused.generated_embeddings, 0)
            self.assertEqual(reused.reused_embeddings, reused.total_texts)
            self.assertEqual(reused.dimensions, 4)
            self.assertEqual(reused_model.encoded_documents, [])

            self._write_graphs(root, extra_description="approved")
            normalized_incremental = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=2,
                checkpoint_percent=50,
            )
            third_model = _DeterministicEmbeddingModel()
            third = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized_incremental.database_path,
                model_name="test/model",
                batch_size=2,
                checkpoint_percent=50,
                expected_dimensions=4,
                semantic_text_selector=self._selector(third_model),
            )
            self.assertNotEqual(third.run_id, first.run_id)
            self.assertGreater(third.reused_embeddings, 0)
            self.assertGreater(third.generated_embeddings, 0)
            self.assertLess(third.generated_embeddings, third.total_texts)
            self.assertEqual(
                len(third_model.encoded_documents),
                third.generated_embeddings,
            )

    def test_interrupted_run_resumes_from_committed_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            normalized = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=1,
                checkpoint_percent=50,
            )
            first_model = _DeterministicEmbeddingModel()

            def interrupt_after_checkpoint(message: str) -> None:
                if "checkpoint persistido" in message:
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                vectorize_semantic_corpus(
                    root,
                    normalization_database_path=normalized.database_path,
                    model_name="test/model",
                    batch_size=1,
                    checkpoint_percent=20,
                    expected_dimensions=4,
                    semantic_text_selector=self._selector(first_model),
                    progress=interrupt_after_checkpoint,
                )

            second_model = _DeterministicEmbeddingModel()
            resumed = vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized.database_path,
                model_name="test/model",
                batch_size=1,
                checkpoint_percent=20,
                expected_dimensions=4,
                semantic_text_selector=self._selector(second_model),
            )
            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(resumed.processed_texts, resumed.total_texts)
            self.assertGreater(len(first_model.encoded_documents), 0)
            self.assertLess(
                len(second_model.encoded_documents),
                resumed.total_texts,
            )

    def test_progress_includes_percentage_eta_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            self._write_graphs(root)
            normalized = normalize_semantic_corpus(
                root,
                layers=("physical",),
                batch_size=2,
                checkpoint_percent=50,
            )
            messages: list[str] = []
            model = _DeterministicEmbeddingModel()
            vectorize_semantic_corpus(
                root,
                normalization_database_path=normalized.database_path,
                model_name="test/model",
                batch_size=1,
                checkpoint_percent=20,
                expected_dimensions=4,
                semantic_text_selector=self._selector(model),
                progress=messages.append,
            )
            checkpoints = [
                message for message in messages if "checkpoint persistido" in message
            ]
            self.assertTrue(checkpoints)
            self.assertTrue(any("%" in message for message in checkpoints))
            self.assertTrue(any("ETA" in message for message in checkpoints))


if __name__ == "__main__":
    unittest.main()
