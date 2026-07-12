#!/usr/bin/env python3
"""Staging store for the two-lane review workflow (scripts/review_store.py).

The daily funnel splits its output into two lanes:

  * AUTO   -- additive, unflagged cards that touch no existing card. Written to
              opinions.json and rendered, then auto-merged (CI-gated) with no human.
  * REVIEW -- anything that overrules or modifies an already-published card, a new
              card that CAUSES such a change (held together with it), or a new card a
              guard flagged (fidelity / completeness / low confidence). NOT written to
              opinions.json. Instead each held item is staged here as one small JSON
              file, and a bundled review PR carries them for a person to accept (merge)
              or veto (comment `/veto <id>` on the PR, or delete the file), one case at
              a time, with no effect on the others.

This module owns that staging format and the two ledgers. Pure standard library plus
safeio (a safe leaf), no network, so update.py (writer), review_apply.py (reader on
merge), and the tests all share one source of truth for where a held item lives and
what it looks like. Every path resolves the module default at call time, so a test can
point the store at a scratch directory by reassigning the module constants.

Layout, all under the repo root:

  review/cards/<cluster_id>.json                 one held new card + why it is held
  review/treatments/<cardcid>__<citercid>.json   one adverse-treatment change to an
                                                 existing card (staged as data, applied
                                                 to the live card only when the PR merges)
  opinions_pending_review.json                   cluster ids currently staged, so the
                                                 funnel does not re-summarize a case whose
                                                 review PR is still open
  opinions_redraft.jsonl                         append-only log of vetoed cases; a veto
                                                 leaves the case UN-seen, so a later run
                                                 rediscovers and redrafts it

The staging files live on the review PR's branch until it merges; the two ledgers live
on main (bookkeeping), so the funnel reads them on its next run.
"""
import json
import os
import re
import shutil

import safeio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_DIR = os.path.join(REPO, "review")
CARDS_DIR = os.path.join(REVIEW_DIR, "cards")
TREAT_DIR = os.path.join(REVIEW_DIR, "treatments")
PENDING_PATH = os.path.join(REPO, "opinions_pending_review.json")
REDRAFT_PATH = os.path.join(REPO, "opinions_redraft.jsonl")
# Per-case veto/decline decisions ride the review branch as review/vetoed.json and
# review/declined.json (see the marker helpers below).


def hold_reasons(entry, flagged_map, crosschecks, completeness, overruling_cids):
    """Why this new card must be HELD for review instead of auto-published, as a list of
    human-readable reasons. Empty list => it is clean and additive, so it auto-publishes.

    A card is held when it: carries a review flag the funnel already raised (low confidence,
    a reporter-style citation, an empty field); trips the fidelity or completeness guard
    (a `flag`, or `unavailable` -- we could not verify it, so it is not auto-published);
    or itself overrules/modifies an already-published card (its cluster is in
    `overruling_cids`, so it is reviewed together with that change). Pure dict logic, shared
    by update.py's routing and the tests."""
    cid = int(entry["cluster_id"])
    reasons = []
    fr = (flagged_map or {}).get(entry.get("name"))
    if fr:
        reasons += list(fr)
    cc = (crosschecks or {}).get(cid)
    if cc and cc.get("verdict") == "flag":
        reasons.append("fidelity flag: " + (cc.get("reason") or "the summary may misstate the holding"))
    elif cc and cc.get("verdict") == "unavailable":
        reasons.append("fidelity guard unavailable; not auto-published without verification")
    cp = (completeness or {}).get(cid)
    if cp and cp.get("verdict") == "flag":
        reasons.append("completeness flag: " + (cp.get("reason") or "the card may omit a material holding"))
    elif cp and cp.get("verdict") == "unavailable":
        reasons.append("completeness guard unavailable; not auto-published without verification")
    if cid in (overruling_cids or set()):
        reasons.append("overrules or modifies an already-published card")
    return reasons


def card_path(cluster_id, root=None):
    return os.path.join(root or REVIEW_DIR, "cards", "%d.json" % int(cluster_id))


def treatment_path(card_cid, citer_cid, root=None):
    return os.path.join(root or REVIEW_DIR, "treatments", "%d__%d.json" % (int(card_cid), int(citer_cid)))


