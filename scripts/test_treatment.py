#!/usr/bin/env python3
"""Hermetic unit tests for treatment.py's pure logic (no network, no API key).

Covers the citation-window text extractor (passage) and, most importantly, the
full-history-vs-incremental sweep decision (sweep_since / swept_full) -- the state
machine behind the fix for a card whose oid is resolved but whose citer search is
cut short by a budget stop: it must NOT be marked fully swept, so the next run
redoes the full-history search instead of dropping to the 200-day window.

Run directly: `python scripts/test_treatment.py`.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import treatment      # noqa: E402  (sys.path shim must run first)

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def test_passage():
    check("empty text -> empty", treatment.passage("", "Smith v. Jones") == "")
    # No party surname present -> falls back to the opening, capped at MAXCHARS.
    body = "a plain paragraph with no matching party names in it at all"
    check("no-match falls back to opening", treatment.passage(body, "Smith v. Jones") == body)
    # Surname present -> a window around it that includes the surname, still capped.
    doc = ("x" * 3000) + " Smith " + ("y" * 3000)
    p = treatment.passage(doc, "Smith v. Jones")
    check("match returns a window containing the surname", "Smith" in p)
    check("window never exceeds MAXCHARS", len(p) <= treatment.MAXCHARS)
    # A very long opening is truncated to MAXCHARS even on the fallback path.
    check("fallback is truncated to MAXCHARS",
          len(treatment.passage("z" * (treatment.MAXCHARS + 5000), "No v. Match")) == treatment.MAXCHARS)


def test_sweep_since():
    card = {"date": "2024-01-15"}
    today = datetime.date(2026, 7, 10)
    # Until a full pass has completed, search from the card's own date (nothing older missed).
    check("not-yet-full searches from card date",
          treatment.sweep_since(card, False, today=today) == "2024-01-15")
    # After a full pass, only the cheap incremental window.
    expect = (today - datetime.timedelta(days=treatment.LOOKBACK_DAYS)).isoformat()
    check("full uses the LOOKBACK_DAYS window",
          treatment.sweep_since(card, True, today=today) == expect)
    check("incremental window is strictly newer than the card date",
          treatment.sweep_since(card, True, today=today) > card["date"])


def test_swept_full():
    # The core of the fix: a stop on a not-yet-full card must leave it not-full.
    check("completed pass on a fresh card -> full", treatment.swept_full(False, "") is True)
    check("stopped pass on a fresh card -> NOT full (redo next run)",
          treatment.swept_full(False, "rest budget") is False)
    check("already-full card stays full after a stop", treatment.swept_full(True, "time budget") is True)
    check("already-full card stays full on a clean run", treatment.swept_full(True, "") is True)


def main():
    print("treatment pure logic:")
    test_passage()
    test_sweep_since()
    test_swept_full()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 12)
    return 0


if __name__ == "__main__":
    sys.exit(main())
