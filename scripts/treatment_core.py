#!/usr/bin/env python3
"""Shared treatment-flag model for Georgia Appellate Watch.

Two paths detect when a later in-scope opinion treats a carded case adversely:
the daily forward escalation (update.py, where the Sonnet triage already reads the
full text and an Opus audit confirms) and the weekend reverse sweep (treatment.py,
which walks each card's citation graph). They record a finding the same way,
through this module, so a card carries one consistent shape no matter which path
flagged it. Pure standard library, so it imports neither caller and creates no
import cycle.

Card treatment fields (all optional; absence means "ok"):
  treatment            "ok" | "caution" | "negative" | "superseded"
                       The machine only ever sets "caution". "negative" and
                       "superseded" are HUMAN settings, applied on Shepard's
                       review (Gate 2) by editing opinions.json. The machine never
                       downgrades a human setting and never marks a card dead.
  treated_by           list of {cluster_id, name, court, date, kind, note}: the
                       later case(s) found to treat this card adversely.
  treatment_auto_note  machine-composed one-line summary (machine owns it).
  treatment_note       human note (optional); the renderer prefers it over the
                       auto note.
  treatment_date       ISO date the machine last recorded adverse treatment for
                       this card. The weekly digest signals a correction when this
                       falls inside its window. A human applying a correction by
                       hand can set it too, to have that correction signaled.
"""
import datetime

# Adverse kinds. "distinguished-narrowing" means the later court narrowed the
# rule, not merely distinguished the case on its facts (which is not adverse).
NEGATIVE_KINDS = {
    "overruled", "reversed", "abrogated", "superseded by statute",
    "limited", "disapproved", "criticized", "distinguished-narrowing",
}

_CITER_FIELDS = ("cluster_id", "name", "court", "date", "kind", "note")


def auto_note(card):
    """Compose the machine-owned one-line treatment summary from treated_by."""
    tb = card.get("treated_by") or []
    if not tb:
        return ""
    kinds = sorted({(x.get("kind") or "negative") for x in tb})
    names = "; ".join(x.get("name") or "a later case" for x in tb[:3])
    more = "" if len(tb) <= 3 else ", and others"
    return ("Possibly %s by a later in-scope decision (%s%s). Confirm on Shepard's."
            % (" / ".join(kinds), names, more))


def flag_caution(card, citer):
    """Record adverse treatment of `card` by `citer`.

    `citer` is a dict with cluster_id, name, court, date, kind, note. Appends it to
    treated_by (deduped by cluster_id), refreshes the auto note, and raises
    treatment to "caution" only if it is currently "ok". It never downgrades a
    human "negative", "superseded", or reviewed "ok": if this citer is already
    recorded it changes nothing, so a flag a human has cleared is not re-raised.

    Returns True only when this call newly raised the card from "ok" to "caution".
    """
    tb = card.setdefault("treated_by", [])
    ccid = citer.get("cluster_id")
    if any(x.get("cluster_id") == ccid for x in tb):
        return False                          # already recorded; respect any review
    tb.append({k: citer.get(k) for k in _CITER_FIELDS})
    card["treatment_auto_note"] = auto_note(card)
    card["treatment_date"] = datetime.date.today().isoformat()   # date of the latest finding; the weekly digest signals corrections by it
    if (card.get("treatment") or "ok") == "ok":
        card["treatment"] = "caution"
        return True
    return False
