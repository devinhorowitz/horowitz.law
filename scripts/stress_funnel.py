#!/usr/bin/env python3
"""Integration stress harness: drive the REAL funnel update.main() in a stubbed sandbox.

Closes the one gap the batch-orchestration fuzzer (stress_batch.py) could not reach: the in-run dedup
SEEDING that only matters inside main(). In batch mode nothing is carded mid-loop, so a later
same-run twin (same court + shared docket) must be caught by the dedup index seeded at collection
time; a bug there would publish duplicate cards. This proves it -- and, more broadly, that the batch
path (OPINIONS_BATCH on) produces the SAME published cards as the synchronous path.

Everything is stubbed: no network, no API key, no repo writes. Every path constant is redirected to a
tempdir and every model/CourtListener seam is replaced, then update.main() runs for real and we
capture the `added` list it hands to route_and_publish.

Run directly: `python scripts/stress_funnel.py`. Exits nonzero on any failure.
"""
import contextlib
import datetime
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch      # noqa: E402
import official_ga  # noqa: E402
import review_store  # noqa: E402
import update     # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


COURT = update.COURTS[0]                       # a real CL court id the feed iterates
AREA = next(iter(update.VALID_AREAS))          # a real practice-area code render accepts
TODAY = datetime.date.today()
RECENT = (TODAY - datetime.timedelta(days=3)).isoformat()

VERDICT = {"relevant": True, "significance": "high", "areas": [AREA],
           "court": update.COURT_MAP.get(COURT), "synopsis": "A holding.", "why": "It matters.",
           "disposition": "affirmed", "confidence": "high", "additional_holdings": []}


def cand(cid, docket, name):
    return {"cluster_id": cid, "caseName": name, "court_id": COURT, "docketNumber": docket,
            "dateFiled": RECENT, "absolute_url": "/opinion/%d/x/" % cid,
            "pdf_url": "https://x/%d.pdf" % cid, "snippet": "snippet"}


@contextlib.contextmanager
def sandbox(candidates, batch_mode):
    """Redirect update's paths to a tempdir and stub every network/model seam; yield a dict that
    captures the `added` cards route_and_publish receives. Restores everything on exit."""
    tmp = tempfile.mkdtemp(prefix="funnel-stress-")
    saved = {}

    def setattr_saved(obj, name, val):
        saved[(id(obj), name)] = (obj, name, getattr(obj, name))
        setattr(obj, name, val)

    # Redirect every file the run reads or writes into the tempdir; seed an empty feed + state.
    for const in ("JSON_PATH", "STATE_PATH", "LOG_PATH", "FABLE_LOG_PATH", "REJECT_PATH",
                  "SA_MANIFEST_PATH", "SA_STATE_PATH", "PR_PATH", "AUTO_PR_PATH", "REVIEW_PR_PATH"):
        setattr_saved(update, const, os.path.join(tmp, const.lower() + ".json"))
    with open(update.JSON_PATH, "w") as f:
        json.dump([], f)                       # no existing cards: isolate in-run dedup from prior state

    captured = {"added": None}

    def fake_route(added, *a, **k):
        captured["added"] = [e["cluster_id"] for e in added]
        return {"noop": not added, "auto": len(added), "held": 0, "treatments": 0, "wrote_auto": bool(added)}

    feed = {"served": False}

    def fake_feed(court, deadline=None):
        if court == COURT and not feed["served"]:
            feed["served"] = True
            return list(candidates)
        return []

    setattr_saved(update, "FUNNEL_BATCH", batch_mode)
    setattr_saved(update, "KEY", "test-key")
    setattr_saved(update.time, "sleep", lambda *a, **k: None)   # the loop paces with sleep(0.4); skip it
    setattr_saved(update, "anthropic_status", lambda: ("operational", "ok"))
    setattr_saved(update, "feed_court", fake_feed)
    setattr_saved(update, "pdf_text", lambda *a, **k: ("Opinion body. " * 200))   # passes _pdf_ok
    setattr_saved(update, "screen", lambda *a, **k: {"pass": True})
    setattr_saved(update, "pretriage", lambda *a, **k: {"pass": True})
    setattr_saved(update, "triage", lambda *a, **k: {"relevant": True, "significance": "high", "note": "", "treats": []})
    setattr_saved(update, "summarize", lambda *a, **k: dict(VERDICT))
    setattr_saved(update, "enriched", lambda *a, **k: {})
    setattr_saved(update, "cluster_precedential_status", lambda *a, **k: "published")
    setattr_saved(update, "crosscheck", lambda *a, **k: {})
    setattr_saved(update, "completeness_check", lambda *a, **k: {})
    setattr_saved(update, "official_download_url", lambda *a, **k: "")
    setattr_saved(official_ga, "official_url_for", lambda *a, **k: "")
    setattr_saved(update, "fable_review_pass", lambda *a, **k: ([], {}))
    setattr_saved(update, "route_and_publish", fake_route)
    setattr_saved(review_store, "load_pending", lambda *a, **k: set())
    setattr_saved(review_store, "load_redraft_ids", lambda *a, **k: set())

    # Batch mode: stub the summarize batch to return one verdict per pending candidate, keyed by cid.
    def fake_run(reqs, deadline=None, interval=20.0, label="batch"):
        return {rq["custom_id"]: {"ok": True, "text": json.dumps(VERDICT), "stop_reason": "end_turn"}
                for rq in reqs}
    setattr_saved(batch, "run", fake_run)

    try:
        yield captured
    finally:
        for obj, name, val in saved.values():
            setattr(obj, name, val)


