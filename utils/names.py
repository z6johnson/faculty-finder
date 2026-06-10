"""Shared person-name utilities.

Used by the EAH reconcile (scripts/eah_enrichment.py) and the identity
resolution layer (enrichment/identity.py) so both match names the same way.
"""

import re


def normalize_name(s):
    """Remove non-alpha chars and lowercase for comparison."""
    return re.sub(r"[^a-z]", "", (s or "").lower())


def email_local(email):
    """Return the local part of an email address (before @)."""
    if not email or "@" not in email:
        return (email or "").lower().strip()
    return email.split("@")[0].lower().strip()


def parse_eah_name(name_str):
    """Parse 'Last, First Middle' into (first_name, last_name)."""
    name_str = (name_str or "").strip()
    if "," not in name_str:
        parts = name_str.split()
        return (parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else "")
    last, rest = name_str.split(",", 1)
    first_parts = rest.strip().split()
    first = first_parts[0] if first_parts else ""
    return first.strip(), last.strip()


def names_compatible(our_first, our_last, their_first, their_last):
    """Loose compatibility check between two already-normalized names.

    Last names must match (or one contains the other, for hyphenated names);
    first names must share a 3-char prefix (handles middle names, nicknames).
    """
    if not our_first or not our_last or not their_first or not their_last:
        return False
    if (our_last != their_last and our_last not in their_last
            and their_last not in our_last):
        return False
    if not (their_first.startswith(our_first[:3])
            or our_first.startswith(their_first[:3])):
        return False
    return True


def name_similarity(our_first, our_last, their_first, their_last):
    """Score how well two names match, in [0, 1].

    Inputs are raw display names. 1.0 = exact normalized match; partial
    credit for hyphenation/containment and first-name prefix matches.
    """
    of, ol = normalize_name(our_first), normalize_name(our_last)
    tf, tl = normalize_name(their_first), normalize_name(their_last)
    if not of or not ol or not tf or not tl:
        return 0.0

    if ol == tl:
        last_score = 1.0
    elif ol in tl or tl in ol:
        last_score = 0.8
    else:
        return 0.0

    if of == tf:
        first_score = 1.0
    elif tf.startswith(of) or of.startswith(tf):
        first_score = 0.85
    elif tf[:3] == of[:3]:
        first_score = 0.6
    elif tf[:1] == of[:1]:
        # Initial-only agreement — weak but non-zero (e.g. "J." vs "Jane")
        first_score = 0.3
    else:
        return 0.0

    return round(0.6 * last_score + 0.4 * first_score, 3)
