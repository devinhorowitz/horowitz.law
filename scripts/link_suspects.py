#!/usr/bin/env python3
"""Two-strike confirmation for the weekly link check.

A link that fails once has not necessarily rotted. The Georgia General Assembly site times
out under load; CourtListener 5xx's during a deploy; a court site rate-limits a runner.
Filing an issue on first sight turns every such blip into a tracking issue that sits open
on an unattended deployment until someone reads it, and the ones that pile up are almost
always the transient ones -- genuine rot is the rare case that persists.

So a failure is recorded, not reported. The URL becomes a SUSPECT with a first-failed
timestamp. At least CONFIRM_AFTER_SEC later (24h), the suspect is re-checked on its own,
without re-crawling the site. Only a URL that fails BOTH times, 24 hours apart, opens an
issue. A suspect that passes the re-check, or that the next crawl finds healthy, is
dropped silently and never becomes an issue.

The state is a committed JSON file rather than an Actions cache: cache eviction would
silently reset the clock, and silently resetting the clock on a rot detector means the
issue never gets filed. Committed state is also auditable -- you can see when a URL first
went bad.

Modes:
  record   read a lychee JSON report, merge its failures into the suspect file, and drop
           suspects the crawl now finds healthy. Files nothing.
  due      write the suspects old enough to confirm, one URL per line, for lychee to re-check.
  confirm  settle those suspects against lychee's re-check report: still failing becomes an
           issue body, passing is dropped. Exit 0 always; the workflow reads the written
           files to decide whether to open, update, or close the issue.

The re-check is run by lychee over the `due` list, not by this module. That is deliberate:
a hand-rolled HTTP client has to imitate the crawl's accepted status codes, retry budget,
and HEAD/GET behaviour, and any drift between the two would "confirm" links the crawl was
perfectly happy with. Using the same binary on a shorter input list removes the second
opinion entirely -- and with it a URL-fetching sink that took its target from a state file.

  python scripts/link_suspects.py record  --report lychee/out.json
  python scripts/link_suspects.py due     --out suspects.txt
  python scripts/link_suspects.py confirm --report lychee/recheck.json --body body.md
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safeio  # noqa: E402  (sys.path shim must run first)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO, "link_suspects.json")

CONFIRM_AFTER_SEC = int(os.environ.get("LINK_CONFIRM_AFTER_SEC", str(24 * 3600)))


# --- state ----------------------------------------------------------------
def load_state(path=STATE_PATH):
    """The suspect file, or an empty state. A corrupt or missing file starts empty rather
    than crashing: losing the clock costs one more 24h cycle, crashing costs the check."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("suspects"), dict):
            return d
    except Exception:
        pass
    return {"suspects": {}}


def save_state(state, path=STATE_PATH):
    # Atomic: the workflow commits this file, so a truncating write killed mid-flight would
    # commit a corrupt clock -- the same discipline golden_set.json uses.
    safeio.atomic_write_json(path, state)


# --- lychee report --------------------------------------------------------
def parse_lychee(report):
    """Failing (url, status, source) triples from a lychee --format json report.

    Lychee reports failures under a per-input map (`fail_map`, and `error_map` in some
    versions), each value a list of per-link objects. This walks whichever of those exist
    rather than assuming one shape, because a lychee upgrade that renames the key would
    otherwise make this silently find nothing -- and finding nothing here means never
    filing an issue. The caller treats "report said failures, parser found none" as a
    parser failure and falls back to reporting, so that silence cannot happen unnoticed.
    """
    out = []
    if not isinstance(report, dict):
        return out
    for key in ("fail_map", "error_map", "failures", "errors"):
        m = report.get(key)
        if not isinstance(m, dict):
            continue
        for source, entries in m.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue
                url = (e.get("url") or e.get("uri") or "").strip()
                if not url or not safe_url(url)[0]:
                    # Dropped at the door as well as at the sink: a mailto:, tel:, or file:
                    # entry is not something a 24h HTTP re-check can adjudicate, so it must
                    # not become a suspect that can never be confirmed or cleared.
                    continue
                status = e.get("status")
                if isinstance(status, dict):
                    status = (status.get("text") or status.get("code")
                              or status.get("details") or "")
                out.append({"url": url, "status": str(status or "failed"),
                            "source": str(source or "")})
    # A URL can fail on several pages; one suspect per URL, first source kept.
    seen, uniq = set(), []
    for f in out:
        if f["url"] in seen:
            continue
        seen.add(f["url"])
        uniq.append(f)
    return uniq


