"""Authenticated admin area.

Single-operator, session-based auth gated by the ADMIN_PASSWORD env var — kept
deliberately simple (no user database) because this is an internal tool. The
on-prem deployment should swap this for UCSD SSO (Shibboleth).

This area is how the sensitive EAH extract enters the system: it is uploaded
straight to the private runtime path (EAH_CSV_PATH) and never touches git.
"""

import functools
import hmac
import json
import logging
import os
import tempfile

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)

from data import db
from data import divisions

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Division slugs -> human labels for the admin UI (from the registry).
DEPTS = [(d.slug, d.label) for d in divisions.DIVISIONS]

# Fields an operator may curate by hand (the rest come from enrichment/EAH).
_TEXT_EDIT_FIELDS = ["title", "research_interests_enriched", "eah_status"]
_LIST_EDIT_FIELDS = ["expertise_keywords", "methodologies", "disease_areas",
                     "populations"]


def _admin_password():
    return os.environ.get("ADMIN_PASSWORD", "")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("is_admin"):
        return redirect(url_for("admin.home"))

    if request.method == "POST":
        expected = _admin_password()
        supplied = request.form.get("password", "")
        if not expected:
            flash("Admin access is not configured (ADMIN_PASSWORD unset).", "error")
        elif hmac.compare_digest(supplied, expected):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            # Only allow local redirects (no open-redirect via ?next=).
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("admin.home")
            return redirect(nxt)
        else:
            flash("Incorrect password.", "error")
    return render_template("admin/login.html")


@admin_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
@login_required
def home():
    return render_template("admin/home.html")


@admin_bp.route("/eah")
@login_required
def eah():
    from scripts import eah_enrichment
    return render_template(
        "admin/eah.html",
        eah_path=eah_enrichment.EAH_PATH,
        eah_exists=os.path.exists(eah_enrichment.EAH_PATH),
        last_result=session.get("last_eah_result"),
    )


@admin_bp.route("/eah/upload", methods=["POST"])
@login_required
def eah_upload():
    from scripts import eah_enrichment

    file = request.files.get("eah_csv")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("admin.eah"))
    if not file.filename.lower().endswith(".csv"):
        flash("Please upload a .csv file.", "error")
        return redirect(url_for("admin.eah"))

    # We always write to the fixed private destination (EAH_CSV_PATH); the
    # uploaded filename is never used, so there is no path-traversal surface.
    dest = eah_enrichment.EAH_PATH
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".csv")
    os.close(fd)
    try:
        file.save(tmp)
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        logger.exception("Failed to save uploaded EAH extract")
        flash("Failed to save the uploaded file.", "error")
        return redirect(url_for("admin.eah"))

    # The reconcile writes straight to SQLite (the source of truth). Faculty
    # absent from EAH are soft-flagged Inactive — never deleted here — so
    # EAH-seeded rows outside the legacy JSON snapshots always survive.
    try:
        result = eah_enrichment.run_eah_reconcile()
    except eah_enrichment.EAHFileMissing:
        flash("Upload saved, but the file could not be read as an EAH extract.", "error")
        return redirect(url_for("admin.eah"))
    except Exception:
        logger.exception("EAH reconcile failed")
        flash("Reconcile failed — the file was saved but not applied. See server logs.", "error")
        return redirect(url_for("admin.eah"))

    session["last_eah_result"] = result
    flash(
        f"EAH reconcile complete — {result['total_matched']} matched, "
        f"{result['total_moved']} moved divisions, "
        f"{result['total_removed_inactive']} flagged inactive, "
        f"{result['total_new_added']} added.",
        "success",
    )
    return redirect(url_for("admin.eah"))


# ---------------------------------------------------------------------------
# Enrichment (run now + schedule + job history)
# ---------------------------------------------------------------------------

@admin_bp.route("/enrichment")
@login_required
def enrichment():
    import scheduler
    conn = db.get_read_conn()
    return render_template(
        "admin/enrichment.html",
        depts=DEPTS,
        recent_jobs=db.list_jobs(conn, limit=15),
        scheduled=scheduler.scheduled_jobs(),
    )


@admin_bp.route("/enrich/run", methods=["POST"])
@login_required
def enrich_run():
    import jobs
    dept = (request.form.get("department") or "").strip().lower()
    valid = {d for d, _ in DEPTS}
    if dept not in valid:
        flash("Unknown department.", "error")
        return redirect(url_for("admin.enrichment"))
    job_id = jobs.submit("enrich", {"department": dept}, trigger="manual")
    flash(f"Enrichment queued for {dept} (job #{job_id}).", "success")
    return redirect(url_for("admin.enrichment"))


