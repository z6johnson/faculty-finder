"""LLM-based data normalizer for enrichment data.

Takes raw data from multiple sources and produces structured,
normalized faculty profile fields using LiteLLM.
"""

import json
import logging

from utils.grant_matcher import _call_llm, _parse_json_response

logger = logging.getLogger(__name__)

NORMALIZE_SYSTEM_PROMPT = """\
You are an academic profile analyst. You will receive raw data about a \
faculty member collected from multiple public sources (university profile, \
NIH/NSF grants, publications from OpenAlex/PubMed/Semantic Scholar and \
discipline-specific indexes, patents, and awards). Your task is to \
produce a clean, structured summary of their research expertise.

Rules:
1. Merge and deduplicate information from all sources.
2. Preserve factual accuracy — do not invent expertise not supported by the data.
3. When sources conflict, prefer the university profile over other sources.
4. Be concise but comprehensive.
5. Factor honors, awards, and patents into the narrative when present — they \
signal recognized expertise and translational work.

Return ONLY valid JSON with this structure:
{
  "research_interests_enriched": "A 2-4 sentence narrative summary of their \
research focus areas, suitable for matching against funding opportunities.",
  "expertise_keywords": ["list", "of", "specific", "expertise", "keywords"],
  "methodologies": ["research methods they use, e.g., RCT, cohort study, \
numerical modeling, field sampling, remote sensing, ..."],
  "disease_areas": ["specific diseases, health conditions, or (for non-health \
disciplines) primary research domains they study, e.g., ocean circulation, \
plate tectonics, coral reef ecology, climate modeling, ..."],
  "populations": ["populations they study (e.g., adolescents, refugees) or, \
for non-health disciplines, primary study systems/regions (e.g., Pacific \
Ocean, deep sea, Antarctic, coastal wetlands, ...)"]
}

If there is insufficient data for a field, use an empty list or null."""


# Sources whose recent_publications get the generic rendering below.
_GENERIC_PUB_SOURCES = ("openalex", "dblp", "arxiv", "nasa_ads", "repec",
                        "escholarship")


def _render_pubs(pubs, limit=10):
    lines = []
    for p in pubs[:limit]:
        line = f"- {p.get('title', 'Untitled')}"
        if p.get("journal"):
            line += f" ({p['journal']}"
            if p.get("year"):
                line += f", {p['year']}"
            line += ")"
        elif p.get("year"):
            line += f" ({p['year']})"
        lines.append(line)
        if p.get("mesh_terms"):
            lines.append(f"  MeSH: {', '.join(p['mesh_terms'][:5])}")
    return lines


