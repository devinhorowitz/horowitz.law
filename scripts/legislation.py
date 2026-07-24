#!/usr/bin/env python3
"""Georgia Legislative Watch: newly enacted state law, filtered for a civil practice.

The appellate watch reads decisions; this reads *statutes*. It watches the Georgia
General Assembly for bills that became law -- signed by the Governor, or allowed to
become law without signature -- and vetoes, and drafts a plain-language card for the
ones that touch a personal-injury / insurance-defense / civil-litigation practice
(tort reform, damages, civil procedure, evidence, insurance, premises, motor-carrier).
It reuses the appellate funnel's shape: a cheap relevance screen drops the bulk
(appropriations, licensing boards, local acts), and only survivors reach the writer.

Why a separate source. There is no clean, free, machine-readable "enacted Georgia
statutes" API from the state; legis.ga.gov sits behind the same class of WAF that
blocks the Court of Appeals docket. LegiScan (legiscan.com) normalizes all 50 states'
and Congress's legislation into one JSON API with a *normalized status enum*, which is
the unlock: it maps Georgia's "signed" and "became law without signature" onto a single
`status == 4` (Passed/enacted), so we do not have to parse the constitutional timing of
each bill ourselves. A free API key (the LEGISCAN_API_KEY secret) enables it.

  LegiScan status enum (bill.status):
    1 Introduced   2 Engrossed   3 Enrolled   4 Passed/enacted   5 Vetoed   6 Failed/dead
  We card 4 (became law) and 5 (vetoed -- a vetoed tort bill is news to this audience too),
  and skip everything below. `status_date` is the enactment/veto date. A per-bill
  `change_hash` lets us re-fetch details only when a bill actually moved, so a run that
  finds nothing new costs a single master-list call.

Design mirrors update.py: standard library only, network and model calls are injectable
seams (`fetch=` and `ai=`) so the whole funnel is unit-testable with no network and no key,
and everything FAILS OPEN -- no key, a LegiScan outage, or a model error yields an empty
run and a logged notice, never a crash and never a false card. Unlike the appellate auto
lane, legislation is low-volume and high-consequence, so v1 has NO auto-publish: every
relevant bill is written for a human to confirm via the review PR (see docs/LEGISLATION.md).

Congress (FRCP-adjacent statutes, the FMCSA authorizing/preemption statutes like the FAAAA)
is the same funnel with `state="US"`; it is left off by default because federal *rules*
(FRCP/FRE, via the Rules Enabling Act) and *agency regulations* (FMCSA rulemaking in the
Federal Register) are different sources, tracked separately -- see docs/LEGISLATION.md.

Run locally (dry, no writes; needs LEGISCAN_API_KEY + ANTHROPIC_API_KEY to hit live data):
    LEGISCAN_API_KEY=... ANTHROPIC_API_KEY=... python scripts/legislation.py
Add --json to print the drafted cards as JSON.

Environment:
  LEGISCAN_API_KEY        required to reach live data; absent = fail-open no-op
  ANTHROPIC_API_KEY       required for the relevance screen and the writer
  LEGISLATION_STATES      comma list of LegiScan jurisdictions to watch (default "GA"; the workflow
                          sets "GA,US"). "US" is the federal overlay, screened strictly. The legacy
                          single-state alias LEGISLATION_STATE is still accepted.
  LEGISLATION_SCREEN_MODEL relevance screen model (default claude-haiku-4-5)
  LEGISLATION_MODEL       card writer model (default claude-opus-5)
  LEGISLATION_MAX         cap on CARDS drafted per run (default 40)
  LEGISLATION_SCREEN_MAX  cap on bills SCREENED per run (default 60); bounds a cold-start, the
                          remainder rolls into the next run. Progress streams to stdout as it goes.
  LEGISCAN_MIN_INTERVAL   min seconds between real LegiScan calls (default 1.0; 0 disables). Courtesy
                          pacing per LegiScan's "play nice" guidance; applies only to live calls.
  LEGISLATION_BATCH       batch the Opus card-write pass via the 50%-priced Message Batches API
                          (default on; screening stays synchronous). Set 0 for the synchronous
                          rollback. LEGISLATION_BATCH_SEC bounds the in-run wait (default 1800).
  LEGISLATION_DEBUG       if 1, log each step
"""
import os
import re
import sys
import json
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import siteconfig  # shared practice-area taxonomy  # noqa: E402

JSON_PATH  = os.path.join(REPO, "legislation.json")
STATE_PATH = os.path.join(REPO, "legislation_state.json")
LOG_PATH   = os.path.join(REPO, "legislation_log.jsonl")

KEY_LEGISCAN = os.environ.get("LEGISCAN_API_KEY", "")
DEFAULT_STATE = "GA"
# The LegiScan jurisdictions to watch, in order. "GA" is the Georgia General Assembly; "US" is the
# U.S. Congress, whose relevant surface for this practice is narrow -- the FAAAA motor-carrier
# preemption statute (49 U.S.C. 14501), statutes authorizing/amending the FMCSA, and the federal
# jurisdiction/procedure statutes that reach a state civil practice -- not FMCSA *regulations*
# (Federal Register) or the FRCP (uscourts.gov), which are separate sources. LEGISLATION_STATE is
# accepted as the legacy single-state alias. Cards from all states share one legislation.json,
# keyed on the globally-unique LegiScan bill_id.
def _states():
    raw = os.environ.get("LEGISLATION_STATES") or os.environ.get("LEGISLATION_STATE") or DEFAULT_STATE
    seen, out = set(), []
    for s in raw.split(","):
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out or [DEFAULT_STATE]