def stage_card(entry, reasons, root=None):
    """Stage one held new card. `entry` is the assembled card object exactly as it would
    land in opinions.json; `reasons` is the list of human-readable reasons it is held
    (a guard flag, low confidence, or that it overrules an existing card). Returns the path.
    (Named `reasons`, not `hold_reasons`, so it does not shadow the module hold_reasons().)"""
    cid = int(entry["cluster_id"])
    obj = {"kind": "card", "cluster_id": cid, "name": entry.get("name", ""),
           "hold_reasons": list(reasons or []), "entry": entry}
    path = card_path(cid, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safeio.atomic_write_json(path, obj)
    return path


def stage_treatment(card_cid, citer, hold_reason, root=None):
    """Stage one adverse-treatment change to an existing card. `card_cid` is the cluster id
    of the already-published card being treated; `citer` is the treatment record (the later
    opinion, its kind and note) as treatment_core.flag_caution consumes it. Applied to the
    live card only when the review PR merges. Returns the path."""
    ccid = int(card_cid)
    xcid = int(citer.get("cluster_id"))
    obj = {"kind": "treatment", "card_cluster_id": ccid, "citer": citer,
           "hold_reason": hold_reason}
    path = treatment_path(ccid, xcid, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safeio.atomic_write_json(path, obj)
    return path


def read_staged(root=None):
    """Read every staged item under `root`. Returns (cards, treatments): cards is a list of
    the card staging objects, treatments a list of the treatment staging objects. Files that
    are absent yield empty lists; an unreadable/undecodable file is skipped, not fatal, so one
    bad line never sinks a merge."""
    root = root or REVIEW_DIR
    cards, treatments = [], []
    for sub, out in ((os.path.join(root, "cards"), cards),
                     (os.path.join(root, "treatments"), treatments)):
        if not os.path.isdir(sub):
            continue
        for fn in sorted(os.listdir(sub)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(sub, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return cards, treatments


def staged_cluster_ids(root=None):
    """The candidate cluster ids currently staged: every held card's cluster and every
    treatment's citing cluster. This is what the funnel skips so it does not re-summarize a
    case whose review PR is still open."""
    cards, treatments = read_staged(root)
    ids = {int(c["cluster_id"]) for c in cards if c.get("cluster_id") is not None}
    for t in treatments:
        cid = (t.get("citer") or {}).get("cluster_id")
        if cid is not None:
            ids.add(int(cid))
    return ids


def load_pending(path=None):
    """The set of cluster ids the funnel should skip because they are awaiting review.
    Missing or unreadable ledger = empty set (fail-open: at worst a case is re-evaluated)."""
    path = path or PENDING_PATH
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return {int(c) for c in obj.get("clusters", [])}
    except (OSError, ValueError, TypeError):
        return set()


def save_pending(clusters, path=None, stamp=None):
    """Write the pending-review ledger (sorted, deduped). `stamp` is an ISO timestamp for the
    record; the caller passes one so this module needs no clock."""
    obj = {"updated": stamp or "", "clusters": sorted({int(c) for c in clusters})}
    path = path or PENDING_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safeio.atomic_write_json(path, obj)


def log_redraft(records, path=None):
    """Append vetoed cases to the redraft log, one JSON object per line. A veto records the
    case here and, by leaving it out of seen_clusters, keeps it eligible for a later run to
    rediscover and redraft. Best-effort: a write failure is the caller's to log, never fatal."""
    if not records:
        return
    path = path or REDRAFT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_redraft_ids(path=None):
    """The set of cluster ids ever logged for redraft (append-only ledger, one JSON object per
    line). Callers subtract the resolved state (seen / already-carded / pending) to get the cases
    still awaiting a redraft, so this stays useful even though the ledger itself never shrinks.
    Tolerant of blank or malformed lines; missing/unreadable ledger = empty set (fail-open)."""
    path = path or REDRAFT_PATH
    ids = set()
    if not os.path.exists(path):
        return ids
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line).get("cluster_id")
                except (ValueError, TypeError):
                    continue
                if c is not None:
                    try:
                        ids.add(int(c))
                    except (ValueError, TypeError):
                        continue
    except OSError:
        return ids
    return ids


def _marker_path(name, root=None):
    return os.path.join(root or REVIEW_DIR, name)


def _read_marker(name, root=None):
    path = _marker_path(name, root)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return {int(c) for c in json.load(f).get("clusters", [])}
    except (OSError, ValueError, TypeError):
        return set()


def _add_marker(name, cluster_id, root=None):
    path = _marker_path(name, root)
    ids = _read_marker(name, root) | {int(cluster_id)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safeio.atomic_write_json(path, {"clusters": sorted(ids)})
    return ids


# Two per-case decisions a human records on the review PR, each a small {"clusters": [...]} file
# that rides the review branch, merges with the PR, and is read ONCE by review_apply:
#   declined.json -- /decline: drop the case AND mark it seen, so the funnel never redrafts it.
#   vetoed.json   -- /veto: drop the case, leave it un-seen, redraft it later. The marker also lets
#                    review_apply REFUSE to publish a vetoed case even if its staged file was
#                    restored to the branch by a racing scan -- the apply-side backstop to the
#                    branch-level --force-with-lease, so the guarantee does not live only in shell.
# Missing/unreadable = empty set (fail-open: at worst a dropped case is re-evaluated).
def read_declined(root=None):
    return _read_marker("declined.json", root)


def add_declined(cluster_id, root=None):
    return _add_marker("declined.json", cluster_id, root)


def read_vetoed(root=None):
    return _read_marker("vetoed.json", root)


def add_vetoed(cluster_id, root=None):
    return _add_marker("vetoed.json", cluster_id, root)


# --- review-veto workflow helpers: the command parse and staged-file lookup the /veto /decline
# handler used to do in grep/sed/glob. Kept here (tested) so the workflow is thin git plumbing and
# the marker write goes through add_vetoed/add_declined above rather than a parallel jq re-write. ---
_CMD_RE = re.compile(r"/(veto|decline)\b\s+(\d+)")


def parse_command(body):
    """Parse a review-PR comment for a `/veto <id>` or `/decline <id>` command. Returns
    (cmd, cluster_id): cmd is 'veto' or 'decline', cluster_id an int; (cmd, None) if the verb is
    present without a valid id; (None, None) if there is no command. First occurrence wins (matching
    the old `head -1`), and the word boundary after the verb rejects look-alikes like '/vetoed 5'."""
    if not isinstance(body, str):
        return (None, None)
    m = _CMD_RE.search(body)
    if m:
        return (m.group(1), int(m.group(2)))
    m2 = re.search(r"/(veto|decline)\b", body)   # verb but no id -> report it so the handler can hint
    return (m2.group(1), None) if m2 else (None, None)


def record_decision(cmd, cluster_id, root=None):
    """Record a parsed /veto or /decline on the review branch's marker -- the single write path
    review_apply reads. Returns the updated id set; unknown cmd raises so a typo fails loudly rather
    than silently recording nothing."""
    if cmd == "veto":
        return add_vetoed(cluster_id, root)
    if cmd == "decline":
        return add_declined(cluster_id, root)
    raise ValueError("unknown review command %r (expected 'veto' or 'decline')" % (cmd,))


def staged_files_for(cluster_id, root=None):
    """The staged files on the review branch for a cluster id: its held-card file, and any treatment
    file where it is the CITING case (named <cardcid>__<citercid>.json). Returns only existing paths,
    so the caller uses one call for both 'is it staged?' (non-empty) and the exact set to `git rm`."""
    root = root or REVIEW_DIR
    cid = int(cluster_id)
    out = []
    cp = card_path(cid, root)
    if os.path.exists(cp):
        out.append(cp)
    tdir = os.path.join(root, "treatments")
    if os.path.isdir(tdir):
        out += [os.path.join(tdir, f) for f in sorted(os.listdir(tdir))
                if f.endswith("__%d.json" % cid)]
    return out


def clear_markers(root=None):
    """Remove both decision markers; they are consumed once, when the batch is applied."""
    for name in ("vetoed.json", "declined.json"):
        try:
            os.remove(_marker_path(name, root))
        except OSError:
            pass


def _staged_cid(sub, filename):
    """The cluster id a staged filename identifies: a card file is <cid>.json; a treatment file is
    <cardcid>__<citercid>.json, keyed on the CITING cluster (the case that was staged, and the id a
    human vetoes/declines). Returns None for an unparseable name."""
    base = filename[:-5] if filename.endswith(".json") else filename
    if sub == "treatments" and "__" in base:
        base = base.split("__", 1)[1]
    try:
        return int(base)
    except ValueError:
        return None


def merge_new_into_branch(new_root, branch_root):
    """Reconcile this run's freshly staged cases into the review branch, deterministically and
    testably (the funnel's git fetch/checkout/push stay in the workflow; only this file-level
    union moves here). `branch_root` is the rebuilt branch's review/ tree -- it already holds the
    prior batch plus the veto/decline markers; `new_root` is this run's freshly staged review/
    tree. Every new card/treatment file is copied into `branch_root`, EXCEPT a case a human already
    vetoed or declined on the branch: skipping it means a re-discovery cannot resurrect a dropped
    case into the PR (a belt to the review_apply backstop's suspenders). Existing files and the
    markers are left intact. Returns (added, skipped) as lists of "sub/filename"."""
    blocked = read_vetoed(branch_root) | read_declined(branch_root)
    added, skipped = [], []
    for sub in ("cards", "treatments"):
        src = os.path.join(new_root, sub)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(branch_root, sub)
        os.makedirs(dst, exist_ok=True)
        for fn in sorted(os.listdir(src)):
            rel = "%s/%s" % (sub, fn)
            cid = _staged_cid(sub, fn)
            if cid is not None and cid in blocked:
                skipped.append(rel)
                continue
            shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
            added.append(rel)
    return added, skipped
