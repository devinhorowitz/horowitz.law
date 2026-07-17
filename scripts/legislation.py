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
  LEGISLATION_STATE       LegiScan state code to watch (default "GA")
  LEGISLATION_SCREEN_MODEL relevance screen model (default claude-haiku-4-5)
  LEGISLATION_MODEL       card writer model (default claude-opus-4-8)
  LEGISLATION_MAX         cap on bills sent to the writer per run (default 40)
  LEGISLATION_DEBUG       if 1, log each step
"""
import os
import sys
import json
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
STATE_CODE   = (os.environ.get("LEGISLATION_STATE", "GA") or "GA").strip().upper()
SCREEN_MODEL = os.environ.get("LEGISLATION_SCREEN_MODEL", "claude-haiku-4-5")
WRITE_MODEL  = os.environ.get("LEGISLATION_MODEL", "claude-opus-4-8")
MAX_RUN      = int(os.environ.get("LEGISLATION_MAX", "40"))
DEBUG        = os.environ.get("LEGISLATION_DEBUG", "") == "1"

API_BASE   = "https://api.legiscan.com/"
UA         = "horowitz.law Georgia Legislative Watch (contact: via horowitz.law)"
TIMEOUT    = 45
MAX_BYTES  = 25 * 1024 * 1024   # cap any single LegiScan read; bounds memory vs a hostile response

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
def _http_get(url):
    """Default fetch seam: GET a URL, return decoded text. Byte-capped. Tests
    inject their own callable of the same shape, so no test ever hits the network."""
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


def enacted_candidates(bills, seen):
    """From master-list summaries, the bills that (a) are enacted or vetoed and
    (b) have moved since we last carded them -- a new bill_id, or a changed
    change_hash on a known one. `seen` maps str(bill_id) -> last change_hash.

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
        bid = str(b.get("bill_id"))
        if seen.get(bid) and seen.get(bid) == b.get("change_hash"):
            continue
        out.append(b)
    return out


def bill_detail(bill_id, key, fetch=None):
    """getBill -> the full bill object (title, description, status, status_date,
    progress[], history[], texts[], url, state_link, ...). Returns {} on any error,
    so a single unreachable bill drops out of the run instead of failing it."""
    try:
        data = api("getBill", key, fetch=fetch, id=bill_id)
    except (LegiScanError, urllib.error.URLError, TimeoutError, Exception) as e:
        _dbg("getBill %s failed: %s" % (bill_id, e))
        return {}
    bill = data.get("bill")
    return bill if isinstance(bill, dict) else {}


def act_number(bill):
    """The chapter/act number a bill received on enactment, from its progress trail.
    LegiScan progress events include 8 = 'Chapter/Act/Statute'. Returns '' if none
    is recorded yet (LegiScan often carries the Act number in the last_action instead)."""
    for ev in (bill.get("progress") or []):
        if isinstance(ev, dict) and int(ev.get("event") or 0) == 8:
            # LegiScan does not always split the act number out; the date is what it
            # reliably carries. Callers use this only as a presence signal.
            return str(ev.get("date") or "")
    return ""


