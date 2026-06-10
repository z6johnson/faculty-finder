"""Discipline-aware source routing.

Replaces the three hardcoded per-school registries that used to live in
enrichment/pipeline.py. Every division gets a CORE of discipline-agnostic
sources (OpenAlex backbone, ORCID, institutional profile scrape, email
inference) plus discipline-specific extras keyed by the division's bundle
(data/divisions.py maps division slug -> bundle).
"""

from data import divisions

from .sources.arxiv import ArxivSource
from .sources.clinical_trials import ClinicalTrialsSource
from .sources.crossref import CrossrefSource
from .sources.dblp import DBLPSource
from .sources.email_pattern import EmailPatternSource
from .sources.escholarship import EScholarshipSource
from .sources.nasa_ads import NASAADSSource
from .sources.nih_reporter import NIHReporterSource
from .sources.nsf_awards import NSFAwardSource
from .sources.openalex import OpenAlexSource
from .sources.orcid import ORCIDSource
from .sources.patents_view import PatentsViewSource
from .sources.pubmed import PubMedSource
from .sources.repec import RePEcSource
from .sources.scripps_profile import ScrippsProfileSource
from .sources.semantic_scholar import SemanticScholarSource
from .sources.ucsd_profile import UCSDProfileSource
from .sources.wikidata import WikidataSource

# Discipline-agnostic backbone, fetched for every division.
CORE = {
    "openalex": OpenAlexSource,
    "orcid": ORCIDSource,
    "ucsd_profile": UCSDProfileSource,
    "email_pattern": EmailPatternSource,
}

# Awards lookup is cheap and ORCID-gated, so every bundle gets it.
_UNIVERSAL_EXTRAS = {
    "wikidata": WikidataSource,
}

# Bundle key (data/divisions.py) -> division-specific extras.
BUNDLES = {
    # Legacy behavior preserved (+ openalex/wikidata from CORE/universal).
    "hwsph": {
        "pubmed": PubMedSource,
        "nih_reporter": NIHReporterSource,
        "semantic_scholar": SemanticScholarSource,
        "clinical_trials": ClinicalTrialsSource,
    },
    "sio": {
        "scripps_profile": ScrippsProfileSource,
        "nsf_awards": NSFAwardSource,
        "nih_reporter": NIHReporterSource,
        "pubmed": PubMedSource,
        "semantic_scholar": SemanticScholarSource,
    },
    "jacobs": {
        "nsf_awards": NSFAwardSource,
        "nih_reporter": NIHReporterSource,
        "pubmed": PubMedSource,
        "semantic_scholar": SemanticScholarSource,
        "dblp": DBLPSource,
        "arxiv": ArxivSource,
        "patents_view": PatentsViewSource,
    },
    # School of Medicine, Skaggs Pharmacy.
    "health": {
        "pubmed": PubMedSource,
        "nih_reporter": NIHReporterSource,
        "clinical_trials": ClinicalTrialsSource,
        "patents_view": PatentsViewSource,
    },
    "bio-sci": {
        "pubmed": PubMedSource,
        "nih_reporter": NIHReporterSource,
        "nsf_awards": NSFAwardSource,
        "patents_view": PatentsViewSource,
    },
    "phys-sci": {
        "nsf_awards": NSFAwardSource,
        "arxiv": ArxivSource,
        "nasa_ads": NASAADSSource,
        "patents_view": PatentsViewSource,
        "crossref": CrossrefSource,
    },
    "soc-sci": {
        "nsf_awards": NSFAwardSource,
        "nih_reporter": NIHReporterSource,
        "repec": RePEcSource,
        "crossref": CrossrefSource,
    },
    "arts-hum": {
        "crossref": CrossrefSource,
        "escholarship": EScholarshipSource,
    },
    # Rady, GPS.
    "econ": {
        "repec": RePEcSource,
        "nsf_awards": NSFAwardSource,
        "crossref": CrossrefSource,
    },
    "default": {
        "crossref": CrossrefSource,
        "nsf_awards": NSFAwardSource,
    },
}


def source_classes_for(department):
    """Source registry (name -> class) for a division slug."""
    bundle = BUNDLES.get(divisions.bundle_for(department), BUNDLES["default"])
    return {**CORE, **bundle, **_UNIVERSAL_EXTRAS}


def all_source_classes():
    """Every known source (for run.py source-name validation)."""
    merged = {**CORE, **_UNIVERSAL_EXTRAS}
    for bundle in BUNDLES.values():
        merged.update(bundle)
    return merged
