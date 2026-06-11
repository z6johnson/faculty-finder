"""Tests for the identity-resolution recall fallbacks and the terminal
no-footprint disposition.

Run with:  python -m unittest discover tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from enrichment import identity, identity_rules
from utils.names import search_name_variants
from tests.test_enrichment_expansion import _TempDBTestCase


class SearchNameVariantsTests(unittest.TestCase):
    def test_middle_name_and_initial(self):
        variants = search_name_variants("Mary Anne", "Smith")
        self.assertIn(("Mary", "Smith"), variants)
        self.assertIn(("M", "Smith"), variants)

    def test_compound_last_name(self):
        variants = search_name_variants("Jane", "Garcia Lopez")
        self.assertIn(("Jane", "GarciaLopez"), variants)
        self.assertIn(("Jane", "Lopez"), variants)
        self.assertIn(("Jane", "Garcia"), variants)

    def test_hyphenated_last_name(self):
        variants = search_name_variants("Wael", "Al-Delaimy")
        self.assertIn(("Wael", "AlDelaimy"), variants)
        self.assertIn(("Wael", "Delaimy"), variants)

    def test_excludes_original_and_dedupes(self):
        variants = search_name_variants("Jane", "Smith")
        self.assertNotIn(("Jane", "Smith"), variants)
        self.assertEqual(len(variants), len(set(variants)))

    def test_empty_names(self):
        self.assertEqual(search_name_variants("", "Smith"), [])
        self.assertEqual(search_name_variants("Jane", ""), [])


def _author(author_id, display_name, ucsd_current=False, ucsd_listed=False,
            works_count=10):
    ucsd = {"id": f"https://openalex.org/{identity.UCSD_INSTITUTION_ID}",
            "display_name": "University of California, San Diego"}
    other = {"id": "https://openalex.org/I97018004",
             "display_name": "Stanford University"}
    return {
        "id": f"https://openalex.org/{author_id}",
        "display_name": display_name,
        "display_name_alternatives": [],
        "last_known_institutions": [ucsd if ucsd_current else other],
        "affiliations": ([{"institution": ucsd}]
                         if (ucsd_current or ucsd_listed)
                         else [{"institution": other}]),
        "topics": [],
        "works_count": works_count,
        "cited_by_count": 100,
    }


class _FakeResp:
    def __init__(self, results):
        self._results = results

    def json(self):
        return {"results": self._results}


class _FakeOpenAlexSearch:
    """Canned author-search responses keyed by (search, has_ucsd_filter)."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.calls = []

    def _params(self, extra=None):
        return dict(extra or {})

    def _get(self, url, params=None):
        params = params or {}
        key = (params.get("search"), "filter" in params)
        self.calls.append(key)
        return _FakeResp(self.by_query.get(key, []))


def _resolver_with(by_query):
    resolver = identity.IdentityResolver.__new__(identity.IdentityResolver)
    resolver._openalex = _FakeOpenAlexSearch(by_query)
    resolver._orcid = None
    return resolver


class OpenAlexRecallFallbackTests(unittest.TestCase):
    FACULTY = {"first_name": "Mary Anne", "last_name": "Smith",
               "department": "som"}

    def test_primary_hit_spends_no_fallback_searches(self):
        resolver = _resolver_with({
            ("Mary Anne Smith", True): [_author("A1", "Mary Anne Smith",
                                                ucsd_current=True)],
        })
        candidates = resolver._openalex_candidates(self.FACULTY)
        self.assertEqual([c["external_id"] for c in candidates], ["A1"])
        self.assertNotIn("via_search", candidates[0]["evidence"])
        self.assertEqual(len(resolver._openalex.calls), 1)

    def test_variant_query_recalls_differently_indexed_profile(self):
        resolver = _resolver_with({
            ("Mary Anne Smith", True): [],
            ("Mary Smith", True): [_author("A2", "Mary Smith",
                                           ucsd_current=True)],
        })
        candidates = resolver._openalex_candidates(self.FACULTY)
        self.assertEqual([c["external_id"] for c in candidates], ["A2"])
        self.assertEqual(candidates[0]["evidence"]["via_search"],
                         "name_variant:Mary Smith")

    def test_variant_hits_dedupe_across_queries(self):
        author = _author("A2", "Mary Smith", ucsd_current=True)
        resolver = _resolver_with({
            ("Mary Anne Smith", True): [],
            ("Mary Smith", True): [author],
            ("M Smith", True): [author],
        })
        candidates = resolver._openalex_candidates(self.FACULTY)
        self.assertEqual([c["external_id"] for c in candidates], ["A2"])

    def test_unfiltered_search_requires_ucsd_evidence(self):
        # New hire: last_known_institution is elsewhere, so the ROR-filtered
        # queries miss, but the affiliation history lists UCSD. A same-name
        # author with no UCSD evidence at all must not enter the queue.
        resolver = _resolver_with({
            ("Mary Anne Smith", False): [
                _author("A3", "Mary Anne Smith", ucsd_listed=True),
                _author("A4", "Mary Anne Smith"),
            ],
        })
        candidates = resolver._openalex_candidates(self.FACULTY)
        self.assertEqual([c["external_id"] for c in candidates], ["A3"])
        self.assertEqual(candidates[0]["evidence"]["via_search"],
                         "no_institution_filter")
        self.assertTrue(candidates[0]["evidence"]["ucsd_listed"])

    def test_variant_search_budget_is_capped(self):
        resolver = _resolver_with({})
        resolver._openalex_candidates(
            {"first_name": "Anna Maria Luisa",
             "last_name": "Garcia-Lopez De-La-Cruz", "department": "som"})
        filtered = [c for c in resolver._openalex.calls if c[1]]
        # primary + at most MAX_VARIANT_SEARCHES variant queries
        self.assertLessEqual(len(filtered),
                             1 + identity.MAX_VARIANT_SEARCHES)