# --------------------------------------------------------------------------- #
# The funnel: relevance screen (cheap) then the card writer (expensive).       #
# --------------------------------------------------------------------------- #
SCREEN_SYSTEM = (
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

WRITE_SYSTEM = (
    "You write for a Georgia civil-litigation and insurance-defense audience of practicing "
    "lawyers. Given a Georgia bill that has become law (or been vetoed), write a tight, "
    "neutral, plain-English card describing what the law actually changes and why a civil "
    "litigator should care. No hype, no editorializing, no legislative history recap. State "
    "what the prior rule was and what it now is where you can tell; if the text does not make "
    "a detail clear, say so rather than guessing. Ground every statement in the provided title "
    "and description -- do not invent code sections, effective dates, or dollar figures that are "
    "not present. Reply with ONLY a JSON object: {\"keep\": true|false, \"areas\": [codes], "
    "\"synopsis\": \"2-4 sentences\", \"impact\": \"one sentence, <=30 words\", "
    "\"effective_date\": \"YYYY-MM-DD or empty if not stated\"}. Set keep=false if, on a full "
    "read, the bill does not in fact affect civil litigation. Valid area codes: "
    + AREA_CODES_STR + "."
)


def _bill_brief(bill):
    """The compact text block both model tiers read: identity + title + description.
    We deliberately do NOT fetch full bill text for v1 -- LegiScan bill descriptions are
    substantive summaries, and skipping the per-document getBillText call keeps a run to
    two model calls per surviving bill and one LegiScan call per enacted bill."""
    parts = [
        "Bill: %s (%s)" % (bill.get("number") or bill.get("bill_number") or "?", STATE_CODE),
        "Status: %s" % STATUS_LABEL.get(int(bill.get("status") or 0), str(bill.get("status"))),
        "Status date: %s" % (bill.get("status_date") or ""),
        "Title: %s" % (bill.get("title") or ""),
        "Description: %s" % (bill.get("description") or ""),
    ]
    la = bill.get("last_action")
    if la:
        parts.append("Last action: %s (%s)" % (la, bill.get("last_action_date") or ""))
    return "\n".join(p for p in parts if p)


def screen_bill(bill, ai, model=None):
    """Cheap relevance gate. Returns (keep: bool, areas: list, reason: str).
    Fail-open on a model/parse error: keep the bill (route it to the writer, which
    reads more and can still decline) rather than silently dropping a real law."""
    model = model or SCREEN_MODEL
    try:
        v = ai({"model": model, "max_tokens": 256, "system": SCREEN_SYSTEM,
                "messages": [{"role": "user", "content": _bill_brief(bill)}]}, "leg-screen")
    except Exception as e:
        _dbg("screen failed, keeping: %s" % e)
        return True, [], "screen-error-kept"
    keep = v.get("relevant") is True
    areas = [a for a in (v.get("areas") or []) if a in siteconfig.AREA_CODES]
    return keep, areas, str(v.get("reason") or "")[:120]


def write_card(bill, ai, model=None):
    """The card writer. Returns a verdict dict or None if the model declines or errors.
    Errors return None (the bill drops from this run and is retried next run because its
    change_hash is only recorded on a successful card), never a partial card."""
    model = model or WRITE_MODEL
    try:
        v = ai({"model": model, "max_tokens": 900, "system": WRITE_SYSTEM,
                "messages": [{"role": "user", "content": _bill_brief(bill)}]}, "leg-write")
    except Exception as e:
        _dbg("write failed: %s" % e)
        return None
    if v.get("keep") is not True:
        return None
    if not str(v.get("synopsis") or "").strip():
        return None
    return v


def build_card(bill, verdict, today=None):
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
        "state": STATE_CODE,
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
def discover(key, fetch=None, today=None):
    """Reach LegiScan and return the enacted/vetoed bills that have moved since we
    last saw them, as (summary, session) pairs, newest sessions first. Reads
    legislation_state.json for the seen change_hashes. Fail-open: returns [] on any
    LegiScan error and logs it."""
    seen = _load_seen()
    try:
        sess_payload = api("getSessionList", key, fetch=fetch, state=STATE_CODE)
    except Exception as e:
        _dbg("getSessionList failed: %s" % e)
        return []
    sessions = sessions_to_watch(sess_payload.get("sessions"), today=today)
    cands = []
    for s in sessions:
        try:
            ml = api("getMasterList", key, fetch=fetch, id=s.get("session_id"))
        except Exception as e:
            _dbg("getMasterList %s failed: %s" % (s.get("session_id"), e))
            continue
        for b in enacted_candidates(masterlist_bills(ml), seen):
            cands.append((b, s))
    return cands


def _default_ai(body, label="call"):
    """Default model seam: delegate to update.anthropic_json (same JSON contract).
    Imported lazily so this module imports cleanly with no ANTHROPIC_API_KEY and so
    tests, which inject their own `ai`, never pull update.py in."""
    import update
    return update.anthropic_json(body, label)


def run(key=None, fetch=None, ai=None, today=None, max_run=None):
    """Full funnel: discover moved enacted/vetoed bills, screen, fetch detail, write.
    Returns (cards, notes). Writes nothing -- the caller (or main) decides whether to
    persist. Fail-open throughout."""
    key = KEY_LEGISCAN if key is None else key
    ai = ai or _default_ai
    max_run = MAX_RUN if max_run is None else max_run
    notes = []
    if not key:
        notes.append("LEGISLATION: no LEGISCAN_API_KEY; skipping (fail-open no-op).")
        return [], notes
    cands = discover(key, fetch=fetch, today=today)
    notes.append("LEGISLATION: %d enacted/vetoed bill(s) moved since last run." % len(cands))
    cards = []
    for b, _sess in cands:
        if len(cards) >= max_run:
            notes.append("LEGISLATION: hit LEGISLATION_MAX=%d; remaining bills retry next run." % max_run)
            break
        keep, areas, reason = screen_bill(b, ai)
        if not keep:
            _dbg("screen dropped %s: %s" % (b.get("number"), reason))
            continue
        detail = bill_detail(b.get("bill_id"), key, fetch=fetch) or b
        # carry the freshest change_hash (the master list's) onto the detail for carding
        if b.get("change_hash"):
            detail["change_hash"] = b.get("change_hash")
        verdict = write_card(detail, ai)
        if not verdict:
            continue
        # let the screen's areas fill in if the writer returned none
        if not verdict.get("areas") and areas:
            verdict["areas"] = areas
        cards.append(build_card(detail, verdict, today=today))
    notes.append("LEGISLATION: drafted %d card(s)." % len(cards))
    return cards, notes


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


def load_cards():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    cards, notes = run()
    for n in notes:
        print(n)
    if as_json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))
    else:
        for c in cards:
            print("  %-8s %-8s %s  [%s]" % (c["number"], c["status"],
                                            (c["title"] or "")[:70], ",".join(c["areas"]) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
