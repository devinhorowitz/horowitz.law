#!/usr/bin/env python3
"""Hermetic unit test for backfill's card drafting (scripts/backfill.py).

Covers the two pieces the Batch API integration rests on, with no network and no API key:
  * _assemble_card_row: a summary with the required fields becomes a card; one missing a
    required field becomes a "no-card" row (SEED is pre-vetted, so relevance never gates).
  * _draft_cards: the synchronous path (stubbing update.summarize), the batch path
    (stubbing batch.run to return results keyed by "<cid>"), a per-request failure that does
    not sink the rest, and a batch timeout that defers the whole set.

Run directly: `python scripts/test_backfill.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill      # noqa: E402  (sys.path shim must run first)
import update        # noqa: E402
import batch         # noqa: E402

FAILS = []
AREA = next(iter(update.VALID_AREAS))          # a valid practice-area code
COURT_ID = next(iter(update.COURT_MAP))        # a CourtListener court id that maps to an internal key


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def pending(cid=1, name="Alpha v. X"):
    return {"cid": cid, "court_id": COURT_ID, "name": name, "docket": "D%d" % cid,
            "date_filed": "2024-01-02", "url": "https://cl/%d" % cid, "text": "OPINION",
            "note": "n", "cl_status": "Published", "src": "pdf",
            "s": {"pass": True, "reason": ""}, "t": {"relevant": True, "significance": "high", "note": ""}}


def summary_card():
    return {"areas": [AREA], "court": None, "dockets": ["D1"], "name": "Alpha v. X",
            "synopsis": "The court reversed on the merits.", "why": "It matters.",
            "relevant": True, "significance": "high"}


def main():
    # A stub assemble_entry so a "card" verdict is observable without building a real card.
    update.assemble_entry = lambda *a, **k: {"cluster_id": a[1], "_card": True}

    # 1. _assemble_card_row: full fields -> a card.
    row, card = backfill._assemble_card_row(summary_card(), pending())
    check("assemble: full summary -> status card", row.get("status") == "card")
    check("assemble: full summary -> a card is built", card is not None and card.get("_card") is True)

    # 2. _assemble_card_row: missing a required field (no areas) -> no-card, no card built.
    v_missing = dict(summary_card(), areas=[])
    row2, card2 = backfill._assemble_card_row(v_missing, pending())
    check("assemble: missing area -> status no-card", row2.get("status") == "no-card")
    check("assemble: missing area -> no card built", card2 is None)
    check("assemble: records the problem", "no valid practice area" in row2.get("problems", []))

    # 3. _draft_cards synchronous (BACKFILL_BATCH off): update.summarize drafts each card.
    backfill.BATCH = False
    update.summarize = lambda *a, **k: summary_card()
    rows, cards, deferred = backfill._draft_cards([pending(1), pending(2, "Beta v. Y")])
    check("sync draft: both drafted as cards", [r["status"] for r in rows] == ["card", "card"])
    check("sync draft: two cards, none deferred", len(cards) == 2 and deferred == 0)

    # 4. _draft_cards batch success: one job, results keyed by "<cid>".
    backfill.BATCH = True
    def _run_ok(reqs, deadline=None, interval=20.0, label="batch"):
        _run_ok.n = len(reqs)
        return {"1": {"ok": True, "text": json.dumps(summary_card())},
                "2": {"ok": True, "text": json.dumps(dict(summary_card(), areas=[]))}}  # card, then no-card
    batch.run = _run_ok
    rows, cards, deferred = backfill._draft_cards([pending(1), pending(2, "Beta v. Y")])
    check("batch draft: one request per pending cluster", _run_ok.n == 2)
    check("batch draft: card + no-card statuses", sorted(r["status"] for r in rows) == ["card", "no-card"])
    check("batch draft: one card built, none deferred", len(cards) == 1 and deferred == 0)

    # 5. _draft_cards batch with a per-request failure (ok=False): error row, rest unaffected.
    def _run_partial(reqs, deadline=None, interval=20.0, label="batch"):
        return {"1": {"ok": False, "type": "errored"},
                "2": {"ok": True, "text": json.dumps(summary_card())}}
    batch.run = _run_partial
    rows, cards, deferred = backfill._draft_cards([pending(1), pending(2, "Beta v. Y")])
    check("batch draft: a failed line -> error row", sorted(r["status"] for r in rows) == ["card", "error"])
    check("batch draft: the good line still cards", len(cards) == 1)

    # 6. _draft_cards batch timeout: the whole (already-fetched) set defers, nothing drafted.
    def _run_timeout(reqs, deadline=None, interval=20.0, label="batch"):
        raise batch.BatchTimeout("bid_x", "not finished")
    batch.run = _run_timeout
    rows, cards, deferred = backfill._draft_cards([pending(1), pending(2, "Beta v. Y")])
    check("batch timeout: defers the whole set", rows == [] and cards == [] and deferred == 2)

    backfill.BATCH = False
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (13 cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
