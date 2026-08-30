#!/usr/bin/env python3
"""Hermetic unit tests for scripts/mcp_feed.py -- the machine change feed behind the MCP server.

Standard library only; no network. Every test drives mcp_feed.build directly with fixture cards, so
nothing here depends on what happens to be in opinions.json today.

What these pin is the one idea the feed exists for: `changed` is max(first_seen, treatment_date), so
a 2023 opinion flagged as treated in 2026 shows up in a 2026 delta. If that regresses, the canary
silently stops reporting the thing it was built to report -- adverse treatment on authority someone
already relied on -- and every consumer keeps polling happily, seeing nothing, and concluding
nothing has happened. There is no louder failure to catch it.

Run directly: `python scripts/test_mcp_feed.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_feed  # noqa: E402  (sys.path shim must run first)

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


NEW_CARD = {
    "cluster_id": 111, "name": "Alpha v. Beta", "court": "ctapp", "date": "2026-08-01",
    "first_seen": "2026-08-01", "areas": ["premises"], "url": "https://cl/111",
    "why": "why it matters", "synopsis": "what happened", "disposition": "affirmed",
    "precedential": "published", "dockets": ["A26A0001"],
}
# The case the whole feed exists for: carded in 2023, flagged in 2026.
TREATED_CARD = {
    "cluster_id": 222, "name": "Aspen American Ins. Co. v. Landstar Ranger, Inc.", "court": "ca11",
    "date": "2023-04-13", "first_seen": "2023-04-13", "areas": ["coverage"],
    "url": "https://cl/222", "why": "duty to defend", "synopsis": "s",
    "treatment": "negative", "treatment_date": "2026-06-12",
    "treatment_note": "Overruled in part", "treated_by": [{"cluster_id": 333, "name": "Later"}],
}
OLD_CARD = {
    "cluster_id": 444, "name": "Gamma v. Delta", "court": "scotga", "date": "2025-01-05",
    "first_seen": "2025-01-05", "areas": ["damages", "auto"], "url": "https://cl/444", "why": "w",
}


def test_change_event():
    check("a fresh card changes when first seen",
          mcp_feed.change_event(NEW_CARD) == ("2026-08-01", "new"))
    check("a treated card changes when it was FLAGGED, not when decided",
          mcp_feed.change_event(TREATED_CARD) == ("2026-06-12", "treatment"))
    # The whole point, stated as its own check: a delta cursor set after the opinion's own date must
    # still catch it. Keying on `date` would return nothing here and the canary would be silent.
    changed, kind = mcp_feed.change_event(TREATED_CARD)
    check("a 2023 opinion flagged in 2026 is visible to a 2026 cursor",
          changed > "2026-01-01" and kind == "treatment")
    check("treatment older than first_seen does not win",
          mcp_feed.change_event({"first_seen": "2026-05-01", "treatment_date": "2024-01-01"})
          == ("2026-05-01", "new"))
    check("date substitutes for a missing first_seen",
          mcp_feed.change_event({"date": "2026-02-02"}) == ("2026-02-02", "new"))
    # A card with no usable date must NOT be dated today: it would then appear in every caller's
    # next poll forever, and each poll would report it as new again.
    check("a dateless card is empty, never now", mcp_feed.change_event({}) == ("", "new"))


def test_build_shape():
    doc = mcp_feed.build([NEW_CARD, TREATED_CARD, OLD_CARD], generated="2026-08-30T00:00:00Z")
    check("schema is declared", doc["schema"] == mcp_feed.SCHEMA)
    check("generated is carried", doc["generated"] == "2026-08-30T00:00:00Z")
    check("all cards present", doc["counts"]["cards"] == 3)
    check("treated cards are counted", doc["counts"]["treated"] == 1)
    ids = [c["cluster_id"] for c in doc["cards"]]
    check("newest change sorts first", ids == [111, 222, 444], "got %r" % (ids,))
    a = next(c for c in doc["cards"] if c["cluster_id"] == 222)
    check("treatment detail travels with the card",
          a["treatment"] == "negative" and a["treatment_note"] == "Overruled in part")
    check("treated_by travels too", a["treated_by"][0]["cluster_id"] == 333)
    check("why is carried -- it is the reason a model can act without a second call",
          doc["cards"][0]["why"] == "why it matters")
    check("source url is carried for verification", doc["cards"][0]["url"] == "https://cl/111")


def test_area_counts_are_the_denominator():
    """A thin area answered without its count is the same failure as a confident drop reason."""
    doc = mcp_feed.build([NEW_CARD, TREATED_CARD, OLD_CARD])
    by = doc["counts"]["by_area"]
    check("counts every area a card claims",
          by == {"auto": 1, "coverage": 1, "damages": 1, "premises": 1}, "got %r" % (by,))
    check("a card with two areas counts in both", by["damages"] == 1 and by["auto"] == 1)
    check("counts sort by size", list(mcp_feed.area_counts(
        [{"areas": ["a"]}, {"areas": ["b"]}, {"areas": ["b"]}]))[0] == "b")
    check("no areas yields an empty map", mcp_feed.area_counts([{}]) == {})


def test_watches():
    leg = [{"bill_id": 1, "number": "HB 1", "title": "T", "first_seen": "2026-07-01",
            "change_hash": "deadbeef", "areas": ["auto"]}]
    cr = [{"id": "fr1", "rule": "26", "first_seen": "2026-06-01", "summary": "s"}]
    doc = mcp_feed.build([], leg, cr)
    kinds = sorted(w["change"] for w in doc["watches"])
    check("watches carry their kind", kinds == ["courtrule", "legislation"])
    check("watches are delta-shaped too", doc["watches"][0]["changed"] == "2026-07-01")
    check("newest watch first", doc["watches"][0]["change"] == "legislation")
    check("the internal dedupe hash is not published",
          all("change_hash" not in w for w in doc["watches"]))
    check("watches are counted", doc["counts"]["watches"] == 2)


def test_build_is_total():
    """A render must never fail because a card is odd; a feed that fails to write is a silent
    outage for every routine polling it."""
    doc = mcp_feed.build([{}, {"cluster_id": 9}], generated="")
    check("a bare card still yields an entry", doc["counts"]["cards"] == 2)
    check("empty inputs are fine", mcp_feed.build([])["counts"]["cards"] == 0)
    check("no watches is fine", mcp_feed.build([NEW_CARD])["watches"] == [])
    check("empty and None fields are dropped, not emitted as null",
          "editor_note" not in mcp_feed.build([NEW_CARD])["cards"][0])


def main():
    print("change semantics:")
    test_change_event()
    print("feed shape:")
    test_build_shape()
    print("denominator:")
    test_area_counts_are_the_denominator()
    print("watches:")
    test_watches()
    print("totality:")
    test_build_is_total()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
