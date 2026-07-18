#!/usr/bin/env python3
"""Invariant tests for scripts/jurisdictions.py. Standard library only; no network.

These guard a QUIET failure: the opinion funnel queries CourtListener for every court id in
COURTS_ALL, then at card time maps that id through COURT_MAP to an internal key. A queried court
whose id is absent from COURT_MAP (add a court, forget the map) is silently dropped -- AFTER the
opinion has been screened, triaged, and summarized by Opus -- landing in the run log's "other"
bucket with no alert. The set relationship below is the assumption that keeps that from happening;
holding it in a test makes the coupling explicit instead of implicit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jurisdictions as J  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("jurisdictions invariants:")
    court_map = dict(J.COURT_MAP)
    courts_all = set(J.COURTS_ALL)
    valid_keys = set(J.VALID_KEYS)

    # Every court the funnel QUERIES must be mappable, or its opinions are silently dropped at
    # card time after full (expensive) processing.
    missing = courts_all - set(court_map)
    check("every COURTS_ALL id is in COURT_MAP (no silent post-summary drop)",
          not missing, "unmapped queried courts: %s" % sorted(missing))

    # Every mapped internal key must be a recognized key, or build_card's fallback validation
    # (v.get('court') in VALID_KEYS) is the only thing standing between a typo and a dropped card.
    bad = {cid: k for cid, k in court_map.items() if k not in valid_keys}
    check("every COURT_MAP value is a VALID_KEY",
          not bad, "map values outside VALID_KEYS: %s" % bad)

    check("COURTS_ALL is non-empty (the funnel has courts to query)", bool(courts_all))
    check("COURT_MAP is non-empty", bool(court_map))

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
