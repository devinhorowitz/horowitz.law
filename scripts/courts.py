#!/usr/bin/env python3
"""CourtListener court discovery and registry validation.

jurisdictions.py pins a curated set of court ids to monitor. This tool talks to
CourtListener's courts API so that set stays honest as the catalog changes,
without the runtime ever depending on network discovery (the registry remains
the offline source of truth; this only informs edits and flags drift).

Modes:
  --validate        Check every court id configured in jurisdictions.py still
                    resolves in CourtListener and is in use. Prints a report; on
                    drift writes court_drift.md and exits 3. Exits 2 on an
                    operational failure (auth, network, or throttle), 0 if clean.
                    The monthly validate-courts workflow runs this.
  --search TERM     List CourtListener courts whose id, name, or jurisdiction
                    matches TERM, with the Atom feed URL, so you can find the id
                    of a new court before adding one line to the registry.
  --list (default)  Print the courts the registry monitors and their feed URLs.
                    Offline; no network.

Env: COURTLISTENER_TOKEN (recommended; inherited via update.cl_get).
"""
import os
import sys
import time
import argparse
import urllib.error

import update          # cl_get (auth, rate-limit, retries), ConfigError
import jurisdictions   # the court registry
import cl_rate         # RateBudgetExceeded

CL = "https://www.courtlistener.com"
OK, OPERR, DRIFT = 0, 2, 3
DRIFT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "court_drift.md")


def feed_url(court_id):
    """The court's Atom feed URL. Mirrors update.feed_court's convention; the only
    other place a court feed URL is built."""
    return "%s/feed/court/%s/" % (CL, court_id)


def monitored_courts():
    """Yield (jurisdiction_key, court) for every court in the registry, all
    jurisdictions (so a jurisdiction added but not yet active is still validated)."""
    for jkey, cfg in sorted(jurisdictions.JURISDICTIONS.items()):
        for c in cfg["courts"]:
            yield jkey, c


def fetch_court(cid, deadline=None):
    """Return CourtListener's court object for id `cid`, or None if it 404s."""
    try:
        return update.cl_get("/api/rest/v4/courts/%s/" % cid, deadline=deadline)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _retired(court):
    """A non-empty reason if CourtListener no longer treats the court as active."""
    if court.get("in_use") is False:
        return "marked not in use"
    if court.get("end_date"):
        return "end date %s" % court["end_date"]
    return ""


def validate():
    rows, drift, seen = [], [], set()
    for jkey, c in monitored_courts():
        cid = c["cl"]
        if cid in seen:
            continue
        seen.add(cid)
        court = fetch_court(cid, deadline=time.time() + 30)
        if court is None:
            rows.append((jkey, cid, "MISSING", "not found in CourtListener"))
            drift.append((cid, "not found in CourtListener"))
            continue
        why = _retired(court)
        if why:
            rows.append((jkey, cid, "RETIRED", why))
            drift.append((cid, why))
        else:
            rows.append((jkey, cid, "ok", court.get("full_name", "")))

    print("Court registry validation: %d id(s) across %d jurisdiction(s)\n"
          % (len(seen), len(jurisdictions.JURISDICTIONS)))
    w = max((len(r[1]) for r in rows), default=4)
    for jkey, cid, status, note in rows:
        print("  [%-4s] %-*s  %-7s  %s" % (jkey, w, cid, status, note))

    if drift:
        lines = ["## Court registry drift detected", "",
                 "The monthly validation found configured court ids that no longer "
                 "resolve cleanly in CourtListener. Update `scripts/jurisdictions.py`.",
                 "", "| court id | issue |", "|---|---|"]
        lines += ["| `%s` | %s |" % (cid, why) for cid, why in drift]
        lines += ["", "_Recheck with `python scripts/courts.py --validate`._"]
        with open(DRIFT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("\nDRIFT: %d id(s) need attention; wrote %s" % (len(drift), os.path.basename(DRIFT_PATH)))
        return DRIFT

    print("\nAll configured court ids resolve and are in use.")
    return OK


def search(term, page_cap=15):
    term_l = term.lower()
    url = "/api/rest/v4/courts/?page_size=100&in_use=true"
    matches, pages = [], 0
    while url and pages < page_cap:
        data = update.cl_get(url, deadline=time.time() + 30)
        for c in data.get("results", []):
            hay = " ".join(str(c.get(k, "")) for k in ("id", "full_name", "short_name", "jurisdiction")).lower()
            if term_l in hay:
                matches.append(c)
        url = data.get("next")
        pages += 1
        if url:
            time.sleep(1)
    truncated = bool(url)

    if not matches:
        print("No CourtListener courts match %r%s." % (term, " (search truncated; refine the term)" if truncated else ""))
        return OK
    print("CourtListener courts matching %r:\n" % term)
    for c in sorted(matches, key=lambda c: c.get("id", "")):
        print("  %-10s %s [%s]" % (c.get("id", ""), c.get("full_name", ""), c.get("jurisdiction", "")))
        print("    feed: %s" % feed_url(c.get("id", "")))
    if truncated:
        print("\n(Search hit the %d-page cap; refine the term if you expect more.)" % page_cap)
    return OK


def list_monitored():
    print("Courts the registry monitors:\n")
    for jkey, cfg in sorted(jurisdictions.JURISDICTIONS.items()):
        print("  %s (%s):" % (jkey, cfg["label"]))
        for c in cfg["courts"]:
            print("    %-8s %-45s %s" % (c["cl"], c["label"], feed_url(c["cl"])))
        print("")
    return OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="CourtListener court discovery and registry validation.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--validate", action="store_true", help="validate configured court ids against CourtListener")
    g.add_argument("--search", metavar="TERM", help="search CourtListener courts by id, name, or jurisdiction")
    g.add_argument("--list", action="store_true", help="list the courts the registry monitors (offline)")
    args = ap.parse_args(argv)
    try:
        if args.validate:
            return validate()
        if args.search:
            return search(args.search)
        return list_monitored()
    except update.ConfigError as e:
        print("  ! CourtListener auth/config error: %s" % e, file=sys.stderr)
        return OPERR
    except cl_rate.RateBudgetExceeded as e:
        print("  ! CourtListener throttled; try again later: %s" % e, file=sys.stderr)
        return OPERR
    except (urllib.error.URLError, OSError) as e:
        print("  ! CourtListener unreachable: %s" % e, file=sys.stderr)
        return OPERR


if __name__ == "__main__":
    sys.exit(main())
