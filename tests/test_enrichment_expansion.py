"""Tests for the all-division enrichment expansion.

Run with:  python -m unittest discover tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from data.divisions import division_for, bundle_for, label_for
from utils.names import name_similarity, names_compatible, parse_eah_name


class DivisionRegistryTests(unittest.TestCase):
    def test_legacy_schools_keep_their_slugs(self):
        self.assertEqual(division_for("School of Public Health")[0], "hwsph")
        self.assertEqual(division_for("Jacobs School of Engineering")[0], "jacobs")
        self.assertEqual(division_for("SIO Biology Section")[0], "sio")
        self.assertEqual(division_for("VC-SIO Other")[0], "sio")

    def test_new_divisions(self):
        self.assertEqual(division_for("School of Medicine")[0], "som")
        self.assertEqual(division_for("Division of Biological Sciences")[0], "bio-sci")
        self.assertEqual(division_for("Division of Physical Sciences")[0], "phys-sci")
        self.assertEqual(division_for("Division of Arts and Humanities")[0], "arts-hum")
        self.assertEqual(division_for("Division of Social Sciences")[0], "soc-sci")
        self.assertEqual(division_for("Rady School of Management")[0], "rady")
        self.assertEqual(division_for("School of Global Policy and Strategy")[0], "gps")
        self.assertEqual(
            division_for("Skaggs School of Pharmacy and Pharmaceutical Sciences")[0],
            "skaggs")

    def test_unknown_division_falls_back_to_slug(self):
        slug, label, bundle = division_for("Office of Innovation and Commercialization")
        self.assertEqual(slug, "office-of-innovation-and-commercialization")
        self.assertEqual(bundle, "default")
        self.assertEqual(division_for("")[0], "other")

    def test_bundles_and_labels(self):
        self.assertEqual(bundle_for("som"), "health")
        self.assertEqual(bundle_for("rady"), "econ")
        self.assertEqual(bundle_for("nonexistent"), "default")
        self.assertIn("Public Health", label_for("hwsph"))


class RoutingTests(unittest.TestCase):
    def test_every_division_gets_core_sources(self):
        from enrichment.routing import source_classes_for
        for slug in ("hwsph", "sio", "jacobs", "som", "arts-hum", "unknown-div"):
            registry = source_classes_for(slug)
            self.assertIn("openalex", registry, slug)
            self.assertIn("orcid", registry, slug)
            self.assertIn("wikidata", registry, slug)

    def test_discipline_extras(self):
        from enrichment.routing import source_classes_for
        self.assertIn("clinical_trials", source_classes_for("som"))
        self.assertIn("escholarship", source_classes_for("arts-hum"))
        self.assertIn("repec", source_classes_for("rady"))
        self.assertIn("nasa_ads", source_classes_for("phys-sci"))
        # Legacy bundles keep their original sources.
        self.assertIn("pubmed", source_classes_for("hwsph"))
        self.assertIn("scripps_profile", source_classes_for("sio"))
        self.assertIn("dblp", source_classes_for("jacobs"))


class NameUtilsTests(unittest.TestCase):
    def test_parse_eah_name(self):
        self.assertEqual(parse_eah_name("Smith, Jane Q"), ("Jane", "Smith"))
        self.assertEqual(parse_eah_name("Plain Name"), ("Plain", "Name"))

    def test_similarity(self):
        self.assertEqual(name_similarity("Jane", "Smith", "Jane", "Smith"), 1.0)
        self.assertGreater(name_similarity("Jane", "Smith", "Janet", "Smith"), 0.8)
        self.assertEqual(name_similarity("Jane", "Smith", "Bob", "Jones"), 0.0)
        # Hyphenated last names get partial credit.
        self.assertGreater(
            name_similarity("Maria", "Garcia", "Maria", "Garcia-Lopez"), 0.7)

    def test_names_compatible(self):
        self.assertTrue(names_compatible("jane", "smith", "janet", "smith"))
        self.assertFalse(names_compatible("jane", "smith", "bob", "smith"))


class MergePolicyTests(unittest.TestCase):
    def test_higher_confidence_source_wins_on_collision(self):
        from enrichment.pipeline import _merge_json_field
        high = [{"title": "Shared Paper", "doi": "10.1/x", "journal": "Good Journal", "year": 2024}]
        low = [{"title": "Shared paper", "doi": "10.1/X", "journal": "Bad Copy", "year": 2024}]
        merged = _merge_json_field("recent_publications", [high, low])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["journal"], "Good Journal")

    def test_title_dedupe_without_doi(self):
        from enrichment.pipeline import _merge_json_field
        a = [{"title": "Deep Learning for Oceans", "year": 2023}]
        b = [{"title": "Deep learning for oceans!", "year": 2023}]
        merged = _merge_json_field("recent_publications", [a, b])
        self.assertEqual(len(merged), 1)

    def test_distinct_entries_merge_and_sort_by_year(self):
        from enrichment.pipeline import _merge_json_field
        a = [{"title": "Old Paper", "year": 2018}]
        b = [{"title": "New Paper", "year": 2025}]
        merged = _merge_json_field("recent_publications", [a, b])
        self.assertEqual([p["title"] for p in merged], ["New Paper", "Old Paper"])

    def test_caps_applied(self):
        from enrichment.pipeline import _merge_json_field
        many = [[{"title": f"Paper {i}", "year": 2000 + i} for i in range(40)]]
        merged = _merge_json_field("recent_publications", many)
        self.assertEqual(len(merged), 20)

    def test_keyword_merge(self):
        from enrichment.pipeline import _merge_json_field
        merged = _merge_json_field(
            "expertise_keywords", [["Machine Learning", "Oceans"],
                                   ["machine learning", "Climate"]])
        self.assertEqual(merged, ["Machine Learning", "Oceans", "Climate"])


class IdentityScoringTests(unittest.TestCase):
    def _author(self, name, current_ucsd=True, domains=("Health Sciences",)):
        inst = {"id": "https://openalex.org/I36258959", "display_name": "UCSD"}
        return {
            "id": "https://openalex.org/A5000000001",
            "display_name": name,
            "display_name_alternatives": [],
            "last_known_institutions": [inst] if current_ucsd else [],
            "affiliations": [{"institution": inst}],
            "topics": [{"domain": {"display_name": d}} for d in domains],
            "works_count": 50,
            "cited_by_count": 1200,
            "orcid": "https://orcid.org/0000-0001-2345-6789",
        }

    def test_exact_match_current_ucsd_scores_high(self):
        from enrichment.identity import _score_openalex_author
        faculty = {"first_name": "Jane", "last_name": "Smith", "department": "som"}
        score, evidence = _score_openalex_author(self._author("Jane Smith"), faculty)
        self.assertGreaterEqual(score, 0.9)
        self.assertEqual(evidence["orcid"], "0000-0001-2345-6789")

    def test_wrong_name_scores_zero(self):
        from enrichment.identity import _score_openalex_author
        faculty = {"first_name": "Jane", "last_name": "Smith", "department": "som"}
        score, _ = _score_openalex_author(self._author("Robert Jones"), faculty)
        self.assertEqual(score, 0.0)

    def test_past_affiliation_and_domain_mismatch_lowers_score(self):
        from enrichment.identity import _score_openalex_author
        faculty = {"first_name": "Jane", "last_name": "Smith", "department": "arts-hum"}
        author = self._author("Jane Smith", current_ucsd=False,
                              domains=("Physical Sciences",))
        score, _ = _score_openalex_author(author, faculty)
        self.assertLess(score, 0.9)


class _TempDBTestCase(unittest.TestCase):
    """Shared temp-DB plumbing: fresh DB file per test, module state reset."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self._old_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db._write_conn = None
        db._local = type(db._local)()  # drop cached per-thread read conns

    def tearDown(self):
        if db._write_conn is not None:
            db._write_conn.close()
            db._write_conn = None
        db.DB_PATH = self._old_db_path
        db._local = type(db._local)()
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)


