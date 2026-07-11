#!/usr/bin/env python3
"""Dead-man's-switch for an unattended deployment (scripts/heartbeat.py).

Every alert in this repo is a GitHub issue opened by a workflow's failure step -- which only
fires if that workflow actually RUNS. The failure modes with no signal are the ones where runs
stop happening or silently no-op: a CourtListener feed-shape change (zero discovery, green check),
a retired model that stalls the funnel, a stuck upstream status-page outage the preflight skips
on, an expired key, or the 60-day scheduled-workflow auto-disable. This job is the out-of-band
check for those: it reads the committed freshness markers and exits nonzero when the funnel looks
stalled, so heartbeat.yml opens an issue. (It cannot catch a total 60-day auto-disable, which
takes this cron down too -- an external monitor via HEARTBEAT_PING_URL is the belt for that; see
heartbeat.yml.)

Two independent signals, thresholds overridable by env:
  * FUNNEL not running  -- public/status.json `scanned_at` older than HEARTBEAT_SCAN_HOURS (default
    48h; the funnel runs every 4h, so 48h is 12 missed slots). This is the strong signal: the funnel
    is failing every run, disabled, or its key/model broke. Exit 3.
  * CONTENT stale        -- the funnel is running, but the newest card's `first_seen` in
    opinions.json is older than HEARTBEAT_CONTENT_DAYS (default 30). A softer signal: a real quiet
    stretch is possible, but it is also how feed-shape drift looks (discovery silently returns
    nothing). Exit 4.
Fresh on both -> exit 0. A missing/unparseable status.json is treated as funnel-stale (exit 3).

Prints a one-paragraph diagnosis and writes it to scripts/heartbeat_alert.md for the issue body.
"""
import datetime
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_PATH = os.path.join(REPO, "public", "status.json")
JSON_PATH = os.path.join(REPO, "opinions.json")
ALERT_PATH = os.path.join(REPO, "scripts", "heartbeat_alert.md")

SCAN_HOURS = float(os.environ.get("HEARTBEAT_SCAN_HOURS", "48"))
CONTENT_DAYS = float(os.environ.get("HEARTBEAT_CONTENT_DAYS", "30"))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts):
    """Parse an ISO stamp (YYYY-MM-DDTHH:MM:SSZ or a bare YYYY-MM-DD date) to aware UTC, or None.

    Tolerant of a trailing 'Z' and of sub-second/offset tails: we normalize by dropping a trailing
    'Z' and try the datetime form on the first 19 chars, then fall back to the bare-date form on the
    first 10. Keying the slice width to the format (not to whether the string contains a 'T') is what
    lets a bare 'YYYY-MM-DD' value -- which is what first_seen actually is -- parse at all.
    """
    if not ts or not isinstance(ts, str):
        return None
    s = ts.rstrip("Z") if ts.endswith("Z") else ts
    for fmt, width in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(s[:width], fmt).replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def _write_alert(body):
    try:
        with open(ALERT_PATH, "w", encoding="utf-8") as f:
            f.write(body + "\n")
    except OSError:
        pass


def check():
    now = _now()
    # --- funnel liveness: when did a scan last complete? ---
    scanned = None
    if os.path.exists(STATUS_PATH):
        try:
            scanned = _parse(json.load(open(STATUS_PATH, encoding="utf-8")).get("scanned_at"))
        except (OSError, ValueError, TypeError):
            scanned = None
    if scanned is None:
        body = ("Heartbeat: `public/status.json` is missing or has no readable `scanned_at`, so the "
                "funnel's last-run time can't be confirmed. The scan-status step may be failing, or "
                "the funnel is not running. Check the **Georgia Appellate Watch update** workflow.")
        print(body)
        _write_alert(body)
        return 3
    scan_age_h = (now - scanned).total_seconds() / 3600.0
    if scan_age_h > SCAN_HOURS:
        body = ("Heartbeat: the funnel has not completed a scan in **%.0f hours** (last `scanned_at` "
                "%s; threshold %.0fh). It runs every 4 hours, so this means runs are failing, the "
                "workflow was auto-disabled after 60 idle days, or a key/model broke. Check the "
                "**Georgia Appellate Watch update** workflow and its recent runs." % (scan_age_h, scanned.date(), SCAN_HOURS))
        print(body)
        _write_alert(body)
        return 3

    # --- content freshness: funnel is running, but is it still finding new cards? ---
    newest = None
    try:
        entries = json.load(open(JSON_PATH, encoding="utf-8"))
        stamps = [_parse(e.get("first_seen") or e.get("date")) for e in entries]
        stamps = [s for s in stamps if s]
        newest = max(stamps) if stamps else None
    except (OSError, ValueError, TypeError):
        newest = None
    if newest is not None:
        content_age_d = (now - newest).total_seconds() / 86400.0
        if content_age_d > CONTENT_DAYS:
            body = ("Heartbeat: the funnel is running (last scan %.0fh ago) but **no new card in %.0f "
                    "days** (newest `first_seen` %s; threshold %.0fd). A quiet appellate stretch is "
                    "possible, but this is also how a CourtListener feed-shape change looks -- silent "
                    "zero discovery. Spot-check that new opinions are actually being found."
                    % (scan_age_h, content_age_d, newest.date(), CONTENT_DAYS))
            print(body)
            _write_alert(body)
            return 4

    print("Heartbeat OK: last scan %.0fh ago; newest card %s."
          % (scan_age_h, newest.date() if newest else "n/a"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(check())
