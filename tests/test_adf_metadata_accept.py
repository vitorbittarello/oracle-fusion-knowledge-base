from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

import requests

from oracle_knowledge.collectors.adf_metadata_collector import AdfMetadataCollector


class FakeJsonResponse:
    def __init__(self, url, payload, status_code, request_headers):
        self.url = url
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.request = SimpleNamespace(headers=request_headers)

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class NegotiatingSession:
    def __init__(self):
        self.headers = {}
        self.auth = None
        self.calls = []

    def mount(self, prefix, adapter):
        return None

    def get(self, url, headers=None, timeout=None, verify=True):
        request_headers = dict(headers or {})
        self.calls.append({"url": url, "headers": request_headers})

        if request_headers.get("Accept") == "*/*":
            return FakeJsonResponse(url, {}, 406, request_headers)

        return FakeJsonResponse(
            url,
            {"Resources": {}},
            200,
            request_headers,
        )


class AdfMetadataAcceptTest(unittest.TestCase):
    def test_retries_after_406_without_content_type_on_get(self):
        session = NegotiatingSession()
        collector = AdfMetadataCollector(
            base_url="https://fusion.example.test",
            username="user",
            password="secret",
            session=session,
            delay_seconds=0,
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            payload = collector.collect(temporary_dir, catalog_only=True)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["headers"]["Accept"], "*/*")
        self.assertEqual(
            session.calls[1]["headers"]["Accept"],
            "application/json",
        )
        self.assertNotIn("Content-Type", session.headers)
        self.assertEqual(payload["stats"]["catalog_resources"], 0)


if __name__ == "__main__":
    unittest.main()
