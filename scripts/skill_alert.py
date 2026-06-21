#!/usr/bin/env python3
"""Skill-authority alert routing for Georgia Appellate Watch (scripts/skill_alert.py).

The alert-out half of the skill integration. update.py already runs a forward
tripwire: the triage model is handed a watch-list of cases and flags any the new
opinion treats adversely, then an Opus audit confirms. Today that watch-list is the
published feed cards. This module extends it to a SECOND list, the bedrock
authorities the qpwb skills rely on (the by_authority index of skill-authorities.json),
most of which are older controlling cases not in the feed at all. When a new opinion
is confirmed to treat one of those authorities adversely, this routes the finding
back to the skills that depend on it and records it, so a practitioner drafting
under one of those skills learns the authority moved.

A relied-on authority is not an opinions.json entry, so a finding here mutates no
card and rides no editorial PR. It is recorded in skill_alert_state.json (metadata)
and surfaced in the run summary; the eventual per-area drip-in slice reads the same
state.

Watch ids carry an "sa:" prefix so update.py's treats loop can tell an authority
hit (string id) from a feed-card hit (integer cluster_id) and route each correctly.

Pure standard library: imports neither update.py nor treatment_core, so no import
cycle. The Anthropic audit call stays in update.py (which owns the client); this
module only builds the watch-list, classifies ids, routes via the manifest, holds
state, and composes summary lines.
"""
import json, re, datetime

PREFIX = "sa:"
_CITER_FIELDS = ("cluster_id", "name", "court", "date", "kind", "note")


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def load_manifest(path):
    """Load skill-authorities.json; return {} on any failure. Fail-open: no manifest
    means the authority watch is simply inactive and the feed-card path is unaffected."""
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def watch_items(manifest):
    """The authority watch-list as [(watch_id, case_name)] over every distinct case in
    by_authority. Only case names are watched (the mechanism is name-based); statutes,
    rules, and bills are not watched here, their currency rides the drip-in."""
    out, seen = [], set()
    for auth in (manifest.get("by_authority") or {}):
        if " v. " not in auth and " v " not in auth:
            continue                                   # keep case names, drop statutes/rules/bills
        key = _slug(auth)
        if key in seen:
            continue
        seen.add(key)
        out.append((PREFIX + key, auth))
    return sorted(out, key=lambda t: t[1])


def feed_index_lines(items):
    """Format authority watch items as the same 'id: name' lines update.py builds for
    feed cards, to append to the triage watch-list."""
    return "\n".join("%s: %s" % (wid, name) for wid, name in items)


def is_authority_id(wid):
    """True if a triage `treats` id is an authority watch id rather than a feed-card
    cluster_id."""
    return isinstance(wid, str) and wid.startswith(PREFIX)


def authority_for_id(items, wid):
    """Resolve a watch id back to its case name, or None if unknown."""
    for w, name in items:
        if w == wid:
            return name
    return None


def relying_skills(manifest, name):
    """Skills that rely on authority `name`, via the by_authority index."""
    return list((manifest.get("by_authority") or {}).get(name) or [])


# -- state (skill_alert_state.json): per authority, the citing opinions already
#    recorded, so a finding is never double-counted across runs --------------------
def load_state(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def save_state(path, state):
    json.dump(state, open(path, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False, sort_keys=True)


def already_seen(state, authority_name, citer_cluster_id):
    """True if this citer was already recorded against this authority (dedup, so the
    audit need not even run again on a re-encounter)."""
    rec = state.get(_slug(authority_name))
    if not rec:
        return False
    return any(x.get("cluster_id") == citer_cluster_id for x in rec.get("treated_by", []))


def record(state, manifest, authority_name, citer):
    """Record that `citer` (cluster_id, name, court, date, kind, note) was confirmed to
    treat `authority_name` adversely. Dedup by citer cluster_id, like treatment_core.
    Returns (newly_recorded, relying_skills)."""
    skills = relying_skills(manifest, authority_name)
    rec = state.setdefault(_slug(authority_name),
                           {"authority": authority_name, "skills": skills, "treated_by": []})
    rec["skills"] = skills                             # refresh in case the manifest changed
    ccid = citer.get("cluster_id")
    if any(x.get("cluster_id") == ccid for x in rec["treated_by"]):
        return False, skills
    rec["treated_by"].append({k: citer.get(k) for k in _CITER_FIELDS})
    rec["last_date"] = datetime.date.today().isoformat()
    return True, skills


def digest_lines(events):
    """Run-summary lines for newly recorded authority treatments. `events` is a list of
    (authority_name, citer, skills)."""
    if not events:
        return []
    out = ["", "### Skill-authority watch", ""]
    for auth, citer, skills in events:
        sk = ", ".join(s.replace("qpwb-", "") for s in skills) or "(no skill mapping)"
        out.append(
            "- **%s** -- possibly %s by the new decision %s. Relied on by: %s. Confirm on Shepard's."
            % (auth, citer.get("kind") or "treated", citer.get("name") or "a later case", sk))
    return out
