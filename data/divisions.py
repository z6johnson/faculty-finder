"""Division registry: maps EAH "Division / School" values to division slugs.

The ``department`` column in the faculty table holds these slugs. The three
legacy schools (hwsph/sio/jacobs) keep their original slugs so existing rows,
URLs, and scheduler slots continue to work; every other UCSD division gets a
slug here. Unknown EAH values fall back to a slugified form of the raw string
so seeding never drops anyone on the floor.

Each entry also names the enrichment source bundle for the division (resolved
in enrichment/routing.py).
"""

import re


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


class Division:
    def __init__(self, slug, label, matcher, bundle="default", active=True):
        self.slug = slug
        self.label = label
        self.matcher = matcher          # callable(division_school_str) -> bool
        self.bundle = bundle            # key into enrichment/routing.py BUNDLES
        # Inactive divisions stay in the registry (so existing rows keep their
        # label/bundle and stay manageable in admin) but are dropped from
        # seeding, enrichment, and the public UI. See excluded_slugs().
        self.active = active


def _eq(expected):
    expected = expected.lower()
    return lambda v: (v or "").strip().lower() == expected


def _contains(*subs):
    subs = tuple(s.lower() for s in subs)
    return lambda v: any(s in (v or "").lower() for s in subs)


# Order matters: first matching entry wins.
DIVISIONS = [
    # Legacy three — slugs and EAH filters must stay aligned with the original
    # SCHOOL_CONFIG in scripts/eah_enrichment.py.
    Division("hwsph", "Herbert Wertheim School of Public Health",
             _eq("School of Public Health"), bundle="hwsph"),
    Division("jacobs", "Jacobs School of Engineering",
             _eq("Jacobs School of Engineering"), bundle="jacobs"),
    Division("sio", "Scripps Institution of Oceanography",
             lambda v: "SIO" in (v or "") or (v or "").strip() == "VC-SIO Other",
             bundle="sio"),

    # School of Medicine is kept in the DB but excluded from seeding,
    # enrichment, and the public UI (active=False).
    Division("som", "School of Medicine",
             _contains("school of medicine"), bundle="health", active=False),
    Division("skaggs", "Skaggs School of Pharmacy and Pharmaceutical Sciences",
             _contains("pharmacy"), bundle="health"),
    Division("bio-sci", "Division of Biological Sciences",
             _contains("biological sciences"), bundle="bio-sci"),
    Division("phys-sci", "Division of Physical Sciences",
             _contains("physical sciences"), bundle="phys-sci"),
    Division("arts-hum", "Division of Arts and Humanities",
             _contains("arts and humanities", "arts & humanities"),
             bundle="arts-hum"),
    Division("soc-sci", "Division of Social Sciences",
             _contains("social sciences"), bundle="soc-sci"),
    Division("rady", "Rady School of Management",
             _contains("rady"), bundle="econ"),
    Division("gps", "School of Global Policy and Strategy",
             _contains("global policy"), bundle="econ"),
]

_BY_SLUG = {d.slug: d for d in DIVISIONS}


def division_for(division_school):
    """Resolve an EAH 'Division / School' string to (slug, label, bundle).

    Unknown values get a slugified fallback so every EAH row is representable.
    """
    for d in DIVISIONS:
        if d.matcher(division_school):
            return d.slug, d.label, d.bundle
    raw = (division_school or "").strip()
    if not raw:
        return "other", "Other / Unassigned", "default"
    return _slug(raw), raw, "default"


def label_for(slug):
    """Human label for a division slug (falls back to the slug itself)."""
    d = _BY_SLUG.get(slug)
    return d.label if d else slug


def bundle_for(slug):
    """Source-bundle key for a division slug."""
    d = _BY_SLUG.get(slug)
    return d.bundle if d else "default"


def known_slugs():
    return [d.slug for d in DIVISIONS]


def active_divisions():
    """Divisions shown in UI dropdowns and operated on by seeding/enrichment."""
    return [d for d in DIVISIONS if d.active]


def excluded_slugs():
    """Slugs of divisions kept in the DB but excluded from seeding, enrichment,
    and the public UI (e.g. 'som'). Used by data.db's _active_division_filter
    and the EAH reconcile."""
    return [d.slug for d in DIVISIONS if not d.active]
