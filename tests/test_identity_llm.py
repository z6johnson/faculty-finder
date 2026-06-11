"""Tests for the LLM identity adjudication sweep.

Run with:  python -m unittest discover tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from enrichment import identity_llm
from tests.test_enrichment_expansion import _TempDBTestCase
from tests.test_identity_rules import _candidate


FACULTY = {"_db_id": 1, "first_name": "Jane", "last_name": "Smith",
           "title": "Professor", "department": "som",
           "email": "jsmith@ucsd.edu",
           "research_interests": "cancer epidemiology"}

# Scores outside TIE_MARGIN so the duplicate-profile collapse leaves both
# candidates in the prompt (the collapse path has its own test below).
CANDS = [
    _candidate("A1", score=0.85, ucsd_current=True, name_similarity=1.0),
    _candidate("A2", score=0.7, ucsd_listed=True, name_similarity=0.9),
]


def _verdict(decision="accept", external_id="A1", confidence=0.95,
             reasoning="match"):
    return {"decision": decision, "candidate_external_id": external_id,
            "confidence": confidence, "reasoning": reasoning}


class ValidateVerdictTests(unittest.TestCase):
    def test_valid_accept_passes(self):
        out = identity_llm.validate_verdict(_verdict(), CANDS)
        self.assertEqual(out["decision"], "accept")
        self.assertEqual(out["candidate_external_id"], "A1")
        self.assertNotIn("guardrail", out)

    def test_unlisted_external_id_becomes_abstain(self):
        out = identity_llm.validate_verdict(_verdict(external_id="A99"), CANDS)
        self.assertEqual(out["decision"], "abstain")
        self.assertEqual(out["guardrail"], "invalid_verdict")

    def test_malformed_inputs_become_abstain(self):
        for raw in (None, "yes", [], {"decision": "maybe"},
                    _verdict(confidence="high"),
                    _verdict(confidence=1.7),
                    _verdict(confidence=-0.1)):
            out = identity_llm.validate_verdict(raw, CANDS)
            self.assertEqual(out["decision"], "abstain")
            self.assertEqual(out["guardrail"], "invalid_verdict")

    def test_non_accept_drops_candidate_id(self):
        out = identity_llm.validate_verdict(
            _verdict(decision="reject_all", external_id="A1"), CANDS)
        self.assertEqual(out["decision"], "reject_all")
        self.assertIsNone(out["candidate_external_id"])


class MergeVerdictTests(unittest.TestCase):
    def test_agreement_keeps_min_confidence(self):
        out = identity_llm.merge_verdicts(_verdict(confidence=0.95),
                                          _verdict(confidence=0.9))
        self.assertEqual(out["decision"], "accept")
        self.assertEqual(out["confidence"], 0.9)

    def test_single_pass_returned_unchanged(self):
        v = _verdict()
        self.assertEqual(identity_llm.merge_verdicts(v, None), v)

    def test_any_disagreement_abstains(self):
        for second in (_verdict(external_id="A2"),
                       _verdict(decision="abstain", external_id=None),
                       _verdict(decision="reject_all", external_id=None)):
            out = identity_llm.merge_verdicts(_verdict(), second)
            self.assertEqual(out["decision"], "abstain")
            self.assertEqual(out["guardrail"],
                             "self_consistency_disagreement")


class GuardrailTests(unittest.TestCase):
    def test_clean_accept_passes(self):
        ok, reason = identity_llm.acceptance_guardrails(
            _verdict(), CANDS, accept_confidence=0.9)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_non_ucsd_candidate_blocked(self):
        cands = [_candidate("A1", score=0.85, name_similarity=1.0)]
        ok, reason = identity_llm.acceptance_guardrails(
            _verdict(), cands, accept_confidence=0.9)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_ucsd_affiliation")

    def test_low_name_similarity_blocked(self):
        cands = [_candidate("A1", score=0.85, ucsd_current=True,
                            name_similarity=0.6)]
        ok, reason = identity_llm.acceptance_guardrails(
            _verdict(), cands, accept_confidence=0.9)
        self.assertFalse(ok)
        self.assertEqual(reason, "name_similarity_below_floor")

    def test_sub_threshold_confidence_blocked(self):
        ok, reason = identity_llm.acceptance_guardrails(
            _verdict(confidence=0.85), CANDS, accept_confidence=0.9)
        self.assertFalse(ok)
        self.assertEqual(reason, "confidence_below_threshold")

    def test_unlisted_candidate_blocked(self):
        ok, reason = identity_llm.acceptance_guardrails(
            _verdict(external_id="A99"), CANDS, accept_confidence=0.9)
        self.assertFalse(ok)
        self.assertEqual(reason, "candidate_not_listed")


class EligibilityTests(unittest.TestCase):
    def test_orcid_rows_excluded(self):
        cands = [_candidate("A1"),
                 _candidate("0000-0001-1111-2222", source="orcid")]
        out = identity_llm.eligible_candidates(cands)
        self.assertEqual([c["external_id"] for c in out], ["A1"])


class PromptTests(unittest.TestCase):
    def test_prompt_contains_faculty_and_candidates(self):
        dossiers = {"A1": {"recent_works": [{"title": "Tumor study",
                                             "year": 2024}],
                           "affiliations": ["UC San Diego (2015-2024)"],
                           "alternatives": ["J. Smith"]}}
        prompt = identity_llm.build_user_prompt(FACULTY, CANDS, dossiers)
        for needle in ("Jane Smith", "cancer epidemiology", "A1", "A2",
                       "Tumor study", "UC San Diego (2015-2024)", "J. Smith"):
            self.assertIn(needle, prompt)

    def test_shuffle_seed_reorders_deterministically(self):
        cands = [_candidate(f"A{i}", score=0.8) for i in range(6)]
        base = identity_llm.build_user_prompt(FACULTY, cands, {})
        shuffled = identity_llm.build_user_prompt(FACULTY, cands, {},
                                                  shuffle_seed=7)
        again = identity_llm.build_user_prompt(FACULTY, cands, {},
                                               shuffle_seed=7)
        self.assertNotEqual(base, shuffled)
        self.assertEqual(shuffled, again)


def _orcid_candidate(orcid_id="0000-0001-1111-2222", employment_verified=True,
                     record_name_similarity=1.0, **evidence):
    evidence.update(employment_verified=employment_verified,
                    record_name_similarity=record_name_similarity)
    return _candidate(orcid_id, source="orcid", score=0.8, **evidence)


class OrcidEligibilityTests(unittest.TestCase):
    def test_orcid_rows_selected(self):
        cands = [_candidate("A1"), _orcid_candidate()]
        out = identity_llm.orcid_eligible_candidates(cands)
        self.assertEqual([c["source"] for c in out], ["orcid"])


class OrcidGuardrailTests(unittest.TestCase):
    def _v(self, oid="0000-0001-1111-2222", confidence=0.96):
        return _verdict(external_id=oid, confidence=confidence)

    def test_clean_accept_passes(self):
        cand = _orcid_candidate()
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(), cand, accept_confidence=0.95)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_unverified_employment_blocked(self):
        cand = _orcid_candidate(employment_verified=False)
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(), cand, accept_confidence=0.95)
        self.assertFalse(ok)
        self.assertEqual(reason, "employment_not_verified")

    def test_low_record_name_similarity_blocked(self):
        cand = _orcid_candidate(record_name_similarity=0.8)
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(), cand, accept_confidence=0.95)
        self.assertFalse(ok)
        self.assertEqual(reason, "name_similarity_below_floor")

    def test_sub_threshold_confidence_blocked(self):
        cand = _orcid_candidate()
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(confidence=0.9), cand, accept_confidence=0.95)
        self.assertFalse(ok)
        self.assertEqual(reason, "confidence_below_threshold")

    def test_mismatched_id_blocked(self):
        cand = _orcid_candidate()
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(oid="0000-0009-9999-9999"), cand, accept_confidence=0.95)
        self.assertFalse(ok)
        self.assertEqual(reason, "candidate_not_listed")

    def test_fresh_dossier_overrides_stored_evidence(self):
        # Stored evidence says verified, but a fresh dossier says it isn't.
        cand = _orcid_candidate(employment_verified=True)
        ok, reason = identity_llm.acceptance_guardrails_orcid(
            self._v(), cand, dossier={"employment_verified": False},
            accept_confidence=0.95)
        self.assertFalse(ok)
        self.assertEqual(reason, "employment_not_verified")


class OrcidPromptTests(unittest.TestCase):
    def test_prompt_contains_faculty_and_profile_evidence(self):
        cand = _orcid_candidate()
        dossier = {"employment_verified": True, "record_name_similarity": 1.0,
                   "recent_works": [{"title": "Tumor study", "year": 2024}],
                   "affiliations": ["Professor, UC San Diego (2016–present)"]}
        prompt = identity_llm.build_orcid_user_prompt(FACULTY, cand, dossier)
        for needle in ("Jane Smith", "cancer epidemiology",
                       "0000-0001-1111-2222", "Tumor study",
                       "UC San Diego", "ucsd_employment_verified=True"):
            self.assertIn(needle, prompt)


class _FakeOpenAlex:
    """No-network stand-in; build_dossiers degrades to stored evidence."""

    def _params(self, extra=None):
        return dict(extra or {})

    def _get(self, url, params=None):
        return None


class _FakeORCID:
    """No-network ORCID stand-in returning a canned record + dossier helpers.

    record=None makes _fetch_full_record return None so build_orcid_dossier
    degrades to the candidate's stored evidence (no fresh verification)."""

    def __init__(self, record={"_": 1}, employment_verified=True,
                 record_name_similarity=1.0, record_email_match=False,
                 works=None, affiliations=None, fundings=None):
        self._record = record
        self._emp = employment_verified
        self._sim = record_name_similarity
        self._email = record_email_match
        self._works = (works if works is not None
                       else [{"title": "Coral reef genomics", "year": 2023}])
        self._affils = (affiliations if affiliations is not None
                        else ["Professor, UC San Diego (2016–present)"])
        self._fundings = fundings

    def _fetch_full_record(self, orcid_id):
        return self._record

    def _verify_record(self, record, orcid_id, first, last, email=None):
        return {"orcid": orcid_id, "employment_verified": self._emp,
                "record_name_similarity": self._sim,
                "record_email_match": self._email}

    def _extract_works(self, record):
        return list(self._works)

    def employment_affiliations(self, record):
        return list(self._affils)

    def _extract_fundings(self, record):
        return list(self._fundings) if self._fundings else None


