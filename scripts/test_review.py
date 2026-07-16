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


def test_route_fable_cleared(tmp):
    # A held (flagged) card the Fable review cleared as a false positive is routed to AUTO --
    # published, not staged, seen advances -- while a held card Fable did NOT clear stays held.
    _point_store(tmp)
    clean = [card(7, "Old Precedent")]
    added = [card(1, "Clean New"), card(2, "Flagged but cleared"), card(3, "Flagged still held")]
    routed = update.route_and_publish(
        added=added, treat_events=[], clean_entries=clean,
        flagged=[("Flagged but cleared", ["low confidence"]), ("Flagged still held", ["low confidence"])],
        crosschecks={}, completeness={}, overruling_cids=set(), pending_review=set(),
        state={"seen_clusters": [7]}, seen={7}, evaluated={1, 2, 3}, have={7}, now_iso="t",
        treat_flags=[], fable_cleared={2},
        fable_verdicts={2: {"clear": True, "recommendation": "accept", "confidence": "high",
                            "assessment": "faithful to the opinion", "available": True, "is_false_positive": True}})
    check("fable-clear: counts 2 auto, 1 held", routed["auto"] == 2 and routed["held"] == 1)
    pub_ids = {e["cluster_id"] for e in json.load(open(update.JSON_PATH))}
    check("fable-clear: the cleared card is auto-published", 2 in pub_ids)
    check("fable-clear: the un-cleared held card is NOT published", 3 not in pub_ids)
    staged = {c["cluster_id"] for c in review_store.read_staged()[0]}
    check("fable-clear: only the un-cleared card is staged", staged == {3})
    check("fable-clear: pending ledger holds only the un-cleared card", review_store.load_pending() == {3})
    seen = set(json.load(open(update.STATE_PATH))["seen_clusters"])
    check("fable-clear: seen advances for cleared+clean, not the held one", seen == {1, 2, 7})


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


def test_flag_caution_no_zombie_date():
    """flag_caution must bump treatment_date ONLY on the ok->caution transition. Recording another
    citer on an already-flagged, or human-resolved (negative/superseded), card must NOT re-date it --
    doing so resurrected a long-dead case in the weekly digest each time a new opinion cited it."""
    import treatment_core

    def citer(cid):
        return {"cluster_id": cid, "name": "Later %d" % cid, "court": "ctapp", "date": "2026-07-16", "kind": "overruled"}

    c = {"name": "A", "treated_by": []}
    raised = treatment_core.flag_caution(c, citer(1))
    check("ok->caution returns True and stamps a date", raised is True and c["treatment"] == "caution" and bool(c.get("treatment_date")))

    sup = {"name": "B", "treatment": "superseded", "treatment_date": "2025-01-10", "treated_by": [{"cluster_id": 7}]}
    r = treatment_core.flag_caution(sup, citer(8))
    check("superseded + new citer returns False", r is False)
    check("superseded + new citer: treatment_date NOT bumped (no digest zombie)", sup["treatment_date"] == "2025-01-10")
    check("superseded + new citer: still superseded, citer recorded", sup["treatment"] == "superseded" and len(sup["treated_by"]) == 2)

    cau = {"name": "C", "treatment": "caution", "treatment_date": "2026-01-01", "treated_by": [{"cluster_id": 3}]}
    treatment_core.flag_caution(cau, citer(4))
    check("caution + new citer: treatment_date not re-bumped", cau["treatment_date"] == "2026-01-01")

    r2 = treatment_core.flag_caution(sup, citer(8))
    check("duplicate citer is a no-op", r2 is False and len(sup["treated_by"]) == 2)


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


def test_redraft_ids_and_exemption(tmp):
    _point_store(tmp)
    check("redraft ids: missing ledger -> empty", review_store.load_redraft_ids() == set())
    # A veto records a redraft; a second veto of a different case appends. Blank/malformed lines
    # and a non-int id must be tolerated without dropping the good records.
    review_store.log_redraft([{"ts": "t1", "cluster_id": 5, "reason": "vetoed in review"}])
    review_store.log_redraft([{"ts": "t2", "cluster_id": 8, "reason": "review PR closed unmerged"}])
    with open(review_store.REDRAFT_PATH, "a", encoding="utf-8") as f:
        f.write("\n")                          # blank line
        f.write("{not json}\n")                # malformed
        f.write('{"reason": "no cluster_id"}\n')  # missing id
        f.write('{"cluster_id": "x"}\n')       # non-int id
    check("redraft ids: parses valid, tolerates junk", review_store.load_redraft_ids() == {5, 8})

    # The funnel's exemption set: still-unresolved redraft ids, minus anything already resolved.
    # Case 5 is still awaiting redraft (exempt from the since floor); case 8 was re-carded and is
    # now published (in `have`), so it must NOT be exempt again -- the set is self-clearing.
    seen, have, pending = {2}, {8}, {3}
    redraft_pending = review_store.load_redraft_ids() - seen - have - pending
    check("redraft exemption: only the unresolved vetoed case is exempt", redraft_pending == {5})
    check("redraft exemption: a re-carded case falls out of the set", 8 not in redraft_pending)


