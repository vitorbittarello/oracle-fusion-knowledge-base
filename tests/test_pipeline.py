from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from oracle_knowledge.common import extract_heading_sections, write_json, write_jsonl
from oracle_knowledge.linker.knowledge_linker import build_graph
from oracle_knowledge.search.hybrid_search import HybridSearch


class SectionExtractionTest(unittest.TestCase):
    def test_extracts_hierarchy_lists_and_tables(self):
        soup = BeautifulSoup(
            """
            <html><body><main>
              <h1>Guide</h1><p>Overview text.</p>
              <h2>Business Rules</h2>
              <ul><li>Rule A</li><li>Rule B</li></ul>
              <table><tr><th>Name</th><th>Description</th></tr>
                     <tr><td>STATUS</td><td>Current status.</td></tr></table>
            </main></body></html>
            """,
            "html.parser",
        )
        sections = extract_heading_sections(soup)
        self.assertEqual(sections[0]["section_path"], ["Guide"])
        rules = next(section for section in sections if section["title"] == "Business Rules")
        self.assertEqual(rules["list_items"], ["Rule A", "Rule B"])
        self.assertEqual(rules["tables"][0][0]["name"], "STATUS")


class KnowledgePipelineTest(unittest.TestCase):
    def test_links_and_searches_validated_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            physical = root / "physical.json"
            functional = root / "functional.jsonl"
            otbi = root / "otbi.json"
            rest = root / "rest.json"
            rules = root / "rules.json"
            entities = root / "entities.json"

            write_json(
                physical,
                {
                    "metadata": {"release_version": "26B"},
                    "skills_catalog": [
                        {
                            "sub_module": "PPM",
                            "components": [
                                {
                                    "table_name": "PJO_PLAN_VERSIONS_VL",
                                    "description": "Financial plan versions.",
                                    "primary_key": ["PLAN_VERSION_ID"],
                                    "fields_to_extract": [
                                        "PLAN_VERSION_ID",
                                        "PROJECT_ID",
                                        "PLAN_CLASS_CODE",
                                        "PLAN_STATUS_CODE",
                                        "CURRENT_PLAN_STATUS_FLAG",
                                        "PROCESSING_TIME",
                                        "VERSION_NUMBER",
                                        "LAST_UPDATE_DATE",
                                    ],
                                    "columns": [
                                        {"name": "PROJECT_ID", "datatype": "NUMBER", "description": "Project identifier."}
                                    ],
                                    "column_semantics": [],
                                    "business_rules": [],
                                    "relationships": {
                                        "outgoing": [
                                            {
                                                "source_table": "PJO_PLAN_VERSIONS_VL",
                                                "source_column": "PROJECT_ID",
                                                "target_table": "PJF_PROJECTS_ALL_B",
                                                "target_column": "PROJECT_ID",
                                            }
                                        ],
                                        "incoming": [],
                                    },
                                    "source_url": "https://example/pjo",
                                },
                                {
                                    "table_name": "PJF_PROJECTS_ALL_B",
                                    "description": "Projects.",
                                    "primary_key": ["PROJECT_ID"],
                                    "fields_to_extract": ["PROJECT_ID", "SEGMENT1"],
                                    "columns": [],
                                    "column_semantics": [],
                                    "business_rules": [],
                                    "relationships": {"outgoing": [], "incoming": []},
                                    "source_url": "https://example/pjf",
                                },
                            ],
                        }
                    ],
                },
            )
            write_jsonl(
                functional,
                [
                    {
                        "id": "functional:budget",
                        "node_type": "functional_section",
                        "title": "Approved Budgets",
                        "text": "Approved project budgets are baselined.",
                        "table_mentions": ["PJO_PLAN_VERSIONS_VL"],
                        "source": {"source_type": "oracle_functional_documentation"},
                        "confidence": "high",
                        "search_text": "approved budget baselined PJO_PLAN_VERSIONS_VL",
                    }
                ],
            )
            write_json(
                otbi,
                {
                    "subject_areas": [
                        {
                            "id": "subject:budget",
                            "node_type": "otbi_subject_area",
                            "name": "Project Control - Budgets Real Time",
                            "title": "Project Control - Budgets Real Time",
                            "transactional_grain": "Budget planning line.",
                            "source": {"source_type": "oracle_otbi_documentation"},
                            "confidence": "high",
                            "search_text": "approved project budget",
                        }
                    ],
                    "business_questions": [
                        {
                            "id": "question:budget",
                            "node_type": "otbi_business_question",
                            "title": "What is the approved budget?",
                            "subject_areas": ["Project Control - Budgets Real Time"],
                            "source": {"source_type": "oracle_otbi_documentation"},
                            "confidence": "high",
                            "search_text": "approved budget",
                        }
                    ],
                    "other_pages": [],
                },
            )
            write_json(
                rest,
                {
                    "resources": [
                        {
                            "id": "resource:budgets",
                            "node_type": "rest_resource",
                            "name": "Project Budgets",
                            "title": "Project Budgets",
                            "source": {"source_type": "oracle_rest_documentation"},
                            "confidence": "high",
                            "search_text": "project budgets",
                        }
                    ],
                    "operations": [
                        {
                            "id": "operation:budget:get",
                            "node_type": "rest_operation",
                            "title": "Get all project budgets",
                            "resource_name": "Project Budgets",
                            "method": "GET",
                            "endpoint_path": "/fscmRestApi/resources/projectBudgets",
                            "source": {"source_type": "oracle_rest_documentation"},
                            "confidence": "high",
                            "search_text": "get project budgets",
                        }
                    ],
                },
            )
            write_json(
                rules,
                {
                    "rules": [
                        {
                            "id": "ppm.current_approved_budget_version",
                            "name": "Current approved budget",
                            "description": "Select the current approved budget per project.",
                            "tables": ["PJO_PLAN_VERSIONS_VL", "PJF_PROJECTS_ALL_B"],
                            "columns": ["PJO_PLAN_VERSIONS_VL.PROJECT_ID"],
                            "conditions": [
                                {"column": "PLAN_CLASS_CODE", "operator": "=", "value": "BUDGET"},
                                {"column": "PLAN_STATUS_CODE", "operator": "=", "value": "B"},
                            ],
                            "ranking": {"partition_by": ["PROJECT_ID"], "order_by": ["VERSION_NUMBER DESC"]},
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
                            "entity_id": "approved_budget_version",
                            "name": "Approved Budget Version",
                            "aliases": ["approved budget", "orçamento aprovado"],
                            "tables": ["PJO_PLAN_VERSIONS_VL"],
                            "subject_areas": ["Project Control - Budgets Real Time"],
                            "rest_resources": ["Project Budgets"],
                            "validated_rules": ["ppm.current_approved_budget_version"],
                        }
                    ]
                },
            )

            graph = build_graph(
                physical_manifest=str(physical),
                functional_fragments=str(functional),
                otbi_catalog=str(otbi),
                rest_catalog=str(rest),
                validated_rules=str(rules),
                entity_aliases=str(entities),
            )
            self.assertGreaterEqual(graph["stats"]["nodes"], 10)
            self.assertGreaterEqual(graph["stats"]["edges"], 8)

            search = HybridSearch(graph)
            results = search.search("orçamento aprovado atual por projeto", limit=10)
            ids = [result["id"] for result in results]
            self.assertIn("ppm.current_approved_budget_version", ids)
            context = search.build_prompt_context(
                "orçamento aprovado atual por projeto",
                limit=10,
            )["context"]
            self.assertIn("validated_environment_rule", context)
            self.assertIn("PLAN_CLASS_CODE", context)


if __name__ == "__main__":
    unittest.main()
