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

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


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
