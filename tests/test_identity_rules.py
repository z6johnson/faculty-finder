"""Tests for the conservative identity auto-accept rules.

Run with:  python -m unittest discover tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from enrichment import identity_rules
from tests.test_enrichment_expansion import _TempDBTestCase


def _candidate(external_id="A1", score=0.9, display_name="Jane Smith",
               source="openalex", **evidence):
    evidence.setdefault("display_name", display_name)
    return {"source": source, "external_id": external_id, "score": score,
            "display_name": display_name, "evidence": evidence}


FACULTY = {"first_name": "Jane", "last_name": "Smith"}


class IdentityRulesTests(unittest.TestCase):
    def test_same_person_by_equal_orcid(self):
        a = _candidate("A1", orcid="0000-0001-1111-2222")
        b = _candidate("A2", orcid="0000-0001-1111-2222")
        self.assertTrue(identity_rules.is_same_person(a, b))

    def test_different_orcids_never_collapse_even_with_same_name(self):
        a = _candidate("A1", orcid="0000-0001-1111-2222", ucsd_listed=True,
                       topic_domains=["Health Sciences"])
        b = _candidate("A2", orcid="0000-0002-3333-4444", ucsd_listed=True,
                       topic_domains=["Health Sciences"])
        self.assertFalse(identity_rules.is_same_person(a, b))

    def test_same_name_requires_ucsd_and_domain_overlap(self):
        a = _candidate("A1", ucsd_listed=True, topic_domains=["Health Sciences"])
        b = _candidate("A2", ucsd_listed=True, topic_domains=["Health Sciences"])
        self.assertTrue(identity_rules.is_same_person(a, b))
        # No domain overlap -> not provably the same person.
        c = _candidate("A3", ucsd_listed=True, topic_domains=["Social Sciences"])
        self.assertFalse(identity_rules.is_same_person(a, c))
        # One side not UCSD-affiliated.
        d = _candidate("A4", topic_domains=["Health Sciences"])
        self.assertFalse(identity_rules.is_same_person(a, d))
        # Different primary display names.
        e = _candidate("A5", display_name="J. Smith", ucsd_listed=True,
                       topic_domains=["Health Sciences"])
        self.assertFalse(identity_rules.is_same_person(a, e))

    def test_name_branch_kill_switch(self):
        a = _candidate("A1", ucsd_listed=True, topic_domains=["Health Sciences"])
        b = _candidate("A2", ucsd_listed=True, topic_domains=["Health Sciences"])
        old = identity_rules.NAME_TIE_COLLAPSE
        identity_rules.NAME_TIE_COLLAPSE = False
        try:
            self.assertFalse(identity_rules.is_same_person(a, b))
        finally:
            identity_rules.NAME_TIE_COLLAPSE = old

    def test_missing_evidence_means_not_same_person(self):
        self.assertFalse(identity_rules.is_same_person(
            {"source": "openalex", "external_id": "A1", "score": 0.9},
            {"source": "openalex", "external_id": "A2", "score": 0.88}))

    def test_collapse_picks_higher_works_count(self):
        a = _candidate("A1", score=0.95, orcid="0000-0001-1111-2222",
                       works_count=12)
        b = _candidate("A2", score=0.93, orcid="0000-0001-1111-2222",
                       works_count=200)
        out = identity_rules.collapse_duplicate_ties([a, b], tie_margin=0.05)
        self.assertTrue(out["collapsed"])
        self.assertEqual(out["canonical"]["external_id"], "A2")
        self.assertEqual(out["effective_score"], 0.95)
        self.assertEqual(out["runner_up"], 0.0)
        self.assertEqual(set(out["cluster_ids"]), {"A1", "A2"})

    def test_collapse_passthrough_without_tie(self):
        a = _candidate("A1", score=0.95)
        b = _candidate("A2", score=0.7)
        out = identity_rules.collapse_duplicate_ties([a, b], tie_margin=0.05)
        self.assertFalse(out["collapsed"])
        self.assertEqual(out["canonical"]["external_id"], "A1")
        self.assertEqual(out["runner_up"], 0.7)

    def test_rival_failing_same_person_keeps_runner_up_high(self):
        a = _candidate("A1", score=0.92, orcid="0000-0001-1111-2222")
        b = _candidate("A2", score=0.9, orcid="0000-0002-3333-4444")
        out = identity_rules.collapse_duplicate_ties([a, b], tie_margin=0.05)
        self.assertFalse(out["collapsed"])
        self.assertEqual(out["runner_up"], 0.9)

    def test_exact_primary_name_match_rejects_alias_only(self):
        # A contaminated profile: alias matched (name_similarity 1.0 in
        # evidence) but the primary display name is someone else.
        cand = _candidate(display_name="Robert Jones", name_similarity=1.0)
        self.assertFalse(identity_rules.exact_primary_name_match(FACULTY, cand))
        # Middle initials on the primary name still count as exact.
        cand = _candidate(display_name="Jane M. Smith")
        self.assertTrue(identity_rules.exact_primary_name_match(FACULTY, cand))

    def test_alias_can_inflate_scoring_similarity(self):
        # Pins the behavior exact_primary_name_match guards against: the
        # scorer takes the max over display_name_alternatives.
        from enrichment.identity import _score_openalex_author
        author = {
            "id": "https://openalex.org/A5000000001",
            "display_name": "Robert Jones",
            "display_name_alternatives": ["Jane Smith"],
            "last_known_institutions": [], "affiliations": [], "topics": [],
        }
        faculty = dict(FACULTY, department="som")
        _, evidence = _score_openalex_author(author, faculty)
        self.assertEqual(evidence["name_similarity"], 1.0)
        self.assertEqual(evidence["display_name"], "Robert Jones")

    def test_corroboration_precheck(self):
        ok = _candidate(name_similarity=1.0, ucsd_listed=True,
                        orcid="0000-0001-1111-2222")
        self.assertTrue(identity_rules.orcid_corroboration_precheck(FACULTY, ok))
        # Missing ORCID, non-exact similarity, alias-only name, no UCSD.
        for bad in (
            _candidate(name_similarity=1.0, ucsd_listed=True),
            _candidate(name_similarity=0.94, ucsd_listed=True,
                       orcid="0000-0001-1111-2222"),
            _candidate(display_name="Robert Jones", name_similarity=1.0,
                       ucsd_listed=True, orcid="0000-0001-1111-2222"),
            _candidate(name_similarity=1.0, orcid="0000-0001-1111-2222"),
        ):
            self.assertFalse(
                identity_rules.orcid_corroboration_precheck(FACULTY, bad))

    def test_corroboration_confirms(self):
        self.assertTrue(identity_rules.orcid_corroboration_confirms(
            {"employment_verified": True, "record_name_similarity": 1.0}))
        self.assertFalse(identity_rules.orcid_corroboration_confirms(
            {"employment_verified": False, "record_name_similarity": 1.0}))
        self.assertFalse(identity_rules.orcid_corroboration_confirms(
            {"employment_verified": True, "record_name_similarity": 0.8}))
        self.assertFalse(identity_rules.orcid_corroboration_confirms(None))

    def test_orcid_fallback_qualifies(self):
        self.assertTrue(identity_rules.orcid_fallback_qualifies(
            {"unique_hit": True, "employment_verified": True,
             "record_name_similarity": 1.0}))
        self.assertTrue(identity_rules.orcid_fallback_qualifies(
            {"record_email_match": True}))
        for bad in (
            {"unique_hit": False, "employment_verified": True,
             "record_name_similarity": 1.0},
            {"unique_hit": True, "employment_verified": False,
             "record_name_similarity": 1.0},
            {"unique_hit": True, "employment_verified": True,
             "record_name_similarity": 0.94},
            {"orcid": "0000-0001-1111-2222", "via": "orcid_affiliation_search"},
            None,
        ):
            self.assertFalse(identity_rules.orcid_fallback_qualifies(bad))


def _orcid_record(employments=(), educations=(), given="Jane",
                  family="Smith", emails=()):
    def group(names, key):
        return [{"summaries": [{key: {"organization": {"name": n}}}]}
                for n in names]
    return {
        "activities-summary": {
            "employments": {"affiliation-group":
                            group(employments, "employment-summary")},
            "educations": {"affiliation-group":
                           group(educations, "education-summary")},
        },
        "person": {
            "name": {"given-names": {"value": given},
                     "family-name": {"value": family}},
            "emails": {"email": [{"email": e} for e in emails]},
        },
    }


class OrcidEmploymentCheckTests(unittest.TestCase):
    def test_employment_counts_education_does_not(self):
        from enrichment.sources.orcid import ORCIDSource
        employed = _orcid_record(employments=["University of California San Diego"])
        self.assertTrue(ORCIDSource._has_ucsd_employment(employed))
        self.assertTrue(ORCIDSource._has_ucsd_affiliation(employed))
        # A UCSD degree alone passes the loose check but not the strict one.
        alum = _orcid_record(educations=["UC San Diego"],
                             employments=["Stanford University"])
        self.assertFalse(ORCIDSource._has_ucsd_employment(alum))
        self.assertTrue(ORCIDSource._has_ucsd_affiliation(alum))

    def test_scripps_research_is_not_ucsd_employment(self):
        from enrichment.sources.orcid import ORCIDSource
        record = _orcid_record(employments=["Scripps Research"])
        self.assertFalse(ORCIDSource._has_ucsd_employment(record))
        # The loose affiliation check still matches it (legacy fetch path).
        self.assertTrue(ORCIDSource._has_ucsd_affiliation(record))

    def test_verify_record_facts(self):
        from enrichment.sources.orcid import ORCIDSource
        record = _orcid_record(employments=["UC San Diego"],
                               given="Jane Marie", family="Smith",
                               emails=["jsmith@ucsd.edu"])
        out = ORCIDSource._verify_record(record, "0000-0001-1111-2222",
                                         "Jane", "Smith",
                                         email="jsmith@ucsd.edu")
        self.assertTrue(out["employment_verified"])
        self.assertEqual(out["record_name_similarity"], 1.0)
        self.assertTrue(out["record_email_match"])
        # Different person behind the ORCID id.
        record = _orcid_record(employments=["UC San Diego"],
                               given="Roberto", family="Vasquez")
        out = ORCIDSource._verify_record(record, "0000-0001-1111-2222",
                                         "Jane", "Smith")
        self.assertEqual(out["record_name_similarity"], 0.0)
        self.assertFalse(out["record_email_match"])


class _StubORCID:
    """Stand-in for ORCIDSource in resolve/re-sweep tests."""

    def __init__(self, verification=None, search_result=(None, 0)):
        self.verification = verification
        self.search_result = search_result
        self.verify_calls = 0

    def verify_ucsd_employment(self, orcid_id, first, last, email=None):
        self.verify_calls += 1
        return self.verification

    def search_by_name_counted(self, first, last):
        return self.search_result


class _ResolverTestBase(_TempDBTestCase):
    def _seed_faculty(self, **overrides):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        fields = {"first_name": "Jane", "last_name": "Smith",
                  "email": "jsmith@ucsd.edu"}
        fields.update(overrides)
        fid = db.upsert_faculty(conn, "som", fields)
        conn.commit()
        faculty = dict(fields, _db_id=fid, department="som")
        row = conn.execute("SELECT stable_key FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        faculty["_stable_key"] = row["stable_key"]
        return conn, fid, faculty

    def _resolver(self, candidates, orcid_stub=None):
        from enrichment.identity import IdentityResolver
        resolver = IdentityResolver.__new__(IdentityResolver)
        resolver._openalex = None
        resolver._orcid = orcid_stub or _StubORCID()
        resolver._openalex_candidates = lambda f: [dict(c) for c in candidates]
        resolver._orcid_fallback = lambda f: None
        return resolver

    def _log_rows(self, conn):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE field_updated='identity'")]


class IdentityResolveRuleTests(_ResolverTestBase):
    def test_tie_collapse_auto_accepts_canonical(self):
        conn, fid, faculty = self._seed_faculty()
        candidates = [
            _candidate("A1", score=0.95, orcid="0000-0001-1111-2222",
                       works_count=12),
            _candidate("A2", score=0.93, orcid="0000-0001-1111-2222",
                       works_count=200),
        ]
        outcome = self._resolver(candidates).resolve(conn, faculty)
        self.assertEqual(outcome, "auto")
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A2")
        self.assertEqual(row["orcid"], "0000-0001-1111-2222")
        self.assertEqual(row["identity_status"], "auto")
        logs = self._log_rows(conn)
        self.assertEqual(logs[0]["method"], "identity_auto_rule")
        self.assertIn("duplicate_tie_collapse", logs[0]["raw_response"])
        conn.close()

    def test_different_orcid_tie_stays_ambiguous(self):
        conn, fid, faculty = self._seed_faculty()
        candidates = [
            _candidate("A1", score=0.95, orcid="0000-0001-1111-2222"),
            _candidate("A2", score=0.93, orcid="0000-0002-3333-4444"),
        ]
        outcome = self._resolver(candidates).resolve(conn, faculty)
        self.assertEqual(outcome, "ambiguous")
        pending = db.list_identity_candidates(conn)
        self.assertEqual(len(pending), 2)
        conn.close()

    def test_orcid_corroboration_accepts_historical_affiliation_case(self):
        conn, fid, faculty = self._seed_faculty()
        # The structural 0.865 case: exact name, UCSD historical, no topics.
        candidates = [_candidate("A1", score=0.865, name_similarity=1.0,
                                 ucsd_listed=True,
                                 orcid="0000-0001-1111-2222")]
        stub = _StubORCID(verification={
            "orcid": "0000-0001-1111-2222", "employment_verified": True,
            "record_name_similarity": 1.0, "record_email_match": False})
        outcome = self._resolver(candidates, stub).resolve(conn, faculty)
        self.assertEqual(outcome, "auto")
        self.assertEqual(stub.verify_calls, 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        logs = self._log_rows(conn)
        self.assertIn("orcid_employment_corroboration", logs[0]["raw_response"])
        conn.close()

    def test_education_only_orcid_does_not_corroborate(self):
        conn, fid, faculty = self._seed_faculty()
        candidates = [_candidate("A1", score=0.865, name_similarity=1.0,
                                 ucsd_listed=True,
                                 orcid="0000-0001-1111-2222")]
        stub = _StubORCID(verification={
            "orcid": "0000-0001-1111-2222", "employment_verified": False,
            "record_name_similarity": 1.0, "record_email_match": False})
        outcome = self._resolver(candidates, stub).resolve(conn, faculty)
        self.assertEqual(outcome, "ambiguous")
        self.assertEqual(len(db.list_identity_candidates(conn)), 1)
        conn.close()

    def test_verified_orcid_fallback_auto_accepts(self):
        conn, fid, faculty = self._seed_faculty()
        fallback = _candidate("0000-0001-1111-2222", score=0.8,
                              source="orcid", orcid="0000-0001-1111-2222",
                              via="orcid_affiliation_search", unique_hit=True,
                              employment_verified=True,
                              record_name_similarity=1.0,
                              record_email_match=False)
        resolver = self._resolver([])
        resolver._orcid_fallback = lambda f: dict(fallback)
        outcome = resolver.resolve(conn, faculty)
        self.assertEqual(outcome, "auto")
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["orcid"], "0000-0001-1111-2222")
        conn.close()

    def test_unverified_orcid_fallback_stays_queued(self):
        conn, fid, faculty = self._seed_faculty()
        fallback = _candidate("0000-0001-1111-2222", score=0.8,
                              source="orcid", orcid="0000-0001-1111-2222",
                              via="orcid_affiliation_search", unique_hit=False,
                              employment_verified=True,
                              record_name_similarity=1.0,
                              record_email_match=False)
        resolver = self._resolver([])
        resolver._orcid_fallback = lambda f: dict(fallback)
        outcome = resolver.resolve(conn, faculty)
        self.assertEqual(outcome, "ambiguous")
        pending = db.list_identity_candidates(conn)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["score"], 0.8)
        conn.close()


class IdentityResweepTests(_ResolverTestBase):
    def _queue(self, conn, fid, candidates):
        db.insert_identity_candidates(conn, fid, candidates)
        db.set_identity_status(conn, fid, "ambiguous")
        conn.commit()

    def test_resweep_applies_each_rule(self):
        from enrichment.identity import resweep_pending
        conn, f1, _ = self._seed_faculty()
        self._queue(conn, f1, [
            _candidate("A1", score=0.95, orcid="0000-0001-1111-2222",
                       works_count=12),
            _candidate("A2", score=0.93, orcid="0000-0001-1111-2222",
                       works_count=200),
        ])
        _, f2, _ = self._seed_faculty(email="jsmith2@ucsd.edu",
                                      last_name="Smithe")
        self._queue(conn, f2, [
            _candidate("A3", score=0.865, display_name="Jane Smithe",
                       name_similarity=1.0, ucsd_listed=True,
                       orcid="0000-0002-3333-4444"),
        ])
        # Non-qualifying: different-ORCID tie must stay pending.
        _, f3, _ = self._seed_faculty(email="jsmith3@ucsd.edu",
                                      last_name="Smyth")
        self._queue(conn, f3, [
            _candidate("A4", score=0.85, display_name="Jane Smyth",
                       orcid="0000-0003-5555-6666", ucsd_listed=True),
            _candidate("A5", score=0.84, display_name="Jane Smyth",
                       orcid="0000-0004-7777-8888", ucsd_listed=True),
        ])

        stub = _StubORCID(verification={
            "orcid": "0000-0002-3333-4444", "employment_verified": True,
            "record_name_similarity": 1.0, "record_email_match": False})
        stats = resweep_pending(orcid_source=stub)

        self.assertEqual(stats["faculty_seen"], 3)
        self.assertEqual(stats["accepted_tie_collapse"], 1)
        self.assertEqual(stats["accepted_orcid_corroboration"], 1)
        self.assertEqual(stats["left_pending"], 1)

        row = conn.execute("SELECT * FROM faculty WHERE id=?", (f1,)).fetchone()
        self.assertEqual(row["openalex_id"], "A2")
        self.assertEqual(row["orcid"], "0000-0001-1111-2222")
        self.assertEqual(row["identity_status"], "confirmed")
        # Cluster sibling rejected, canonical accepted.
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates"
            " WHERE faculty_id=?", (f1,))}
        self.assertEqual(statuses, {"A1": "rejected", "A2": "accepted"})

        row = conn.execute("SELECT * FROM faculty WHERE id=?", (f2,)).fetchone()
        self.assertEqual(row["openalex_id"], "A3")
        self.assertEqual(row["identity_status"], "confirmed")

        row = conn.execute("SELECT * FROM faculty WHERE id=?", (f3,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        self.assertEqual(len(db.list_identity_candidates(conn)), 2)

        logs = self._log_rows(conn)
        self.assertEqual({l["method"] for l in logs}, {"identity_auto_rule"})
        conn.close()

    def test_resweep_is_idempotent(self):
        from enrichment.identity import resweep_pending
        conn, fid, _ = self._seed_faculty()
        self._queue(conn, fid, [
            _candidate("A1", score=0.95, orcid="0000-0001-1111-2222"),
            _candidate("A2", score=0.93, orcid="0000-0001-1111-2222"),
        ])
        first = resweep_pending(orcid_source=_StubORCID())
        second = resweep_pending(orcid_source=_StubORCID())
        self.assertEqual(first["accepted_tie_collapse"], 1)
        self.assertEqual(second["faculty_seen"], 0)
        self.assertEqual(len(self._log_rows(conn)), 1)
        conn.close()

    def test_lookup_budget_zero_skips_orcid_rules(self):
        from enrichment.identity import resweep_pending
        conn, f1, _ = self._seed_faculty()
        self._queue(conn, f1, [
            _candidate("A1", score=0.95, orcid="0000-0001-1111-2222"),
            _candidate("A2", score=0.93, orcid="0000-0001-1111-2222"),
        ])
        _, f2, _ = self._seed_faculty(email="jsmith2@ucsd.edu",
                                      last_name="Smithe")
        self._queue(conn, f2, [
            _candidate("A3", score=0.865, display_name="Jane Smithe",
                       name_similarity=1.0, ucsd_listed=True,
                       orcid="0000-0002-3333-4444"),
        ])
        stub = _StubORCID(verification={
            "orcid": "0000-0002-3333-4444", "employment_verified": True,
            "record_name_similarity": 1.0, "record_email_match": False})
        stats = resweep_pending(orcid_source=stub, max_orcid_lookups=0)
        self.assertEqual(stats["accepted_tie_collapse"], 1)
        self.assertEqual(stats["accepted_orcid_corroboration"], 0)
        self.assertEqual(stats["orcid_lookups"], 0)
        self.assertEqual(stub.verify_calls, 0)
        conn.close()

    def test_legacy_orcid_fallback_row_reverified(self):
        from enrichment.identity import resweep_pending
        conn, fid, _ = self._seed_faculty()
        # Pre-rules fallback row: evidence has only {orcid, via}.
        self._queue(conn, fid, [
            {"source": "orcid", "external_id": "0000-0001-1111-2222",
             "score": 0.8, "display_name": "Jane Smith",
             "evidence": {"orcid": "0000-0001-1111-2222",
                          "via": "orcid_affiliation_search"}},
        ])
        stub = _StubORCID(
            verification={"orcid": "0000-0001-1111-2222",
                          "employment_verified": True,
                          "record_name_similarity": 1.0,
                          "record_email_match": False},
            search_result=("0000-0001-1111-2222", 1))
        stats = resweep_pending(orcid_source=stub)
        self.assertEqual(stats["accepted_orcid_fallback"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["orcid"], "0000-0001-1111-2222")
        self.assertEqual(row["identity_status"], "confirmed")
        conn.close()

    def test_legacy_row_with_changed_search_result_stays_pending(self):
        from enrichment.identity import resweep_pending
        conn, fid, _ = self._seed_faculty()
        self._queue(conn, fid, [
            {"source": "orcid", "external_id": "0000-0001-1111-2222",
             "score": 0.8, "display_name": "Jane Smith",
             "evidence": {"orcid": "0000-0001-1111-2222",
                          "via": "orcid_affiliation_search"}},
        ])
        stub = _StubORCID(search_result=("0000-0009-9999-9999", 2))
        stats = resweep_pending(orcid_source=stub)
        self.assertEqual(stats["accepted_orcid_fallback"], 0)
        self.assertEqual(stats["left_pending"], 1)
        self.assertEqual(len(db.list_identity_candidates(conn)), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