LEGISLATION_STATES = _states()
SCREEN_MODEL = os.environ.get("LEGISLATION_SCREEN_MODEL", "claude-haiku-4-5")
WRITE_MODEL  = os.environ.get("LEGISLATION_MODEL", "claude-opus-5")
MAX_RUN      = int(os.environ.get("LEGISLATION_MAX", "40"))     # cap on CARDS drafted per run
# Cap on bills SCREENED per run. The cold-start problem: with an empty change_hash state every
# enacted/vetoed bill in the whole biennium (plus US Congress) is "new", so an unbounded first run
# screens hundreds serially. Bounding screens keeps every run short; the unscreened remainder is
# left un-seen and simply rolls into the next weekly run, draining the backlog over a few Sundays.
SCREEN_MAX   = int(os.environ.get("LEGISLATION_SCREEN_MAX", "60"))
DEBUG        = os.environ.get("LEGISLATION_DEBUG", "") == "1"

# Batch the (Opus) card-WRITE pass through the 50%-priced Message Batches API, mirroring the opinion
# funnel's OPINIONS_BATCH. Screening stays synchronous (Haiku, cheap, and its fail-open keep should be
# instant); only the write -- one Opus call per screened-relevant bill -- is batched, which is where
# the cost sits on a busy session or a cold start. LEGISLATION_BATCH=0 is the instant synchronous
# rollback. BATCH_SEC bounds the in-run wait before an unfinished batch defers to the next run.
LEGISLATION_BATCH = os.environ.get("LEGISLATION_BATCH", "on").strip().lower() in ("1", "true", "yes", "on")
BATCH_SEC = int(os.environ.get("LEGISLATION_BATCH_SEC", "1800"))

# LegiScan's page-7 "API Operations" timing table: the MINIMUM resolution at which each operation's
# data can change. Polling faster than this returns unchanged, cached data that STILL spends a query
# (LegiScan flags it as a "cache hit"). We persist the last-poll time per operation and skip a
# re-poll inside its window -- caching the small session list so a skipped getSessionList still
# works -- so the watch is provably compliant no matter how often it is triggered. getBill needs no
# guard: it is already gated by change_hash, so an unchanged bill is never re-fetched. Seconds.
POLL_MIN = {
    "session": int(os.environ.get("LEGISCAN_SESSIONLIST_MIN", str(24 * 3600))),  # getSessionList: daily
    "master":  int(os.environ.get("LEGISCAN_MASTERLIST_MIN", str(3600))),        # getMasterList: hourly
}

API_BASE   = "https://api.legiscan.com/"
UA         = "horowitz.law Georgia Legislative Watch (contact: via horowitz.law)"
TIMEOUT    = 45
MAX_BYTES  = 25 * 1024 * 1024   # cap any single LegiScan read; bounds memory vs a hostile response
# Courtesy pacing: keep at least this many seconds between real LegiScan calls, per LegiScan's
# "play nice, respect the free public service" guidance. Our volume is already tiny (a couple of
# calls per run, change_hash-gated), so this is politeness insurance, not a throughput lever. It
# applies only to the default network seam (_http_get); an injected test `fetch` is never paced.
# Set LEGISCAN_MIN_INTERVAL=0 to disable.
MIN_INTERVAL = float(os.environ.get("LEGISCAN_MIN_INTERVAL", "1.0"))
_last_call = [0.0]   # monotonic timestamp of the last real request, in a 1-cell list for closure write

# LegiScan normalized bill.status -> our normalized posture. We card only these two;
# everything else (introduced, engrossed, enrolled-but-not-yet-enacted, failed) is skipped.
STATUS_ENACTED = 4
STATUS_VETOED  = 5
STATUS_LABEL   = {STATUS_ENACTED: "enacted", STATUS_VETOED: "vetoed"}

AREA_CODES_STR = ", ".join(siteconfig.AREA_CODES)


def _dbg(msg):
    if DEBUG:
        print("  . " + msg)


# --------------------------------------------------------------------------- #
# Network seam: LegiScan RPC.                                                  #
# --------------------------------------------------------------------------- #
def _pace():
    """Block until at least MIN_INTERVAL seconds have elapsed since the last real request. The
    first call never waits; disabled when MIN_INTERVAL <= 0."""
    if MIN_INTERVAL <= 0:
        return
    wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if 0 < wait <= MIN_INTERVAL:   # bound the wait so a clock jump cannot stall the run
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _http_get(url):
    """Default fetch seam: GET a URL, return decoded text. Byte-capped, and paced to at least
    MIN_INTERVAL seconds between calls (LegiScan courtesy). Tests inject their own callable of the
    same shape, so no test ever hits the network -- or the pacer."""
    _pace()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8", "replace")


class LegiScanError(RuntimeError):
    """A LegiScan response that was reached but not usable (status != OK, or a
    payload we cannot parse). Distinct from a transport error so the caller can
    tell 'the service said no' from 'the service was unreachable'."""


def api(op, key, fetch=None, **params):
    """One LegiScan RPC call. Returns the parsed JSON envelope (a dict with
    'status':'OK' and the op-specific payload). Raises LegiScanError on a non-OK
    envelope. The key is passed as the `key` query parameter, per LegiScan."""
    fetch = fetch or _http_get
    q = {"key": key, "op": op}
    q.update({k: v for k, v in params.items() if v is not None})
    url = API_BASE + "?" + urllib.parse.urlencode(q)
    raw = fetch(url)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise LegiScanError("%s: unparseable response (%s)" % (op, e))
    if not isinstance(data, dict) or (data.get("status") or "").upper() != "OK":
        # LegiScan returns {"status":"ERROR","alert":{"message": "..."}} on a bad key,
        # an exhausted quota, or a bad parameter. Surface its own message.
        msg = ""
        alert = data.get("alert") if isinstance(data, dict) else None
        if isinstance(alert, dict):
            msg = alert.get("message") or ""
        raise LegiScanError("%s: LegiScan status=%r %s" % (op, (data or {}).get("status"), msg))
    return data


