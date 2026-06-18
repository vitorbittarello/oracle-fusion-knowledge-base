from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from oracle_knowledge.collectors.otbi_collector import OtbiCollector
from oracle_knowledge.collectors.rest_collector import RestCollector


class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def get_soup(self, url, force_refresh=False):
        return BeautifulSoup(self.pages[url], "html.parser"), {
            "fetched_at": "2026-06-16T00:00:00+00:00",
            "last_modified": None,
        }


class CollectorParsingTest(unittest.TestCase):
    def test_otbi_subject_area_and_business_question(self):
        toc = "https://example.test/faopm/toc.htm"
        subject = "https://example.test/faopm/budgets-SA-1.html"
        question = "https://example.test/faopm/approved-BQ-1.html"
        pages = {
            toc: f"""<html><body><ul>
                <li><a href="{subject}">Project Control - Budgets Real Time</a></li>
                <li><a href="{question}">What is the approved budget?</a></li>
            </ul></body></html>""",
            subject: """<html><body><main>
                <h1>Project Control - Budgets Real Time</h1>
                <h2>Description</h2><p>Analyze project budgets.</p>
                <h2>Business Questions</h2><ul><li>What is the approved budget?</li></ul>
                <h2>Job Roles</h2><ul><li>Project Manager</li></ul>
                <h2>Duty Roles</h2><ul><li>Project Budget Transaction Analysis Duty</li></ul>
                <h2>Time Reporting</h2><p>Current data.</p>
                <h2>Transactional Grain</h2><p>Budget planning line.</p>
            </main></body></html>""",
            question: """<html><body><main>
                <h1>What is the approved budget?</h1>
                <h2>Subject Areas</h2><ul><li>Project Control - Budgets Real Time</li></ul>
                <h2>Job Roles</h2><ul><li>Project Manager</li></ul>
            </main></body></html>""",
        }
        collector = OtbiCollector()
        collector.client = FakeClient(pages)
        payload = collector.collect({"toc_url": toc, "release": "26B"})
        self.assertEqual(payload["stats"]["subject_areas"], 1)
        self.assertEqual(payload["stats"]["business_questions"], 1)
        self.assertEqual(
            payload["subject_areas"][0]["transactional_grain"],
            "Budget planning line.",
        )
        self.assertEqual(
            payload["business_questions"][0]["subject_areas"],
            ["Project Control - Budgets Real Time"],
        )

    def test_rest_operation_and_resource(self):
        toc = "https://example.test/fapap/toc.htm"
        operation = "https://example.test/fapap/op-projectbudgets-get.html"
        pages = {
            toc: f"""<html><body><ul><li><a href="#">Tasks</a><ul>
                <li><a href="#">Project Budgets</a><ul>
                    <li><a href="{operation}">Get all project budgets</a></li>
                </ul></li>
            </ul></li></ul></body></html>""",
            operation: """<html><body><main>
                <h1>Get all project budgets</h1>
                <p>get</p>
                <p>/fscmRestApi/resources/11.13.18.05/projectBudgets</p>
                <p>Gets all project budgets.</p>
                <h2>Request</h2>
                <h3>Query Parameters</h3>
                <table><tr><th>Name</th><th>Type</th><th>Description</th></tr>
                    <tr><td>q</td><td>string</td><td>Filter expression.</td></tr></table>
                <h2>Response</h2>
                <h3>Nested Schema : items</h3>
                <table><tr><th>Name</th><th>Type</th><th>Description</th></tr>
                    <tr><td>PlanVersionId</td><td>integer</td><td>Version identifier.</td></tr></table>
            </main></body></html>""",
        }
        collector = RestCollector()
        collector.client = FakeClient(pages)
        payload = collector.collect({"toc_url": toc, "release": "26B"})
        self.assertEqual(payload["stats"]["operations"], 1)
        op = payload["operations"][0]
        self.assertEqual(op["method"], "GET")
        self.assertIn("projectBudgets", op["endpoint_path"])
        self.assertEqual(op["parameters"][0]["name"], "q")
        self.assertEqual(op["attributes"][0]["name"], "PlanVersionId")


if __name__ == "__main__":
    unittest.main()