def _llm_returning(*responses):
    """Fake llm_call yielding canned JSON strings in order (last repeats)."""
    queue = list(responses)

    def call(system_prompt, user_prompt):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return call


class LLMSweepTests(_TempDBTestCase):
    def _seed(self, candidates, **overrides):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        fields = {"first_name": "Jane", "last_name": "Smith",
                  "email": "jsmith@ucsd.edu"}
        fields.update(overrides)
        fid = db.upsert_faculty(conn, "som", fields)
        db.insert_identity_candidates(conn, fid, candidates)
        db.set_identity_status(conn, fid, "ambiguous")
        conn.commit()
        return conn, fid

    def _sweep(self, llm_response, **kwargs):
        kwargs.setdefault("openalex_source", _FakeOpenAlex())
        kwargs.setdefault("orcid_source", _FakeORCID())
        kwargs.setdefault("llm_call", _llm_returning(llm_response))
        return identity_llm.llm_sweep_pending(**kwargs)

    def test_accept_confirms_faculty_and_logs(self):
        conn, fid = self._seed(CANDS)
        stats = self._sweep(json.dumps(_verdict()))
        self.assertEqual(stats["accepted_llm"], 1)
        self.assertEqual(stats["llm_calls"], 2)  # self-consistency second pass
        self.assertEqual(stats["left_pending"], 0)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        self.assertEqual(row["identity_status"], "confirmed")
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A1": "accepted", "A2": "rejected"})
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE field_updated='identity'")]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["method"], "identity_llm_rule")
        raw = json.loads(logs[0]["raw_response"])
        self.assertEqual(raw["rule"], "llm_adjudication")
        self.assertEqual(raw["verdict_1"]["candidate_external_id"], "A1")
        conn.close()

    def test_disagreeing_passes_abstain(self):
        conn, fid = self._seed(CANDS)
        llm = _llm_returning(json.dumps(_verdict("accept", "A1")),
                             json.dumps(_verdict("accept", "A2")))
        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["accepted_llm"], 0)
        self.assertEqual(stats["abstained"], 1)
        self.assertEqual(stats["left_pending"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        conn.close()

    def test_abstain_annotates_and_stays_pending(self):
        conn, fid = self._seed(CANDS)
        stats = self._sweep(json.dumps(
            _verdict("abstain", None, 0.4, "could be either")))
        self.assertEqual(stats["abstained"], 1)
        self.assertEqual(stats["llm_calls"], 1)  # no second pass on abstain
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM identity_candidates")]
        for r in rows:
            self.assertEqual(r["status"], "pending")
            self.assertEqual(r["llm_verdict"], "abstain")
            self.assertEqual(r["llm_reasoning"], "could be either")
            self.assertIsNotNone(r["llm_evaluated_at"])
        conn.close()

    def test_reject_all_never_rejects_rows(self):
        conn, fid = self._seed(CANDS)
        stats = self._sweep(json.dumps(
            _verdict("reject_all", None, 0.95, "different field entirely")))
        self.assertEqual(stats["reject_all_flagged"], 1)
        self.assertEqual(stats["accepted_llm"], 0)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM identity_candidates")]
        for r in rows:
            self.assertEqual(r["status"], "pending")
            self.assertEqual(r["llm_verdict"], "reject")
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        conn.close()

    def test_dry_run_annotates_but_never_decides(self):
        conn, fid = self._seed(CANDS)
        stats = self._sweep(json.dumps(_verdict()), dry_run=True)
        self.assertEqual(stats["accepted_llm"], 1)
        rows = {r["external_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM identity_candidates")}
        self.assertEqual(rows["A1"]["status"], "pending")
        self.assertEqual(rows["A1"]["llm_verdict"], "accept")
        self.assertEqual(rows["A2"]["llm_verdict"], "abstain")
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        self.assertIsNone(row["openalex_id"])
        conn.close()

    def test_unlisted_id_verdict_never_accepts(self):
        conn, fid = self._seed(CANDS)
        stats = self._sweep(json.dumps(_verdict(external_id="A99")))
        self.assertEqual(stats["accepted_llm"], 0)
        self.assertEqual(stats["abstained"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        conn.close()

    def test_guardrail_blocks_non_ucsd_accept(self):
        conn, fid = self._seed(
            [_candidate("A1", score=0.85, name_similarity=1.0)])
        stats = self._sweep(json.dumps(_verdict()))
        self.assertEqual(stats["accepted_llm"], 0)
        self.assertEqual(stats["guardrail_blocked"], 1)
        self.assertEqual(stats["abstained"], 0)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        conn.close()

    def _orcid_cand(self, orcid_id="0000-0001-1111-2222", **evidence):
        evidence.setdefault("employment_verified", True)
        evidence.setdefault("record_name_similarity", 1.0)
        evidence.setdefault("unique_hit", True)
        return _candidate(orcid_id, source="orcid", score=0.8, **evidence)

    def test_orcid_only_accept_confirms_faculty_and_logs(self):
        oid = "0000-0001-1111-2222"
        conn, fid = self._seed([self._orcid_cand(oid)])
        stats = self._sweep(json.dumps(_verdict(external_id=oid)))
        self.assertEqual(stats["orcid_eligible"], 1)
        self.assertEqual(stats["orcid_accepted"], 1)
        self.assertEqual(stats["eligible"], 0)
        self.assertEqual(stats["llm_calls"], 2)  # paraphrased second pass
        self.assertEqual(stats["left_pending"], 0)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["orcid"], oid)
        self.assertIsNone(row["openalex_id"])
        self.assertEqual(row["identity_status"], "confirmed")
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE field_updated='identity'")]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["method"], "identity_llm_rule")
        self.assertEqual(json.loads(logs[0]["raw_response"])["rule"],
                         "llm_orcid_adjudication")
        conn.close()

    def test_orcid_accept_blocked_when_employment_unverified(self):
        oid = "0000-0001-1111-2222"
        conn, fid = self._seed([self._orcid_cand(oid)])
        stats = self._sweep(
            json.dumps(_verdict(external_id=oid)),
            orcid_source=_FakeORCID(employment_verified=False))
        self.assertEqual(stats["orcid_accepted"], 0)
        self.assertEqual(stats["orcid_guardrail_blocked"], 1)
        self.assertEqual(stats["left_pending"], 1)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM identity_candidates")]
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["llm_verdict"], "abstain")
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        self.assertIsNone(row["orcid"])
        conn.close()

    def test_orcid_accept_blocked_below_confidence_floor(self):
        # ORCID floor is 0.95: an OpenAlex-passing 0.92 still abstains here.
        oid = "0000-0001-1111-2222"
        conn, fid = self._seed([self._orcid_cand(oid)])
        stats = self._sweep(
            json.dumps(_verdict(external_id=oid, confidence=0.92)))
        self.assertEqual(stats["orcid_accepted"], 0)
        self.assertEqual(stats["orcid_guardrail_blocked"], 1)
        conn.close()

    def test_orcid_abstain_annotates_and_stays_pending(self):
        oid = "0000-0001-1111-2222"
        conn, fid = self._seed([self._orcid_cand(oid)])
        stats = self._sweep(json.dumps(
            _verdict("abstain", None, 0.3, "topics don't fit department")))
        self.assertEqual(stats["orcid_annotated"], 1)
        self.assertEqual(stats["llm_calls"], 1)  # no second pass on abstain
        row = conn.execute(
            "SELECT * FROM identity_candidates").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["llm_verdict"], "abstain")
        self.assertEqual(row["llm_reasoning"], "topics don't fit department")
        self.assertIsNotNone(row["llm_evaluated_at"])
        conn.close()

    def test_orcid_dry_run_accept_promotes_without_new_calls(self):
        oid = "0000-0001-1111-2222"
        conn, fid = self._seed([self._orcid_cand(oid)])
        dry = self._sweep(json.dumps(_verdict(external_id=oid)), dry_run=True)
        self.assertEqual(dry["orcid_accepted"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertIsNone(row["orcid"])  # dry run did not commit

        def llm(system_prompt, user_prompt):
            raise AssertionError("promotion must not re-bill the LLM")

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["promoted"], 1)
        self.assertEqual(stats["llm_calls"], 0)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["orcid"], oid)
        self.assertEqual(row["identity_status"], "confirmed")
        raw = json.loads([dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE field_updated='identity'")
            ][0]["raw_response"])
        self.assertEqual(raw["rule"], "llm_orcid_annotation_promoted")
        conn.close()

    def test_mixed_group_stays_openalex_only(self):
        # OpenAlex + ORCID candidates: the OpenAlex path runs, the ORCID path
        # does not (richer evidence wins), and accepting writes openalex_id.
        conn, fid = self._seed(
            CANDS + [self._orcid_cand("0000-0002-9999-8888")])
        stats = self._sweep(json.dumps(_verdict(external_id="A1")))
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["orcid_eligible"], 0)
        self.assertEqual(stats["accepted_llm"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        conn.close()

    def test_recently_evaluated_group_skipped_unless_forced(self):
        conn, fid = self._seed(CANDS)
        first = self._sweep(json.dumps(_verdict("abstain", None, 0.4)))
        self.assertEqual(first["llm_calls"], 1)
        second = self._sweep(json.dumps(_verdict("abstain", None, 0.4)))
        self.assertEqual(second["skipped_recent"], 1)
        self.assertEqual(second["llm_calls"], 0)
        third = self._sweep(json.dumps(_verdict("abstain", None, 0.4)),
                            force=True)
        self.assertEqual(third["skipped_recent"], 0)
        self.assertEqual(third["llm_calls"], 1)
        conn.close()

    def test_call_budget_stops_sweep(self):
        conn, f1 = self._seed(CANDS)
        f2_cands = [_candidate("A3", score=0.85, ucsd_current=True,
                               name_similarity=1.0)]
        fields = {"first_name": "Bob", "last_name": "Jones",
                  "email": "bjones@ucsd.edu"}
        f2 = db.upsert_faculty(conn, "som", fields)
        db.insert_identity_candidates(conn, f2, f2_cands)
        db.set_identity_status(conn, f2, "ambiguous")
        conn.commit()
        stats = self._sweep(json.dumps(_verdict("abstain", None, 0.4)),
                            max_llm_calls=1)
        self.assertEqual(stats["llm_calls"], 1)
        conn.close()

    def test_cluster_collapse_presents_one_candidate_and_adopts_orcid(self):
        # Two duplicate profiles of the same person (same ORCID): the LLM
        # must see one candidate; accepting the canonical adopts the
        # sibling's ORCID.
        conn, fid = self._seed([
            _candidate("A1", score=0.85, ucsd_current=True,
                       name_similarity=1.0, works_count=200),
            _candidate("A2", score=0.84, ucsd_current=True,
                       name_similarity=1.0, works_count=3,
                       orcid="0000-0001-1111-2222"),
        ])
        prompts = []

        def llm(system_prompt, user_prompt):
            prompts.append(user_prompt)
            return json.dumps(_verdict(external_id="A1"))

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["accepted_llm"], 1)
        self.assertEqual(prompts[0].count("CANDIDATE "), 1)
        self.assertIn("collapses 2 duplicate", prompts[0])
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        self.assertEqual(row["orcid"], "0000-0001-1111-2222")
        # The duplicate profile is merged as an alternate, not dropped.
        self.assertEqual(json.loads(row["openalex_id_alt"]), ["A2"])
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A1": "accepted", "A2": "merged"})
        conn.close()

    def test_unparseable_response_is_an_error_not_an_abstain(self):
        # An unparseable response (after one retry) says nothing about the
        # candidates: it must count as an error and leave the rows
        # un-stamped so a later sweep retries them.
        conn, fid = self._seed(CANDS)
        stats = self._sweep("I think it's probably the first one?")
        self.assertEqual(stats["accepted_llm"], 0)
        self.assertEqual(stats["abstained"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["llm_calls"], 2)  # first call + parse retry
        for r in conn.execute("SELECT * FROM identity_candidates"):
            self.assertIsNone(r["llm_verdict"])
            self.assertIsNone(r["llm_evaluated_at"])
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        conn.close()

    def test_parse_retry_recovers_valid_verdict(self):
        conn, fid = self._seed(CANDS)
        llm = _llm_returning("Sure! Here is my verdict",
                             json.dumps(_verdict()))
        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["accepted_llm"], 1)
        # pass 1 + parse retry + self-consistency pass 2
        self.assertEqual(stats["llm_calls"], 3)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        conn.close()

    def _seed_many(self, n):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        for i in range(n):
            fid = db.upsert_faculty(conn, "som", {
                "first_name": f"F{i}", "last_name": "Smith",
                "email": f"f{i}@ucsd.edu"})
            db.insert_identity_candidates(conn, fid, [
                _candidate(f"A{i}", score=0.85, ucsd_current=True,
                           name_similarity=1.0)])
            db.set_identity_status(conn, fid, "ambiguous")
        conn.commit()
        return conn

    def test_budget_error_aborts_sweep_immediately(self):
        conn = self._seed_many(3)

        calls = []

        def llm(system_prompt, user_prompt):
            calls.append(1)
            raise RuntimeError("Budget has been exceeded! Current cost: 200")

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["budget_errors"], 1)
        self.assertEqual(stats["aborted"], "llm_budget_exhausted")
        # Aborted on the first group: one call attempt, no parse retry
        # (the call itself failed), later groups untouched.
        self.assertEqual(len(calls), 1)
        for r in conn.execute("SELECT * FROM identity_candidates"):
            self.assertIsNone(r["llm_evaluated_at"])
        conn.close()

    def test_consecutive_errors_abort_sweep(self):
        conn = self._seed_many(5)

        def llm(system_prompt, user_prompt):
            raise RuntimeError("connection reset")

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["errors"], 3)
        self.assertEqual(stats["aborted"], "consecutive_errors")
        for r in conn.execute("SELECT * FROM identity_candidates"):
            self.assertIsNone(r["llm_evaluated_at"])
        conn.close()

    def test_real_sweep_promotes_dry_run_accept_without_llm_calls(self):
        conn, fid = self._seed(CANDS)
        dry = self._sweep(json.dumps(_verdict()), dry_run=True)
        self.assertEqual(dry["accepted_llm"], 1)

        def llm(system_prompt, user_prompt):
            raise AssertionError("promotion must not re-bill the LLM")

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["promoted"], 1)
        self.assertEqual(stats["llm_calls"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["left_pending"], 0)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A1")
        self.assertEqual(row["identity_status"], "confirmed")
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A1": "accepted", "A2": "rejected"})
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM enrichment_log WHERE field_updated='identity'")]
        self.assertEqual(len(logs), 1)
        raw = json.loads(logs[0]["raw_response"])
        self.assertEqual(raw["rule"], "llm_annotation_promoted")
        conn.close()

    def test_promotion_re_runs_guardrails(self):
        # A stored accept annotation that no longer passes guardrails (here:
        # sub-threshold confidence, crafted directly) must not be promoted;
        # the group stays parked under the recency stamp.
        conn, fid = self._seed(CANDS)
        conn.execute(
            "UPDATE identity_candidates SET llm_verdict='accept',"
            " llm_confidence=0.5, llm_reasoning='weak',"
            " llm_evaluated_at=datetime('now') WHERE external_id='A1'")
        conn.execute(
            "UPDATE identity_candidates SET llm_verdict='abstain',"
            " llm_confidence=0.5, llm_evaluated_at=datetime('now')"
            " WHERE external_id='A2'")
        conn.commit()

        def llm(system_prompt, user_prompt):
            raise AssertionError("should be skipped as recently evaluated")

        stats = self._sweep(None, llm_call=llm)
        self.assertEqual(stats["promoted"], 0)
        self.assertEqual(stats["skipped_recent"], 1)
        row = conn.execute("SELECT * FROM faculty WHERE id=?",
                           (fid,)).fetchone()
        self.assertEqual(row["identity_status"], "ambiguous")
        self.assertIsNone(row["openalex_id"])
        conn.close()


class FacultySchemaMigrationTests(_TempDBTestCase):
    def test_old_faculty_table_gains_openalex_id_alt(self):
        conn = db.connect(readonly=False)
        # Minimal pre-merge faculty table: no openalex_id_alt column (but
        # everything schema.sql's indexes reference and that the column
        # migrations don't add).
        conn.execute("""
            CREATE TABLE faculty (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stable_key TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                department_label TEXT NOT NULL,
                first_name TEXT, last_name TEXT,
                pi_eligible INTEGER,
                has_profile INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")
        conn.commit()
        db.init_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(faculty)")}
        self.assertIn("openalex_id_alt", cols)
        conn.close()


class CandidateSchemaMigrationTests(_TempDBTestCase):
    def test_old_identity_candidates_table_gains_llm_columns(self):
        conn = db.connect(readonly=False)
        # Pre-LLM production table: no llm_* columns.
        conn.execute("""
            CREATE TABLE identity_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                faculty_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                display_name TEXT, affiliation TEXT,
                score REAL NOT NULL, evidence TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, decided_at TEXT
            )""")
        conn.commit()
        db.init_schema(conn)
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(identity_candidates)")}
        for col in ("llm_verdict", "llm_confidence", "llm_reasoning",
                    "llm_evaluated_at"):
            self.assertIn(col, cols)
        conn.close()


if __name__ == "__main__":
    unittest.main()