# --------------------------------------------------------------------------- #
# Session resolution and enacted-bill discovery.                              #
# --------------------------------------------------------------------------- #
def sessions_to_watch(session_list, today=None):
    """Pick the LegiScan sessions worth scanning from a getSessionList payload.

    Georgia's General Assembly runs a two-year term (a regular session that spans
    an odd year and the even year after it), plus occasional special sessions. A bill
    enacted in year one carries into year two, so we watch every session whose activity
    window reaches the last two calendar years -- that covers the live biennium and any
    just-closed session whose late-signed bills we might still be seeing for the first
    time. Ordered newest first. Robust to missing year fields (such a session is kept,
    not silently dropped)."""
    today = today or datetime.date.today()
    floor = today.year - 1
    out = []
    for s in (session_list or []):
        if not isinstance(s, dict) or not s.get("session_id"):
            continue
        year_end = s.get("year_end") or s.get("year_start") or 0
        try:
            keep = int(year_end) >= floor
        except (TypeError, ValueError):
            keep = True  # unparseable year: keep it rather than miss a session
        if keep:
            out.append(s)
    out.sort(key=lambda s: (s.get("year_end") or 0, s.get("session_id") or 0), reverse=True)
    return out


def masterlist_bills(payload):
    """Flatten a getMasterList payload into a list of per-bill summary dicts.

    LegiScan returns {'masterlist': {'session': {...}, '0'|'1'|...: {bill}, ...}}, where
    the 'session' entry (and any non-bill metadata) must be skipped. Each bill summary
    carries bill_id, number, change_hash, url, status, status_date, last_action[_date],
    title, description."""
    ml = (payload or {}).get("masterlist")
    if not isinstance(ml, dict):
        return []
    out = []
    for k, v in ml.items():
        if k == "session" or not isinstance(v, dict) or not v.get("bill_id"):
            continue
        out.append(v)
    return out


# Resolution number-prefixes to skip BEFORE screening, per jurisdiction. A legislature adopts
# ceremonial and procedural resolutions (commendations, condolences, memorials, chamber rules) by
# the hundreds or thousands and marks them "passed", so without this they swamp the screen budget
# and the substantive bills are never reached -- exactly what the first live run showed (60 screened,
# all commendations, 0 cards). The prefix meaning is JURISDICTION-SPECIFIC: in Georgia "HR"/"SR" are
# House/Senate Resolutions, but in the U.S. Congress "HR" is a House BILL and "S" a Senate bill, so
# only true resolution prefixes are listed per state. (Georgia constitutional amendments are also
# "HR" and are skipped with the rest; they are ratified at the ballot, not enacted as statutes.)
_RESOLUTION_PREFIX = {
    "GA": ("HR", "SR"),
    # Congress simple/concurrent resolutions never become law. Both the THOMAS/GPO style
    # (HRES/SRES/HCONRES/SCONRES) and the shorter HCR/SCR style are listed so the pre-screen
    # filter catches them whichever way LegiScan normalizes the number. Joint resolutions
    # (HJRES/SJRES) are deliberately NOT listed -- those can be enacted.
    "US": ("HRES", "SRES", "HCONRES", "SCONRES", "HCR", "SCR"),
}


def is_resolution(number, state):
    """True if a bill number is a (skippable) resolution in this jurisdiction. Keys off the
    leading alpha prefix of the number; unknown states never skip (fail-open to screening)."""
    m = re.match(r"[A-Za-z]+", str(number or "").replace(".", "").replace(" ", ""))
    return bool(m) and m.group(0).upper() in _RESOLUTION_PREFIX.get(state, ())


