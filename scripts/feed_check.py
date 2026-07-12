#!/usr/bin/env python3
"""Feed-shape canary for the CourtListener Atom feeds the funnel discovers from.

The funnel finds new opinions by parsing each court's Atom feed (update._parse_feed). If
CourtListener ever changes that feed's shape -- renames a field, changes the `/opinion/<id>/`
link pattern the parser keys on, restructures the entries -- the parser silently yields nothing
and the funnel publishes zero cards with NO error. The heartbeat eventually notices (content
stale after 30 days), but by design this catches it SAME-DAY: it fetches each feed, counts the
raw <entry> elements, runs the REAL parser over them, and flags the one unambiguous drift
signature -- entries are present but the parser extracts no candidates from ANY court.

Only that signature hard-alerts, because it cannot false-positive: the same parser feeds every
court, so a real quiet stretch still leaves the parser working on whatever entries exist, while a
format change zeroes the yield across the board even though entries are there. An all-empty or
all-unreachable result is left as a soft notice (a quiet weekend or a transient CL outage looks
identical, and the heartbeat backstops a total format change that produces no <entry> at all).

Feeds are the free `/feed/` path -- no REST quota, no token. Exit codes, consumed by the workflow:
  0  healthy   at least one court's feed yields candidates -- the parser works.
  3  DRIFT     feeds have entries but the parser extracted zero across every court with entries.
  4  soft      every feed was empty or unreachable -- not proof of drift; log a notice, no issue.

Run: `python scripts/feed_check.py`. Importable for tests (assess() takes injectable fetch/parse).
"""
import os
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # COURTS, feed_get, _parse_feed, ATOM

ALERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_check_alert.md")


def _fetch(court):
    return update.feed_get("https://www.courtlistener.com/feed/court/%s/" % court)


def _count_entries(raw):
    """Number of <entry> elements in the feed. Raises on non-XML (caught by assess as unreachable)."""
    return len(ET.fromstring(raw).findall(update.ATOM + "entry"))


def assess(courts, fetch=_fetch, parse=None, count=_count_entries):
    """Fetch and shape-check each court's feed. fetch(court)->bytes, parse(raw,court)->candidates,
    count(raw)->int; all injectable so tests need no network. Returns (code, rows, totals) where
    rows is per-court (court, status, entries, parsed, note) and totals is (raw, parsed, reachable)."""
    parse = parse or update._parse_feed
    rows, total_raw, total_parsed, reachable = [], 0, 0, 0
    for c in courts:
        try:
            raw = fetch(c)
        except Exception as e:
            rows.append((c, "unreachable", 0, 0, str(e)[:80]))
            continue
        try:
            n_entries = count(raw)
        except Exception as e:
            # Not valid Atom XML -- most often a transient CL error page, not a permanent format
            # change; treated as soft (unreachable), and the heartbeat backstops a lasting break.
            rows.append((c, "unparseable-xml", 0, 0, str(e)[:80]))
            continue
        reachable += 1
        try:
            n_parsed = len(parse(raw, c))
            note = ""
        except Exception as e:
            n_parsed, note = 0, "parse raised: " + str(e)[:60]
        total_raw += n_entries
        total_parsed += n_parsed
        status = "ok" if n_parsed else ("empty" if not n_entries else "no-candidates")
        rows.append((c, status, n_entries, n_parsed, note))
    return _code(total_raw, total_parsed), rows, (total_raw, total_parsed, reachable)


def _code(total_raw, total_parsed):
    if total_parsed > 0:
        return 0            # parser works somewhere -> healthy
    if total_raw > 0:
        return 3            # entries exist but nothing parsed anywhere -> drift
    return 4                # nothing to assess (all empty/unreachable) -> soft


def _write_alert(body):
    try:
        with open(ALERT_PATH, "w", encoding="utf-8") as f:
            f.write(body + "\n")
    except OSError:
        pass


def main():
    courts = list(update.COURTS)
    code, rows, (raw, parsed, reachable) = assess(courts)
    line = "feed_check: %d/%d courts reachable; %d raw entries, %d parsed candidates" % (
        reachable, len(courts), raw, parsed)
    print(line)
    for c, status, ne, np_, note in rows:
        print("  %-14s %-16s entries=%-4d parsed=%-4d %s" % (c, status, ne, np_, note))
    if code == 3:
        drifted = ", ".join("%s(%d entries)" % (c, ne) for c, s, ne, np_, n in rows if ne and not np_)
        body = ("Feed-shape DRIFT: the CourtListener court feeds returned **%d entries** but the "
                "funnel's parser (`update._parse_feed`) extracted **0 candidates** from any court "
                "(%s). This is how a feed-format change looks -- a renamed field or a changed "
                "`/opinion/<id>/` link pattern -- and it silently stops all discovery. Compare a live "
                "feed (e.g. https://www.courtlistener.com/feed/court/%s/) against `feed_court`/"
                "`_parse_feed` in scripts/update.py and update the parser." % (raw, drifted, courts[0]))
        print("::error::" + line)
        _write_alert(body)
    elif code == 4:
        _write_alert("Feed-shape check: every court feed was empty or unreachable (%d/%d reachable). "
                     "Likely a quiet stretch or a transient CourtListener outage, not proof of drift; "
                     "the heartbeat backstops a lasting break." % (reachable, len(courts)))
        print("::notice::feed_check: no entries to assess (empty or unreachable); not alerting")
    return code


if __name__ == "__main__":
    sys.exit(main())
