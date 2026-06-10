import logging
import os
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from admin import admin_bp
from data import db
from utils.document_parser import extract_text
from utils.grant_matcher import process_grant, process_text

load_dotenv()

app = Flask(__name__)
# The EAH extract can be larger than a funding doc, so allow a generous upload
# ceiling for the (auth-gated) admin endpoints.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

# Session secret for the admin area. Must be set in production; a missing key
# falls back to an ephemeral random one (logs out admins on restart).
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies in production; relaxed locally so dev over http works.
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# CORS only needed for local development (same-origin on Vercel)
CORS(app, origins=["http://localhost:*", "http://127.0.0.1:*"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not os.environ.get("SECRET_KEY"):
    logger.warning("SECRET_KEY not set — using an ephemeral key; admin sessions "
                   "will not survive a restart.")
if not os.environ.get("ADMIN_PASSWORD"):
    logger.warning("ADMIN_PASSWORD not set — the /admin area is locked out until "
                   "it is configured.")

app.register_blueprint(admin_bp)


def _init_runtime():
    """One-time process setup: ensure the schema (jobs table), recover jobs
    left running by a previous crash, and start the weekly scheduler."""
    try:
        db.ensure_schema()
        db.fail_stale_jobs()
    except Exception:
        logger.exception("Runtime DB init failed (continuing).")
    if os.environ.get("ENABLE_SCHEDULER", "true").lower() != "false":
        try:
            import scheduler
            scheduler.start()
        except Exception:
            logger.exception("Scheduler failed to start (continuing).")


_init_runtime()

ALLOWED_EXTENSIONS = {"pdf", "txt"}

# Division slugs accepted by the API. "all" spans every division and "other"
# is the registry's fallback bucket. The db layer treats "all"/None as no
# department filter.
from data.divisions import known_slugs

VALID_DEPTS = set(known_slugs()) | {"all", "other"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# Fields to include in the faculty directory API response
FACULTY_DIRECTORY_FIELDS = [
    "first_name", "last_name", "degrees", "title", "email",
    "department", "department_label",
    "research_interests", "research_interests_enriched",
    "expertise_keywords", "disease_areas", "methodologies", "populations",
    "h_index", "profile_url", "orcid",
    "funded_grants", "recent_publications",
    "committee_service", "integrity_flags",
]


@app.route("/")
def index():
    """Serve the frontend."""
    return send_from_directory(".", "index.html")


@app.route("/api/faculty")
def faculty_directory():
    """Return faculty data for the expert directory (browsing/filtering).

    Query params:
        dept: "sio"/"jacobs"/"hwsph", or "all" (default) for every school.
        q:     full-text search query.
        limit/offset: pagination (limit capped at 50).
    """
    dept = request.args.get("dept", "").strip().lower() or "all"
    if dept not in VALID_DEPTS:
        return jsonify({"error": f"Unknown department: {dept}. Use a division "
                                 f"slug ({', '.join(sorted(VALID_DEPTS))})."}), 400

    query = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 20)), 50)
    offset = int(request.args.get("offset", 0))

    conn = db.get_read_conn()
    results, total = db.search_faculty(
        conn, dept, query, limit, offset, FACULTY_DIRECTORY_FIELDS
    )
    return jsonify({"results": results, "total": total, "offset": offset, "limit": limit})


@app.route("/api/match", methods=["POST"])
def match():
    """Match a file-uploaded funding opportunity against faculty."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF and TXT files are supported"}), 400

    try:
        text = extract_text(file)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dept = request.form.get("dept", "").strip().lower() or "all"
    if dept not in VALID_DEPTS:
        return jsonify({"error": f"Unknown department: {dept}"}), 400

    try:
        results = process_grant(text, department=dept, conn=db.get_read_conn())
    except Exception as e:
        logger.exception("Document processing failed")
        return jsonify({"error": _friendly_error(e)}), 500

    return jsonify(results)


@app.route("/api/match-text", methods=["POST"])
def match_text():
    """Match manually entered expertise text against faculty."""
    data = request.get_json(silent=True)
    if not data or not data.get("text"):
        return jsonify({"error": "No text provided"}), 400

    text = data["text"].strip()
    if len(text) < 20:
        return jsonify({"error": "Please provide at least 20 characters of text"}), 400

    if len(text) > 60000:
        return jsonify({"error": "Text is too long. Maximum 60,000 characters."}), 400

    dept = data.get("dept", "").strip().lower() or "all"
    if dept not in VALID_DEPTS:
        return jsonify({"error": f"Unknown department: {dept}"}), 400

    try:
        results = process_text(text, department=dept, conn=db.get_read_conn())
    except Exception as e:
        logger.exception("Text processing failed")
        return jsonify({"error": _friendly_error(e)}), 500

    return jsonify(results)


def _friendly_error(e):
    """Convert exception to user-friendly error message."""
    detail = str(e)
    if "api_key" in detail.lower() or "auth" in detail.lower():
        return "LLM API credentials are not configured. Check LITELLM_API_KEY and LITELLM_API_BASE."
    elif "connect" in detail.lower() or "timeout" in detail.lower():
        return "Could not reach the LLM API. Please try again shortly."
    elif "parse" in detail.lower() or "json" in detail.lower():
        return "The model returned an unparseable response. Please try again."
    else:
        return f"Failed to analyze the document: {detail}"


@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "File is too large. Maximum size is 10 MB."}), 413