def build_context(faculty_dict, raw_enrichment_data):
    """Build the LLM normalization context string.

    Exposed separately so the pipeline can fingerprint it (faculty.raw_hash)
    and skip the LLM call when nothing changed since the previous run.
    Returns None when there is not enough data to normalize.
    """
    parts = []

    name = f"{faculty_dict.get('first_name', '')} {faculty_dict.get('last_name', '')}"
    title = faculty_dict.get("title", "")
    parts.append(f"Faculty: {name}, {title}")

    if faculty_dict.get("research_interests"):
        parts.append(
            f"Original research interests (from university directory): "
            f"{faculty_dict['research_interests']}"
        )

    for source_name, data in raw_enrichment_data.items():
        if not data:
            continue

        if source_name in ("ucsd_profile", "scripps_profile") and data.get("research_interests_enriched"):
            parts.append(
                f"UCSD Profile description: {data['research_interests_enriched']}"
            )

        if source_name == "nih_reporter" and data.get("funded_grants"):
            grants_text = []
            for g in data["funded_grants"][:10]:
                grants_text.append(
                    f"- {g.get('title', 'Untitled')} "
                    f"({g.get('agency', 'NIH')})"
                )
                if g.get("abstract"):
                    grants_text.append(f"  Abstract excerpt: {g['abstract'][:200]}")
            parts.append("NIH-funded grants:\n" + "\n".join(grants_text))

        if source_name == "pubmed" and data.get("recent_publications"):
            pubs_text = []
            for p in data["recent_publications"][:10]:
                line = f"- {p.get('title', 'Untitled')}"
                if p.get("journal"):
                    line += f" ({p['journal']}"
                    if p.get("year"):
                        line += f", {p['year']}"
                    line += ")"
                pubs_text.append(line)
                if p.get("mesh_terms"):
                    pubs_text.append(f"  MeSH: {', '.join(p['mesh_terms'][:5])}")
            parts.append("Recent PubMed publications:\n" + "\n".join(pubs_text))

        if source_name == "nsf_awards" and data.get("funded_grants"):
            grants_text = []
            for g in data["funded_grants"][:10]:
                grants_text.append(
                    f"- {g.get('title', 'Untitled')} "
                    f"(NSF{', ' + g['nsf_program'] if g.get('nsf_program') else ''})"
                )
                if g.get("abstract"):
                    grants_text.append(f"  Abstract excerpt: {g['abstract'][:200]}")
            parts.append("NSF-funded grants:\n" + "\n".join(grants_text))

        if source_name == "semantic_scholar":
            metrics = []
            if data.get("h_index") is not None:
                metrics.append(f"h-index: {data['h_index']}")
            if data.get("paper_count") is not None:
                metrics.append(f"{data['paper_count']} papers")
            if data.get("citation_count") is not None:
                metrics.append(f"{data['citation_count']} citations")
            if metrics:
                parts.append(f"Semantic Scholar metrics: {', '.join(metrics)}")
            if data.get("recent_publications"):
                pubs_text = []
                for p in data["recent_publications"][:8]:
                    line = f"- {p.get('title', 'Untitled')}"
                    if p.get("journal"):
                        line += f" ({p['journal']}"
                        if p.get("year"):
                            line += f", {p['year']}"
                        line += ")"
                    pubs_text.append(line)
                parts.append("Recent Semantic Scholar publications:\n" + "\n".join(pubs_text))

        if source_name == "orcid" and data.get("works_count"):
            parts.append(f"ORCID: {data['works_count']} total works")
            if data.get("recent_works"):
                parts.append(
                    "Recent ORCID works:\n" +
                    "\n".join(f"- {w}" for w in data["recent_works"][:5])
                )

        if source_name == "openalex":
            metrics = []
            if data.get("h_index") is not None:
                metrics.append(f"h-index: {data['h_index']}")
            if data.get("works_count") is not None:
                metrics.append(f"{data['works_count']} works")
            if data.get("citation_count") is not None:
                metrics.append(f"{data['citation_count']} citations")
            if metrics:
                parts.append(f"OpenAlex metrics: {', '.join(metrics)}")
            if data.get("expertise_keywords"):
                parts.append("OpenAlex research topics: "
                             + ", ".join(data["expertise_keywords"][:15]))

        if source_name in _GENERIC_PUB_SOURCES and data.get("recent_publications"):
            label = source_name.replace("_", " ")
            parts.append(f"Recent publications ({label}):\n"
                         + "\n".join(_render_pubs(data["recent_publications"])))

        if source_name == "clinical_trials" and data.get("funded_grants"):
            trials_text = [f"- {t.get('title', 'Untitled')} ({t.get('status', '')})"
                           for t in data["funded_grants"][:8]]
            parts.append("Clinical trials (as investigator):\n" + "\n".join(trials_text))

        if source_name == "patents_view" and data.get("patents"):
            pat_text = []
            for p in data["patents"][:8]:
                line = f"- {p.get('title', 'Untitled')}"
                if p.get("year"):
                    line += f" ({p['year']})"
                pat_text.append(line)
            parts.append("US patents (UC-assigned):\n" + "\n".join(pat_text))

        if source_name == "wikidata" and data.get("awards"):
            award_text = []
            for a in data["awards"][:10]:
                line = f"- {a.get('name', '')}"
                if a.get("year"):
                    line += f" ({a['year']})"
                award_text.append(line)
            parts.append("Honors and awards:\n" + "\n".join(award_text))

    # Fallback: if raw enrichment data didn't include grants or publications
    # (e.g. source API was down or returned nothing this run), use data that
    # was fetched in a previous run and is already stored on the faculty record.
    has_grants_context = any(
        "grants" in p.lower() for p in parts[1:]  # skip the name line
    )
    has_pubs_context = any(
        "publication" in p.lower() for p in parts[1:]
    )

    if not has_grants_context and faculty_dict.get("funded_grants"):
        grants_text = []
        for g in faculty_dict["funded_grants"][:10]:
            grants_text.append(
                f"- {g.get('title', 'Untitled')} "
                f"({g.get('agency', 'Unknown')})"
            )
            if g.get("abstract"):
                grants_text.append(f"  Abstract excerpt: {g['abstract'][:200]}")
        if grants_text:
            parts.append("Previously fetched grants:\n" + "\n".join(grants_text))

    if not has_pubs_context and faculty_dict.get("recent_publications"):
        pubs_text = []
        for p in faculty_dict["recent_publications"][:10]:
            line = f"- {p.get('title', 'Untitled')}"
            if p.get("journal"):
                line += f" ({p['journal']}"
                if p.get("year"):
                    line += f", {p['year']}"
                line += ")"
            pubs_text.append(line)
            if p.get("mesh_terms"):
                pubs_text.append(f"  MeSH: {', '.join(p['mesh_terms'][:5])}")
        if pubs_text:
            parts.append("Previously fetched publications:\n" + "\n".join(pubs_text))

    has_awards_context = any("award" in p.lower() for p in parts[1:])
    if not has_awards_context and faculty_dict.get("awards"):
        award_text = []
        for a in faculty_dict["awards"][:10]:
            line = f"- {a.get('name', '') if isinstance(a, dict) else a}"
            if isinstance(a, dict) and a.get("year"):
                line += f" ({a['year']})"
            award_text.append(line)
        parts.append("Honors and awards:\n" + "\n".join(award_text))

    has_patents_context = any("patent" in p.lower() for p in parts[1:])
    if not has_patents_context and faculty_dict.get("patents"):
        pat_text = [f"- {p.get('title', '') if isinstance(p, dict) else p}"
                    for p in faculty_dict["patents"][:8]]
        parts.append("US patents:\n" + "\n".join(pat_text))

    if len(parts) <= 1:
        # Only have the name — not enough data to normalize
        return None

    return "\n\n".join(parts)


def normalize_from_context(name, context):
    """Run the LLM over a pre-built context string."""
    try:
        raw = _call_llm(NORMALIZE_SYSTEM_PROMPT, context, max_tokens=1000, temperature=0.1)
        return _parse_json_response(raw)
    except Exception:
        logger.exception("LLM normalization failed for %s", name)
        return None


def normalize_faculty_data(faculty_dict, raw_enrichment_data):
    """Use LLM to produce structured profile fields from raw enrichment data.

    Convenience wrapper over build_context + normalize_from_context (the
    pipeline calls them separately so it can fingerprint the context).
    """
    context = build_context(faculty_dict, raw_enrichment_data)
    if not context:
        return None
    name = f"{faculty_dict.get('first_name', '')} {faculty_dict.get('last_name', '')}"
    return normalize_from_context(name, context)