class NoFootprintRuleTests(unittest.TestCase):
    def test_non_pi_eligible_qualifies(self):
        self.assertTrue(identity_rules.no_research_footprint(
            {"pi_eligible": 0, "title": "Assistant Researcher"}))

    def test_emeritus_title_qualifies(self):
        self.assertTrue(identity_rules.no_research_footprint(
            {"pi_eligible": 1, "title": "Professor Emeritus"}))
        self.assertTrue(identity_rules.no_research_footprint(
            {"job_code_description": "PROF EMERITUS-HCOMP"}))

    def test_active_pi_does_not_qualify(self):
        self.assertFalse(identity_rules.no_research_footprint(
            {"pi_eligible": 1, "title": "Professor"}))

    def test_unknown_pi_eligibility_does_not_qualify(self):
        self.assertFalse(identity_rules.no_research_footprint(
            {"pi_eligible": None, "title": "Professor"}))


class MarkNoFootprintTests(_TempDBTestCase):
    def _seed(self, status="not_found", **overrides):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        fields = {"first_name": "Jane", "last_name": "Smith",
                  "email": "jsmith@ucsd.edu", "title": "Professor Emeritus",
                  "pi_eligible": 0}
        fields.update(overrides)
        fid = db.upsert_faculty(conn, "som", fields)
        db.set_identity_status(conn, fid, status)
        conn.commit()
        return conn, fid

    def _status(self, conn, fid):
        return conn.execute("SELECT identity_status FROM faculty WHERE id=?",
                            (fid,)).fetchone()[0]

    def test_marks_and_logs_not_found_without_footprint(self):
        conn, fid = self._seed()
        marked = identity.mark_no_footprint(conn)
        self.assertEqual(marked, 1)
        self.assertEqual(self._status(conn, fid), "no_footprint")
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE faculty_id=?", (fid,))]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["method"], "identity_no_footprint_rule")
        self.assertEqual(logs[0]["new_value"], "no_footprint")
        conn.close()

    def test_research_bearing_not_found_stays(self):
        conn, fid = self._seed(title="Professor", pi_eligible=1)
        self.assertEqual(identity.mark_no_footprint(conn), 0)
        self.assertEqual(self._status(conn, fid), "not_found")
        conn.close()

    def test_ambiguous_rows_untouched(self):
        conn, fid = self._seed(status="ambiguous")
        self.assertEqual(identity.mark_no_footprint(conn), 0)
        self.assertEqual(self._status(conn, fid), "ambiguous")
        conn.close()

    def test_resweep_applies_terminal_disposition(self):
        conn, fid = self._seed()
        stats = identity.resweep_pending(orcid_source=object())
        self.assertEqual(stats["no_footprint_marked"], 1)
        self.assertEqual(self._status(conn, fid), "no_footprint")
        conn.close()

    def test_resweep_can_skip_terminal_disposition(self):
        conn, fid = self._seed()
        stats = identity.resweep_pending(orcid_source=object(),
                                         mark_terminal=False)
        self.assertEqual(stats["no_footprint_marked"], 0)
        self.assertEqual(self._status(conn, fid), "not_found")
        conn.close()

    def test_empty_research_preserves_no_footprint_status(self):
        # A still-empty re-search must not demote the terminal status back
        # into the retried-weekly pool.
        conn, fid = self._seed(status="no_footprint")
        resolver = identity.IdentityResolver.__new__(identity.IdentityResolver)
        resolver._openalex_candidates = lambda f: []
        resolver._orcid_fallback = lambda f: None
        faculty = {"_db_id": fid, "identity_status": "no_footprint",
                   "first_name": "Jane", "last_name": "Smith"}
        outcome = resolver.resolve(conn, faculty)
        self.assertEqual(outcome, "not_found")
        self.assertEqual(self._status(conn, fid), "no_footprint")
        conn.close()


if __name__ == "__main__":
    unittest.main()