def enacted_candidates(bills, seen, state=None):
    """From master-list summaries, the bills that (a) are enacted or vetoed, (b) are not ceremonial
    resolutions for `state` (when given), and (c) have moved since we last carded them -- a new
    bill_id, or a changed change_hash on a known one. `seen` maps str(bill_id) -> last change_hash.

    change_hash is the whole point of the master list: a run where nothing enacted
    changed returns [] after a single call, so the schedule is nearly free."""
    out = []
    for b in bills:
        try:
            status = int(b.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status not in (STATUS_ENACTED, STATUS_VETOED):
            continue
        if state and is_resolution(b.get("number") or b.get("bill_number"), state):
            continue
        bid = str(b.get("bill_id"))
        # Membership test (not truthiness) and a normalized change_hash, so a bill LegiScan returns
        # with an empty/absent change_hash is still deduped once seen instead of re-screened forever
        # (seen stores "" for a missing hash; "" is falsy, which the old `seen.get(bid) and ...` missed).
        if bid in seen and seen[bid] == (b.get("change_hash") or ""):
            continue
        out.append(b)
    return out


def bill_detail(bill_id, key, fetch=None):
    """getBill -> the full bill object (title, description, status, status_date,
    progress[], history[], texts[], url, state_link, ...). Returns {} on any error,
    so a single unreachable bill drops out of the run instead of failing it."""
    try:
        data = api("getBill", key, fetch=fetch, id=bill_id)
    except Exception as e:   # fail-open: any error (LegiScan, transport, parse) drops this bill
        _dbg("getBill %s failed: %s" % (bill_id, e))
        return {}
    bill = data.get("bill")
    return bill if isinstance(bill, dict) else {}


# --------------------------------------------------------------------------- #
# The funnel: relevance screen (cheap) then the card writer (expensive).       #
# --------------------------------------------------------------------------- #
# The screen framing differs by jurisdiction. Georgia is the curated core: be PERMISSIVE, because
# a state civil-litigation bill is common. Congress is a federal OVERLAY: be STRICT, because the
# base rate of relevance is tiny -- almost all enacted federal law (appropriations, the NDAA,
# foreign affairs, agency reauthorizations, tax administration, post-office namings) is irrelevant
# to a state personal-injury / insurance-defense practice, and only a narrow set actually reaches
# it. Getting this bar right is what keeps the federal overlay from flooding the feed with noise.
def _label(state):
    return {"GA": "Georgia", "US": "federal (U.S. Congress)"}.get(state, state)


def _screen_system(state):
    if state == "US":
        return (
            "You are a strict triage filter for a Georgia civil-litigation and insurance-defense "
            "law practice. You are shown a FEDERAL bill (U.S. Congress) that has become law or been "
            "vetoed. Almost all federal law is IRRELEVANT to a state civil tort/insurance practice; "
            "your default is DROP. Keep ONLY a bill that directly changes the ground such a practice "
            "litigates on: motor-carrier / trucking law and FAAAA preemption (49 U.S.C. 14501), the "
            "FMCSA's statutory mandate, federal diversity/removal or class-action jurisdiction (CAFA, "
            "28 U.S.C. 1332/1441/1332(d)), the Rules Enabling Act or a statute directly amending the "
            "Federal Rules of Civil Procedure or Evidence, federal caps or standards on tort damages, "
            "or a federal insurance statute that preempts or overrides state coverage law. DROP "
            "everything else: appropriations, the NDAA, foreign affairs, immigration, criminal law, "
            "healthcare/benefits administration, agency reauthorizations unrelated to civil "
            "litigation, tax, and namings/commendations. Reply with ONLY a JSON object: "
            "{\"relevant\": true|false, \"areas\": [codes], \"reason\": \"<=15 words\"}. Valid area "
            "codes: " + AREA_CODES_STR + "."
        )
    return (
        "You are a triage filter for a Georgia civil-litigation and insurance-defense law "
        "practice. You are shown a Georgia bill that has become law (or been vetoed). Decide "
        "whether it plausibly affects that practice: tort/negligence liability, damages "
        "(including caps and apportionment), civil procedure, evidence, insurance (coverage, "
        "bad faith), premises liability, negligent security, expert testimony, motor-carrier / "
        "trucking, wrongful death, or the courts and litigation generally. Be PERMISSIVE: if it "
        "plausibly touches civil litigation or you are unsure, keep it. DROP only what is clearly "
        "unrelated: appropriations and the budget, criminal-only law, licensing boards, local and "
        "special acts, elections, education, tax administration, procurement, naming/commendation. "
        "Reply with ONLY a JSON object: {\"relevant\": true|false, \"areas\": [codes], "
        "\"reason\": \"<=15 words\"}. Valid area codes: " + AREA_CODES_STR + "."
    )


def _write_system(state):
    origin = ("a federal bill (U.S. Congress) that has become law (or been vetoed)" if state == "US"
              else "a Georgia bill that has become law (or been vetoed)")
    fed = (" For a federal law, say plainly how it reaches a GEORGIA civil practice (for example, "
           "by preempting or overriding state law, or by changing federal jurisdiction or the "
           "federal rules)." if state == "US" else "")
    return (
        "You write for a Georgia civil-litigation and insurance-defense audience of practicing "
        "lawyers. Given " + origin + ", write a tight, neutral, plain-English card describing what "
        "the law actually changes and why a civil litigator should care." + fed + " No hype, no "
        "editorializing, no legislative history recap. State what the prior rule was and what it "
        "now is where you can tell; if the text does not make a detail clear, say so rather than "
        "guessing. Ground every statement in the provided title and description -- do not invent "
        "code sections, effective dates, or dollar figures that are not present. Reply with ONLY a "
        "JSON object: {\"keep\": true|false, \"areas\": [codes], \"synopsis\": \"2-4 sentences\", "
        "\"impact\": \"one sentence, <=30 words\", \"effective_date\": \"YYYY-MM-DD or empty if not "
        "stated\"}. Set keep=false if, on a full read, the bill does not in fact affect a Georgia "
        "civil practice. Valid area codes: " + AREA_CODES_STR + "."
    )


def _bill_brief(bill, state=DEFAULT_STATE):
    """The compact text block both model tiers read: identity + title + description.
    We deliberately do NOT fetch full bill text for v1 -- LegiScan bill descriptions are
    substantive summaries, and skipping the per-document getBillText call keeps a run to
    two model calls per surviving bill and one LegiScan call per enacted bill."""
    parts = [
        "Jurisdiction: %s" % _label(state),
        "Bill: %s (%s)" % (bill.get("number") or bill.get("bill_number") or "?", state),
        "Status: %s" % STATUS_LABEL.get(int(bill.get("status") or 0), str(bill.get("status"))),
        "Status date: %s" % (bill.get("status_date") or ""),
        "Title: %s" % (bill.get("title") or ""),
        "Description: %s" % (bill.get("description") or ""),
    ]
    la = bill.get("last_action")
    if la:
        parts.append("Last action: %s (%s)" % (la, bill.get("last_action_date") or ""))
    return "\n".join(p for p in parts if p)


def screen_bill(bill, ai, state=DEFAULT_STATE, model=None):
    """Cheap relevance gate, jurisdiction-aware (strict for the federal overlay). Returns
    (keep: bool, areas: list, reason: str). Fail-open on a model/parse error: keep the bill
    (route it to the writer, which reads more and can still decline) rather than silently
    dropping a real law."""
    model = model or SCREEN_MODEL
    try:
        v = ai({"model": model, "max_tokens": 256, "system": _screen_system(state),
                "messages": [{"role": "user", "content": _bill_brief(bill, state)}]}, "leg-screen")
    except Exception as e:
        _dbg("screen failed, keeping: %s" % e)
        return True, [], "screen-error-kept"
    keep = v.get("relevant") is True
    areas = [a for a in (v.get("areas") or []) if a in siteconfig.AREA_CODES]
    return keep, areas, str(v.get("reason") or "")[:120]


# Sentinel distinguishing a TRANSIENT writer failure (a model/parse error -- retry next run,
# do not record the bill as seen) from a DEFINITIVE decline (the model read the bill and said it
# does not belong -- record it seen so we never pay to re-screen it unless it changes). A bare
# None conflates the two; that difference is what keeps a flaky run from burying a real law AND
# keeps a settled non-match from being re-screened every week.
WRITER_ERROR = object()


def _write_body(bill, state=DEFAULT_STATE, model=None):
    """The Messages body for one card write. Shared by the synchronous write_card() and the batch
    path (batch.from_body), so both build byte-identical requests."""
    return {"model": model or WRITE_MODEL, "max_tokens": 900, "system": _write_system(state),
            "messages": [{"role": "user", "content": _bill_brief(bill, state)}]}


def _write_verdict(v):
    """Parse a writer response dict into a verdict: the dict on a keep, None on a DEFINITIVE decline
    (read it and said no, or an empty synopsis). Shared by the sync and batch paths."""
    if v.get("keep") is not True:
        return None
    if not str(v.get("synopsis") or "").strip():
        return None
    return v


def write_card(bill, ai, state=DEFAULT_STATE, model=None):
    """The card writer, jurisdiction-aware. Returns a verdict dict on a keep, None on a DEFINITIVE
    decline, or the WRITER_ERROR sentinel on a transient model/parse error. Never a partial card."""
    try:
        v = ai(_write_body(bill, state, model), "leg-write")
    except Exception as e:
        _dbg("write failed: %s" % e)
        return WRITER_ERROR
    return _write_verdict(v)


def build_card(bill, verdict, state=DEFAULT_STATE, today=None):
    """Assemble a legislation card from a bill detail object and the writer verdict.
    Keyed on LegiScan bill_id (unique, stable, the permalink slug). Areas fall back to
    the screen's areas via the verdict; an empty area list is allowed (it still files
    under 'all')."""
    today = (today or datetime.date.today()).isoformat()
    status = int(bill.get("status") or 0)
    areas = [a for a in (verdict.get("areas") or []) if a in siteconfig.AREA_CODES]
    eff = str(verdict.get("effective_date") or "").strip()
    if not (len(eff) == 10 and eff[4] == "-" and eff[7] == "-"):
        eff = ""
    return {
        "bill_id": int(bill.get("bill_id")),
        "state": state,
        "number": bill.get("number") or bill.get("bill_number") or "",
        "title": (bill.get("title") or "").strip(),
        "status": STATUS_LABEL.get(status, str(status)),
        "status_date": (bill.get("status_date") or "").strip(),
        "effective_date": eff,
        "areas": areas,
        "synopsis": str(verdict.get("synopsis") or "").strip(),
        "impact": str(verdict.get("impact") or "").strip(),
        "url": (bill.get("url") or "").strip(),
        "state_link": (bill.get("state_link") or "").strip(),
        "change_hash": bill.get("change_hash") or "",
        "first_seen": today,
    }


# --------------------------------------------------------------------------- #
# Orchestration.                                                              #
# --------------------------------------------------------------------------- #
def _fresh(polls, opkey, min_seconds, now):
    """True if opkey was polled more recently than its LegiScan min-resolution window, so a re-poll
    would only spend a cached query. `polls` maps opkey -> ISO datetime string. Missing/unparseable
    timestamps are stale (poll)."""
    ts = (polls or {}).get(opkey)
    if not ts:
        return False
    try:
        last = datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    return 0 <= (now - last).total_seconds() < min_seconds


def discover(key, state=DEFAULT_STATE, fetch=None, today=None, seen=None, pollstate=None, now=None):
    """Reach LegiScan for one jurisdiction and return the enacted/vetoed bills that have moved
    since we last saw them, as (summary, session) pairs, newest sessions first. `seen` (str
    bill_id -> change_hash) defaults to legislation_state.json.

    `pollstate` (a mutable dict with "polls" and "sessioncache", read AND updated in place)
    enforces LegiScan's page-7 timing guidelines: getSessionList is skipped inside its 24h window
    (the cached session list is reused) and getMasterList inside its 1h window (that session yields
    no candidates this run), so a re-run never spends a cache-hit query. `now` is the reference
    time (datetime). Fail-open: returns [] on any LegiScan error and logs it."""
    seen = _load_seen() if seen is None else seen
    now = now or datetime.datetime.now()
    ps = pollstate if pollstate is not None else {}
    polls = ps.setdefault("polls", {})
    scache = ps.setdefault("sessioncache", {})

    skey = "session:%s" % state
    if _fresh(polls, skey, POLL_MIN["session"], now) and scache.get(state) is not None:
        all_sessions = scache[state]
        _dbg("%s: getSessionList skipped (polled < %ds ago); reusing %d cached session(s)"
             % (state, POLL_MIN["session"], len(all_sessions)))
    else:
        try:
            sess_payload = api("getSessionList", key, fetch=fetch, state=state)
        except Exception as e:
            # Surface unconditionally (not just under DEBUG): a swallowed error here previously read
            # as a benign "0 moved", hiding a real outage or a bad key. Fail-open, but loudly.
            print("LEGISLATION[%s]: getSessionList FAILED (%s); treating as 0 this run." % (state, e), flush=True)
            return []
        all_sessions = sess_payload.get("sessions") or []
        polls[skey] = now.isoformat()
        scache[state] = all_sessions
        if not all_sessions:
            # A SUCCESSFUL getSessionList that lists zero sessions for a configured jurisdiction is a
            # config/shape anomaly, not a quiet week: every valid LegiScan state has sessions. The
            # likely cause is a renamed/invalid state code or a changed response shape -- which would
            # otherwise read as a silent "0 moved" green run indefinitely. Surface it loudly, matching
            # the getSessionList-FAILED path above.
            print("LEGISLATION[%s]: getSessionList returned OK but listed ZERO sessions -- a valid "
                  "jurisdiction always has sessions, so verify the state code %r and the response "
                  "shape (a silent zero here otherwise hides a broken filter)." % (state, state), flush=True)
    sessions = sessions_to_watch(all_sessions, today=today)
    _dbg("%s: getSessionList -> %d session(s); %d in watch window: %s"
         % (state, len(all_sessions), len(sessions),
            [(s.get("session_id"), s.get("session_name") or s.get("year_start")) for s in sessions]))
    cands = []
    for s in sessions:
        mkey = "master:%s" % s.get("session_id")
        if _fresh(polls, mkey, POLL_MIN["master"], now):
            _dbg("%s session %s: getMasterList skipped (polled < %ds ago; nothing can have changed)"
                 % (state, s.get("session_id"), POLL_MIN["master"]))
            continue
        try:
            ml = api("getMasterList", key, fetch=fetch, id=s.get("session_id"))
        except Exception as e:
            print("LEGISLATION[%s]: getMasterList %s FAILED (%s); skipping that session."
                  % (state, s.get("session_id"), e), flush=True)
            continue
        polls[mkey] = now.isoformat()
        bills = masterlist_bills(ml)
        cand = enacted_candidates(bills, seen, state=state)
        if DEBUG:
            import collections
            dist = collections.Counter(int(b.get("status") or 0) for b in bills)
            n_res = sum(1 for b in bills if is_resolution(b.get("number") or b.get("bill_number"), state))
            _dbg("%s session %s: %d bills, status counts %s, %d resolutions skipped, %d candidate(s)"
                 % (state, s.get("session_id"), len(bills), dict(dist), n_res, len(cand)))
        for b in cand:
            cands.append((b, s))
    return cands


def _default_ai(body, label="call"):
    """Default model seam: delegate to update.anthropic_json (same JSON contract).
    Imported lazily so this module imports cleanly with no ANTHROPIC_API_KEY and so
    tests, which inject their own `ai`, never pull update.py in."""
    import update
    return update.anthropic_json(body, label)


def _draft_cards(pending, deadline=None):
    """Write the cards for the screened-relevant bills in `pending` as ONE 50%-priced Message Batches
    job (LEGISLATION_BATCH), mirroring the funnel's _draft_pending. `pending` is a list of dicts each
    with `bid` (str) and `detail` (the bill object) and `state`. Returns {bid: verdict|None|
    WRITER_ERROR}, the SAME verdict space the synchronous write_card produces, so run()'s downstream
    (seen / card) logic is identical either way. Recovery mirrors the sync per-bill error:

      * a whole-batch timeout or transport failure -> every bill WRITER_ERROR (defer, retry next run);
      * a per-request batch error or an unparseable body -> that one bill WRITER_ERROR (retries);
      * a success parses through _write_verdict to a keep-verdict or a definitive decline (None).
    """
    import batch
    import update
    reqs = [batch.from_body(str(p["bid"]), _write_body(p["detail"], p["state"])) for p in pending]
    try:
        results = batch.run(reqs, deadline=deadline, label="legislation-write")
    except (batch.BatchTimeout, batch.BatchError) as e:
        print("  ! legislation write batch deferred (%s); %d draft(s) roll to next run"
              % (e, len(pending)), flush=True)
        return {p["bid"]: WRITER_ERROR for p in pending}
    out = {}
    for p in pending:
        res = results.get(str(p["bid"]))
        if not res or not res.get("ok"):
            out[p["bid"]] = WRITER_ERROR       # unavailable / errored line -> retry next run
            continue
        try:
            v = update.parse_json(res["text"])
        except Exception:
            out[p["bid"]] = WRITER_ERROR       # unparseable body -> retry next run
            continue
        out[p["bid"]] = _write_verdict(v)
    return out


def run(key=None, fetch=None, ai=None, today=None, max_run=None, states=None, screen_max=None,
        pollstate=None, now=None, batch_enabled=False):
    """Full funnel over every configured jurisdiction: discover moved enacted/vetoed bills, screen
    (permissive for Georgia, strict for the federal overlay), fetch detail, write. Returns
    (cards, notes, seen_updates). `seen_updates` maps str(bill_id) -> change_hash for every bill
    that reached a DEFINITIVE outcome this run (carded, or read and declined by the screen or the
    writer), so the caller can record it as seen and never re-pay to screen it unless it changes.
    A bill that only hit a transient error is deliberately absent, so it retries next run. Bounded
    by max_run (bills queued for a card write) and screen_max (screens) so a cold-start stays short.

    `batch_enabled` routes the Opus write pass through the 50%-priced Message Batches API in ONE job
    (screening stays synchronous); it defaults False so a direct/test caller with an injected `ai`
    gets the synchronous path unchanged, and main() passes LEGISLATION_BATCH. The verdict space is
    identical either way, so the seen/card bookkeeping below does not branch on it. Streams progress
    to stdout (flushed) so a long run is never silent. Writes nothing itself. Fail-open."""
    key = KEY_LEGISCAN if key is None else key
    ai = ai or _default_ai
    max_run = MAX_RUN if max_run is None else max_run
    screen_max = SCREEN_MAX if screen_max is None else screen_max
    states = states or LEGISLATION_STATES
    notes = []
    seen_updates = {}
    cards = []

    def note(msg):
        notes.append(msg)
        print(msg, flush=True)     # stream live so a long cold-start shows progress, not silence

    if not key:
        note("LEGISLATION: no LEGISCAN_API_KEY; skipping (fail-open no-op).")
        return [], notes, seen_updates
    seen = _load_seen()
    # pollstate (the LegiScan timing guard) is threaded in by the caller so it can be persisted;
    # a direct/test caller that passes none gets the on-disk state, which is empty in tests.
    if pollstate is None:
        pollstate = _load_pollstate()
    now = now or datetime.datetime.now()
    screened = 0
    stop = False
    # Screened-relevant bills awaiting a card write, in discovery order. The write pass runs after
    # this loop -- as one batch (batch_enabled) or a synchronous loop -- so both share the exact
    # downstream seen/card logic. `areas` carries the screen's areas as the writer's fallback.
    pending = []
    for state in states:
        if stop:
            break
        cands = discover(key, state=state, fetch=fetch, today=today, seen=seen,
                         pollstate=pollstate, now=now)
        note("LEGISLATION[%s]: %d enacted/vetoed bill(s) moved since last run." % (state, len(cands)))
        for b, _sess in cands:
            if len(pending) >= max_run:
                note("LEGISLATION: hit LEGISLATION_MAX=%d cards; remaining bills retry next run." % max_run)
                stop = True
                break
            if screened >= screen_max:
                note("LEGISLATION: hit LEGISLATION_SCREEN_MAX=%d; remaining bills retry next run." % screen_max)
                stop = True
                break
            screened += 1
            bid, ch = str(b.get("bill_id")), (b.get("change_hash") or "")
            keep, areas, reason = screen_bill(b, ai, state=state)
            if not keep:
                _dbg("screen dropped %s %s: %s" % (state, b.get("number"), reason))
                seen_updates[bid] = ch      # definitively not relevant; do not re-screen unless it changes
                continue
            detail = bill_detail(b.get("bill_id"), key, fetch=fetch) or b
            # carry the freshest change_hash (the master list's) onto the detail for carding
            if b.get("change_hash"):
                detail["change_hash"] = b.get("change_hash")
            pending.append({"bid": bid, "ch": ch, "detail": detail, "state": state, "areas": areas})

    # Write pass: one batch job, or the synchronous per-bill path. Same verdict space either way.
    if batch_enabled and pending:
        note("LEGISLATION: batching %d card write(s) (LEGISLATION_BATCH)." % len(pending))
        verdicts = _draft_cards(pending, deadline=time.time() + BATCH_SEC)
    else:
        verdicts = {p["bid"]: write_card(p["detail"], ai, state=p["state"]) for p in pending}

    for p in pending:
        bid, ch, detail, state, areas = p["bid"], p["ch"], p["detail"], p["state"], p["areas"]
        verdict = verdicts.get(bid, WRITER_ERROR)
        if verdict is WRITER_ERROR:
            continue                        # transient: no seen record, retry next run
        if verdict is None:
            seen_updates[bid] = ch          # writer read it and declined: definitive
            continue
        if not verdict.get("areas") and areas:
            verdict["areas"] = areas        # let the screen's areas fill in if the writer returned none
        card = build_card(detail, verdict, state=state, today=today)
        cards.append(card)
        seen_updates[bid] = ch
        print("  + [%s] %s %s  %s" % (state, card.get("number") or "?", card.get("status") or "?",
                                      (card.get("title") or "")[:60]), flush=True)
    note("LEGISLATION: screened %d, drafted %d card(s)." % (screened, len(cards)))
    return cards, notes, seen_updates


# --------------------------------------------------------------------------- #
# State + persistence helpers.                                               #
# --------------------------------------------------------------------------- #
def _load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        seen = data.get("seen") if isinstance(data, dict) else None
        return seen if isinstance(seen, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _load_pollstate():
    """Load the LegiScan timing guard state -- {"polls": {opkey: iso}, "sessioncache": {state: [...]}}
    -- from the state file. Missing/malformed keys default to empty so the guard simply never fires
    (every operation reads as stale and is polled), i.e. fail-open to the old always-poll behavior."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"polls": {}, "sessioncache": {}}
        polls = data.get("polls")
        scache = data.get("sessioncache")
        return {"polls": polls if isinstance(polls, dict) else {},
                "sessioncache": scache if isinstance(scache, dict) else {}}
    except FileNotFoundError:
        return {"polls": {}, "sessioncache": {}}
    except Exception:
        return {"polls": {}, "sessioncache": {}}


def load_cards():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_cards(cards):
    import safeio
    safeio.atomic_write_text(JSON_PATH, json.dumps(cards, ensure_ascii=False, indent=2) + "\n")


def save_seen(seen, today=None, pollstate=None):
    """Persist the seen change_hash map. Stored under a "seen" key (what _load_seen reads) so
    the file can carry run metadata alongside it without touching the lookup shape. When
    `pollstate` is given, its "polls" (opkey -> last-poll ISO) and "sessioncache" (state ->
    raw session list) ride along in the same file so the LegiScan timing guard survives across
    runs; omitting it drops those keys (the guard resets to always-poll, which is safe)."""
    import safeio
    today = (today or datetime.date.today()).isoformat()
    doc = {"seen": seen, "updated": today, "count": len(seen)}
    if pollstate is not None:
        doc["polls"] = pollstate.get("polls", {})
        doc["sessioncache"] = pollstate.get("sessioncache", {})
    safeio.atomic_write_text(
        STATE_PATH,
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def append_log(rec, cap=2000):
    """Append one per-run record to the bounded run log (observability), like the opinion
    pipeline's log. Best-effort: a log failure never fails the run."""
    import safeio
    try:
        lines = []
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            pass
        lines.append(json.dumps(rec, ensure_ascii=False))
        safeio.atomic_write_text(LOG_PATH, "\n".join(lines[-cap:]) + "\n")
    except Exception as e:
        _dbg("log append failed: %s" % e)


def merge_cards(existing, new_cards):
    """Merge new cards into the existing list, keyed on bill_id. A re-carded bill
    (its change_hash moved) replaces its prior card but keeps the original first_seen,
    so a later amendment updates the synopsis without resetting the discovery date.
    Returns (merged, added_count, updated_count), newest status_date first."""
    by_id = {}
    for c in existing:
        if isinstance(c, dict) and c.get("bill_id") is not None:
            by_id[int(c["bill_id"])] = c
    added = updated = 0
    for c in new_cards:
        bid = int(c["bill_id"])
        if bid in by_id:
            c = dict(c, first_seen=by_id[bid].get("first_seen") or c.get("first_seen"))
            updated += 1
        else:
            added += 1
        by_id[bid] = c
    merged = sorted(by_id.values(),
                    key=lambda c: (c.get("status_date") or "", c.get("bill_id") or 0),
                    reverse=True)
    return merged, added, updated


def _pr_body(added, updated, cards):
    """The review-PR body: a person confirms every legislation card before it publishes."""
    lines = ["## Georgia Legislative Watch: newly enacted / vetoed law", "",
             "The watch found Georgia legislation that became law (signed, or allowed to become law "
             "without signature) or was vetoed and is relevant to a civil-litigation practice. "
             "**Every card here is held for your review** — nothing publishes on the machine alone. "
             "Read each against the enrolled bill, edit `legislation.json` on this branch if needed, "
             "and merge to publish.", "",
             "| Bill | Status | Areas | Bill |", "|---|---|---|---|"]
    for c in cards:
        lines.append("| %s | %s | %s | [%s](%s) |" % (
            c.get("number") or "?", c.get("status") or "?",
            ", ".join(c.get("areas") or []) or "—",
            (c.get("title") or "")[:80], c.get("url") or ""))
    lines += ["", "%d new, %d updated." % (added, updated), "",
              "_AI-drafted summaries; the enrolled bill is the authority._"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    apply = "--apply" in argv
    # Load the LegiScan timing guard here so run() can update it in place and we can persist it
    # below: a re-run inside an operation's min-resolution window must not spend a cache-hit query.
    pollstate = _load_pollstate()
    # run() streams its notes live; do not reprint them here. Production batches the write pass
    # (LEGISLATION_BATCH, default on); LEGISLATION_BATCH=0 is the synchronous rollback.
    cards, notes, seen_updates = run(pollstate=pollstate, batch_enabled=LEGISLATION_BATCH)

    if apply and not KEY_LEGISCAN:
        # Nothing ran (fail-open no-op); do not touch any file.
        print("LEGISLATION_CONTENT_CHANGED=0")
        return 0

    if apply:
        # Merge new cards into legislation.json (keyed on bill_id, preserving first_seen) and record
        # the seen change_hashes. Persist state on every run (even a no-card run advances seen so a
        # settled non-match is not re-screened); write the PR body only when a card actually changed.
        existing = load_cards()
        merged, added, updated = merge_cards(existing, cards)
        seen = _load_seen()
        seen.update(seen_updates)
        content_changed = bool(added or updated)
        if content_changed:
            save_cards(merged)
        save_seen(seen, pollstate=pollstate)
        append_log({"cards": len(cards), "added": added, "updated": updated,
                    "seen_total": len(seen), "notes": notes})
        pr_path = os.path.join(REPO, "scripts", "pr_body_legislation.md")
        if content_changed:
            import safeio
            safeio.atomic_write_text(pr_path, _pr_body(added, updated, cards))
        # A machine-readable signal for the workflow: did content change (open a PR) or not (skip)?
        print("LEGISLATION_CONTENT_CHANGED=%s" % ("1" if content_changed else "0"))
        print("LEGISLATION: %d added, %d updated; %d bills tracked." % (added, updated, len(seen)))

    if as_json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))
    elif not apply:
        for c in cards:
            print("  %-8s %-8s %s  [%s]" % (c["number"], c["status"],
                                            (c["title"] or "")[:70], ",".join(c["areas"]) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
