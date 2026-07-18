#!/usr/bin/env python3
"""Hermetic unit test for the funnel dead-man's-switch (scripts/heartbeat.py), no network.

heartbeat.check() is the only alert path that fires when the funnel STOPS running (every other
alert needs a workflow to run and fail). So its exit codes are load-bearing: 3 = funnel stalled
(status.json stale/missing -> open issue), 4 = content stale (no new cards -> open issue), 0 =
healthy (close issues + ping the external monitor). A regression that made a stall parse as
healthy would silently swallow the one signal we have. This drives every branch against synthetic
status.json/opinions.json files in a temp dir, with a pinned "now", so it needs no clock or disk
state of its own.

Run directly: `python scripts/test_heartbeat.py`.
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heartbeat  # noqa: E402

FAILS = []
NOW = datetime.datetime(2026, 7, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def run(status, cards, scan_hours=48.0, content_days=30.0):
    """Point heartbeat's module paths at synthetic files with a pinned clock, restoring after.

    status: dict written as public/status.json, or None to omit the file entirely.
    cards:  list written as opinions.json.
    Returns the exit code from check().
    """
    saved = (heartbeat.STATUS_PATH, heartbeat.JSON_PATH, heartbeat.ALERT_PATH,
             heartbeat._now, heartbeat.SCAN_HOURS, heartbeat.CONTENT_DAYS)
    with tempfile.TemporaryDirectory() as d:
        sp = os.path.join(d, "status.json")
        jp = os.path.join(d, "opinions.json")
        if status is not None:
            with open(sp, "w") as f:
                json.dump(status, f)
        with open(jp, "w") as f:
            json.dump(cards, f)
        heartbeat.STATUS_PATH = sp
        heartbeat.JSON_PATH = jp
        heartbeat.ALERT_PATH = os.path.join(d, "alert.md")
        heartbeat._now = lambda: NOW
        heartbeat.SCAN_HOURS = scan_hours
        heartbeat.CONTENT_DAYS = content_days
        try:
            return heartbeat.check()
        finally:
            (heartbeat.STATUS_PATH, heartbeat.JSON_PATH, heartbeat.ALERT_PATH,
             heartbeat._now, heartbeat.SCAN_HOURS, heartbeat.CONTENT_DAYS) = saved


def stamp(days_ago, with_time=True):
    t = NOW - datetime.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if with_time else t.strftime("%Y-%m-%d")


def main():
    print("heartbeat exit codes:")

    # Healthy: fresh scan and a recent card -> exit 0.
    code = run({"scanned_at": stamp(0.1)}, [{"first_seen": stamp(2, with_time=False)}])
    check("healthy funnel + fresh content -> 0", code == 0)

    # Funnel stalled: scanned_at older than the 48h threshold -> exit 3.
    code = run({"scanned_at": stamp(3)}, [{"first_seen": stamp(1, with_time=False)}])
    check("stale scanned_at -> 3", code == 3, "got %r" % code)

    # Missing status.json entirely (scan-status step never ran) -> treated as funnel-stale, exit 3.
    code = run(None, [{"first_seen": stamp(1, with_time=False)}])
    check("missing status.json -> 3", code == 3, "got %r" % code)

    # status.json present but scanned_at unparseable -> funnel-stale, exit 3.
    code = run({"scanned_at": "not-a-date"}, [{"first_seen": stamp(1, with_time=False)}])
    check("unparseable scanned_at -> 3", code == 3, "got %r" % code)

    # Funnel running but newest card older than the 30d content threshold -> exit 4.
    code = run({"scanned_at": stamp(0.1)}, [{"first_seen": stamp(45, with_time=False)}])
    check("fresh scan but stale content -> 4", code == 4, "got %r" % code)

    # Funnel-stall (3) must win over content-stall (4) when both are true: the scan check runs first.
    code = run({"scanned_at": stamp(5)}, [{"first_seen": stamp(45, with_time=False)}])
    check("both stale -> 3 (funnel signal wins)", code == 3, "got %r" % code)

    # An empty opinions.json with a fresh scan is healthy (no cards yet != stalled) -> exit 0.
    code = run({"scanned_at": stamp(0.1)}, [])
    check("fresh scan + empty opinions.json -> 0", code == 0, "got %r" % code)

    # opinions.json that isn't a list of cards (corrupt / reshaped) must NOT pass silently: the
    # freshness net reads this file, so a malformed payload is content-stale, not OK -> exit 4.
    code = run({"scanned_at": stamp(0.1)}, {"reshaped": "not a list"})
    check("fresh scan + non-list opinions.json -> 4", code == 4, "got %r" % code)

    # Entries present but NONE carries a parseable date (e.g. the date format drifted) -> exit 4,
    # not a silent OK (the bug: a None `newest` skipped the check and returned 0).
    code = run({"scanned_at": stamp(0.1)}, [{"first_seen": "07/18/2026"}, {"date": "n/a"}])
    check("entries with no parseable date -> 4", code == 4, "got %r" % code)

    # A bare-date scanned_at (no time component) must still parse -- this is the slice-width bug
    # the first cut had: keying the slice to "T in ts" left the date form unreachable.
    code = run({"scanned_at": stamp(0.1, with_time=False)}, [{"first_seen": stamp(2, with_time=False)}])
    check("bare-date scanned_at parses -> 0", code == 0, "got %r" % code)

    # The threshold is honored from env-equivalent overrides (params here): tighten content to 1d.
    code = run({"scanned_at": stamp(0.1)}, [{"first_seen": stamp(2, with_time=False)}], content_days=1.0)
    check("content threshold override tightens to 4", code == 4, "got %r" % code)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (11 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
