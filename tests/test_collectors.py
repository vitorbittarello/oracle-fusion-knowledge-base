from __future__ import annotations

import json
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


class FakeJsonResponse:
    def __init__(self, url, payload, status_code=200):
        self.url = url
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class FakeJsonSession:
    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.auth = None
        self.calls = []

    def mount(self, prefix, adapter):
        return None

    def get(self, url, headers=None, timeout=None, verify=True):
        self.calls.append(url)
        response = self.pages[url]
        if isinstance(response, tuple):
            payload, status_code = response
        else:
            payload, status_code = response, 200
        return FakeJsonResponse(url, payload, status_code=status_code)


class AdfMetadataCollectorTest(unittest.TestCase):
    def test_collects_catalog_and_resource_describes_automatically(self):
        import tempfile
        from pathlib import Path

        from oracle_knowledge.collectors.adf_metadata_collector import (
            AdfMetadataCollector,
        )

        base_url = "https://fusion.example.test"
        catalog_url = (
            f"{base_url}/fscmRestApi/resources/latest/describe"
            "?metadataMode=minimal&includeChildren=true"
        )
        custom_url = (
            f"{base_url}/fscmRestApi/resources/latest/APCUSTOMBM_c/describe"
        )
        purchase_orders_url = (
            f"{base_url}/fscmRestApi/resources/latest/purchaseOrders/describe"
        )
        pages = {
            catalog_url: {
                "Resources": {
                    "APCUSTOMBM_c": {
                        "title": "AP CUSTOM BM",
                        "children": {
                            "Attachment": {"title": "Anexos"}
                        },
                    },
                    "purchaseOrders": {"title": "Purchase Orders"},
                }
            },
            custom_url: {
                "Resources": {
                    "APCUSTOMBM_c": {
                        "title": "AP CUSTOM BM",
                        "attributes": [
                            {
                                "name": "Id",
                                "type": "number",
                                "mandatory": True,
                                "queryable": True,
                            },
                            {
                                "name": "ExternalReference_c",
                                "type": "string",
                                "title": "External Reference",
                                "maxLength": 80,
                            },
                        ],
                        "children": {
                            "Attachment": {"title": "Anexos"}
                        },
                    }
                }
            },
            purchase_orders_url: {
                "Resources": {
                    "purchaseOrders": {
                        "title": "Purchase Orders",
                        "attributes": [
                            {"name": "OrderNumber", "type": "string"}
                        ],
                        "children": {
                            "DFF": {"title": "Descriptive Flexfields"}
                        },
                    }
                }
            },
        }
        session = FakeJsonSession(pages)
        collector = AdfMetadataCollector(
            base_url=base_url,
            username="user",
            password="secret",
            session=session,
            delay_seconds=0,
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            payload = collector.collect(temporary_dir)
            root = Path(temporary_dir)

            self.assertEqual(payload["stats"]["catalog_resources"], 2)
            self.assertEqual(payload["stats"]["collected_resources"], 2)
            self.assertEqual(payload["stats"]["custom_attributes"], 1)
            self.assertEqual(payload["stats"]["flexfield_children"], 1)
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "catalog.json").is_file())
            self.assertTrue(
                (root / "raw/resources/APCUSTOMBM_c.json").is_file()
            )
            self.assertTrue((root / "modules/unclassified.json").is_file())
            unclassified = json.loads(
                (root / "modules/unclassified.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                unclassified["resources"],
                ["APCUSTOMBM_c", "purchaseOrders"],
            )

            custom = next(
                item
                for item in payload["resources"]
                if item["name"] == "APCUSTOMBM_c"
            )
            self.assertTrue(custom["is_custom"])
            self.assertEqual(
                custom["attributes"][1]["title"],
                "External Reference",
            )

    def test_custom_only_limits_describe_requests_and_resume_reuses_files(self):
        import tempfile

        from oracle_knowledge.collectors.adf_metadata_collector import (
            AdfMetadataCollector,
        )

        base_url = "https://fusion.example.test"
        catalog_url = (
            f"{base_url}/fscmRestApi/resources/latest/describe"
            "?metadataMode=minimal&includeChildren=true"
        )
        custom_url = (
            f"{base_url}/fscmRestApi/resources/latest/APCUSTOMBM_c/describe"
        )
        pages = {
            catalog_url: {
                "Resources": {
                    "APCUSTOMBM_c": {"title": "AP CUSTOM BM"},
                    "purchaseOrders": {"title": "Purchase Orders"},
                }
            },
            custom_url: {
                "Resources": {
                    "APCUSTOMBM_c": {
                        "title": "AP CUSTOM BM",
                        "attributes": [],
                    }
                }
            },
        }
        session = FakeJsonSession(pages)
        collector = AdfMetadataCollector(
            base_url=base_url,
            username="user",
            password="secret",
            session=session,
            delay_seconds=0,
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            first = collector.collect(temporary_dir, custom_only=True)
            first_call_count = len(session.calls)
            second = collector.collect(temporary_dir, custom_only=True)

            self.assertEqual(first["stats"]["selected_resources"], 1)
            self.assertEqual(second["stats"]["reused_resources"], 1)
            self.assertEqual(len(session.calls), first_call_count)