# --- record ---------------------------------------------------------------
def record(state, failures, now):
    """Merge a crawl's failures into the suspects, and drop the ones it now finds healthy.

    A suspect absent from `failures` either passed this crawl or is no longer linked from
    the site; both mean it is not rot, so it is dropped and its clock discarded. Returns
    (state, added, still_failing, recovered)."""
    suspects = dict(state.get("suspects") or {})
    failing = {f["url"]: f for f in failures}
    added, still = [], []
    for url, f in failing.items():
        cur = suspects.get(url)
        if cur:
            cur["last_failed"] = now
            cur["last_status"] = f["status"]
            still.append(url)
        else:
            suspects[url] = {"first_failed": now, "last_failed": now,
                             "first_status": f["status"], "last_status": f["status"],
                             "source": f["source"]}
            added.append(url)
    recovered = [u for u in suspects if u not in failing]
    for u in recovered:
        del suspects[u]
    return {"suspects": suspects}, added, still, recovered


# --- re-check -------------------------------------------------------------
def safe_url(url):
    """Is this a URL the re-check can adjudicate? Returns (ok, reason).

    Only http(s) is re-checkable: a mailto:, tel:, or file: entry cannot be confirmed or
    cleared by a link check, so it must never become a suspect that sits in the state
    forever. Embedded credentials are refused separately -- a suspect's URL is written into
    a public issue body, so http://user:pw@host would publish the secret."""
    try:
        p = urlsplit(url)
    except Exception as e:
        return False, "unparseable URL (%s)" % type(e).__name__
    if p.scheme not in ("http", "https"):
        return False, "refused: scheme %r is not http(s)" % (p.scheme or "")
    if not p.netloc:
        return False, "refused: no host"
    if "@" in p.netloc:
        return False, "refused: URL carries embedded credentials"
    return True, ""


def due(state, now, after=None):
    """Suspects first seen failing at least CONFIRM_AFTER_SEC ago -- the ones a re-check can
    actually confirm. A suspect younger than that is left alone; that wait is the point."""
    after = CONFIRM_AFTER_SEC if after is None else after
    return sorted(u for u, s in (state.get("suspects") or {}).items()
                  if now - _first(s, now) >= after)


def _first(suspect, default):
    """A suspect's clock. Explicitly None-checked rather than `or default`: a first_failed of
    0.0 is falsy but perfectly valid, and `or` would silently restart that suspect's 24h
    every time it was read -- the one bug that makes a dead link never confirm."""
    v = suspect.get("first_failed")
    return float(default if v is None else v)


def confirm(state, now, failed_urls, after=None):
    """Settle every due suspect against a re-check the caller already ran.

    `failed_urls` is the set of due URLs that failed again -- parsed from a second lychee
    run over the suspects alone. Anything due and NOT in that set passed and is dropped.
    Confirmed suspects stay in the state so a later run can see them recover and close the
    issue. Returns (state, confirmed, recovered)."""
    failed = set(failed_urls or ())
    suspects = dict(state.get("suspects") or {})
    confirmed, recovered = [], []
    for url in due({"suspects": suspects}, now, after):
        if url not in failed:
            recovered.append(url)
            del suspects[url]
            continue
        s = dict(suspects[url])
        s.update({"last_failed": now, "confirmed_at": now})
        suspects[url] = s
        confirmed.append(url)
    return {"suspects": suspects}, confirmed, recovered


