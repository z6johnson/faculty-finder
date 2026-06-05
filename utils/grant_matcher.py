import json
import logging
import os
import re
import time

from litellm import completion

logger = logging.getLogger(__name__)

MAX_INTERESTS_LENGTH = 300
PRE_FILTER_CANDIDATES = 60


def _get_model():
    model = os.getenv("LITELLM_MODEL", "api-gpt-oss-120b")
    if "/" not in model:
        model = f"openai/{model}"
    return model


def _call_llm(system_prompt, user_prompt, max_tokens=2000, temperature=0.1,
               json_mode=False):
    """Make a LiteLLM completion call and return the content string."""
    kwargs = dict(
        model=_get_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        api_key=os.getenv("LITELLM_API_KEY"),
        api_base=os.getenv("LITELLM_API_BASE"),
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = completion(**kwargs)
    except Exception:
        if json_mode:
            # Model may not support JSON mode — retry without it
            logger.info("JSON mode not supported by model, retrying without it")
            kwargs.pop("response_format", None)
            response = completion(**kwargs)
        else:
            raise

    content = response.choices[0].message.content
    if not content:
        raise ValueError("LLM returned an empty response")
    return content


def _parse_json_response(text):
    """Parse JSON from an LLM response, handling markdown fences."""
    if not text or not isinstance(text, str):
        raise ValueError("Could not parse JSON from LLM response: empty input")

    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting from markdown code fence
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding the first { or [ and matching to last } or ]
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    # Try to recover a truncated JSON array — find last complete object
    arr_start = text.find("[")
    if arr_start != -1:
        substring = text[arr_start:]
        last_brace = substring.rfind("}")
        if last_brace > 0:
            candidate = substring[: last_brace + 1] + "]"
            try:
                result = json.loads(candidate)
                logger.warning("Recovered truncated JSON array (%d items)", len(result))
                return result
            except json.JSONDecodeError:
                pass

    raise ValueError("Could not parse JSON from LLM response")


def _unwrap_matches_list(parsed):
    """Ensure the parsed match result is a list of match objects.

    Models sometimes wrap the array in an object like {"matches": [...]}.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("matches", "results", "faculty_matches", "ranked_matches"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        for value in parsed.values():
            if isinstance(value, list):
                return value
    raise ValueError("Expected a JSON array of matches from LLM response")


# ---------------------------------------------------------------------------
# Keyword Pre-filter — reduces LLM token cost for large faculty sets
# ---------------------------------------------------------------------------

def _normalize_keyword(kw):
    """Lowercase and strip a keyword for comparison."""
    return kw.strip().lower()


def _extract_requirement_keywords(requirements):
    """Pull searchable terms from extracted requirements."""
    terms = set()
    for field in ("overall_research_themes",):
        for item in requirements.get(field, []):
            terms.add(_normalize_keyword(item))

    for inv_req in requirements.get("investigator_requirements", []):
        for item in inv_req.get("expertise_areas", []):
            terms.add(_normalize_keyword(item))
        for item in inv_req.get("qualifications", []):
            for word in item.lower().split():
                if len(word) > 3:
                    terms.add(word)

    # Also split multi-word terms into individual words for partial matching
    expanded = set()
    for term in terms:
        expanded.add(term)
        for word in term.split():
            if len(word) > 3:
                expanded.add(word)
    return expanded


def _faculty_keyword_score(faculty, requirement_keywords):
    """Score a faculty member's keyword overlap with requirements."""
    faculty_text = " ".join([
        (faculty.get("research_interests_enriched") or ""),
        (faculty.get("research_interests") or ""),
        " ".join(faculty.get("expertise_keywords") or []),
        " ".join(faculty.get("disease_areas") or []),
        " ".join(faculty.get("methodologies") or []),
        " ".join(faculty.get("populations") or []),
    ]).lower()

    if not faculty_text.strip():
        return 0

    score = 0
    for kw in requirement_keywords:
        if kw in faculty_text:
            score += 1
    return score


def _pre_filter_faculty(faculty_with_interests, requirements, max_candidates=PRE_FILTER_CANDIDATES):
    """Pre-filter faculty using keyword overlap to reduce LLM input size.

    For small faculty lists (≤ max_candidates), returns all faculty unchanged.
    For larger lists, returns the top max_candidates by keyword relevance.
    """
    if len(faculty_with_interests) <= max_candidates:
        return faculty_with_interests

    keywords = _extract_requirement_keywords(requirements)
    if not keywords:
        return faculty_with_interests

    scored = [
        (f, _faculty_keyword_score(f, keywords))
        for f in faculty_with_interests
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Always include at least max_candidates, even those with score 0
    filtered = [f for f, _ in scored[:max_candidates]]
    logger.info("Pre-filtered %d → %d faculty using %d keywords",
                len(faculty_with_interests), len(filtered), len(keywords))
    return filtered


# ---------------------------------------------------------------------------
# LLM Prompts — neutral language (no "grant" terminology)
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = """\
You are an expert research funding analyst. You will be given the text of a \
funding opportunity document. Extract the requirements and produce a structured \
summary in JSON format. Focus on:

1. A brief summary of the funding opportunity (2-3 sentences capturing the \
purpose, scope, and what the funder is looking for)
2. Investigator requirements — what expertise, qualifications, and roles \
the opportunity describes (use the document's own terminology for roles; do not \
impose labels like "PI" or "Co-PI" unless the document explicitly uses them)
3. Key personnel or team composition needs
4. Required expertise areas and disciplines
5. Preferred qualifications (degrees, experience level, specific skills)
6. Any eligibility constraints (career stage, institution type, etc.)

Return ONLY valid JSON with this structure:
{
  "grant_title": "string or null",
  "funding_agency": "string or null",
  "grant_summary": "A 2-3 sentence summary of the funding opportunity, its \
purpose, and what it seeks to fund.",
  "investigator_requirements": [
    {
      "role": "role as described in the document (e.g., Lead Investigator, \
Project Director, or simply Investigator)",
      "expertise_areas": ["list of required expertise domains"],
      "qualifications": ["list of degree/experience requirements"],
      "constraints": ["any eligibility constraints"]
    }
  ],
  "overall_research_themes": ["list of broad research themes"]
}

If investigator roles are not explicitly defined, create a single entry with \
role "Investigator" containing all requirements. \
Do NOT invent requirements that are not stated or clearly implied."""

MATCH_SYSTEM_PROMPT = """\
You are a research collaboration matchmaker for UC San Diego. You will receive:
1. Extracted requirements from a funding opportunity
2. A list of faculty members with their research interests

Your task: Rank the faculty by how well their research interests align with \
the opportunity requirements. For each match, evaluate these dimensions:

- expertise_alignment (0-100): How closely the faculty member's research \
interests and expertise match the required research areas
- methodological_fit (0-100): How well the researcher's methods align with \
the methodological needs of the opportunity
- track_record (0-100): Strength of relevant publication and funding history \
relative to the opportunity's scope

The overall match_score should reflect a weighted synthesis of these \
dimensions (expertise alignment is most important, followed by \
methodological fit, then track record).

Do NOT assign or recommend investigator roles (PI, Co-PI, etc.). The \
purpose of this tool is to identify aligned faculty — role decisions belong \
to the research team.

Return ONLY valid JSON as an array of matches, ordered by relevance (best \
first). Include AT MOST 15 matches. Only include faculty with meaningful \
alignment (score >= 40). Each match object:
{
  "faculty_id": <integer index from the faculty list>,
  "match_score": <integer 0-100>,
  "expertise_alignment": <integer 0-100>,
  "methodological_fit": <integer 0-100>,
  "track_record": <integer 0-100>,
  "match_reasoning": "2-3 sentence explanation of why this faculty member \
is a strong match, referencing specific research interests and opportunity \
requirements."
}"""


def extract_grant_requirements(grant_text):
    """Call #1: Extract investigator requirements from funding opportunity text."""
    user_prompt = (
        "Here is the funding opportunity document text:\n\n"
        "---\n"
        f"{grant_text}\n"
        "---\n\n"
        "Extract the requirements and summary as specified."
    )
    last_error = None
    for attempt in range(2):
        if attempt > 0:
            logger.warning("Retrying extract_grant_requirements after parse failure (attempt %d)", attempt + 1)
            time.sleep(1)
        raw = _call_llm(EXTRACT_SYSTEM_PROMPT, user_prompt, max_tokens=2000, temperature=0,
                        json_mode=True)
        try:
            return _parse_json_response(raw)
        except ValueError as exc:
            logger.warning("Failed to parse extract response (attempt %d): %s\nRaw response (first 500 chars): %s",
                          attempt + 1, exc, raw[:500] if raw else raw)
            last_error = exc
    raise last_error


def _truncate(text, max_length):
    """Truncate text to max_length, appending '...' if shortened."""
    if not text or len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def match_faculty(requirements, faculty_with_interests):
    """Call #2: Match faculty against extracted opportunity requirements."""
    # Build compact faculty summary
    lines = []
    for idx, f in enumerate(faculty_with_interests):
        degrees = ", ".join(f.get("degrees") or [])
        name = f"{f['first_name']} {f['last_name']}"
        interests = _truncate(
            f.get("_effective_interests")
            or f.get("research_interests_enriched")
            or f.get("research_interests")
            or "N/A",
            MAX_INTERESTS_LENGTH,
        )
        summary = f"ID:{idx} | {name}, {degrees} | {f['title']} | Interests: {interests}"
        extras = []
        if f.get("expertise_keywords"):
            extras.append(f"Keywords: {', '.join(f['expertise_keywords'][:8])}")
        if f.get("funded_grants"):
            extras.append(f"Funded projects: {len(f['funded_grants'])}")
        if f.get("h_index"):
            extras.append(f"h-index: {f['h_index']}")
        if extras:
            summary += f" | {' | '.join(extras)}"
        lines.append(summary)
    faculty_summary = "\n".join(lines)

    user_prompt = (
        "## Funding Opportunity Requirements\n"
        f"{json.dumps(requirements, indent=2)}\n\n"
        "## Faculty Directory\n"
        f"{faculty_summary}\n\n"
        "Rank the best-matching faculty for this opportunity."
    )

    # Retry once on parse failure
    last_error = None
    for attempt in range(2):
        if attempt > 0:
            logger.warning("Retrying match_faculty after parse failure (attempt %d)", attempt + 1)
            time.sleep(1)
        raw = _call_llm(MATCH_SYSTEM_PROMPT, user_prompt, max_tokens=4000, temperature=0.05,
                        json_mode=True)
        try:
            parsed = _parse_json_response(raw)
            matches = _unwrap_matches_list(parsed)
            break
        except ValueError as exc:
            logger.warning("Failed to parse match response (attempt %d): %s\nRaw response (first 500 chars): %s",
                          attempt + 1, exc, raw[:500] if raw else raw)
            last_error = exc
    else:
        raise last_error

    # Enrich matches with full faculty data
    enriched = []
    for m in matches:
        fid = m.get("faculty_id")
        if fid is None or fid < 0 or fid >= len(faculty_with_interests):
            continue
        faculty = faculty_with_interests[fid]
        enriched.append(
            {
                "rank": len(enriched) + 1,
                "first_name": faculty["first_name"],
                "last_name": faculty["last_name"],
                "degrees": faculty.get("degrees", []),
                "title": faculty.get("title", ""),
                "email": faculty.get("email"),
                "department": faculty.get("department", ""),
                "department_label": faculty.get("department_label", ""),
                "research_interests": faculty.get("research_interests", ""),
                "match_score": m.get("match_score", 0),
                "expertise_alignment": m.get("expertise_alignment", 0),
                "methodological_fit": m.get("methodological_fit", 0),
                "track_record": m.get("track_record", 0),
                "match_reasoning": m.get("match_reasoning", ""),
            }
        )

    # Sort by score descending
    enriched.sort(key=lambda x: x["match_score"], reverse=True)
    for i, m in enumerate(enriched):
        m["rank"] = i + 1

    return enriched


def _has_research_profile(faculty):
    """Check if a faculty member has enough data to be matched."""
    return bool(
        faculty.get("research_interests")
        or faculty.get("research_interests_enriched")
        or faculty.get("expertise_keywords")
    )


def _match_against_db(requirements, department, conn):
    """Shared matching tail: pull candidates from SQLite/FTS5 and rank them.

    The candidate pre-filter (top-N by keyword relevance) now runs in the
    database instead of scanning every faculty record in memory.
    """
    from data import db

    if conn is None:
        conn = db.get_read_conn()

    with_profile, without_profile = db.count_match_pool(conn, department)
    keywords = _extract_requirement_keywords(requirements)
    candidates = db.fetch_match_candidates(
        conn, department, keywords,
        pool_with_profile=with_profile, limit=PRE_FILTER_CANDIDATES,
    )

    matches = match_faculty(requirements, candidates)

    return {
        "grant_summary": requirements,
        "matches": matches,
        "faculty_without_interests_count": without_profile,
        "total_faculty_considered": with_profile,
    }


def process_grant(grant_text, department=None, conn=None):
    """Coordinate the full matching pipeline for file-uploaded documents.

    Args:
        grant_text: Extracted text from the funding opportunity document.
        department: Department key ("hwsph"/"sio"/"jacobs"/"all").
        conn: Optional read-only SQLite connection (defaults to the shared one).

    Returns:
        Dict with grant_summary, matches, and metadata.
    """
    requirements = extract_grant_requirements(grant_text)
    return _match_against_db(requirements, department, conn)


def process_text(text, department=None, conn=None):
    """Coordinate the matching pipeline for manually entered expertise text.

    Uses the same two-stage pipeline: extract requirements from the text,
    then match against faculty.

    Args:
        text: Free-text description of expertise requirements.
        department: Department key ("hwsph"/"sio"/"jacobs"/"all").
        conn: Optional read-only SQLite connection (defaults to the shared one).

    Returns:
        Dict with grant_summary, matches, and metadata.
    """
    # The extraction prompt works well with free text too — it will
    # structure whatever requirements are described
    requirements = extract_grant_requirements(text)
    return _match_against_db(requirements, department, conn)
