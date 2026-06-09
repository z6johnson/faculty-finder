"""Authenticated admin area.

Single-operator, session-based auth gated by the ADMIN_PASSWORD env var — kept
deliberately simple (no user database) because this is an internal tool. The
on-prem deployment should swap this for UCSD SSO (Shibboleth).

This area is how the sensitive EAH extract enters the system: it is uploaded
straight to the private runtime path (EAH_CSV_PATH) and never touches git.
"""

import functools
import hmac
import logging
import os
import tempfile

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)

from data import db

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Department keys -> human labels for the admin UI.
DEPTS = [("hwsph", "Public Health"), ("sio", "Scripps"), ("jacobs", "Jacobs")]

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
    from scripts.migrate_json_to_sqlite import run_migration

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

    try:
        result = eah_enrichment.run_eah_reconcile()
    except eah_enrichment.EAHFileMissing:
        flash("Upload saved, but the file could not be read as an EAH extract.", "error")
        return redirect(url_for("admin.eah"))
    except Exception:
        logger.exception("EAH reconcile failed")
        flash("Reconcile failed — the file was saved but not applied. See server logs.", "error")
        return redirect(url_for("admin.eah"))

    # Re-sync the runtime SQLite DB from the JSON snapshots the reconcile wrote.
    # rebuild=True is required: reconcile PRUNES inactive faculty from the JSON,
    # and a plain upsert would leave those now-inactive rows lingering in the DB
    # (defeating employment verification). A full rebuild makes the DB exactly
    # match the reconciled JSON.
    try:
        run_migration(rebuild=True)
    except Exception:
        logger.exception("DB re-sync after EAH reconcile failed")
        flash("Reconcile applied to JSON, but the database re-sync failed. See logs.", "error")
        return redirect(url_for("admin.eah"))

    session["last_eah_result"] = result
    flash(
        f"EAH reconcile complete — {result['total_matched']} matched, "
        f"{result['total_removed_inactive']} removed (inactive), "
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
    stats = {key: db.load_status(conn, key) for key, _ in DEPTS}
    return render_template(
        "admin/status.html",
        depts=DEPTS,
        stats=stats,
        recent_jobs=db.list_jobs(conn, limit=10),
        audit=db.recent_enrichment_log(conn, limit=40),
    )


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
    from enrichment import pipeline

    conn = db.get_read_conn()
    rec = db.admin_get_faculty(conn, faculty_id)
    if not rec:
        flash("Faculty not found.", "error")
        return redirect(url_for("admin.faculty_list"))

    dept = rec["department"]          # 'hwsph' | 'sio' | 'jacobs'
    stable_key = rec["stable_key"]

    edits = {}
    for f in _TEXT_EDIT_FIELDS:
        edits[f] = (request.form.get(f) or "").strip()
    for f in _LIST_EDIT_FIELDS:
        raw = request.form.get(f) or ""
        edits[f] = [s.strip() for s in raw.split(",") if s.strip()]

    # Update the JSON snapshot (the source of truth that survives rebuilds),
    # then upsert the same record into the live DB.
    data = pipeline._load_faculty(dept)
    target = next((r for r in data["faculty"]
                   if db.compute_stable_key(dept, r) == stable_key), None)
    if target is None:
        flash("Could not locate this record in the data file — not saved.", "error")
        return redirect(url_for("admin.faculty_edit", faculty_id=faculty_id))

    target.update(edits)
    pipeline._save_faculty(data, dept)

    wconn = db.connect(readonly=False)
    try:
        db.upsert_faculty(wconn, dept, target)
        wconn.commit()
    finally:
        wconn.close()

    flash("Saved.", "success")
    return redirect(url_for("admin.faculty_edit", faculty_id=faculty_id))