# --- reporting ------------------------------------------------------------
def _ago(seconds):
    h = int(seconds // 3600)
    return "%dh" % h if h < 48 else "%dd" % (h // 24)


def issue_body(state, confirmed, now):
    """The issue body: only URLs that failed twice, 24h apart, with when each went bad."""
    lines = ["Each link below failed the weekly crawl **and** failed an independent "
             "re-check at least 24 hours later. Links that failed once and recovered are "
             "not listed -- they never open an issue.", ""]
    lines.append("| URL | Failing since | First seen as | Latest re-check |")
    lines.append("|---|---|---|---|")
    for url in confirmed:
        s = (state.get("suspects") or {}).get(url) or {}
        first = _first(s, now)
        lines.append("| %s | %s ago | %s | %s |"
                     % (url, _ago(now - first), s.get("first_status") or "?",
                        s.get("last_status") or "?"))
    srcs = sorted({(state.get("suspects") or {}).get(u, {}).get("source", "") for u in confirmed})
    srcs = [s for s in srcs if s]
    if srcs:
        lines += ["", "Linked from: " + ", ".join("`%s`" % s for s in srcs)]
    return "\n".join(lines) + "\n"


# --- CLI ------------------------------------------------------------------
def _emit(path, text):
    if path:
        safeio.atomic_write_text(path, text)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("record")
    r.add_argument("--report", required=True, help="lychee --format json output")
    r.add_argument("--state", default=STATE_PATH)
    r.add_argument("--exit-code", default="0", help="lychee's exit code, for the parser check")
    d = sub.add_parser("due")
    d.add_argument("--state", default=STATE_PATH)
    d.add_argument("--out", required=True, help="write the due URLs here, one per line, for lychee")
    c = sub.add_parser("confirm")
    c.add_argument("--state", default=STATE_PATH)
    c.add_argument("--report", default="", help="lychee JSON from the re-check of the due URLs")
    c.add_argument("--body", default="", help="write the issue body here when anything is confirmed")
    a = p.parse_args(argv)
    now = time.time()

    if a.mode == "record":
        try:
            with open(a.report, encoding="utf-8") as f:
                report = json.load(f)
        except Exception as e:
            print("::warning::link_suspects: unreadable lychee report (%s); reporting directly" % e)
            print("parser_ok=false")
            return 0
        failures = parse_lychee(report)
        # Safety valve. If lychee says it found problems and this parser extracts none, the
        # report shape changed and every failure would be swallowed -- the two-strike rule
        # would silently become "never file an issue". Say so and let the workflow fall back
        # to reporting directly rather than going quiet.
        if a.exit_code not in ("0", "") and not failures:
            print("::warning::link_suspects: lychee exited %s but no failures parsed; "
                  "report shape may have changed. Falling back to direct reporting." % a.exit_code)
            print("parser_ok=false")
            return 0
        state, added, still, recovered = record(load_state(a.state), failures, now)
        save_state(state, a.state)
        print("parser_ok=true")
        print("record: %d failing (%d new, %d repeat), %d recovered, %d suspect(s) tracked"
              % (len(failures), len(added), len(still), len(recovered), len(state["suspects"])))
        for u in added:
            print("  + new suspect   %s" % u)
        for u in recovered:
            print("  - recovered     %s" % u)
        return 0

    if a.mode == "due":
        urls = due(load_state(a.state), now)
        _emit(a.out, "".join(u + "\n" for u in urls))
        print("due: %d suspect(s) old enough to confirm" % len(urls))
        for u in urls:
            print("  ? re-checking   %s" % u)
        print("due_count=%d" % len(urls))
        return 0

    # confirm. The re-check was run by lychee itself, over the `due` list -- the same tool,
    # flags, and accept codes as the crawl, so there is no second opinion to keep in sync.
    state = load_state(a.state)
    pending = due(state, now)
    failed = []
    if a.report:
        try:
            with open(a.report, encoding="utf-8") as f:
                failed = [f_["url"] for f_ in parse_lychee(json.load(f))]
        except Exception as e:
            # Fail SAFE, not silent: with no readable re-check, confirm nothing this round.
            # A suspect keeps its clock and is re-checked tomorrow; the alternative -- treating
            # an unreadable report as "everything passed" -- would drop real rot on a hiccup.
            print("::warning::link_suspects: unreadable re-check report (%s); confirming "
                  "nothing this round, suspects keep their clock" % e)
            print("confirmed=0")
            return 0
    state, confirmed, recovered = confirm(state, now, failed)
    save_state(state, a.state)
    print("confirm: %d due, %d confirmed, %d recovered, %d still waiting out the 24h"
          % (len(pending), len(confirmed), len(recovered),
             len(state["suspects"]) - len(confirmed)))
    for u in confirmed:
        print("  ! confirmed dead %s" % u)
    for u in recovered:
        print("  - recovered      %s" % u)
    if confirmed:
        _emit(a.body, issue_body(state, confirmed, now))
    print("confirmed=%d" % len(confirmed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
