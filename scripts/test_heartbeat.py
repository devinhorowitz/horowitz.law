#!/usr/bin/env python3
"""Hermetic unit test for the funnel dead-man's-switch (scripts/heartbeat.py), no network.

heartbeat.check() is the only alert path that fires when the funnel STOPS running (every other
alert needs a workflow to run and fail). So its exit codes are load-bearing: 3 = funnel stalled
(status.json stale/missing -> open issue), 4 = content stale (no new cards -> open issue), 0 =
healthy (close issues + ping the external monitor). A regression that made a stall parse as
healthy would silently swallow the one signal we have. This drives every branch against synthetic
status.json/opinions.json files in a temp dir, with a pinned "now", so it needs no clock or disk
state of its own.

skill_manifest_check() is covered the same way (5 = stale, 0 = fresh/absent/off). Its branches are
easy to get backwards in the direction that hurts: an ABSENT manifest must read as 0, because a
deployment that never adopted the skill-authority watch must not get a permanent issue about a file
it does not want -- while a manifest that is present but UNDATABLE must read as 5, because a
freshness net that cannot read its own timestamp must never report fresh.

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


def run_manifest(manifest, days=90.0, write=True):
    """Point heartbeat's manifest path at a synthetic file with a pinned clock, restoring after.

    manifest: the object written as skill-authorities.json; pass write=False to omit the file.
    Returns the exit code from skill_manifest_check()."""
    saved = (heartbeat.SA_PATH, heartbeat.SA_ALERT_PATH, heartbeat._now, heartbeat.SA_DAYS)
    with tempfile.TemporaryDirectory() as d:
        mp = os.path.join(d, "skill-authorities.json")
        if write:
            with open(mp, "w") as f:
                if isinstance(manifest, str):
                    f.write(manifest)      # deliberately-corrupt payload
                else:
                    json.dump(manifest, f)
        heartbeat.SA_PATH = mp
        heartbeat.SA_ALERT_PATH = os.path.join(d, "sm.md")
        heartbeat._now = lambda: NOW
        heartbeat.SA_DAYS = days
        try:
            return heartbeat.skill_manifest_check()
        finally:
            (heartbeat.SA_PATH, heartbeat.SA_ALERT_PATH,
             heartbeat._now, heartbeat.SA_DAYS) = saved


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

    # --- the skill-authority manifest's age (a separate signal, separate exit code) ---
    def gen(days_ago):
        return (NOW - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    code = run_manifest({"generated_at": gen(10), "skills": [1, 2]})
    check("manifest generated 10d ago (threshold 90) -> 0", code == 0, "got %r" % code)

    code = run_manifest({"generated_at": gen(120), "skills": [1, 2], "skills_root": "/mnt/skills/user"})
    check("manifest generated 120d ago (threshold 90) -> 5", code == 5, "got %r" % code)

    # Exactly at the threshold is fresh; one day past it is not. Pins the boundary so a later
    # rewrite can't quietly flip the comparison and move the alert by a day in either direction.
    check("manifest exactly at the threshold -> 0", run_manifest({"generated_at": gen(90)}) == 0)
    check("manifest one day past the threshold -> 5", run_manifest({"generated_at": gen(91)}) == 5)

    # Absent -> 0. update.py treats a missing manifest as "the watch is not in use"; this must
    # agree, or a deployment that never adopted it gets a permanent issue about a file it does
    # not want.
    code = run_manifest(None, write=False)
    check("manifest absent (watch not in use) -> 0", code == 0, "got %r" % code)

    # Present but undatable -> 5, never a silent 0: same reasoning as the malformed-opinions.json
    # branch. A net that cannot read its own timestamp must not report fresh.
    check("manifest present, no generated_at -> 5", run_manifest({"skills": []}) == 5)
    check("manifest present, unparseable date -> 5", run_manifest({"generated_at": "last June"}) == 5)
    check("manifest present, corrupt JSON -> 5", run_manifest("{not json") == 5)
    check("manifest present, JSON but not an object -> 5", run_manifest([1, 2, 3]) == 5)

    # The kill switch is deliberate, and must beat every alerting branch above -- including a
    # corrupt file -- or "retire this check" would not actually retire it.
    check("threshold 0 disables the check (stale manifest)",
          run_manifest({"generated_at": gen(9999)}, days=0.0) == 0)
    check("threshold 0 disables the check (corrupt manifest)",
          run_manifest("{not json", days=0.0) == 0)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (22 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
