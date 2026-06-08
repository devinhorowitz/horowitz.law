#!/usr/bin/env python3
"""Jurisdiction registry for the appellate-watch pipeline.

One place that defines, per jurisdiction, the courts to monitor (their
CourtListener ids, the internal/display keys, the human labels, and the short
citation suffixes), plus the docket and citation patterns and the jurisdiction
name. Everything jurisdiction-specific downstream is DERIVED here for the active
jurisdiction:

  update.py     COURTS, COURTS_ALL, COURT_MAP, VALID_KEYS, CITE_RE, DOCKET_RE
  render.py     COURT_LABELS, TITLE_SUFFIX
  treatment.py  SCOPE_COURTS

so adding a state is one entry in JURISDICTIONS below, not edits scattered across
the pipeline. The three model prompts in update.py are still written for Georgia
and are NOT templated here; parameterizing them (and wiring per-jurisdiction
output files and schedules) is a later phase, validated against a second-state
test. Georgia is the only active jurisdiction today.

To add a jurisdiction: add an entry whose courts cover the state high court, the
state intermediate appellate court, the federal circuit that covers the state
(Tennessee is the Sixth, Georgia is the Eleventh), and the U.S. Supreme Court,
with that jurisdiction's docket and reporter patterns. Selecting it is a separate
step; see OPINIONS_JURISDICTION.

Env:
  OPINIONS_JURISDICTION   active jurisdiction key (default "ga")
  OPINIONS_COURTS         override the CourtListener court ids the active feed
                          iterates (comma-separated; default = its full set).
                          Does not change COURTS_ALL, COURT_MAP, or labels.
"""
import os
import re

# --- the registry ------------------------------------------------------------
# label:  jurisdiction name (used in prompts in a later phase)
# courts: feed order. cl = CourtListener court id; key = internal/display key;
#         label = human name; suffix = short citation suffix for titles/RSS;
#         system = "state" or "federal", the federal-vs-state grouping the site
#         filters on (a state court is always state; ca11/scotus always federal).
# docket_re / cite_re: jurisdiction-flavored patterns (state courts + the
#         relevant federal circuit and the U.S. Supreme Court).
JURISDICTIONS = {
    "ga": {
        "label": "Georgia",
        "courts": [
            {"cl": "ga",      "key": "scotga", "label": "Supreme Court of Georgia",                       "suffix": " (Ga.)",          "system": "state"},
            {"cl": "gactapp", "key": "ctapp",  "label": "Court of Appeals of Georgia",                    "suffix": " (Ga. Ct. App.)", "system": "state"},
            {"cl": "ca11",    "key": "ca11",   "label": "U.S. Court of Appeals for the Eleventh Circuit", "suffix": " (11th Cir.)",     "system": "federal"},
            {"cl": "scotus",  "key": "scotus", "label": "Supreme Court of the United States",             "suffix": " (U.S.)",         "system": "federal"},
        ],
        # Ga. Court of Appeals (A24A1234) and Supreme Court of Georgia (S24A1234 /
        # S24G1234) share letter+yy+letter+4; federal dockets are yy-NNNNN (4-5
        # digits, kept tight to avoid matching statute cites like 51-12).
        "docket_re": r"\b(?:[AS]\d{2}[A-Z]\d{4}|\d{2}-\d{4,5})\b",
        # Ga. reporters (Ga., Ga. App., S.E.2d) + federal (U.S., S. Ct., L. Ed.,
        # F./F.2d/3d/4th, F. Supp., Westlaw).
        "cite_re": r"\b\d+\s+(?:Ga\.?\s*App\.?|Ga\.?|S\.?\s*E\.?\s*2d|S\.?\s*E\.?|U\.?\s*S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?\s*(?:2d)?|F\.?(?:2d|3d|4th)?|F\.?\s*Supp\.?|WL)\s+\d+",
    },
}

DEFAULT_JURISDICTION = "ga"


def active_key():
    k = (os.environ.get("OPINIONS_JURISDICTION") or DEFAULT_JURISDICTION).strip().lower()
    if k not in JURISDICTIONS:
        raise ValueError(
            "unknown OPINIONS_JURISDICTION %r; known: %s"
            % (k, ", ".join(sorted(JURISDICTIONS)))
        )
    return k


# --- derived constants for the ACTIVE jurisdiction ---------------------------
ACTIVE = active_key()
_cfg = JURISDICTIONS[ACTIVE]
_courts = _cfg["courts"]

LABEL = _cfg["label"]                                   # e.g. "Georgia"
COURTS_ALL = [c["cl"] for c in _courts]                 # full CourtListener id set
# Feed/iteration list; OPINIONS_COURTS narrows it (escape hatch) without touching
# COURT_MAP, COURTS_ALL, or the labels.
COURTS = [c.strip() for c in os.environ.get("OPINIONS_COURTS", ",".join(COURTS_ALL)).split(",") if c.strip()]
COURT_MAP = {c["cl"]: c["key"] for c in _courts}        # CourtListener id -> internal key
COURT_LABELS = {c["key"]: c["label"] for c in _courts}  # internal key -> human label
COURT_SYSTEM = {c["key"]: c["system"] for c in _courts}  # internal key -> "state" | "federal"
TITLE_SUFFIX = {c["key"]: c["suffix"] for c in _courts}  # internal key -> citation suffix
VALID_KEYS = tuple(c["key"] for c in _courts)           # internal keys (fallback validation)
JURISDICTION = ACTIVE                                    # active jurisdiction key, e.g. "ga"
DOCKET_RE = re.compile(_cfg["docket_re"])
CITE_RE = re.compile(_cfg["cite_re"], re.I)
