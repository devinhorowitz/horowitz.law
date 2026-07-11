#!/usr/bin/env python3
"""Hermetic unit test for the two-lane review workflow (review_store + review_apply).

No network, no API key. Covers:
  * hold_reasons: a clean additive card auto-publishes ([]), while a guard flag, a low-
    confidence flag, an unavailable guard, or an overruling cluster holds it.
  * the staging store: stage/read round-trip, the pending ledger, the redraft log, and the
    staged-cluster-id set the funnel skips.
  * review_apply.apply_merged: surviving cards are appended (idempotent), surviving treatment
    changes are applied to the live card, accepted clusters join seen, and a case that was
    pending but is no longer staged (vetoed) is redraft-logged and left UN-seen.
  * review_apply.apply_closed_unmerged: opinions.json is untouched and every pending case is
    redraft-logged.

Run directly: `python scripts/test_review.py`.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_store   # noqa: E402
import review_apply   # noqa: E402
import update         # noqa: E402
import render         # noqa: E402

FAILS = []
COURT = next(iter(render.COURT_LABELS))     # a court key the PR-body renderer knows
AREA = next(iter(render.AREA_LABELS))       # a practice-area key the renderer knows


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def card(cid, name="Alpha v. X", treatment=None):
    e = {"cluster_id": cid, "name": name, "court": COURT, "date": "2026-03-01",
         "dockets": ["A26A%04d" % cid], "areas": [AREA], "synopsis": "s", "why": "w",
         "disposition": "affirmed", "url": "https://www.courtlistener.com/opinion/%d/" % cid}
    if treatment:
        e["treatment"] = treatment
    return e


def _point_store(tmp):
    """Redirect the store and the apply/funnel paths into a scratch dir."""
    review_store.REVIEW_DIR = os.path.join(tmp, "review")
    review_store.CARDS_DIR = os.path.join(review_store.REVIEW_DIR, "cards")
    review_store.TREAT_DIR = os.path.join(review_store.REVIEW_DIR, "treatments")
    review_store.PENDING_PATH = os.path.join(tmp, "opinions_pending_review.json")
    review_store.REDRAFT_PATH = os.path.join(tmp, "opinions_redraft.jsonl")
    # The veto/decline markers resolve under REVIEW_DIR (repointed above), so nothing else to set.
    update.JSON_PATH = os.path.join(tmp, "opinions.json")
    update.STATE_PATH = os.path.join(tmp, "opinions_state.json")
    update.AUTO_PR_PATH = os.path.join(tmp, "pr_body_auto.md")
    update.REVIEW_PR_PATH = os.path.join(tmp, "pr_body_review.md")
    render.render = lambda entries: (len(entries), len(entries))   # no real render in the test


def test_route_and_publish(tmp):
    _point_store(tmp)
    clean = [card(7, "Old Precedent")]
    added = [card(1, "Clean New"), card(2, "Flagged New"), card(3, "Overruler v. Old")]
    treat_events = [{"card_cid": 7, "citer": {"cluster_id": 3, "name": "Overruler v. Old",
                                              "kind": "overruled", "court": "ctapp",
                                              "date": "2026-04-01", "note": ""}}]
    routed = update.route_and_publish(
        added=added, treat_events=treat_events, clean_entries=clean,
        flagged=[("Flagged New", ["low confidence"])], crosschecks={}, completeness={},
        overruling_cids={3}, pending_review=set(), state={"seen_clusters": [7]},
        seen={7}, evaluated={1, 2, 3}, have={7}, now_iso="t",
        treat_flags=[("Old Precedent", "Overruler v. Old", "overruled")])

    check("route: counts (1 auto, 2 held, 1 treatment)",
          routed == {"auto": 1, "held": 2, "treatments": 1, "wrote_auto": True, "noop": False})
    published = json.load(open(update.JSON_PATH))
    pub_ids = {e["cluster_id"] for e in published}
    check("route: only the clean card is auto-published", pub_ids == {7, 1})
    check("route: the flagged and overruling cards are NOT published", not ({2, 3} & pub_ids))
    staged_cards, staged_treats = review_store.read_staged()
    check("route: flagged + overruling cards are staged", {c["cluster_id"] for c in staged_cards} == {2, 3})
    check("route: the treatment change is staged", len(staged_treats) == 1
          and staged_treats[0]["card_cluster_id"] == 7)
    check("route: pending ledger = held clusters", review_store.load_pending() == {2, 3})
    state = json.load(open(update.STATE_PATH))
    seen = set(state["seen_clusters"])
    check("route: seen advances for the auto card, not the held ones", seen == {1, 7})


def test_route_noop(tmp):
    _point_store(tmp)
    routed = update.route_and_publish(
        added=[], treat_events=[], clean_entries=[card(7)], flagged=[], crosschecks={},
        completeness={}, overruling_cids=set(), pending_review=set(),
        state={"seen_clusters": [7]}, seen={7}, evaluated={7, 8}, have={7}, now_iso="t",
        treat_flags=[])
    check("route noop: reported as a no-op", routed["noop"] is True)
    check("route noop: opinions.json not written", not os.path.exists(update.JSON_PATH))
    state = json.load(open(update.STATE_PATH))
    check("route noop: evaluated clusters marked seen", set(state["seen_clusters"]) == {7, 8})


def test_hold_reasons():
    e = card(1)
    check("hold: clean additive card auto-publishes", review_store.hold_reasons(e, {}, {}, {}, set()) == [])
    check("hold: a funnel review flag holds it",
          review_store.hold_reasons(e, {"Alpha v. X": ["low confidence"]}, {}, {}, set()) == ["low confidence"])
    held = review_store.hold_reasons(e, {}, {1: {"verdict": "flag", "reason": "misreads"}}, {}, set())
    check("hold: a fidelity flag holds it", any("fidelity flag" in r for r in held))
    held = review_store.hold_reasons(e, {}, {1: {"verdict": "unavailable"}}, {}, set())
    check("hold: an unavailable guard holds it", any("unavailable" in r for r in held))
    held = review_store.hold_reasons(e, {}, {}, {}, {1})
    check("hold: an overruling cluster holds it", any("overrules or modifies" in r for r in held))


def test_store_roundtrip(tmp):
    _point_store(tmp)
    review_store.stage_card(card(1), ["fidelity flag: x"])
    citer = {"cluster_id": 99, "name": "Later v. Z", "kind": "overruled"}
    review_store.stage_treatment(7, citer, "adverse")
    cards, treats = review_store.read_staged()
    check("store: one card and one treatment read back", len(cards) == 1 and len(treats) == 1)
    check("store: card carries its entry and reasons",
          cards[0]["entry"]["cluster_id"] == 1 and cards[0]["hold_reasons"] == ["fidelity flag: x"])
    check("store: staged ids = held card + treatment citer", review_store.staged_cluster_ids() == {1, 99})
    review_store.save_pending({1, 99, 7}, stamp="t")
    check("store: pending ledger round-trips", review_store.load_pending() == {1, 7, 99})
    review_store.log_redraft([{"cluster_id": 5}])
    check("store: redraft log appends a line", os.path.exists(review_store.REDRAFT_PATH))


def test_apply_merged(tmp):
    _point_store(tmp)
    # Seed opinions.json with one already-published card that a staged treatment will modify.
    target = card(7, "Old Precedent")
    json.dump([target], open(update.JSON_PATH, "w"))
    json.dump({"seen_clusters": [7], "last_filed": "2026-03-01"}, open(update.STATE_PATH, "w"))

    # Stage: one surviving card (cid 1) and one treatment (card 7 treated by citer 99).
    # The pending ledger also lists cid 2 -- a case that was staged then VETOED (no file now).
    review_store.stage_card(card(1, "New Held Card"), ["fidelity flag: verify"])
    review_store.stage_treatment(7, {"cluster_id": 99, "name": "Overruler v. Y", "kind": "overruled",
                                     "court": "ctapp", "date": "2026-04-01", "note": ""}, "adverse")
    review_store.save_pending({1, 2, 99}, stamp="t")

    counts = review_apply.apply_merged()
    entries = json.load(open(update.JSON_PATH))
    by_id = {e["cluster_id"]: e for e in entries}
    check("apply: surviving card appended", 1 in by_id and by_id[1]["name"] == "New Held Card")
    check("apply: treatment applied to the live card", (by_id[7].get("treatment") or "") == "caution"
          and any(x.get("cluster_id") == 99 for x in by_id[7].get("treated_by", [])))
    state = json.load(open(update.STATE_PATH))
    seen = set(state.get("seen_clusters", []))
    check("apply: accepted clusters join seen (card 1, citer 99)", {1, 99} <= seen)
    check("apply: vetoed cluster 2 left UN-seen", 2 not in seen)
    redraft = [json.loads(l) for l in open(review_store.REDRAFT_PATH) if l.strip()]
    check("apply: vetoed cluster 2 redraft-logged", any(r.get("cluster_id") == 2 for r in redraft))
    check("apply: pending ledger cleared", review_store.load_pending() == set())
    check("apply: staging tree emptied", review_store.read_staged() == ([], []))
    check("apply: counts report 1 card, 1 treatment, 1 veto",
          counts == {"accepted_cards": 1, "accepted_treatments": 1, "vetoed": 1, "declined": 0, "skipped": 0})


def test_apply_idempotent_card(tmp):
    _point_store(tmp)
    json.dump([card(1, "Already There")], open(update.JSON_PATH, "w"))
    json.dump({"seen_clusters": [1]}, open(update.STATE_PATH, "w"))
    review_store.stage_card(card(1, "Dup Attempt"), ["fidelity flag"])
    review_store.save_pending({1}, stamp="t")
    counts = review_apply.apply_merged()
    entries = json.load(open(update.JSON_PATH))
    check("idempotent: a cluster already carded is not duplicated", len(entries) == 1)
    check("idempotent: it is reported skipped", counts["skipped"] == 1 and counts["accepted_cards"] == 0)


def test_apply_closed_unmerged(tmp):
    _point_store(tmp)
    json.dump([card(7, "Untouched")], open(update.JSON_PATH, "w"))
    review_store.stage_card(card(1), ["fidelity flag"])
    review_store.save_pending({1, 2}, stamp="t")
    review_apply.apply_closed_unmerged()
    entries = json.load(open(update.JSON_PATH))
    check("closed: opinions.json untouched", len(entries) == 1 and entries[0]["name"] == "Untouched")
    redraft = [json.loads(l) for l in open(review_store.REDRAFT_PATH) if l.strip()]
    check("closed: all pending redraft-logged", {r["cluster_id"] for r in redraft} == {1, 2})
    check("closed: pending cleared and staging emptied",
          review_store.load_pending() == set() and review_store.read_staged() == ([], []))


def test_apply_declined(tmp):
    _point_store(tmp)
    json.dump([card(7, "Existing")], open(update.JSON_PATH, "w"))
    json.dump({"seen_clusters": []}, open(update.STATE_PATH, "w"))
    # cid 1 stays staged (accepted). cid 5 was /decline'd (file dropped, id in declined.json).
    # cid 2 was /veto'd (file dropped, NOT in declined.json). Decline and veto must diverge.
    review_store.stage_card(card(1, "Kept Card"), ["fidelity flag"])
    review_store.add_declined(5)
    review_store.save_pending({1, 2, 5}, stamp="t")

    counts = review_apply.apply_merged()
    seen = set(json.load(open(update.STATE_PATH)).get("seen_clusters", []))
    redraft_ids = {json.loads(l).get("cluster_id") for l in open(review_store.REDRAFT_PATH)} \
        if os.path.exists(review_store.REDRAFT_PATH) else set()
    check("decline: declined cluster 5 marked seen", 5 in seen)
    check("decline: declined cluster 5 NOT redraft-logged", 5 not in redraft_ids)
    check("decline: vetoed cluster 2 left un-seen AND redraft-logged", 2 not in seen and 2 in redraft_ids)
    check("decline: accepted cluster 1 seen", 1 in seen)
    check("decline: counts report 1 declined, 1 veto, 1 accepted",
          counts["declined"] == 1 and counts["vetoed"] == 1 and counts["accepted_cards"] == 1)
    check("decline: declined.json consumed with the batch",
          not os.path.exists(review_store._marker_path("declined.json")))


def test_apply_declined_only(tmp):
    # A batch whose only human action is a decline (no accepted cards): opinions.json must be
    # untouched, but the declined cluster must still be marked seen so it never returns.
    _point_store(tmp)
    json.dump([card(7, "Existing")], open(update.JSON_PATH, "w"))
    json.dump({"seen_clusters": []}, open(update.STATE_PATH, "w"))
    review_store.add_declined(5)
    review_store.save_pending({5}, stamp="t")
    counts = review_apply.apply_merged()
    entries = json.load(open(update.JSON_PATH))
    seen = set(json.load(open(update.STATE_PATH)).get("seen_clusters", []))
    check("decline-only: opinions.json untouched", len(entries) == 1 and entries[0]["name"] == "Existing")
    check("decline-only: cluster 5 marked seen with no accepted card", 5 in seen)
    check("decline-only: counts 0 accepted, 1 declined", counts["accepted_cards"] == 0 and counts["declined"] == 1)


def test_apply_veto_backstop(tmp):
    # The apply-side backstop: a vetoed case whose staged file was RESTORED to the branch by a
    # racing scan (defeating the branch lease) must still NOT be published -- review_apply reads
    # vetoed.json as authoritative and refuses the case even though its file is present.
    _point_store(tmp)
    json.dump([card(7, "Existing")], open(update.JSON_PATH, "w"))
    json.dump({"seen_clusters": []}, open(update.STATE_PATH, "w"))
    review_store.stage_card(card(3, "Clobbered-back veto"), ["fidelity flag"])  # file present (the clobber)
    review_store.add_vetoed(3)                                                  # but marked vetoed
    review_store.save_pending({3}, stamp="t")
    counts = review_apply.apply_merged()
    entries = json.load(open(update.JSON_PATH))
    seen = set(json.load(open(update.STATE_PATH)).get("seen_clusters", []))
    redraft_ids = {json.loads(l).get("cluster_id") for l in open(review_store.REDRAFT_PATH)} \
        if os.path.exists(review_store.REDRAFT_PATH) else set()
    check("backstop: a restored-but-vetoed card is NOT published", 3 not in {e["cluster_id"] for e in entries})
    check("backstop: it is left un-seen and redraft-logged", 3 not in seen and 3 in redraft_ids)
    check("backstop: counted as vetoed, not accepted", counts["vetoed"] == 1 and counts["accepted_cards"] == 0)


def test_merge_new_into_branch(tmp):
    # The funnel's file-level reconciliation (scripts/review_stage.py -> merge_new_into_branch):
    # this run's new stage unions into the rebuilt branch, but a case already vetoed/declined on
    # the branch is NOT resurrected, and existing files + markers are preserved.
    new_root = os.path.join(tmp, "new")
    branch = os.path.join(tmp, "branch")
    # This run freshly stages: card 10 (will be blocked by a branch veto), card 12 (ok), and a
    # treatment whose citer 30 was declined on the branch.
    review_store.stage_card(card(10, "Fresh but vetoed"), ["flag"], root=new_root)
    review_store.stage_card(card(12, "Fresh ok"), ["flag"], root=new_root)
    review_store.stage_treatment(20, {"cluster_id": 30, "name": "Citer", "kind": "overruled",
                                      "court": "ctapp", "date": "2026-01-01", "note": ""}, "adverse", root=new_root)
    # The branch already holds an older card 11 and markers for the vetoed/declined cases.
    review_store.stage_card(card(11, "Older held"), ["flag"], root=branch)
    review_store.add_vetoed(10, root=branch)
    review_store.add_declined(30, root=branch)

    added, skipped = review_store.merge_new_into_branch(new_root, branch)
    br_cards, br_treats = review_store.read_staged(root=branch)
    ids = {c["cluster_id"] for c in br_cards}
    citer_ids = {(t.get("citer") or {}).get("cluster_id") for t in br_treats}
    check("merge: older branch card kept", 11 in ids)
    check("merge: an unblocked new card is added", 12 in ids)
    check("merge: a vetoed case is not re-added by the union", 10 not in ids)
    check("merge: a declined treatment citer is not re-added", 30 not in citer_ids)
    check("merge: markers preserved on the branch",
          review_store.read_vetoed(branch) == {10} and review_store.read_declined(branch) == {30})
    check("merge: reports 1 added, 2 skipped", len(added) == 1 and len(skipped) == 2)


def main():
    print("review routing + apply:")
    test_hold_reasons()
    for t in (test_store_roundtrip, test_route_and_publish, test_route_noop,
              test_apply_merged, test_apply_idempotent_card, test_apply_closed_unmerged,
              test_apply_declined, test_apply_declined_only, test_apply_veto_backstop,
              test_merge_new_into_branch):
        tmp = tempfile.mkdtemp(prefix="review_test_")
        try:
            t(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (49 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
