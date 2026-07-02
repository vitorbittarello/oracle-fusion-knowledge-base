from __future__ import annotations

import unittest

import numpy as np

from oracle_knowledge.search.semantic_context import (
    E5_BASE_MODEL,
    E5_LARGE_INSTRUCT_MODEL,
    SemanticTextSelector,
    resolve_embedding_model_profile,
    semantic_context_config_for_model,
)


class _CaptureModel:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def encode(self, texts, **kwargs):
        values = [str(value) for value in texts]
        self.calls.append(values)
        return np.ones((len(values), self.dimensions), dtype=np.float32)


class SemanticModelProfileTest(unittest.TestCase):
    def test_base_profile_uses_query_and_passage_prefixes(self) -> None:
        profile = resolve_embedding_model_profile(E5_BASE_MODEL)
        self.assertEqual(profile.dimensions, 768)

        model = _CaptureModel(profile.dimensions)
        selector = SemanticTextSelector(
            semantic_context_config_for_model(E5_BASE_MODEL, batch_size=2),
            model=model,
        )

        selector.encode_query("acordo de compra")
        selector.encode_documents(["Purchase agreement header"])

        self.assertEqual(model.calls[0], ["query: acordo de compra"])
        self.assertEqual(model.calls[1], ["passage: Purchase agreement header"])

    def test_large_instruct_profile_preserves_instruction_format(self) -> None:
        profile = resolve_embedding_model_profile(E5_LARGE_INSTRUCT_MODEL)
        self.assertEqual(profile.dimensions, 1024)

        model = _CaptureModel(profile.dimensions)
        selector = SemanticTextSelector(
            semantic_context_config_for_model(
                E5_LARGE_INSTRUCT_MODEL,
                batch_size=2,
            ),
            model=model,
        )

        selector.encode_query("acordo de compra")
        selector.encode_documents(["Purchase agreement header"])

        self.assertTrue(model.calls[0][0].startswith("Instruct:"))
        self.assertIn("Query: acordo de compra", model.calls[0][0])
        self.assertEqual(model.calls[1], ["Purchase agreement header"])

    def test_unsupported_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Modelo semântico não suportado"):
            resolve_embedding_model_profile("example/unsupported-model")


if __name__ == "__main__":
    unittest.main()
