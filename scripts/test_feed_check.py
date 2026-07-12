#!/usr/bin/env python3
"""Hermetic unit test for the feed-shape canary (scripts/feed_check.py), no network.

feed_check.assess() runs the funnel's REAL feed parser (update._parse_feed) over each court's feed
and must fire exit 3 only on the unambiguous drift signature -- entries present, zero candidates
parsed across every court -- while staying quiet on a genuinely empty feed or one healthy court. It
takes an injectable fetch, so these drive synthetic Atom feeds (a healthy one, a drifted one whose
`/opinion/<id>/` link pattern changed, an empty one) straight through the production parser.

Run directly: `python scripts/test_feed_check.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feed_check  # noqa: E402
import update       # noqa: E402

FAILS = []

HEALTHY = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Alpha v. Beta</title>
    <link rel="alternate" href="https://www.courtlistener.com/opinion/12345/alpha-v-beta/"/>
    <published>2026-07-01T00:00:00Z</published>
    <summary>Opinion text. Case No. A26A0123.</summary>
  </entry>
</feed>'''

# Same feed, but CL changed the opinion link pattern (/opinion/ -> /decision/): entries are still
# there, but _parse_feed's `/opinion/(\d+)/` extraction matches nothing, so it yields 0 candidates.
DRIFTED = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Alpha v. Beta</title>
    <link rel="alternate" href="https://www.courtlistener.com/decision/12345/alpha-v-beta/"/>
    <published>2026-07-01T00:00:00Z</published>
    <summary>Opinion text.</summary>
  </entry>
  <entry>
    <title>Gamma v. Delta</title>
    <link rel="alternate" href="https://www.courtlistener.com/decision/12346/gamma-v-delta/"/>
    <published>2026-07-02T00:00:00Z</published>
    <summary>Opinion text.</summary>
  </entry>
</feed>'''

EMPTY = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Court feed</title></feed>'''

NOT_XML = b'503 Service Unavailable'   # plain text -> ET.fromstring raises ParseError


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def stub(mapping):
    def fetch(court):
        v = mapping[court]
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


COURTS = ["gactapp", "ga", "ca11"]


def main():
    print("feed-shape canary:")

    # Pure code mapping.
    check("_code: entries + parsed -> 0", feed_check._code(5, 3) == 0)
    check("_code: entries, zero parsed -> 3 (drift)", feed_check._code(5, 0) == 3)
    check("_code: nothing -> 4 (soft)", feed_check._code(0, 0) == 4)

    # All healthy -> 0, candidates parsed.
    code, rows, (raw, parsed, reach) = feed_check.assess(COURTS, fetch=stub({c: HEALTHY for c in COURTS}))
    check("all-healthy -> exit 0", code == 0, "got %d" % code)
    check("all-healthy: candidates parsed", parsed == len(COURTS) and raw == len(COURTS))

    # All drifted (entries present, /opinion/ pattern changed) -> 3.
    code, rows, (raw, parsed, reach) = feed_check.assess(COURTS, fetch=stub({c: DRIFTED for c in COURTS}))
    check("all-drifted -> exit 3", code == 3, "got %d" % code)
    check("all-drifted: raw entries seen but zero parsed", raw == 2 * len(COURTS) and parsed == 0)
    check("all-drifted: rows flag no-candidates", all(s == "no-candidates" for _, s, ne, np_, _ in rows if ne))

    # One healthy court among drifted -> exit 0: the parser demonstrably still works, so this is
    # NOT drift (a real format change would zero every court's yield).
    m = {c: DRIFTED for c in COURTS}; m[COURTS[0]] = HEALTHY
    code, _, (raw, parsed, _) = feed_check.assess(COURTS, fetch=stub(m))
    check("one-healthy-among-drifted -> exit 0 (no false positive)", code == 0 and parsed == 1)

    # All empty -> 4 (quiet, not drift).
    code, _, (raw, parsed, _) = feed_check.assess(COURTS, fetch=stub({c: EMPTY for c in COURTS}))
    check("all-empty -> exit 4", code == 4 and raw == 0)

    # All unreachable -> 4 (transient outage, not drift).
    code, rows, (_, _, reach) = feed_check.assess(COURTS, fetch=stub({c: OSError("boom") for c in COURTS}))
    check("all-unreachable -> exit 4", code == 4 and reach == 0)
    check("unreachable rows recorded", all(s == "unreachable" for _, s, _, _, _ in rows))

    # A non-XML error page is treated as soft (unreachable), not drift.
    code, rows, _ = feed_check.assess(COURTS, fetch=stub({c: NOT_XML for c in COURTS}))
    check("non-XML response -> exit 4 (soft, not drift)", code == 4)
    check("non-XML rows flag unparseable-xml", all(s == "unparseable-xml" for _, s, _, _, _ in rows))

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
