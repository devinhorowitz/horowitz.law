#!/usr/bin/env python3
"""State-machine stress harness for the two-lane REVIEW workflow (review_store + review_apply).

The funnel holds any card that overrules/modifies a published card, or that a guard flagged, as a
staged file in a bundled review PR. A human resolves each case one of three ways: accept (leave the
file, merge), `/veto <id>` (drop it -- left UN-seen so a later run redrafts it), or `/decline <id>`
(drop it AND mark it seen, so it never returns). review_apply reconciles opinions.json with that
decision when the PR merges. Getting this wrong is serious in BOTH directions: publishing a case a
human vetoed, or silently losing one they merely wanted redrafted.

This drives the REAL review_apply.apply_merged() in a stubbed sandbox over randomized scenarios and
asserts the load-bearing invariants:

  - a vetoed OR declined case is NEVER published (the marker backstop holds even if a racing scan
    restored its staged file);
  - a vetoed case is left un-seen AND redraft-logged (it will come back);
  - a declined case is marked seen AND never redraft-logged (it will not);
  - an accepted card is published AND marked seen, with no duplicate ever appended to opinions.json;
  - pre-existing cards are untouched, and the pending ledger is cleared.

It also fuzzes review_store.parse_command -- the `/veto`/`/decline` parser that reads untrusted PR
comments -- for crash-safety and grammar (word boundary, first-wins, verb-without-id).

Run directly: `python scripts/stress_review.py [iterations]`. Exits nonzero on any failure.
"""
import contextlib
import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render          # noqa: E402
import review_apply    # noqa: E402
import review_store    # noqa: E402
import update          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def card_entry(cid, name=None):
    return {"cluster_id": cid, "name": name or ("Case %d v. State" % cid), "court": "gactapp",
            "date": "2026-06-01", "dockets": ["A26A%04d" % (cid % 10000)], "areas": ["auto"],
            "disposition": "affirmed", "synopsis": "A holding.", "why": "It matters.",
            "url": "https://x/%d" % cid, "first_seen": "2026-06-01", "additional_holdings": []}


@contextlib.contextmanager
def sandbox():
    """Redirect every review_store / update path to a tempdir and stub render, so apply_merged runs
    with no repo writes and no rendering. Yields the tempdir."""
    tmp = tempfile.mkdtemp(prefix="review-stress-")
    review = os.path.join(tmp, "review")
    saved = {}

    def sv(obj, attr, val):
        saved[(id(obj), attr)] = (obj, attr, getattr(obj, attr))
        setattr(obj, attr, val)

    sv(review_store, "REVIEW_DIR", review)
    sv(review_store, "CARDS_DIR", os.path.join(review, "cards"))
    sv(review_store, "TREAT_DIR", os.path.join(review, "treatments"))
    sv(review_store, "PENDING_PATH", os.path.join(tmp, "opinions_pending_review.json"))
    sv(review_store, "REDRAFT_PATH", os.path.join(tmp, "opinions_redraft.jsonl"))
    sv(update, "JSON_PATH", os.path.join(tmp, "opinions.json"))
    sv(update, "STATE_PATH", os.path.join(tmp, "opinions_state.json"))
    sv(render, "render", lambda *a, **k: None)
    try:
        yield tmp
    finally:
        for obj, attr, val in saved.values():
            setattr(obj, attr, val)
        shutil.rmtree(tmp, ignore_errors=True)


def published_ids():
    if not os.path.exists(update.JSON_PATH):
        return []
    return [int(e["cluster_id"]) for e in json.load(open(update.JSON_PATH)) if e.get("cluster_id") is not None]


def seen_ids():
    if not os.path.exists(update.STATE_PATH):
        return set()
    return set(json.load(open(update.STATE_PATH)).get("seen_clusters", []))