@admin_bp.route("/identity/run", methods=["POST"])
@login_required
def identity_run():
    import jobs
    params = {"pi_only": bool(request.form.get("pi_only"))}
    dept = (request.form.get("department") or "").strip().lower()
    if dept and dept in {d for d, _ in DEPTS}:
        params["department"] = dept
    job_id = jobs.submit("identity_resolve", params, trigger="manual")
    flash(f"Identity resolution queued (job #{job_id}).", "success")
    return redirect(url_for("admin.enrichment"))


@admin_bp.route("/backfill/run", methods=["POST"])
@login_required
def backfill_run():
    import jobs
    params = {"pi_only": bool(request.form.get("pi_only"))}
    batch = (request.form.get("batch_size") or "").strip()
    if batch.isdigit():
        params["batch_size"] = int(batch)
    job_id = jobs.submit("backfill", params, trigger="manual")
    flash(f"Backfill queued (job #{job_id}).", "success")
    return redirect(url_for("admin.enrichment"))


@admin_bp.route("/escholarship/run", methods=["POST"])
@login_required
def escholarship_run():
    import jobs
    job_id = jobs.submit("escholarship_harvest", {}, trigger="manual")
    flash(f"eScholarship harvest queued (job #{job_id}).", "success")
    return redirect(url_for("admin.enrichment"))


@admin_bp.route("/eah/reconcile", methods=["POST"])
@login_required
def eah_reconcile_run():
    import jobs
    job_id = jobs.submit("eah_reconcile", trigger="manual")
    flash(f"EAH reconcile queued (job #{job_id}).", "success")
    return redirect(url_for("admin.enrichment"))


# ---------------------------------------------------------------------------
# Status & audit dashboard
# ---------------------------------------------------------------------------

@admin_bp.route("/status")
@login_required
def status():
    conn = db.get_read_conn()
    return render_template(
        "admin/status.html",
        divisions=db.load_status_by_division(conn),
        recent_jobs=db.list_jobs(conn, limit=10),
        audit=db.recent_enrichment_log(conn, limit=40),
    )


# ---------------------------------------------------------------------------
# Identity review queue
# ---------------------------------------------------------------------------

def _cluster_identity_candidates(candidates):
    """Cluster one faculty's pending rows into same-person groups so the
    reviewer makes one decision per person, not one per OpenAlex profile.

    Display-only: reuses identity_rules.is_same_person pairwise (no score
    margin — the rows are shown together regardless). Each cluster's
    canonical is the largest profile by works/citations; siblings get
    rejected automatically when the canonical is accepted."""
    from enrichment import identity_rules

    parsed = []
    for cand in candidates:
        cand = dict(cand)
        try:
            cand["evidence_dict"] = json.loads(cand.get("evidence") or "{}")
        except ValueError:
            cand["evidence_dict"] = {}
        parsed.append(cand)

    def _same(a, b):
        return (a["source"] == "openalex" and b["source"] == "openalex"
                and identity_rules.is_same_person(
                    {"evidence": a["evidence_dict"],
                     "display_name": a.get("display_name")},
                    {"evidence": b["evidence_dict"],
                     "display_name": b.get("display_name")}))

    def _rank(c):
        return (c["evidence_dict"].get("works_count") or 0,
                c["evidence_dict"].get("cited_by_count") or 0,
                c.get("external_id") or "")

    clusters = []
    for cand in parsed:
        for cluster in clusters:
            if all(_same(member, cand) for member in cluster):
                cluster.append(cand)
                break
        else:
            clusters.append([cand])

    out = []
    for cluster in clusters:
        canonical = max(cluster, key=_rank)
        siblings = [c for c in cluster if c["id"] != canonical["id"]]
        out.append({"canonical": canonical, "siblings": siblings,
                    "member_ids": [c["id"] for c in cluster]})
    return out


@admin_bp.route("/identity")
@login_required
def identity_queue():
    conn = db.get_read_conn()
    dept = (request.args.get("dept") or "").strip().lower() or None
    if dept and dept not in {d for d, _ in DEPTS}:
        dept = None
    candidates = db.list_identity_candidates(conn, department=dept)
    # Group by faculty for display.
    grouped = []
    for cand in candidates:
        if not grouped or grouped[-1]["faculty_id"] != cand["faculty_id"]:
            grouped.append({
                "faculty_id": cand["faculty_id"],
                "name": f"{cand['f_first']} {cand['f_last']}",
                "title": cand["f_title"],
                "division": cand["f_division_school"] or cand["f_department"],
                "email": cand["f_email"],
                "candidates": [],
            })
        grouped[-1]["candidates"].append(cand)
    for g in grouped:
        g["clusters"] = _cluster_identity_candidates(g["candidates"])
    return render_template("admin/identity_queue.html", groups=grouped,
                           depts=DEPTS, dept=dept)


