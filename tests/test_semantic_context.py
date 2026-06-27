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


class CandidateEmbeddingModel:
    def encode(self, texts, **kwargs):
        if texts and texts[0].startswith("Instruct:"):
            return np.asarray(
                [[1.0, 0.0, 0.0]],
                dtype=np.float32,
            )

        vectors = []

        for text in texts:
            normalized = text.casefold()

            if any(
                term in normalized
                for term in (
                    "purchase agreement",
                    "payment terms",
                    "amount released",
                    "supplier identifier",
                    "agreement header",
                    "purchasing agreements real time",
                )
            ):
                vectors.append([1.0, 0.0, 0.0])
            elif any(
                term in normalized
                for term in (
                    "buyer job role",
                    "category manager role",
                    "purchasing options window",
                )
            ):
                vectors.append([0.05, 0.95, 0.0])
            else:
                vectors.append([0.45, 0.55, 0.0])

        return np.asarray(vectors, dtype=np.float32)


class SemanticTextSelectorTest(unittest.TestCase):
    def test_score_documents_uses_most_relevant_segments(self):
        selector = SemanticTextSelector(
            SemanticContextConfig(
                candidate_top_segments=1,
            ),
            model=CandidateEmbeddingModel(),
        )
        scores = selector.score_documents(
            "condições de pagamento do acordo",
            [
                (
                    "Audit information records technical updates. "
                    "Payment terms define settlement for the purchase agreement."
                ),
                "Buyer job role grants access to procurement pages.",
            ],
        )

        self.assertEqual(len(scores), 2)
        self.assertGreater(scores[0], scores[1])

    def test_semantic_reranking_filters_low_relevance_tail(self):
        selector = SemanticTextSelector(
            SemanticContextConfig(
                candidate_rerank_weight=0.80,
                candidate_minimum_relative_score=0.55,
                candidate_preserve_top_results=1,
                candidate_group_score_ratio=0.10,
                candidate_top_segments=1,
            ),
            model=CandidateEmbeddingModel(),
        )
        search = HybridSearch(
            {"nodes": [], "edges": []},
            semantic_text_selector=selector,
        )

        def result(
                identifier,
                node_type,
                title,
                score,
                description,
                source_type,
        ):
            return {
                "id": identifier,
                "node_type": node_type,
                "title": title,
                "score": score,
                "summary": description,
                "source": {"source_type": source_type},
                "sources": [],
                "modules": ["procurement"],
                "node": {
                    "id": identifier,
                    "node_type": node_type,
                    "title": title,
                    "description": description,
                    "source": {"source_type": source_type},
                },
            }

        ranked = search._semantic_rerank_results(
            "acordo de compra valor liberado fornecedor condições de pagamento",
            [
                result(
                    "entity:agreement",
                    "business_entity",
                    "Purchase Agreement",
                    40.0,
                    "Purchase agreement business meaning and supplier context.",
                    "curated_entity_map",
                ),
                result(
                    "column:amount",
                    "physical_column",
                    "PO_HEADERS_ALL.AMOUNT_RELEASED",
                    12.0,
                    "Amount released against the purchase agreement.",
                    "oracle_data_dictionary",
                ),
                result(
                    "table:headers",
                    "physical_table",
                    "PO_HEADERS_ALL",
                    8.0,
                    "Agreement header with supplier and payment terms.",
                    "oracle_data_dictionary",
                ),
                result(
                    "otbi:agreements",
                    "otbi_subject_area",
                    "Procurement - Purchasing Agreements Real Time",
                    8.0,
                    "Purchasing agreements real time subject area.",
                    "oracle_otbi_documentation",
                ),
                result(
                    "otbi:buyer",
                    "otbi_reference_page",
                    "Buyer",
                    7.0,
                    "Buyer job role grants security access.",
                    "oracle_otbi_documentation",
                ),
                result(
                    "otbi:category-manager",
                    "otbi_reference_page",
                    "Category Manager",
                    7.0,
                    "Category manager role secures sourcing pages.",
                    "oracle_otbi_documentation",
                ),
                result(
                    "table:parameters",
                    "physical_table",
                    "PO_SYSTEM_PARAMETERS_ALL",
                    6.8,
                    "Purchasing options window system configuration.",
                    "oracle_data_dictionary",
                ),
            ],
        )
        identifiers = [item["id"] for item in ranked]

        self.assertEqual(
            identifiers,
            [
                "entity:agreement",
                "column:amount",
                "table:headers",
                "otbi:agreements",
            ],
        )
        self.assertTrue(
            all("semantic_score" in item for item in ranked)
        )
        self.assertTrue(
            all("context_score" in item for item in ranked)
        )
        self.assertNotIn("otbi:buyer", identifiers)
        self.assertNotIn("table:parameters", identifiers)

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


    def test_group_coverage_is_followed_by_global_ranking(self):
        long_text = "Relevant documented passage. " * 20
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
                "evidence": {
                    "qualified_name": "PO_HEADERS_ALL.AMOUNT_RELEASED",
                },
                "_summary_source": long_text,
            },
            {
                "id": "column:terms",
                "node_type": "physical_column",
                "title": "PO_HEADERS_ALL.TERMS_ID",
                "score": 11.0,
                "summary": "",
                "source": {"source_type": "oracle_data_dictionary"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {
                    "qualified_name": "PO_HEADERS_ALL.TERMS_ID",
                },
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
            {
                "id": "otbi:buyer",
                "node_type": "otbi_reference_page",
                "title": "Buyer",
                "score": 6.0,
                "summary": "",
                "source": {"source_type": "oracle_otbi_documentation"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {},
                "_summary_source": long_text,
            },
            {
                "id": "table:style",
                "node_type": "physical_table",
                "title": "PO_DOC_STYLE_HEADERS",
                "score": 5.0,
                "summary": "",
                "source": {"source_type": "oracle_data_dictionary"},
                "sources": [],
                "modules": ["procurement"],
                "evidence": {"primary_key": ["STYLE_ID"]},
                "_summary_source": long_text,
            },
        ]

        selected = HybridSearch._diversify_selection(
            blocks,
            max_items=5,
            max_characters=6000,
            query="agreement supplier payment terms",
            maximum_summary_characters=500,
        )

        self.assertEqual(
            [block["id"] for block in selected],
            [
                "business:agreement",
                "column:amount",
                "column:terms",
                "table:headers",
                "otbi:agreements",
            ],
        )
        self.assertNotIn(
            "otbi:buyer",
            [block["id"] for block in selected],
        )
        self.assertNotIn(
            "table:style",
            [block["id"] for block in selected],
        )

        rows = []
        for block in selected:
            row = HybridSearch._public_context_block(block)
            row["summary"] = "x" * block["_summary_max_characters"]
            rows.append(row)

        rendered = HybridSearch._render_prompt_context(
            "agreement supplier payment terms",
            rows,
        )
        self.assertLessEqual(len(rendered), 6000)

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
            max_characters=800,
        )

        self.assertEqual(len(payload["results"]), 1)
        self.assertLessEqual(payload["characters"], 800)
        self.assertEqual(
            payload["characters"],
            len(payload["context"]),
        )
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
