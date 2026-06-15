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
        # F./F.2d/3d/4th, F. Supp., Westlaw). A bare "F" requires its period so a
        # stray "F 150" in a synopsis is not flagged as a reporter citation;
        # "F2d"/"F3d"/"F4th" without the period still match.
        "cite_re": r"\b\d+\s+(?:Ga\.?\s*App\.?|Ga\.?|S\.?\s*E\.?\s*2d|S\.?\s*E\.?|U\.?\s*S\.?|S\.?\s*Ct\.?|L\.?\s*Ed\.?\s*(?:2d)?|F\.(?:2d|3d|4th)?|F(?:2d|3d|4th)|F\.?\s*Supp\.?|WL)\s+\d+",
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
JURISDICTION = ACTIVE                                    # active jurisdiction key, e.g. "ga"
# Federal-overlay jurisdictions: states this watch does not cover in full,
# registered AFTER active_key() so they can be overlaid and (partially) monitored
# but never selected as the active jurisdiction. A federal court's decisions bind
# them by judicial hierarchy (the Eleventh Circuit sits over Georgia, Florida, and
# Alabama; the Supreme Court over everything). They appear in the site's
# jurisdiction filter with a "\u00b7 federal" label and never on the subscribe
# form: filters show what exists, subscriptions promise curation, and the screen
# curates for Georgia relevance only until a state is covered in full (mode: "full").
#
# Florida is monitored at the high-court level: the Supreme Court of Florida is in
# the feed and screened for Georgia relevance like any other court, so most
# Florida-law decisions are dropped at triage and surface only in the rejection
# log, while the District Courts of Appeal -- where most Florida appellate law
# lives, and which CourtListener does not currently ingest -- await a separate
# source. Its cards carry jurisdiction "fl" (see COURT_JURISDICTION) so they file
# under the Florida filter, not the active-jurisdiction fallback. Alabama stays
# overlay-only for now (both its courts are in CourtListener when we turn them on).
JURISDICTIONS["fl"] = {
    "label": "Florida", "mode": "overlay", "filter_note": "supreme court",
    "courts": [
        {"cl": "fla", "key": "scotfl", "label": "Supreme Court of Florida", "suffix": " (Fla.)", "system": "state"},
    ],
}
JURISDICTIONS["al"] = {"label": "Alabama", "mode": "overlay", "courts": []}

# Court tables, derived from the active jurisdiction's courts plus any courts of
# other registered jurisdictions monitored at less than full coverage (today just
# the Supreme Court of Florida). Each monitored extra carries its owning
# jurisdiction key ("jx") so a state card stamps under the right state rather than
# the active-jurisdiction fallback. Active courts come first; OPINIONS_COURTS still
# narrows the feed list (escape hatch) without touching the maps.
_extra_courts = []
for _jk, _jcfg in JURISDICTIONS.items():
    if _jk == ACTIVE:
        continue
    for _c in (_jcfg.get("courts") or []):
        if isinstance(_c, dict):
            _extra_courts.append({**_c, "jx": _jk})
_all_courts = list(_courts) + _extra_courts

COURTS_ALL = [c["cl"] for c in _all_courts]                  # full CourtListener id set (monitored)
COURTS = [c.strip() for c in os.environ.get("OPINIONS_COURTS", ",".join(COURTS_ALL)).split(",") if c.strip()]
COURT_MAP = {c["cl"]: c["key"] for c in _all_courts}         # CourtListener id -> internal key
COURT_LABELS = {c["key"]: c["label"] for c in _all_courts}   # internal key -> human label
COURT_SYSTEM = {c["key"]: c["system"] for c in _all_courts}  # internal key -> "state" | "federal"
TITLE_SUFFIX = {c["key"]: c["suffix"] for c in _all_courts}  # internal key -> citation suffix
VALID_KEYS = tuple(c["key"] for c in _all_courts)            # internal keys (fallback validation)
COURT_JURISDICTION = {c["key"]: c.get("jx", ACTIVE) for c in _all_courts}  # internal key -> owning jurisdiction

def jurisdiction_mode(key):
    return JURISDICTIONS.get(key, {}).get("mode", "full")

def jurisdiction_filter_note(key):
    """Optional jurisdiction-filter label note: a partially covered state can name
    its coverage (Florida shows "supreme court", its only screened state court).
    When absent the renderer falls back to the mode-based default ("\u00b7 federal"
    for overlays, nothing for a fully covered state)."""
    return JURISDICTIONS.get(key, {}).get("filter_note")

# Which registered jurisdictions a federal court's published decisions bind, by
# pure judicial hierarchy. "*" means every registered jurisdiction, present and
# future, so a new state in the registry inherits the SCOTUS overlay with no
# data change anywhere. Derived at render time -- never stored on cards -- so
# bindingness can never go stale.
FEDERAL_BINDS = {"ca11": ("ga", "fl", "al"), "scotus": "*"}

def court_binds(court):
    """The registered jurisdiction keys a decision from this court binds, or
    None for a state court (whose card carries its own jurisdiction)."""
    b = FEDERAL_BINDS.get(court)
    if b is None:
        return None
    keys = list(JURISDICTIONS)
    return keys if b == "*" else [k for k in b if k in JURISDICTIONS]

ALL_JURISDICTIONS = [(k, v["label"]) for k, v in JURISDICTIONS.items()]  # (key, label) per registered jurisdiction, for the page's jurisdiction selector
DOCKET_RE = re.compile(_cfg["docket_re"])
CITE_RE = re.compile(_cfg["cite_re"], re.I)
