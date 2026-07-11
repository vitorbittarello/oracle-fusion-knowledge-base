from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np

from oracle_knowledge.common import write_json
from oracle_knowledge.indexing import (
    build_index_bundle,
    default_index_bundle_path,
    resolve_index_source,
)
from oracle_knowledge.search.indexed_store import IndexedGraphBundleStore
from oracle_knowledge.search.semantic_context import (
    E5_BASE_MODEL,
    E5_LARGE_INSTRUCT_MODEL,
    SemanticTextSelector,
    semantic_context_config_for_model,
)
from oracle_knowledge.semantic_index_cache import (
    resolve_semantic_embedding_cache,
)
from oracle_knowledge.semantic_normalization import normalize_semantic_corpus
from oracle_knowledge.semantic_vectorization import vectorize_semantic_corpus


class _EmbeddingModel768:
    def __init__(self) -> None:
        self.text_count = 0

    def encode(self, texts, **kwargs):
        self.text_count += len(texts)
        vectors = np.zeros((len(texts), 768), dtype=np.float32)
        for index in range(len(texts)):
            vectors[index, index % 768] = 1.0
        return vectors


class SemanticIndexCacheTest(unittest.TestCase):
    def _write_graphs(self, root: Path) -> None:
        physical = {
            "version": "3.0.0",
            "generated_at": "2026-07-01T00:00:00+00:00",
            "graph_layer": "physical",
            "nodes": [
                {
                    "id": "table:po-headers-all",
                    "node_type": "physical_table",
                    "name": "PO_HEADERS_ALL",
                    "title": "PO_HEADERS_ALL",
                    "search_text": "purchase agreement header",
                    "modules": ["procurement"],
                    "source": {"source_type": "oracle_data_dictionary"},
                },
                {
                    "id": "column:po-headers-all-segment1",
                    "node_type": "physical_column",
                    "name": "SEGMENT1",
                    "title": "PO_HEADERS_ALL.SEGMENT1",
                    "qualified_name": "PO_HEADERS_ALL.SEGMENT1",
                    "search_text": "purchase agreement number",
                    "modules": ["procurement"],
                    "source": {"source_type": "oracle_data_dictionary"},
                },
            ],
            "edges": [
                {
                    "source": "table:po-headers-all",
                    "target": "column:po-headers-all-segment1",
                    "type": "contains_column",
                    "weight": 1.0,
                }
            ],
            "stats": {"nodes": 2, "edges": 1},
        }
        write_json(root / "physical.json", physical)
        write_json(
            root / "graph_bundle.json",
            {
                "version": "1.0.0",
                "generated_at": "2026-07-01T00:00:00+00:00",
                "graphs": {"physical": str(root / "physical.json")},
                "stats": {"physical": physical["stats"]},
            },
        )

    def _build_cache(self, root: Path):
        self._write_graphs(root)
        normalized = normalize_semantic_corpus(
            root,
            layers=("physical",),
            batch_size=2,
            checkpoint_percent=50,
        )
        model = _EmbeddingModel768()
        selector = SemanticTextSelector(
            semantic_context_config_for_model(
                E5_BASE_MODEL,
                device="cpu",
                batch_size=2,
            ),
            model=model,
        )
        vectorized = vectorize_semantic_corpus(
            root,
            normalization_database_path=normalized.database_path,
            model_name=E5_BASE_MODEL,
            batch_size=2,
            checkpoint_percent=50,
            expected_dimensions=768,
            semantic_text_selector=selector,
        )
        cache = resolve_semantic_embedding_cache(
            root,
            model_name=E5_BASE_MODEL,
            normalization_database_path=normalized.database_path,
            embedding_database_path=vectorized.database_path,
        )
        return cache, selector, model

    def test_build_index_consumes_persisted_embeddings_without_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cache, selector, model = self._build_cache(root)
            encoded_before_build = model.text_count

            result = build_index_bundle(
                root,
                layers=("physical",),
                semantic_text_selector=selector,
                semantic_embedding_cache=cache,
            )

            self.assertEqual(
                result.bundle_path,
                default_index_bundle_path(root, E5_BASE_MODEL),
            )
            self.assertEqual(model.text_count, encoded_before_build)
            self.assertTrue(result.bundle_path.is_file())

            physical_path = result.bundle_path.parent / "physical.sqlite"
            with closing(__import__("sqlite3").connect(physical_path)) as connection:
                roots = connection.execute(
                    "SELECT COUNT(*) FROM semantic_root_refs"
                ).fetchone()[0]
                segments = connection.execute(
                    "SELECT COUNT(*) FROM semantic_segment_refs"
                ).fetchone()[0]
                copied_root_vectors = connection.execute(
                    "SELECT COUNT(*) FROM semantic_roots"
                ).fetchone()[0]
                copied_segment_vectors = connection.execute(
                    "SELECT COUNT(*) FROM semantic_segments"
                ).fetchone()[0]
                metadata = dict(
                    connection.execute(
                        "SELECT key, value_json FROM index_metadata"
                    ).fetchall()
                )
            self.assertEqual(roots, 1)
            self.assertGreaterEqual(segments, 1)
            self.assertEqual(copied_root_vectors, 0)
            self.assertEqual(copied_segment_vectors, 0)
            self.assertIn("semantic_embedding_cache", metadata)

            with IndexedGraphBundleStore(
                root,
                bundle_path=result.bundle_path,
                semantic_text_selector=selector,
            ) as store:
                ranked = store.semantic_roots(
                    "purchase agreement",
                    "physical",
                    module_ids=None,
                    limit=5,
                )
            self.assertEqual(ranked[0][0]["id"], "table:po-headers-all")

    def test_resolve_index_source_selects_bundle_for_requested_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cache, selector, _ = self._build_cache(root)
            result = build_index_bundle(
                root,
                layers=("physical",),
                semantic_text_selector=selector,
                semantic_embedding_cache=cache,
            )

            self.assertEqual(
                resolve_index_source(root, model_name=E5_BASE_MODEL),
                result.bundle_path,
            )
            self.assertEqual(
                resolve_index_source(root, model_name=E5_LARGE_INSTRUCT_MODEL),
                default_index_bundle_path(root, E5_LARGE_INSTRUCT_MODEL),
            )

    def test_bundle_rejects_search_with_another_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cache, selector, _ = self._build_cache(root)
            result = build_index_bundle(
                root,
                layers=("physical",),
                semantic_text_selector=selector,
                semantic_embedding_cache=cache,
            )
            incompatible_selector = SemanticTextSelector(
                semantic_context_config_for_model(E5_LARGE_INSTRUCT_MODEL)
            )
            with self.assertRaisesRegex(ValueError, "outro modelo semântico"):
                IndexedGraphBundleStore(
                    root,
                    bundle_path=result.bundle_path,
                    semantic_text_selector=incompatible_selector,
                )


if __name__ == "__main__":
    unittest.main()