class SchemaMigrationTests(_TempDBTestCase):
    def test_old_schema_gains_new_columns(self):
        conn = db.connect(readonly=False)
        # Simulate a pre-expansion production table (no identity/awards cols).
        conn.execute("""
            CREATE TABLE faculty (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stable_key TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                department_label TEXT NOT NULL,
                first_name TEXT, last_name TEXT, orcid TEXT,
                h_index INTEGER, pi_eligible INTEGER,
                has_profile INTEGER NOT NULL DEFAULT 0,
                grants_count INTEGER NOT NULL DEFAULT 0,
                pubs_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")
        conn.commit()
        db.init_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(faculty)")}
        for col in ("openalex_id", "identity_status", "citation_count",
                    "works_count", "raw_hash", "awards", "patents"):
            self.assertIn(col, cols)
        conn.close()

    def test_fresh_schema_has_identity_tables(self):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("identity_candidates", tables)
        self.assertIn("escholarship_pubs", tables)
        conn.close()


EAH_CSV_HEADER = (
    "Report header line 1\n"
    "Report header line 2\n"
    "Report header line 3\n"
    "Employee Name,Email,Employee Class,Job Code,Job Code Description,"
    "PI Eligibility Flag Current,VC Area,Division / School,Dept / Unit,"
    "Department L2,Department L3,Department L4,Department L5,Department,"
    "Department Code,Column1\n"
)


class EAHReconcileRegressionTests(_TempDBTestCase):
    """The Phase-0 fix: an EAH reconcile must never delete DB rows."""

    def _seed(self):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        # A legacy enriched faculty (in EAH) and an EAH-seeded SOM row.
        db.upsert_faculty(conn, "hwsph", {
            "first_name": "Jane", "last_name": "Smith",
            "email": "jsmith@ucsd.edu", "title": "Professor",
            "research_interests_enriched": "Health policy.",
        })
        db.upsert_faculty(conn, "som", {
            "first_name": "Ravi", "last_name": "Patel",
            "email": "rpatel@health.ucsd.edu", "title": "Associate Professor",
        })
        # A row that no longer appears in EAH (left the university).
        db.upsert_faculty(conn, "hwsph", {
            "first_name": "Gone", "last_name": "Person",
            "email": "gone@ucsd.edu", "title": "Professor",
        })
        conn.commit()
        conn.close()

    def _write_eah_csv(self):
        rows = [
            '"Smith, Jane",jsmith@ucsd.edu,Academic,001,PROF-AY,Y,VC Health,'
            'School of Public Health,FMPH,L2,L3,L4,L5,FMPH,123,Active',
            # Ravi moved divisions: SOM -> Jacobs.
            '"Patel, Ravi",rpatel@health.ucsd.edu,Academic,002,ASSOC PROF-AY,Y,'
            'VC Acad,Jacobs School of Engineering,CSE,L2,L3,L4,L5,CSE,456,Active',
            # Someone brand new in a division we have never tracked.
            '"Nguyen, Linh",lnguyen@ucsd.edu,Academic,003,ASST PROF-AY,Y,'
            'VC Acad,Division of Arts and Humanities,Music,L2,L3,L4,L5,Music,789,Active',
        ]
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(EAH_CSV_HEADER)
            f.write("\n".join(rows) + "\n")
        return path

    def test_reconcile_preserves_flags_moves_and_adds(self):
        from scripts import eah_enrichment

        self._seed()
        csv_path = self._write_eah_csv()
        old_path = eah_enrichment.EAH_PATH
        eah_enrichment.EAH_PATH = csv_path
        try:
            result = eah_enrichment.run_eah_reconcile()
        finally:
            eah_enrichment.EAH_PATH = old_path
            os.unlink(csv_path)

        conn = db.connect(readonly=False)
        total = conn.execute("SELECT COUNT(*) FROM faculty").fetchone()[0]
        # 3 seeded + 1 new — nothing deleted.
        self.assertEqual(total, 4)

        # Jane matched and kept her enrichment.
        jane = conn.execute(
            "SELECT * FROM faculty WHERE email='jsmith@ucsd.edu'").fetchone()
        self.assertEqual(jane["department"], "hwsph")
        self.assertEqual(jane["eah_status"], "Active")
        self.assertEqual(jane["research_interests_enriched"], "Health policy.")
        self.assertEqual(jane["pi_eligible"], 1)

        # Ravi moved divisions instead of being flagged + duplicated.
        ravi = conn.execute(
            "SELECT * FROM faculty WHERE email='rpatel@health.ucsd.edu'").fetchone()
        self.assertEqual(ravi["department"], "jacobs")

        # The departed faculty is soft-flagged, not deleted.
        gone = conn.execute(
            "SELECT * FROM faculty WHERE email='gone@ucsd.edu'").fetchone()
        self.assertIsNotNone(gone)
        self.assertEqual(gone["eah_status"], "Inactive")

        # The new arts-hum hire was inserted unresolved.
        linh = conn.execute(
            "SELECT * FROM faculty WHERE email='lnguyen@ucsd.edu'").fetchone()
        self.assertEqual(linh["department"], "arts-hum")
        self.assertEqual(linh["identity_status"], "unresolved")

        # Inactive rows stay out of the matching pool.
        with_profile, _ = db.count_match_pool(conn, None)
        rows = conn.execute(
            "SELECT eah_status, has_profile FROM faculty").fetchall()
        active_with_profile = sum(
            1 for r in rows
            if r["has_profile"] and (r["eah_status"] or "") not in ("Inactive", "Duplicate"))
        self.assertEqual(with_profile, active_with_profile)

        self.assertEqual(result["total_matched"], 2)
        self.assertEqual(result["total_new_added"], 1)
        self.assertEqual(result["total_removed_inactive"], 1)
        self.assertEqual(result["total_moved"], 1)
        conn.close()


class IdentityCandidateFlowTests(_TempDBTestCase):
    def test_accept_candidate_writes_ids_and_rejects_siblings(self):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        fid = db.upsert_faculty(conn, "som", {
            "first_name": "Ravi", "last_name": "Patel",
            "email": "rpatel@health.ucsd.edu",
        })
        db.insert_identity_candidates(conn, fid, [
            {"source": "openalex", "external_id": "A5111", "score": 0.85,
             "display_name": "Ravi Patel",
             "evidence": {"orcid": "0000-0002-1111-2222"}},
            {"source": "openalex", "external_id": "A5222", "score": 0.82,
             "display_name": "R. Patel", "evidence": {}},
        ])
        db.set_identity_status(conn, fid, "ambiguous")
        conn.commit()

        pending = db.list_identity_candidates(conn)
        self.assertEqual(len(pending), 2)
        best = next(c for c in pending if c["external_id"] == "A5111")

        db.decide_identity_candidate(conn, best["id"], "accept",
                                     reject_siblings=True)
        conn.commit()

        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A5111")
        self.assertEqual(row["orcid"], "0000-0002-1111-2222")
        self.assertEqual(row["identity_status"], "confirmed")
        self.assertEqual(db.list_identity_candidates(conn), [])

        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A5111": "accepted", "A5222": "rejected"})
        conn.close()

    def _seed_patel(self, candidates):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        fid = db.upsert_faculty(conn, "som", {
            "first_name": "Ravi", "last_name": "Patel",
            "email": "rpatel@health.ucsd.edu",
        })
        db.insert_identity_candidates(conn, fid, candidates)
        conn.commit()
        return conn, fid

    def test_accept_without_reject_siblings_leaves_them_pending(self):
        conn, fid = self._seed_patel([
            {"source": "openalex", "external_id": "A5111", "score": 0.85,
             "display_name": "Ravi Patel", "evidence": {}},
            {"source": "openalex", "external_id": "A5222", "score": 0.82,
             "display_name": "R. Patel", "evidence": {}},
        ])
        best = next(c for c in db.list_identity_candidates(conn)
                    if c["external_id"] == "A5111")
        db.decide_identity_candidate(conn, best["id"], "accept")
        conn.commit()

        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A5111": "accepted", "A5222": "pending"})
        conn.close()

    def test_merge_records_alternate_id_and_adopts_orcid(self):
        conn, fid = self._seed_patel([
            {"source": "openalex", "external_id": "A5111", "score": 0.85,
             "display_name": "Ravi Patel", "evidence": {}},
            {"source": "openalex", "external_id": "A5222", "score": 0.82,
             "display_name": "R. Patel",
             "evidence": {"orcid": "0000-0002-1111-2222"}},
        ])
        by_id = {c["external_id"]: c
                 for c in db.list_identity_candidates(conn)}

        # No primary yet: merge falls back to accept.
        db.decide_identity_candidate(conn, by_id["A5111"]["id"], "merge")
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A5111")
        self.assertEqual(row["identity_status"], "confirmed")

        # With a primary, merge records an alternate and adopts the ORCID.
        db.decide_identity_candidate(conn, by_id["A5222"]["id"], "merge")
        conn.commit()
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["openalex_id"], "A5111")
        self.assertEqual(json.loads(row["openalex_id_alt"]), ["A5222"])
        self.assertEqual(row["orcid"], "0000-0002-1111-2222")
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A5111": "accepted", "A5222": "merged"})

        # Idempotent: re-adding the alternate or the primary is a no-op.
        db.add_openalex_alt(conn, fid, "A5222")
        db.add_openalex_alt(conn, fid, "A5111")
        row = conn.execute("SELECT * FROM faculty WHERE id=?", (fid,)).fetchone()
        self.assertEqual(json.loads(row["openalex_id_alt"]), ["A5222"])
        conn.close()

    def test_reopen_restores_only_auto_rejected_batch(self):
        conn, fid = self._seed_patel([
            {"source": "openalex", "external_id": "A1", "score": 0.9,
             "display_name": "Ravi Patel", "evidence": {}},
            {"source": "openalex", "external_id": "A2", "score": 0.8,
             "display_name": "R. Patel", "evidence": {}},
            {"source": "openalex", "external_id": "A3", "score": 0.7,
             "display_name": "Ravi P.", "evidence": {}},
        ])
        by_id = {c["external_id"]: c
                 for c in db.list_identity_candidates(conn)}
        # Manually rejected first (its own decided_at) — must stay rejected.
        db.decide_identity_candidate(conn, by_id["A3"]["id"], "reject")
        # Accept auto-rejects the remaining pending sibling.
        db.decide_identity_candidate(conn, by_id["A1"]["id"], "accept",
                                     reject_siblings=True)
        conn.commit()

        n = db.reopen_identity_candidates(conn, fid)
        conn.commit()
        self.assertEqual(n, 1)
        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses,
                         {"A1": "accepted", "A2": "pending", "A3": "rejected"})
        conn.close()

    def test_data_migration_reopens_auto_rejected_siblings(self):
        conn, fid = self._seed_patel([
            {"source": "openalex", "external_id": "A1", "score": 0.9,
             "display_name": "Ravi Patel", "evidence": {}},
            {"source": "openalex", "external_id": "A2", "score": 0.8,
             "display_name": "R. Patel", "evidence": {}},
        ])
        # Simulate the legacy accept that auto-rejected the sibling at the
        # same instant.
        conn.execute("UPDATE identity_candidates SET status='accepted',"
                     " decided_at='2026-01-01T00:00:00+00:00'"
                     " WHERE external_id='A1'")
        conn.execute("UPDATE identity_candidates SET status='rejected',"
                     " decided_at='2026-01-01T00:00:00+00:00'"
                     " WHERE external_id='A2'")
        # Re-arm the one-time migration and re-run init_schema.
        conn.execute("DELETE FROM meta WHERE key ="
                     " 'data_migration:reopen_auto_rejected_siblings'")
        conn.commit()
        db.init_schema(conn)

        statuses = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id, status FROM identity_candidates")}
        self.assertEqual(statuses, {"A1": "accepted", "A2": "pending"})
        # Guarded: the meta key persists so it won't re-run.
        self.assertIsNotNone(db.get_meta(
            conn, "data_migration:reopen_auto_rejected_siblings"))
        conn.close()

    def test_backfill_selection_requires_resolved_identity(self):
        conn = db.connect(readonly=False)
        db.init_schema(conn)
        resolved = db.upsert_faculty(conn, "som", {
            "first_name": "A", "last_name": "Resolved", "email": "a@ucsd.edu",
            "pi_eligible": True, "identity_status": "auto",
        })
        db.upsert_faculty(conn, "som", {
            "first_name": "B", "last_name": "Unresolved", "email": "b@ucsd.edu",
            "pi_eligible": True,
        })
        conn.commit()
        candidates = db.fetch_backfill_candidates(conn, pi_only=True)
        self.assertEqual([c["_db_id"] for c in candidates], [resolved])
        conn.close()


class NormalizerContextTests(unittest.TestCase):
    def test_context_includes_awards_patents_and_openalex(self):
        from enrichment.normalizer import build_context
        faculty = {"first_name": "Jane", "last_name": "Smith",
                   "title": "Professor"}
        raw = {
            "openalex": {
                "h_index": 30, "works_count": 120, "citation_count": 4000,
                "expertise_keywords": ["Medieval History", "Paleography"],
                "recent_publications": [{"title": "On Manuscripts", "year": 2025}],
            },
            "patents_view": {"patents": [{"title": "Widget", "year": 2024}]},
            "wikidata": {"awards": [{"name": "Guggenheim Fellowship", "year": 2023}]},
        }
        context = build_context(faculty, raw)
        self.assertIn("OpenAlex metrics", context)
        self.assertIn("Medieval History", context)
        self.assertIn("On Manuscripts", context)
        self.assertIn("Guggenheim Fellowship", context)
        self.assertIn("Widget", context)

    def test_context_fingerprint_is_stable(self):
        from enrichment.pipeline import _raw_fingerprint
        self.assertEqual(_raw_fingerprint("abc"), _raw_fingerprint("abc"))
        self.assertNotEqual(_raw_fingerprint("abc"), _raw_fingerprint("abd"))


if __name__ == "__main__":
    unittest.main()