def run_funnel(candidates, batch_mode):
    with sandbox(candidates, batch_mode) as captured:
        update.main()
    return set(captured["added"] or [])


def main():
    print("funnel integration stress (real main(), stubbed I/O):")

    # 1. Parity on distinct candidates: sync and batch publish the same set, all of them.
    distinct = [cand(2001, "A26A0001", "Alpha v. Beta"),
                cand(2002, "A26A0002", "Gamma v. Delta"),
                cand(2003, "A26A0003", "Epsilon v. Zeta")]
    sync = run_funnel(distinct, batch_mode=False)
    bat = run_funnel(distinct, batch_mode=True)
    check("distinct: sync cards all three", sync == {2001, 2002, 2003}, repr(sync))
    check("distinct: batch cards all three", bat == {2001, 2002, 2003}, repr(bat))
    check("distinct: sync == batch (parity)", sync == bat)

    # 2. In-run twin (same court + shared docket, different cluster id): exactly one cards, in BOTH
    #    modes -- the batch-mode case is the one the dedup-index seeding exists for.
    twins = [cand(3001, "A26A9999", "Owner v. Insurer"),
             cand(3002, "A26A9999", "Owner v. Insurer (corrected republish)"),   # twin of 3001
             cand(3003, "A26A0100", "Unrelated v. Case")]
    sync = run_funnel(twins, batch_mode=False)
    bat = run_funnel(twins, batch_mode=True)
    check("twin: sync dedups to two cards", len(sync) == 2 and 3003 in sync, repr(sync))
    check("twin: batch dedups to two cards (seeding works)", len(bat) == 2 and 3003 in bat, repr(bat))
    check("twin: sync == batch (same case survives in both)", sync == bat, "%r vs %r" % (sync, bat))
    check("twin: batch never publishes both twins", not ({3001, 3002} <= bat))

    # 3. A larger mixed run: several distinct + a twin pair; parity holds and no duplicate cluster ids.
    many = [cand(4000 + i, "A26B%04d" % i, "Case %d v. State" % i) for i in range(8)]
    many.append(cand(4999, "A26B0003", "Case 3 v. State (twin)"))   # twin of 4003
    sync = run_funnel(many, batch_mode=False)
    bat = run_funnel(many, batch_mode=True)
    check("mixed: sync == batch (parity)", sync == bat, "%r vs %r" % (sync, bat))
    check("mixed: the twin pair collapses to one in batch", len({4003, 4999} & bat) == 1, repr(bat & {4003, 4999}))
    check("mixed: batch cards the 8 distinct + one of the twin pair", len(bat) == 8, "%d" % len(bat))

    # 4. Randomized fuzz: many runs of random candidate sets with random in-run twins. Every run must
    #    hold sync==batch parity and emit no duplicate cluster ids -- the invariants that break if the
    #    batch-mode dedup seeding is ever wrong.
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rng = random.Random(20260716)
    worst = 0
    for it in range(iters):
        n = rng.randint(1, 9)
        cands, dockets = [], []
        for j in range(n):
            cid = 10000 + it * 100 + j
            # ~35% chance this candidate reuses an earlier docket in this run -> an in-run twin.
            if dockets and rng.random() < 0.35:
                dk = rng.choice(dockets)
            else:
                dk = "R%02dD%03d" % (it % 100, j)
                dockets.append(dk)
            cands.append(cand(cid, dk, "Party%d v. State" % j))
        rng.shuffle(cands)
        s = run_funnel(cands, batch_mode=False)
        b = run_funnel(cands, batch_mode=True)
        if s != b:
            check("fuzz run %d: sync == batch" % it, False, "%r vs %r" % (s, b))
        # No two published cards may share a docket (a slipped in-run twin).
        pub_dockets = [c["docketNumber"] for c in cands if c["cluster_id"] in b]
        if len(pub_dockets) != len(set(pub_dockets)):
            check("fuzz run %d: batch published a duplicate docket" % it, False, repr(sorted(pub_dockets)))
        worst = max(worst, n)
    check("fuzz: %d random runs, all sync==batch parity, no duplicate dockets (max %d candidates/run)"
          % (iters, worst), True)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL FUNNEL STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