def test_review_command_helpers(tmp):
    # parse_command: the /veto /decline parse the workflow used to do in grep/sed.
    check("parse: /veto <id>", review_store.parse_command("/veto 123") == ("veto", 123))
    check("parse: /decline <id>", review_store.parse_command("/decline 456") == ("decline", 456))
    check("parse: verb inside a sentence", review_store.parse_command("please /decline 789 thanks") == ("decline", 789))
    check("parse: first command wins", review_store.parse_command("/veto 12 /decline 34") == ("veto", 12))
    check("parse: verb with no id", review_store.parse_command("/veto") == ("veto", None))
    check("parse: look-alike /vetoed is not a command", review_store.parse_command("/vetoed 5") == (None, None))
    check("parse: no command", review_store.parse_command("looks good, merging") == (None, None))
    check("parse: non-string is safe", review_store.parse_command(None) == (None, None))

    # staged_files_for: the card + citing-treatment lookup used for the 'is it staged?' check and
    # the exact set to git rm.
    _point_store(tmp)
    review_store.stage_card(card(5, "Held"), ["fidelity flag"])
    review_store.stage_treatment(7, {"cluster_id": 5, "name": "Citer v. Card", "kind": "overruled"}, "adverse")
    files = review_store.staged_files_for(5)
    check("staged_files_for: finds the held card and the citing treatment", len(files) == 2
          and any(f.endswith("cards/5.json") for f in files)
          and any(f.endswith("7__5.json") for f in files))
    check("staged_files_for: all returned paths exist", all(os.path.exists(f) for f in files))
    check("staged_files_for: empty for an unstaged id", review_store.staged_files_for(999) == [])

    # record_decision: the single marker write the workflow calls, dispatching to the right ledger.
    review_store.record_decision("veto", 11)
    review_store.record_decision("decline", 22)
    check("record_decision: veto lands in vetoed.json", review_store.read_vetoed() == {11})
    check("record_decision: decline lands in declined.json", review_store.read_declined() == {22})
    try:
        review_store.record_decision("bogus", 33)
        check("record_decision: unknown cmd raises", False)
    except ValueError:
        check("record_decision: unknown cmd raises", True)


def test_seam_hold_veto_rediscover(tmp):
    """End-to-end seam across route -> apply -> re-discovery: a card HELD by routing and then VETOED
    at apply time must stay re-discoverable -- un-seen, redraft-logged, AND admitted by the funnel's
    redraft-exemption set (so the `since` floor cannot silently drop it). This is the exact confluence
    bug #3 lived in; a per-subsystem test that stops at route, or at apply, never exercises it."""
    _point_store(tmp)
    # Route: card 5 is flagged -> held (staged + pending + un-seen); card 1 is clean -> auto-published.
    update.route_and_publish(
        added=[card(1, "Clean"), card(5, "Flagged")], treat_events=[], clean_entries=[],
        flagged=[("Flagged", ["low confidence"])], crosschecks={}, completeness={},
        overruling_cids=set(), pending_review=set(), state={"seen_clusters": []},
        seen=set(), evaluated={1, 5}, have=set(), now_iso="t", treat_flags=[])
    routed_seen = set(json.load(open(update.STATE_PATH))["seen_clusters"])
    check("seam: card 5 is held (pending, not yet seen)",
          review_store.load_pending() == {5} and 5 not in routed_seen)

    # Model /veto at merge: card 5 stays in the pending ledger but its staged file is gone, so
    # apply_merged records it for redraft and leaves it un-seen (card 1 already auto-published).
    os.remove(review_store.card_path(5))
    review_apply.apply_merged()

    seen = set(json.load(open(update.STATE_PATH)).get("seen_clusters", []))
    have = {e["cluster_id"] for e in json.load(open(update.JSON_PATH))}
    # The funnel's exemption set (update.py): still-unresolved redraft ids minus resolved state.
    redraft_pending = review_store.load_redraft_ids() - seen - have - review_store.load_pending()
    check("seam: vetoed card 5 left un-seen", 5 not in seen)
    check("seam: vetoed card 5 redraft-logged", 5 in review_store.load_redraft_ids())
    check("seam: funnel would re-admit card 5 past the since floor", 5 in redraft_pending)


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
    test_flag_caution_no_zombie_date()
    test_hold_reasons()
    for t in (test_store_roundtrip, test_redraft_ids_and_exemption,
              test_route_and_publish, test_route_fable_cleared, test_route_noop,
              test_apply_merged, test_apply_idempotent_card, test_apply_closed_unmerged,
              test_apply_declined, test_apply_declined_only, test_apply_veto_backstop,
              test_review_command_helpers, test_seam_hold_veto_rediscover, test_merge_new_into_branch):
        tmp = tempfile.mkdtemp(prefix="review_test_")
        try:
            t(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (83 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
