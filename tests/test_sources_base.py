"""Tests for BaseSource request retry/backoff behavior.

Run with:  python -m unittest discover tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from enrichment.sources import base as base_mod
from enrichment.sources.base import BaseSource


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class _Source(BaseSource):
    source_name = "test"
    min_request_interval = 0.01

    def fetch(self, faculty_dict):
        return None

    def fields_provided(self):
        return []


class BackoffTests(unittest.TestCase):
    def _source(self, responses):
        src = _Source()
        src._session = _Session(responses)
        return src

    def test_429_is_retried_and_slows_the_run_down(self):
        src = self._source([_Resp(429, {"Retry-After": "1"}), _Resp(200)])
        with mock.patch.object(base_mod.time, "sleep") as sleep:
            resp = src._get("http://example.test/x")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(src._session.calls, 2)
        # Backoff slept at least the exponential floor (Retry-After was 1s,
        # floor for attempt 0 is 2s).
        backoffs = [c.args[0] for c in sleep.call_args_list if c.args[0] >= 2]
        self.assertTrue(backoffs)
        # Adaptive politeness: the per-request interval doubled.
        self.assertAlmostEqual(src.min_request_interval, 0.02)

    def test_retry_after_header_is_honored(self):
        src = self._source([_Resp(429, {"Retry-After": "7"}), _Resp(200)])
        with mock.patch.object(base_mod.time, "sleep") as sleep:
            src._get("http://example.test/x")
        self.assertIn(7.0, [c.args[0] for c in sleep.call_args_list])

    def test_gives_up_after_max_attempts(self):
        src = self._source([_Resp(429), _Resp(429), _Resp(429)])
        with mock.patch.object(base_mod.time, "sleep"):
            resp = src._get("http://example.test/x")
        self.assertIsNone(resp)
        self.assertEqual(src._session.calls, 3)

    def test_503_is_retried(self):
        src = self._source([_Resp(503), _Resp(200)])
        with mock.patch.object(base_mod.time, "sleep"):
            resp = src._get("http://example.test/x")
        self.assertEqual(resp.status_code, 200)
        # 503 is transient, not throttling: interval stays put.
        self.assertAlmostEqual(src.min_request_interval, 0.01)

    def test_other_errors_are_not_retried(self):
        src = self._source([_Resp(404), _Resp(200)])
        with mock.patch.object(base_mod.time, "sleep"):
            resp = src._get("http://example.test/x")
        self.assertIsNone(resp)
        self.assertEqual(src._session.calls, 1)


class _FakeJSONResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class OpenAlexAlternateProfileTests(unittest.TestCase):
    def test_fetch_merges_alternate_profiles(self):
        from enrichment.sources.openalex import (OpenAlexSource, AUTHORS_URL,
                                                 WORKS_URL)
        src = OpenAlexSource()
        seen = []

        def fake_get(url, params=None):
            seen.append((url, dict(params or {})))
            if url == f"{AUTHORS_URL}/A1":
                return _FakeJSONResp({
                    "id": "https://openalex.org/A1", "works_count": 100,
                    "cited_by_count": 1000, "summary_stats": {"h_index": 20}})
            if url == f"{AUTHORS_URL}/A2":
                return _FakeJSONResp({
                    "id": "https://openalex.org/A2", "works_count": 5,
                    "cited_by_count": 50, "summary_stats": {"h_index": 3}})
            if url == WORKS_URL:
                return _FakeJSONResp({"results": [
                    {"display_name": "Paper", "publication_year": 2025}]})
            return None

        src._get = fake_get
        result = src.fetch({"openalex_id": "A1", "openalex_id_alt": ["A2"],
                            "first_name": "Jane", "last_name": "Smith"})
        # Counts summed across profiles, h_index is the max.
        self.assertEqual(result["works_count"], 105)
        self.assertEqual(result["citation_count"], 1050)
        self.assertEqual(result["h_index"], 20)
        # Works are fetched across both profiles in one OR-filter request.
        works_params = next(p for (u, p) in seen if u == WORKS_URL)
        self.assertEqual(works_params["filter"],
                         "authorships.author.id:A1|A2")

    def test_fetch_without_alternates_unchanged(self):
        from enrichment.sources.openalex import (OpenAlexSource, AUTHORS_URL,
                                                 WORKS_URL)
        src = OpenAlexSource()
        seen = []

        def fake_get(url, params=None):
            seen.append((url, dict(params or {})))
            if url == f"{AUTHORS_URL}/A1":
                return _FakeJSONResp({
                    "id": "https://openalex.org/A1", "works_count": 100})
            if url == WORKS_URL:
                return _FakeJSONResp({"results": []})
            return None

        src._get = fake_get
        result = src.fetch({"openalex_id": "A1",
                            "first_name": "Jane", "last_name": "Smith"})
        self.assertEqual(result["works_count"], 100)
        works_params = next(p for (u, p) in seen if u == WORKS_URL)
        self.assertEqual(works_params["filter"], "authorships.author.id:A1")


if __name__ == "__main__":
    unittest.main()
