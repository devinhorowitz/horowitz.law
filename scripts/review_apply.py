#!/usr/bin/env python3
"""Apply a merged (or closed) review batch (scripts/review_apply.py).

The daily funnel stages every held case -- a new card that overrules or modifies an
existing card, a new card that causes such a change, or a guard-flagged card -- as a
small JSON file under review/ on the bot/opinions-review branch, and opens a bundled
review PR. A person accepts a case by leaving its file in the PR and merging, and vetoes
one by commenting `/veto <id>` (which drops its file) or deleting the file. This script
runs when that PR resolves and reconciles opinions.json with the human's decision:

  MERGED (the default mode): the surviving staged files are the accepted cases.
    * each surviving card is appended to opinions.json (idempotent: a cluster already
      carded is skipped);
    * each surviving treatment change is applied to its live card via the production
      treatment_core.flag_caution, exactly as the funnel would have;
    * accepted clusters are added to seen_clusters so the funnel does not re-evaluate them;
    * vetoed clusters (in the pending ledger but no longer staged) are logged to the
      redraft ledger and deliberately LEFT OUT of seen_clusters, so a later run
      rediscovers and redrafts them;
    * the pages are re-rendered, the staging tree is emptied, and the pending ledger is
      cleared. All of this commits straight to main -- it is the mechanical apply of a
      decision a human already made, not new editorial content.

  CLOSED-UNMERGED (`--closed-unmerged`): the human threw the whole batch away. Every
    pending cluster is logged for redraft and left un-seen, the staging tree is emptied,
    and the pending ledger is cleared. opinions.json is untouched.

Reuses update.py (paths, state) and render.py, so what lands here matches the daily feed.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update          # noqa: E402  paths, state shape
import render          # noqa: E402  the one renderer the funnel uses
import safeio          # noqa: E402
import review_store    # noqa: E402
import treatment_core  # noqa: E402  the production flag_caution


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_entries():
    return json.load(open(update.JSON_PATH, encoding="utf-8")) if os.path.exists(update.JSON_PATH) else []


def _load_state():
    if os.path.exists(update.STATE_PATH):
        return json.load(open(update.STATE_PATH, encoding="utf-8"))
    return {}


def _rm_staged():
    """Remove the staging tree so a resolved batch does not linger. Files are also removed
    from git by the workflow; deleting here keeps a local/idempotent run clean."""
    for sub in (review_store.CARDS_DIR, review_store.TREAT_DIR):
        if not os.path.isdir(sub):
            continue
        for fn in os.listdir(sub):
            try:
                os.remove(os.path.join(sub, fn))
            except OSError:
                pass


def _summary(md):
    try:
        safeio.step_summary(md)
    except Exception:
        pass


def apply_merged():
    """Reconcile opinions.json with the accepted (surviving) staged cases. Returns a dict of
    counts for the run summary."""
    cards, treatments = review_store.read_staged()
    pending = review_store.load_pending()
    entries = _load_entries()
    by_id = {int(e["cluster_id"]): e for e in entries if e.get("cluster_id") is not None}

    accepted_ids, added_names, applied_treats, skipped = set(), [], [], []

    # 1. Accepted new cards -> append (idempotent on cluster_id).
    for c in cards:
        entry = c.get("entry") or {}
        cid = entry.get("cluster_id")
        if cid is None:
            skipped.append("card file missing cluster_id"); continue
        cid = int(cid)
        if cid in by_id:
            skipped.append("card %d already in opinions.json; skipped" % cid); continue
        entries.append(entry)
        by_id[cid] = entry
        accepted_ids.add(cid)
        added_names.append(entry.get("name", str(cid)))

    # 2. Accepted treatment changes -> apply to the live card via the production path.
    for t in treatments:
        card_cid = t.get("card_cluster_id")
        citer = t.get("citer") or {}
        if card_cid is None or citer.get("cluster_id") is None:
            skipped.append("treatment file missing ids"); continue
        card = by_id.get(int(card_cid))
        if not card:
            skipped.append("treatment target card %s not found; skipped" % card_cid); continue
        treatment_core.flag_caution(card, citer)   # dedups and respects a human-set treatment
        applied_treats.append((card.get("name", ""), citer.get("name", "")))
        accepted_ids.add(int(citer["cluster_id"]))

    # 3. Vetoed = pending when the PR opened, minus everything still staged (accepted).
    surviving = review_store.staged_cluster_ids()
    vetoed = sorted(pending - surviving)
    if vetoed:
        review_store.log_redraft([{"ts": _stamp(), "cluster_id": v, "reason": "vetoed in review"}
                                  for v in vetoed])

    # 4. Persist. Accepted clusters join seen_clusters; vetoed ones are deliberately left out
    #    so the funnel rediscovers and redrafts them. Then re-render and clear the batch.
    if accepted_ids:      # non-empty iff a card or a treatment was accepted (both add to it)
        safeio.atomic_write_json(update.JSON_PATH, entries)
        state = _load_state()
        seen = set(state.get("seen_clusters", [])) | accepted_ids
        state["seen_clusters"] = sorted(seen)[-update.SEEN_CAP:]
        if entries:
            state["last_filed"] = max(e["date"] for e in entries if e.get("date"))
        state["updated"] = _stamp()
        safeio.atomic_write_json(update.STATE_PATH, state)
        render.render(entries)

    review_store.save_pending(set(), stamp=_stamp())   # batch resolved
    _rm_staged()

    counts = {"accepted_cards": len(added_names), "accepted_treatments": len(applied_treats),
              "vetoed": len(vetoed), "skipped": len(skipped)}
    print("review apply (merged): %d card(s), %d treatment(s) accepted; %d vetoed; %d skipped"
          % (counts["accepted_cards"], counts["accepted_treatments"], counts["vetoed"], counts["skipped"]))
    for s in skipped:
        print("  . " + s)
    _summary("### Review batch applied %s\n\n- accepted: %d card(s), %d treatment change(s)\n"
             "- vetoed (left for redraft): %d\n"
             % (_stamp(), counts["accepted_cards"], counts["accepted_treatments"], counts["vetoed"])
             + ("".join("- accepted card: %s\n" % n for n in added_names)))
    return counts


def apply_closed_unmerged():
    """The batch was closed without merging: everything pending is vetoed. Log all for redraft,
    leave opinions.json untouched, empty the staging tree, and clear the pending ledger."""
    pending = review_store.load_pending()
    if pending:
        review_store.log_redraft([{"ts": _stamp(), "cluster_id": v, "reason": "review PR closed unmerged"}
                                  for v in sorted(pending)])
    review_store.save_pending(set(), stamp=_stamp())
    _rm_staged()
    print("review apply (closed unmerged): %d case(s) left for redraft" % len(pending))
    _summary("### Review batch discarded %s\n\n- %d case(s) closed unmerged; left eligible for redraft\n"
             % (_stamp(), len(pending)))
    return {"vetoed": len(pending)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closed-unmerged", action="store_true",
                    help="the review PR was closed without merging; discard the batch and redraft-log all pending")
    args = ap.parse_args()
    if args.closed_unmerged:
        apply_closed_unmerged()
    else:
        apply_merged()
    return 0


if __name__ == "__main__":
    sys.exit(main())