def test_parse_command():
    ps = review_store.parse_command
    check("parse: /veto 123 -> ('veto', 123)", ps("please /veto 123 thanks") == ("veto", 123))
    check("parse: /decline 9 -> ('decline', 9)", ps("/decline 9") == ("decline", 9))
    check("parse: verb without id -> (verb, None)", ps("I want to /veto this") == ("veto", None))
    check("parse: no command -> (None, None)", ps("looks good, merging") == (None, None))
    check("parse: /vetoed 5 (look-alike) is NOT a veto", ps("this was /vetoed 5 yesterday") == (None, None))
    check("parse: first command wins", ps("/decline 1 then /veto 2") == ("decline", 1))
    check("parse: non-string body -> (None, None)", ps(None) == (None, None) and ps(12345) == (None, None))
    check("parse: newline-separated command is found", ps("LGTM\n\n/veto 42\n") == ("veto", 42))

    rng = random.Random(1234567)
    frag = ["/veto", "/decline", "/vetoed", "/declined", "veto", "123", "0", "99999999999", " ", "\n",
            "\t", "/veto ", "lgtm", "merge", "-5", "1.5", "id", ":", "٥", "/VETO", "#", "/veto\t7"]
    for _ in range(3000):
        body = "".join(rng.choice(frag) for _ in range(rng.randint(0, 10)))
        try:
            cmd, cid = ps(body)
        except Exception as e:
            check("parse_command never crashes", False, "%r on %r" % (e, body)); break
        if cmd not in (None, "veto", "decline") or not (cid is None or isinstance(cid, int)):
            check("parse_command returns well-typed results", False, "%r -> %r" % (body, (cmd, cid))); break
        # record_decision must round-trip any parsed command into the right marker without crashing.
        if cmd and cid is not None:
            with sandbox():
                try:
                    ids = review_store.record_decision(cmd, cid)
                except Exception as e:
                    check("record_decision handles any parsed command", False, "%r" % e); break
                marker = review_store.read_vetoed() if cmd == "veto" else review_store.read_declined()
                if cid not in marker or ids != marker:
                    check("record_decision writes the id to the right marker", False,
                          "%s %d -> %r" % (cmd, cid, marker)); break
    else:
        check("parse_command: 3000 hostile comment bodies, no crash, well-typed", True)


def build_scenario(rng):
    """A randomized review batch. Returns (fates, preexisting) where fates maps cid -> role in
    {'accept','veto','veto_restored','decline'} and preexisting is a list of bystander cids already
    in opinions.json that must survive untouched."""
    n = rng.randint(0, 8)
    base = 900000 + rng.randint(0, 1000) * 100
    fates = {}
    for i in range(n):
        fates[base + i] = rng.choice(["accept", "veto", "veto_restored", "decline"])
    preexisting = [base + 50 + i for i in range(rng.randint(0, 3))]
    return fates, preexisting


def run_scenario(fates, preexisting, stamp="2026-07-16T00:00:00Z"):
    """Stage a batch per `fates`, seed bystanders, run the real apply_merged(), return the outcome."""
    with sandbox():
        # Bystanders already in opinions.json (must be preserved).
        pre_entries = [card_entry(c) for c in preexisting]
        if pre_entries:
            os.makedirs(os.path.dirname(update.JSON_PATH), exist_ok=True)
            json.dump(pre_entries, open(update.JSON_PATH, "w"))

        pending = set(fates)
        for cid, fate in fates.items():
            if fate in ("accept", "veto_restored"):
                review_store.stage_card(card_entry(cid), ["overrules or modifies an already-published card"])
            # 'veto' and 'decline' leave NO staged file (the human dropped it).
            if fate == "veto_restored":
                review_store.add_vetoed(cid)          # marker present though the file was restored
            if fate == "decline":
                review_store.add_declined(cid)
        review_store.save_pending(pending, stamp=stamp)

        # Pre-existing redraft ids, so we can tell which cids were NEWLY redraft-logged this run.
        redraft_before = review_store.load_redraft_ids()
        review_apply.apply_merged()
        return {"published": set(published_ids()), "seen": seen_ids(),
                "redraft_new": review_store.load_redraft_ids() - redraft_before,
                "pending_after": review_store.load_pending(),
                "published_list": published_ids(), "preexisting": set(preexisting)}


