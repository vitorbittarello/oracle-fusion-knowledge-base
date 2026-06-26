from __future__ import annotations

import unittest

import numpy as np

from oracle_knowledge.search.hybrid_search import HybridSearch
from oracle_knowledge.search.semantic_context import (
    SemanticContextConfig,
    SemanticTextSelector,
)


class FakeEmbeddingModel:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.documents: list[str] = []

    def encode(self, texts, **kwargs):
        if texts and texts[0].startswith("Instruct:"):
            self.queries.extend(texts)
            return np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32)

        self.documents.extend(texts)
        vectors = []

        for text in texts:
            normalized = text.casefold()
            if "payment terms" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            elif "supplier" in normalized:
                vectors.append([0.95, 0.05, 0.0])
            elif "agreement dates" in normalized:
                vectors.append([0.80, 0.20, 0.0])
            else:
                vectors.append([0.05, 0.95, 0.0])

        return np.asarray(vectors, dtype=np.float32)


class SemanticTextSelectorTest(unittest.TestCase):
    def test_short_text_does_not_load_model(self):
        selector = SemanticTextSelector(
            SemanticContextConfig()
        )

        result = selector.select_relevant_text(
            "payment terms",
            "Short documented text.",
            max_characters=200,
        )

        self.assertEqual(result, "Short documented text.")
        self.assertIsNone(selector._model)

    def test_selects_relevant_segments_and_restores_source_order(self):
        model = FakeEmbeddingModel()
        selector = SemanticTextSelector(
            SemanticContextConfig(
                maximum_segment_characters=200,
                mmr_lambda=0.90,
            ),
            model=model,
        )
        text = (
            "Supplier information identifies the trading partner. "
            "Audit columns record technical updates. "
            "Payment terms define the settlement conditions. "
            "Agreement dates define the validity period."
        )

        result = selector.select_relevant_text(
            "condições de pagamento e fornecedor",
            text,
            max_characters=125,
        )

        self.assertIn("Supplier information", result)
        self.assertIn("Payment terms", result)
        self.assertNotIn("Audit columns", result)
        self.assertLess(
            result.index("Supplier information"),
            result.index("Payment terms"),
        )
        self.assertTrue(
            model.queries[0].startswith("Instruct:")
        )
        self.assertIn("Query:", model.queries[0])

    def test_respects_character_budget(self):
        model = FakeEmbeddingModel()
        selector = SemanticTextSelector(
            SemanticContextConfig(
                maximum_segment_characters=80,
            ),
            model=model,
        )
        text = (
            "Payment terms define settlement conditions. "
            "Supplier information identifies the trading partner. "
            "Agreement dates define the validity period."
        )

        result = selector.select_relevant_text(
            "payment terms",
            text,
            max_characters=70,
        )

        self.assertLessEqual(len(result), 70)
        self.assertIn("Payment terms", result)

    def test_build_prompt_context_uses_semantic_summary_only_after_search(self):
        model = FakeEmbeddingModel()
        selector = SemanticTextSelector(
            SemanticContextConfig(
                maximum_segment_characters=160,
                summary_max_characters=125,
                mmr_lambda=0.90,
            ),
            model=model,
        )
        graph = {
            "nodes": [
                {
                    "id": "table:purchase-agreements",
                    "node_type": "physical_table",
                    "name": "PURCHASE_AGREEMENTS",
                    "title": "PURCHASE_AGREEMENTS",
                    "description": (
                        "Supplier information identifies the trading partner. "
                        "Audit columns record technical updates. "
                        "Payment terms define the settlement conditions. "
                        "Agreement dates define the validity period."
                    ),
                    "primary_key": ["AGREEMENT_ID"],
                    "result_grain": {
                        "description": "One row per agreement.",
                    },
                    "business_rules": [],
                    "confidence": "high",
                    "source": {
                        "source_type": "oracle_data_dictionary",
                    },
                    "search_text": (
                        "purchase agreement supplier payment terms "
                        "condições de pagamento fornecedor"
                    ),
                }
            ],
            "edges": [],
        }
        search = HybridSearch(
            graph,
            semantic_text_selector=selector,
        )

        ranked = search.search(
            "condições de pagamento e fornecedor",
            limit=5,
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(model.queries, [])
        self.assertIn(
            "Audit columns",
            ranked[0]["summary"],
        )

        payload = search.build_prompt_context(
            "condições de pagamento e fornecedor",
            limit=5,
            max_characters=2000,
        )
        summary = payload["results"][0]["summary"]

        self.assertIn("Supplier information", summary)
        self.assertIn("Payment terms", summary)
        self.assertNotIn("Audit columns", summary)
        self.assertLessEqual(len(summary), 125)
        self.assertTrue(model.queries)


    def test_budget_is_distributed_between_relevant_groups(self):
        long_text = "Relevant documented passage. " * 200
        blocks = [
            {
                "id": "business:agreement",
                "node_type": "business_entity",
                "title": "Purchase Agreement",
                "score": 40.0,
                "summary": "",
                "source": {"source_type": "curated_entity_map"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {"entity_id": "purchase_agreement"},
                "_summary_source": long_text,
            },
            {
                "id": "column:amount",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.AMOUNT_RELEASED",
                "score": 12.0,
                "summary": "",
                "source": {"source_type": "oracle_data_dictionary"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {"qualified_name": "PO_HEADERS_ALL.AMOUNT_RELEASED"},
                "_summary_source": long_text,
            },
            {
                "id": "table:headers",
                "node_type": "physical_table",
                "title": "PO_HEADERS_ALL",
                "score": 8.0,
                "summary": "",
                "source": {"source_type": "oracle_data_dictionary"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {"primary_key": ["PO_HEADER_ID"]},
                "_summary_source": long_text,
            },
            {
                "id": "otbi:agreements",
                "node_type": "otbi_subject_area",
                "title": "Procurement - Purchasing Agreements Real Time",
                "score": 8.0,
                "summary": "",
                "source": {"source_type": "oracle_otbi_documentation"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {"transactional_grain": "Header and line."},
                "_summary_source": long_text,
            },
        ]

        selected = HybridSearch._diversify_selection(
            blocks,
            max_items=10,
            max_characters=6000,
            maximum_summary_characters=5000,
        )

        groups = {
            HybridSearch._context_evidence_group(block)
            for block in selected
        }
        allocated = {
            HybridSearch._context_evidence_group(block): block[
                "_summary_max_characters"
            ]
            for block in selected
        }

        self.assertEqual(
            groups,
            {
                "business_context",
                "physical_columns",
                "physical_tables",
                "otbi",
            },
        )
        self.assertGreater(
            allocated["business_context"],
            allocated["physical_columns"],
        )
        self.assertGreater(
            allocated["physical_columns"],
            allocated["physical_tables"],
        )
        self.assertLessEqual(
            sum(block["_allocated_characters"] for block in selected),
            6000,
        )
        self.assertEqual(
            [block["id"] for block in selected],
            [block["id"] for block in blocks],
        )

    def test_context_summary_source_ignores_structured_values(self):
        node = {
            "description": (
                "Purchase agreement header information includes supplier "
                "and payment terms."
            ),
            "transactional_grain": {
                "description": "One row per agreement.",
            },
            "time_reporting": (
                "Historical reporting uses the agreement document date."
            ),
            "business_rules": [
                {
                    "rule_type": "optimistic_locking",
                    "rule": "Used to implement optimistic locking.",
                    "source": "oracle_documentation",
                    "confidence": "high",
                }
            ],
            "conditions": [
                {
                    "column": "STATUS",
                    "operator": "=",
                    "value": "OPEN",
                }
            ],
        }

        result = HybridSearch._context_summary_source(node)

        self.assertEqual(
            result,
            (
                "Purchase agreement header information includes supplier "
                "and payment terms. Historical reporting uses the "
                "agreement document date."
            ),
        )
        self.assertNotIn("rule_type", result)
        self.assertNotIn("confidence", result)
        self.assertNotIn("One row per agreement", result)
        self.assertNotIn("STATUS", result)

    def test_context_uses_dynamic_summary_budget_and_hides_internal_fields(self):
        model = FakeEmbeddingModel()
        selector = SemanticTextSelector(
            SemanticContextConfig(
                maximum_segment_characters=180,
                summary_max_characters=500,
                mmr_lambda=0.90,
            ),
            model=model,
        )
        graph = {
            "nodes": [
                {
                    "id": "table:purchase-agreements-dynamic",
                    "node_type": "physical_table",
                    "name": "PURCHASE_AGREEMENTS_DYNAMIC",
                    "title": "PURCHASE_AGREEMENTS_DYNAMIC",
                    "description": (
                        "Supplier information identifies the trading partner. "
                        "Audit columns record technical updates. "
                        "Payment terms define the settlement conditions. "
                        "Agreement dates define the validity period. "
                        "Payment terms can also control scheduled settlement. "
                        "Supplier sites identify the purchasing location."
                    ),
                    "primary_key": ["AGREEMENT_ID"],
                    "result_grain": {
                        "description": "One row per agreement.",
                    },
                    "business_rules": [],
                    "confidence": "high",
                    "source": {
                        "source_type": "oracle_data_dictionary",
                    },
                    "search_text": (
                        "purchase agreement supplier payment terms "
                        "condições de pagamento fornecedor"
                    ),
                }
            ],
            "edges": [],
        }
        search = HybridSearch(
            graph,
            semantic_text_selector=selector,
        )

        payload = search.build_prompt_context(
            "condições de pagamento e fornecedor",
            limit=5,
            max_characters=550,
        )

        self.assertEqual(len(payload["results"]), 1)
        self.assertLessEqual(payload["characters"], 550)
        self.assertLess(
            len(payload["results"][0]["summary"]),
            500,
        )
        self.assertFalse(
            any(
                key.startswith("_")
                for key in payload["results"][0]
            )
        )
        self.assertTrue(model.queries)


if __name__ == "__main__":
    unittest.main()
