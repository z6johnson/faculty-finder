"""Pure decision rules for conservative identity auto-acceptance.

Shared by IdentityResolver.resolve() (at resolution time) and
resweep_pending() (over already-queued identity_candidates rows) so each
rule exists exactly once. Everything here is network-free. Evidence dicts
come from _score_openalex_author or stored identity_candidates.evidence
JSON; older rows may lack newer keys, and missing data always means
"not eligible", never an error.

Wrong matches are costly downstream (publications/grants attributed to the
wrong person), so every rule errs toward leaving items in the manual queue.
"""

from utils.names import name_similarity, normalize_name

# Kill switch for the name-based branch of is_same_person. The
# ORCID-equality branch is airtight; this one carries a small residual risk
# of collapsing two ORCID-less UCSD people whose names differ only by middle
# name/initial and whose topic domains don't conflict.
NAME_TIE_COLLAPSE = True

# ORCID corroboration: the ORCID record's own name must match the faculty
# at least this well.
MIN_RECORD_NAME_SIMILARITY = 0.9


def evidence_orcid(candidate):
    """Bare ORCID iD from a candidate's evidence, or None."""
    evidence = candidate.get("evidence") or {}
    orcid = (evidence.get("orcid") or "").strip()
    return orcid.rsplit("/", 1)[-1] if orcid else None


def _primary_display_name(candidate):
    evidence = candidate.get("evidence") or {}
    return candidate.get("display_name") or evidence.get("display_name") or ""


def _ucsd_affiliated(candidate):
    evidence = candidate.get("evidence") or {}
    return bool(evidence.get("ucsd_current") or evidence.get("ucsd_listed"))


def exact_primary_name_match(faculty, candidate):
    """True when the candidate's *primary* display name exactly matches the
    faculty name after normalization.

    evidence.name_similarity is the max over display_name_alternatives too,
    so a merged/contaminated OpenAlex profile can carry a 1.0 from an alias
    while the profile is really someone else — aliases never qualify here.
    """
    parts = _primary_display_name(candidate).split()
    if len(parts) < 2:
        return False
    return name_similarity(faculty.get("first_name", ""),
                           faculty.get("last_name", ""),
                           parts[0], parts[-1]) == 1.0


def _compatible_display_names(a, b):
    """True when two primary display names refer to the same name modulo
    middle names/initials — OpenAlex's usual split pattern ("Wael K.
    Al-Delaimy" vs "Wael Al-Delaimy"). First and last tokens must match
    exactly; middle parts may differ only by absence or abbreviation
    ("K." vs "" vs "Kareem"). Conflicting middles ("K." vs "J.") mean
    distinct people."""
    a_parts = a.split()
    b_parts = b.split()
    if len(a_parts) < 2 or len(b_parts) < 2:
        return False
    if normalize_name(a_parts[0]) != normalize_name(b_parts[0]):
        return False
    if normalize_name(a_parts[-1]) != normalize_name(b_parts[-1]):
        return False
    a_mid = normalize_name("".join(a_parts[1:-1]))
    b_mid = normalize_name("".join(b_parts[1:-1]))
    return (not a_mid or not b_mid
            or a_mid.startswith(b_mid) or b_mid.startswith(a_mid))


def is_same_person(best, rival):
    """Conservatively decide whether two OpenAlex candidates are duplicate
    profiles of one person (OpenAlex splits authors often)."""
    best_orcid = evidence_orcid(best)
    rival_orcid = evidence_orcid(rival)
    if best_orcid and rival_orcid:
        # Equal ORCIDs prove same person; different ORCIDs prove distinct
        # people regardless of how similar the names look.
        return best_orcid == rival_orcid
    if not NAME_TIE_COLLAPSE:
        return False
    if not _compatible_display_names(_primary_display_name(best),
                                     _primary_display_name(rival)):
        return False
    if not (_ucsd_affiliated(best) and _ucsd_affiliated(rival)):
        return False
    best_domains = {d.lower() for d in
                    (best.get("evidence") or {}).get("topic_domains") or []}
    rival_domains = {d.lower() for d in
                     (rival.get("evidence") or {}).get("topic_domains") or []}
    # Disjoint topic domains argue for two different people; missing topic
    # data (typical for tiny stub profiles) doesn't block the collapse.
    if best_domains and rival_domains and not (best_domains & rival_domains):
        return False
    return True


def collapse_duplicate_ties(candidates, tie_margin):
    """Collapse rivals within tie_margin of the best candidate that are
    duplicate profiles of the same person into one cluster.

    candidates: scored dicts sorted descending by score. Only
    openalex-source rivals participate; others stay ordinary rivals.

    Returns {canonical, effective_score, runner_up, cluster_ids, collapsed}
    where runner_up is the best score *outside* the cluster — the value the
    caller's ambiguity test should use.
    """
    if not candidates:
        return {"canonical": None, "effective_score": 0.0, "runner_up": 0.0,
                "cluster_ids": [], "collapsed": False}
    best = candidates[0]
    cluster = [best]
    rest = []
    for rival in candidates[1:]:
        # Pairwise against every member: middle-initial compatibility is not
        # transitive ("Jane K. Smith" and "Jane R. Smith" both match plain
        # "Jane Smith" but conflict with each other), and two ORCID-carrying
        # rivals must not both join through an ORCID-less best.
        if (best.get("source") == "openalex"
                and rival.get("source") == "openalex"
                and best["score"] - rival["score"] <= tie_margin
                and all(is_same_person(member, rival) for member in cluster)):
            cluster.append(rival)
        else:
            rest.append(rival)

    def _rank(c):
        evidence = c.get("evidence") or {}
        return (evidence.get("works_count") or 0,
                evidence.get("cited_by_count") or 0,
                c.get("external_id") or "")

    return {
        "canonical": max(cluster, key=_rank),
        "effective_score": best["score"],
        "runner_up": max((c["score"] for c in rest), default=0.0),
        "cluster_ids": [c.get("external_id") for c in cluster],
        "collapsed": len(cluster) > 1,
    }


def orcid_corroboration_precheck(faculty, candidate):
    """Offline eligibility for ORCID employment corroboration: an exact-name
    UCSD-affiliated OpenAlex candidate that carries an ORCID. Network
    verification (ORCIDSource.verify_ucsd_employment) still required."""
    if candidate.get("source") != "openalex":
        return False
    evidence = candidate.get("evidence") or {}
    if evidence.get("name_similarity") != 1.0:
        return False
    if not exact_primary_name_match(faculty, candidate):
        return False
    if not _ucsd_affiliated(candidate):
        return False
    return evidence_orcid(candidate) is not None


def orcid_corroboration_confirms(verification):
    """Verdict on ORCIDSource.verify_ucsd_employment output (None = fetch
    failure = fail closed)."""
    if not verification:
        return False
    return (bool(verification.get("employment_verified"))
            and (verification.get("record_name_similarity") or 0.0)
            >= MIN_RECORD_NAME_SIMILARITY)


def orcid_fallback_qualifies(evidence):
    """An orcid-source candidate auto-accepts only when its record exposed
    the faculty's exact email, or the affiliation search hit was unique AND
    employment-verified AND the record's own name matched exactly."""
    if not evidence:
        return False
    if evidence.get("record_email_match"):
        return True
    return (bool(evidence.get("unique_hit"))
            and bool(evidence.get("employment_verified"))
            and evidence.get("record_name_similarity") == 1.0)