def assert_invariants(label, fates, out):
    blocked = {c for c, f in fates.items() if f in ("veto", "veto_restored", "decline")}
    accept = {c for c, f in fates.items() if f == "accept"}
    veto = {c for c, f in fates.items() if f in ("veto", "veto_restored")}
    decline = {c for c, f in fates.items() if f == "decline"}

    ok = True
    if blocked & out["published"]:
        check("%s: a vetoed/declined case is NEVER published" % label, False,
              "leaked %r" % (blocked & out["published"])); ok = False
    if not accept <= out["published"]:
        check("%s: every accepted card is published" % label, False,
              "missing %r" % (accept - out["published"])); ok = False
    if not accept <= out["seen"]:
        check("%s: every accepted card is marked seen" % label, False,
              "missing %r" % (accept - out["seen"])); ok = False
    if veto & out["seen"]:
        check("%s: a vetoed case is left un-seen" % label, False, "seen %r" % (veto & out["seen"])); ok = False
    if not veto <= out["redraft_new"]:
        check("%s: every vetoed case is redraft-logged" % label, False,
              "missing %r" % (veto - out["redraft_new"])); ok = False
    if not decline <= out["seen"]:
        check("%s: every declined case is marked seen" % label, False,
              "missing %r" % (decline - out["seen"])); ok = False
    if decline & out["redraft_new"]:
        check("%s: a declined case is NOT redraft-logged" % label, False,
              "redrafted %r" % (decline & out["redraft_new"])); ok = False
    if not out["preexisting"] <= out["published"]:
        check("%s: pre-existing cards are preserved" % label, False,
              "dropped %r" % (out["preexisting"] - out["published"])); ok = False
    if len(out["published_list"]) != len(set(out["published_list"])):
        check("%s: no duplicate cluster_id in opinions.json" % label, False,
              "%r" % out["published_list"]); ok = False
    if out["pending_after"]:
        check("%s: the pending ledger is cleared after apply" % label, False,
              "%r" % out["pending_after"]); ok = False
    return ok


def main():
    print("review lane stress (parse_command + real apply_merged state machine):")
    print("- parse_command:")
    test_parse_command()

    print("- apply_merged invariants (explicit cases):")
    # One of each fate together, plus a bystander.
    fates = {900100: "accept", 900101: "veto", 900102: "veto_restored", 900103: "decline"}
    out = run_scenario(fates, [900150])
    if assert_invariants("mixed batch", fates, out):
        check("mixed batch: accept published+seen, veto un-seen+redrafted, decline seen, restored-veto blocked", True)

    # Idempotency: a card already in opinions.json that is re-staged is not appended twice.
    with sandbox():
        json.dump([card_entry(900200)], open(update.JSON_PATH, "w"))
        review_store.stage_card(card_entry(900200), ["dup"])
        review_store.save_pending({900200}, stamp="2026-07-16T00:00:00Z")
        review_apply.apply_merged()
        pl = published_ids()
    check("idempotent: an already-carded re-staged case is not duplicated", pl.count(900200) == 1, "%r" % pl)

    print("- apply_merged invariants (randomized fuzz):")
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    rng = random.Random(20260716)
    bad = 0
    for it in range(iters):
        fates, pre = build_scenario(rng)
        try:
            out = run_scenario(fates, pre)
        except Exception as e:
            check("fuzz it=%d: apply_merged did not crash" % it, False, "%r on fates=%r" % (e, fates)); bad = 1; break
        if not assert_invariants("fuzz it=%d" % it, fates, out):
            bad = 1; break
    if not bad:
        check("fuzz: %d randomized review batches, all invariants held" % iters, True)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS[:20]))
        return 1
    print("\nALL REVIEW STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