@admin_bp.route("/identity/resweep", methods=["POST"])
@login_required
def identity_resweep():
    import jobs
    job_id = jobs.submit("identity_resweep", {}, trigger="manual")
    flash(f"Auto-accept re-sweep queued (job #{job_id}).", "success")
    return redirect(url_for("admin.identity_queue"))


@admin_bp.route("/identity/<int:candidate_id>/decide", methods=["POST"])
@login_required
def identity_decide(candidate_id):
    accept = request.form.get("decision") == "accept"
    # Sibling rows clustered with this candidate in the review UI (duplicate
    # profiles of the same person): rejected together, and on accept the
    # canonical may adopt a sibling's ORCID (same person).
    sibling_ids = [int(s) for s in
                   (request.form.get("cluster_ids") or "").split(",")
                   if s.strip().isdigit() and int(s) != candidate_id]
    conn = db.connect(readonly=False)
    try:
        sibling_orcid = None
        if accept:
            for sid in sibling_ids:
                sib = db.get_identity_candidate(conn, sid)
                if not sib:
                    continue
                evidence = json.loads(sib["evidence"] or "{}")
                if evidence.get("orcid"):
                    sibling_orcid = evidence["orcid"]
                    break
        cand = db.decide_identity_candidate(conn, candidate_id, accept)
        if cand and accept and sibling_orcid:
            conn.execute(
                "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                (sibling_orcid, cand["faculty_id"]))
        if cand and not accept:
            for sid in sibling_ids:
                db.decide_identity_candidate(conn, sid, accept=False)
        conn.commit()
    finally:
        conn.close()
    if cand:
        n_extra = len(sibling_ids) if not accept else 0
        flash(("Accepted" if accept else "Rejected")
              + f" {cand['source']} match for faculty #{cand['faculty_id']}."
              + (f" ({n_extra} duplicate profile(s) rejected too.)"
                 if n_extra else ""),
              "success")
    else:
        flash("Candidate not found or already decided.", "error")
    return redirect(url_for("admin.identity_queue"))


@admin_bp.route("/identity/<int:faculty_id>/not-findable", methods=["POST"])
@login_required
def identity_not_findable(faculty_id):
    conn = db.connect(readonly=False)
    try:
        db.reject_faculty_identity(conn, faculty_id)
        conn.commit()
    finally:
        conn.close()
    flash(f"Faculty #{faculty_id} marked not findable — excluded from enrichment.",
          "success")
    return redirect(url_for("admin.identity_queue"))


# ---------------------------------------------------------------------------
# Faculty review / curate
# ---------------------------------------------------------------------------

@admin_bp.route("/faculty")
@login_required
def faculty_list():
    conn = db.get_read_conn()
    dept = (request.args.get("dept") or "").strip().lower() or None
    if dept and dept not in {d for d, _ in DEPTS}:
        dept = None
    query = (request.args.get("q") or "").strip() or None
    rows, total = db.admin_list_faculty(conn, department=dept, query=query, limit=50)
    return render_template("admin/faculty_list.html", rows=rows, total=total,
                           depts=DEPTS, dept=dept, query=query or "")


@admin_bp.route("/faculty/<int:faculty_id>")
@login_required
def faculty_edit(faculty_id):
    conn = db.get_read_conn()
    rec = db.admin_get_faculty(conn, faculty_id)
    if not rec:
        flash("Faculty not found.", "error")
        return redirect(url_for("admin.faculty_list"))
    return render_template("admin/faculty_edit.html", rec=rec,
                           list_fields=_LIST_EDIT_FIELDS)


@admin_bp.route("/faculty/<int:faculty_id>", methods=["POST"])
@login_required
def faculty_save(faculty_id):
    conn = db.get_read_conn()
    rec = db.admin_get_faculty(conn, faculty_id)
    if not rec:
        flash("Faculty not found.", "error")
        return redirect(url_for("admin.faculty_list"))

    for f in _TEXT_EDIT_FIELDS:
        rec[f] = (request.form.get(f) or "").strip()
    for f in _LIST_EDIT_FIELDS:
        raw = request.form.get(f) or ""
        rec[f] = [s.strip() for s in raw.split(",") if s.strip()]

    # SQLite is the source of truth; write the edit straight to the row.
    wconn = db.connect(readonly=False)
    try:
        db.save_faculty_record(wconn, faculty_id, rec)
        wconn.commit()
    finally:
        wconn.close()

    flash("Saved.", "success")
    return redirect(url_for("admin.faculty_edit", faculty_id=faculty_id))
