#!/usr/bin/env python3
"""Hermetic unit tests for the Georgia Legislative Watch (scripts/legislation.py). No network, no key.

Every network call is an injected `fetch` seam and every model call an injected `ai` seam, so the
whole funnel -- session resolution, the enacted/vetoed status filter, change_hash dedup, the
relevance screen, the writer, card assembly, and merge -- runs against canned LegiScan JSON and
canned model verdicts. The load-bearing invariants: only status 4 (enacted) and 5 (vetoed) card;
an unchanged change_hash is skipped so a quiet run is free; the screen fails OPEN (a model error
keeps the bill for the writer, never silently drops a real law); the writer fails CLOSED (an error
or a decline yields no card, never a partial one); no LEGISCAN_API_KEY is a clean no-op, not a crash.

Run directly: `python scripts/test_legislation.py`.
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legislation as L  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# --- a fake LegiScan: routes canned JSON by the op= (and id=) in the URL ------------------------
SESSIONS = {
    "status": "OK",
    "sessions": [
        {"session_id": 2065, "year_start": 2025, "year_end": 2026, "session_name": "2025-2026 Regular Session"},
        {"session_id": 2001, "year_start": 2025, "year_end": 2025, "session_name": "2025 Special Session"},
        {"session_id": 1899, "year_start": 2023, "year_end": 2024, "session_name": "2023-2024 Regular Session"},
        {"session_id": 1500, "year_start": 2019, "year_end": 2020, "session_name": "2019-2020 Regular Session"},
    ],
}
# One master list for the current session: an enacted tort bill, a vetoed bill, an introduced bill
# (skipped), and an enacted appropriations bill (screened out downstream, but a candidate here).
MASTERLIST = {
    "status": "OK",
    "masterlist": {
        "session": {"session_id": 2065, "session_name": "2025-2026 Regular Session"},
        "0": {"bill_id": 111, "number": "SB 68", "change_hash": "h-sb68-v1", "status": 4,
              "status_date": "2025-04-21", "url": "https://legiscan.com/GA/bill/SB68/2025",
              "title": "Tort reform; apportionment and damages",
              "description": "Revises apportionment of fault and limits certain damages.",
              "last_action": "Effective date", "last_action_date": "2025-04-21"},
        "1": {"bill_id": 222, "number": "SB 69", "change_hash": "h-sb69-v1", "status": 5,
              "status_date": "2025-05-01", "url": "https://legiscan.com/GA/bill/SB69/2025",
              "title": "Litigation financing", "description": "Regulates third-party litigation financing.",
              "last_action": "Veto", "last_action_date": "2025-05-01"},
        "2": {"bill_id": 333, "number": "HB 10", "change_hash": "h-hb10-v1", "status": 1,
              "status_date": "2025-02-01", "url": "https://legiscan.com/GA/bill/HB10/2025",
              "title": "Introduced only", "description": "Still in committee."},
        "3": {"bill_id": 444, "number": "HB 900", "change_hash": "h-hb900-v1", "status": 4,
              "status_date": "2025-04-10", "url": "https://legiscan.com/GA/bill/HB900/2025",
              "title": "General appropriations", "description": "The state budget for FY2026."},
    },
}
BILLS = {
    111: {"bill_id": 111, "number": "SB 68", "status": 4, "status_date": "2025-04-21",
          "change_hash": "h-sb68-v1", "url": "https://legiscan.com/GA/bill/SB68/2025",
          "state_link": "https://www.legis.ga.gov/legislation/68",
          "title": "Tort reform; apportionment and damages",
          "description": "Revises apportionment of fault among parties and limits certain damages.",
          "progress": [{"date": "2025-04-21", "event": 8}]},
    222: {"bill_id": 222, "number": "SB 69", "status": 5, "status_date": "2025-05-01",
          "change_hash": "h-sb69-v1", "url": "https://legiscan.com/GA/bill/SB69/2025",
          "state_link": "https://www.legis.ga.gov/legislation/69",
          "title": "Litigation financing", "description": "Regulates third-party litigation financing."},
    444: {"bill_id": 444, "number": "HB 900", "status": 4, "status_date": "2025-04-10",
          "change_hash": "h-hb900-v1", "title": "General appropriations",
          "description": "The state budget for FY2026."},
}


# --- a fake U.S. Congress: one relevant federal statute (FAAAA/motor-carrier) and one not (NDAA) ---
US_SESSIONS = {
    "status": "OK",
    "sessions": [
        {"session_id": 3000, "year_start": 2025, "year_end": 2026, "session_name": "119th Congress"},
    ],
}
US_MASTERLIST = {
    "status": "OK",
    "masterlist": {
        "session": {"session_id": 3000, "session_name": "119th Congress"},
        "0": {"bill_id": 5001, "number": "HR 100", "change_hash": "h-hr100-v1", "status": 4,
              "status_date": "2025-06-01", "url": "https://legiscan.com/US/bill/HR100/2025",
              "title": "Motor Carrier Safety and FAAAA Preemption Clarification Act",
              "description": "Amends 49 U.S.C. 14501 to clarify FAAAA preemption of negligent-hiring "
                             "claims against motor-carrier brokers."},
        "1": {"bill_id": 5002, "number": "HR 200", "change_hash": "h-hr200-v1", "status": 4,
              "status_date": "2025-06-02", "url": "https://legiscan.com/US/bill/HR200/2025",
              "title": "National Defense Authorization Act for FY2026",
              "description": "Authorizes appropriations for the Department of Defense."},
    },
}
US_BILLS = {
    5001: {"bill_id": 5001, "number": "HR 100", "status": 4, "status_date": "2025-06-01",
           "change_hash": "h-hr100-v1", "url": "https://legiscan.com/US/bill/HR100/2025",
           "title": "Motor Carrier Safety and FAAAA Preemption Clarification Act",
           "description": "Amends 49 U.S.C. 14501 to clarify FAAAA preemption of negligent-hiring "
                          "claims against motor-carrier brokers."},
    5002: {"bill_id": 5002, "number": "HR 200", "status": 4, "status_date": "2025-06-02",
           "change_hash": "h-hr200-v1", "title": "National Defense Authorization Act for FY2026",
           "description": "Authorizes appropriations for the Department of Defense."},
}


def fake_fetch(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    op = q.get("op", [""])[0]
    state = q.get("state", ["GA"])[0].upper()
    if q.get("key", [""])[0] == "BADKEY":
        return json.dumps({"status": "ERROR", "alert": {"message": "Invalid API Key"}})
    if op == "getSessionList":
        return json.dumps(US_SESSIONS if state == "US" else SESSIONS)
    if op == "getMasterList":
        sid = int(q.get("id", ["0"])[0])
        return json.dumps(US_MASTERLIST if sid == 3000 else MASTERLIST)
    if op == "getBill":
        bid = int(q.get("id", ["0"])[0])
        return json.dumps({"status": "OK", "bill": US_BILLS.get(bid) or BILLS.get(bid, {})})
    return json.dumps({"status": "ERROR", "alert": {"message": "unknown op"}})


def counting_fetch(ops):
    """Wrap fake_fetch, appending each LegiScan `op` to `ops`, so a test can assert exactly which
    operations hit the wire -- the point of the timing guard is that a skipped op makes NO call."""
    def f(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        ops.append(q.get("op", [""])[0])
        return fake_fetch(url)
    return f


def make_ai(script):
    """An `ai` seam that returns canned verdicts keyed by the call label. `script`
    maps label -> callable(body)->dict, or label -> dict."""
    def ai(body, label="call"):
        h = script.get(label)
        if h is None:
            raise AssertionError("unexpected ai label %r" % label)
        if callable(h):
            return h(body)
        return h
    return ai


def main():
    print("legislation watch:")

    # Hermetic seams. The funnel/batch L.run() calls below do NOT pass pollstate=/now=, so run()
    # falls to _load_pollstate() and datetime.datetime.now() -- both of which read live state:
    #   * _load_pollstate() reads the real committed legislation_state.json. Once the Legislative
    #     Watch commits fresh "polls" timestamps, _fresh() returns True inside the 1h master window,
    #     discover() SKIPS the getMasterList poll, 0 candidates flow, and the card-producing GA
    #     assertions fail -- a schedule-phased flake that breaks CI for ~1h after every watch run.
    #   * _load_seen() reads the real committed seen-map (7-digit bill ids; no collision with the
    #     111/222 fixtures today, but stubbed for the same hygiene as the sibling watches).
    # Stub both to empty so these runs are deterministic regardless of the state file and wall clock.
    # The dedicated timing-guard tests below are unaffected: they pass explicit pollstate=/now=, and
    # run() only calls _load_pollstate() when pollstate is None.
    L._load_seen = lambda: {}
    L._load_pollstate = lambda: {"polls": {}, "sessioncache": {}}

    # --- session resolution ---
    watched = L.sessions_to_watch(SESSIONS["sessions"], today=__import__("datetime").date(2026, 7, 17))
    ids = [s["session_id"] for s in watched]
    check("sessions_to_watch keeps the live biennium", 2065 in ids)
    check("sessions_to_watch keeps a session ending at the year-1 boundary (inclusive)", 2001 in ids)
    check("sessions_to_watch drops a biennium two+ years closed", 1899 not in ids)
    check("sessions_to_watch drops a session years past", 1500 not in ids)
    check("sessions_to_watch orders newest first", ids == sorted(ids, reverse=True))
    check("sessions_to_watch keeps a session with an unparseable year",
          any(s["session_id"] == 9 for s in L.sessions_to_watch([{"session_id": 9, "year_end": "n/a"}])))

    # --- master list flattening ---
    bills = L.masterlist_bills(MASTERLIST)
    check("masterlist_bills skips the 'session' meta entry", all(b.get("bill_id") for b in bills))
    check("masterlist_bills returns every real bill", {b["bill_id"] for b in bills} == {111, 222, 333, 444})

    # --- enacted/vetoed status filter + change_hash dedup ---
    cands = L.enacted_candidates(bills, seen={})
    cids = {b["bill_id"] for b in cands}
    check("enacted_candidates keeps status 4 (enacted) and 5 (vetoed)", 111 in cids and 222 in cids)
    check("enacted_candidates drops an introduced (status 1) bill", 333 not in cids)
    check("enacted_candidates keeps an enacted appropriations bill (screen drops it later)", 444 in cids)
    unchanged = L.enacted_candidates(bills, seen={"111": "h-sb68-v1"})
    check("enacted_candidates skips a bill whose change_hash is unchanged",
          111 not in {b["bill_id"] for b in unchanged})
    moved = L.enacted_candidates(bills, seen={"111": "h-sb68-OLD"})
    check("enacted_candidates re-includes a bill whose change_hash moved",
          111 in {b["bill_id"] for b in moved})
    # A bill LegiScan returns with no change_hash normalizes to "" and must still dedup once seen
    # (the old truthiness test re-screened it forever).
    nohash = [{"bill_id": 55, "number": "SB 5", "status": 4}]  # no change_hash key
    check("enacted_candidates dedups a hash-less bill once seen (stored as \"\")",
          55 not in {b["bill_id"] for b in L.enacted_candidates(nohash, seen={"55": ""})})
    check("enacted_candidates still screens a hash-less bill that is unseen",
          55 in {b["bill_id"] for b in L.enacted_candidates(nohash, seen={})})

    # --- resolution filter (jurisdiction-aware): ceremonial resolutions must not consume budget ---
    check("GA HR is a (skippable) resolution", L.is_resolution("HR 61", "GA"))
    check("GA SR is a (skippable) resolution", L.is_resolution("SR 3", "GA"))
    check("GA HB/SB are NOT resolutions", not L.is_resolution("HB 100", "GA") and not L.is_resolution("SB 68", "GA"))
    check("US HR is a BILL, not a resolution (must be kept)", not L.is_resolution("HR 100", "US"))
    check("US S is a BILL, not a resolution", not L.is_resolution("S 1234", "US"))
    check("US HRES/HCONRES ARE resolutions", L.is_resolution("HRES 5", "US") and L.is_resolution("HCONRES 2", "US"))
    check("US HCR/SCR (short-style concurrent resolutions) are also skipped",
          L.is_resolution("HCR 10", "US") and L.is_resolution("SCR 4", "US"))
    check("US joint resolutions (HJRES/SJRES) are NOT skipped -- they can be enacted",
          not L.is_resolution("HJRES 1", "US") and not L.is_resolution("SJRES 2", "US"))
    check("an unknown jurisdiction never skips (fail-open to screening)", not L.is_resolution("HR 1", "ZZ"))
    ga_bills = [{"bill_id": 900, "number": "HR 61", "status": 4, "change_hash": "z"},
                {"bill_id": 901, "number": "SB 68", "status": 4, "change_hash": "z"}]
    ga_cand = {b["bill_id"] for b in L.enacted_candidates(ga_bills, seen={}, state="GA")}
    check("enacted_candidates(state=GA) drops the ceremonial HR and keeps the SB",
          900 not in ga_cand and 901 in ga_cand)
    us_bills = [{"bill_id": 902, "number": "HR 100", "status": 4, "change_hash": "z"},
                {"bill_id": 903, "number": "HRES 5", "status": 4, "change_hash": "z"}]
    us_cand = {b["bill_id"] for b in L.enacted_candidates(us_bills, seen={}, state="US")}
    check("enacted_candidates(state=US) keeps the HR bill and drops the HRES resolution",
          902 in us_cand and 903 not in us_cand)

    # --- api envelope handling ---
    raised = False
    try:
        L.api("getSessionList", "BADKEY", fetch=fake_fetch, state="GA")
    except L.LegiScanError:
        raised = True
    check("api raises LegiScanError on a non-OK envelope (bad key)", raised)
    payload = L.api("getSessionList", "GOODKEY", fetch=fake_fetch, state="GA")
    check("api returns the parsed OK payload", payload.get("status") == "OK" and "sessions" in payload)

    # --- silent-zero guard: a SUCCESSFUL getSessionList listing ZERO sessions is a config anomaly
    #     (every valid state has sessions), not a quiet week, and must be surfaced loudly ---
    import io as _io
    import contextlib as _cl

    def empty_sessions_fetch(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if q.get("op", [""])[0] == "getSessionList":
            return json.dumps({"status": "OK", "sessions": []})
        return json.dumps({"status": "OK"})

    _buf = _io.StringIO()
    with _cl.redirect_stdout(_buf):
        cands0 = L.discover("GOODKEY", state="GA", fetch=empty_sessions_fetch,
                            today=__import__("datetime").date(2026, 7, 17), seen={})
    _out = _buf.getvalue()
    check("zero sessions yields no candidates", cands0 == [])
    check("a zero-session getSessionList prints a loud config-anomaly warning",
          "ZERO sessions" in _out and "GA" in _out)

    # --- relevance screen: fail-open, area filtering, drop ---
    sb68 = BILLS[111]
    keep, areas, _ = L.screen_bill(sb68, make_ai({"leg-screen": {"relevant": True, "areas": ["damages", "bogus"], "reason": "tort"}}))
    check("screen keeps a relevant bill", keep)
    check("screen filters invalid area codes", areas == ["damages"])
    drop_keep, _, _ = L.screen_bill(BILLS[444], make_ai({"leg-screen": {"relevant": False, "areas": [], "reason": "budget"}}))
    check("screen drops an irrelevant bill", not drop_keep)

    def boom(body):
        raise RuntimeError("model down")
    open_keep, _, reason = L.screen_bill(sb68, make_ai({"leg-screen": boom}))
    check("screen FAILS OPEN: a model error keeps the bill", open_keep and reason == "screen-error-kept")

    # --- writer: fail-closed on decline / error / empty synopsis ---
    good_v = {"keep": True, "areas": ["damages", "procedure"], "synopsis": "Changes apportionment.",
              "impact": "Alters how fault is divided.", "effective_date": "2025-04-21"}
    v = L.write_card(sb68, make_ai({"leg-write": good_v}))
    check("writer returns a verdict on a good card", v and v["synopsis"].startswith("Changes"))
    check("writer declines (None) on keep=false",
          L.write_card(sb68, make_ai({"leg-write": {"keep": False}})) is None)
    check("writer declines (None) on an empty synopsis",
          L.write_card(sb68, make_ai({"leg-write": {"keep": True, "synopsis": "  "}})) is None)
    check("writer signals a TRANSIENT error distinctly (WRITER_ERROR, not None)",
          L.write_card(sb68, make_ai({"leg-write": boom})) is L.WRITER_ERROR)

    # --- card assembly ---
    card = L.build_card(sb68, good_v, today=__import__("datetime").date(2026, 7, 17))
    check("card is keyed on the integer bill_id", card["bill_id"] == 111)
    check("card carries the normalized status", card["status"] == "enacted")
    check("card carries the enactment date and change_hash",
          card["status_date"] == "2025-04-21" and card["change_hash"] == "h-sb68-v1")
    check("card filters areas to the taxonomy", card["areas"] == ["damages", "procedure"])
    check("card keeps a valid effective_date", card["effective_date"] == "2025-04-21")
    bad_eff = L.build_card(sb68, dict(good_v, effective_date="soon"), today=__import__("datetime").date(2026, 7, 17))
    check("card blanks a malformed effective_date", bad_eff["effective_date"] == "")

    # --- full run: no key is a clean no-op ---
    cards, notes, seen = L.run(key="", fetch=fake_fetch, ai=make_ai({}))
    check("run with no key is a fail-open no-op",
          cards == [] and seen == {} and any("no LEGISCAN_API_KEY" in n for n in notes))

    # --- full run: end to end, screen drops the appropriations bill, writer cards the rest ---
    def screen_router(body):
        txt = body["messages"][0]["content"]
        relevant = "appropriations" not in txt.lower() and "budget" not in txt.lower()
        return {"relevant": relevant, "areas": ["damages"], "reason": "x"}

    def write_router(body):
        txt = body["messages"][0]["content"]
        return {"keep": True, "areas": ["damages"], "synopsis": "Synopsis for " + txt.split("\n")[0],
                "impact": "It matters.", "effective_date": ""}

    ai = make_ai({"leg-screen": screen_router, "leg-write": write_router})
    cards, notes, seen = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai,
                               today=__import__("datetime").date(2026, 7, 17))
    got = {c["bill_id"] for c in cards}
    check("run cards the enacted tort bill and the vetoed bill", 111 in got and 222 in got)
    check("run's screen dropped the appropriations bill", 444 not in got)
    check("run never cards an introduced bill", 333 not in got)
    check("run records a carded bill as seen (its change_hash)", seen.get("111") == "h-sb68-v1")
    check("run records a screen-dropped bill as seen (won't re-screen unless it changes)",
          seen.get("444") == "h-hb900-v1")
    check("run never records an introduced bill in seen", "333" not in seen)

    # a transient writer error must NOT be recorded seen (so it retries next run)
    err_ai = make_ai({"leg-screen": {"relevant": True, "areas": [], "reason": "x"},
                      "leg-write": boom})
    _, _, seen_err = L.run(key="GOODKEY", fetch=fake_fetch, ai=err_ai,
                           today=__import__("datetime").date(2026, 7, 17))
    check("a transient writer error leaves the bill un-seen (retries next run)",
          "111" not in seen_err and "222" not in seen_err)

    # --- federal overlay (LegiScan state="US") ---
    check("federal screen is STRICT (default DROP)", "default is DROP" in L._screen_system("US"))
    check("georgia screen is PERMISSIVE", "PERMISSIVE" in L._screen_system("GA"))
    check("federal writer prompt frames reaching a Georgia practice",
          "GEORGIA civil practice" in L._write_system("US"))
    us_bill = US_BILLS[5001]
    check("build_card stamps the federal jurisdiction on a US card",
          L.build_card(us_bill, good_v, state="US")["state"] == "US")
    check("_bill_brief labels the jurisdiction for the model",
          "federal (U.S. Congress)" in L._bill_brief(us_bill, "US"))

    # A strict federal screen (drops NDAA/appropriations, keeps the FAAAA statute); the writer keeps.
    def us_screen(body):
        txt = body["messages"][0]["content"].lower()
        rel = ("faaaa" in txt or "motor carrier" in txt or "14501" in txt) and "appropriations" not in txt
        return {"relevant": rel, "areas": ["auto"], "reason": "x"}

    us_ai = make_ai({"leg-screen": us_screen, "leg-write": write_router})
    fed_cards, fed_notes, _ = L.run(key="GOODKEY", fetch=fake_fetch, ai=us_ai, states=["US"],
                                    today=__import__("datetime").date(2026, 7, 17))
    fed_ids = {c["bill_id"] for c in fed_cards}
    check("federal run cards the FAAAA / motor-carrier statute", 5001 in fed_ids)
    check("federal run drops the NDAA / appropriations statute", 5002 not in fed_ids)
    check("federal card carries state US", all(c["state"] == "US" for c in fed_cards))

    # Both jurisdictions in one run, into one card set keyed on the globally-unique bill_id.
    both_ai = make_ai({"leg-screen": lambda b: (us_screen(b) if "u.s. congress" in b["messages"][0]["content"].lower()
                                                else screen_router(b)),
                       "leg-write": write_router})
    both, bnotes, _ = L.run(key="GOODKEY", fetch=fake_fetch, ai=both_ai, states=["GA", "US"],
                            today=__import__("datetime").date(2026, 7, 17))
    both_ids = {c["bill_id"] for c in both}
    check("multi-state run carries Georgia and federal cards together",
          111 in both_ids and 5001 in both_ids)
    check("multi-state run drops both jurisdictions' noise", 444 not in both_ids and 5002 not in both_ids)
    check("multi-state run notes each jurisdiction", any("[GA]" in n for n in bnotes) and any("[US]" in n for n in bnotes))

    # --- run honors LEGISLATION_MAX ---
    capped, cnotes, _ = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai, max_run=1, states=["GA"],
                              today=__import__("datetime").date(2026, 7, 17))
    check("run honors max_run and says remaining bills retry",
          len(capped) == 1 and any("hit LEGISLATION_MAX" in n for n in cnotes))

    # --- run honors the screen cap (bounds a cold-start; the rest rolls to next run) ---
    scapped, snotes, sseen = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai, screen_max=1, states=["GA"],
                                   today=__import__("datetime").date(2026, 7, 17))
    check("run honors screen_max and stops after screening the cap",
          any("LEGISLATION_SCREEN_MAX=1" in n for n in snotes) and len(sseen) <= 1)
    check("run's final note reports screened and drafted counts",
          any(n.startswith("LEGISLATION: screened ") for n in snotes))

    # --- merge_cards: add vs update, first_seen preserved, sorted by status_date desc ---
    c1 = L.build_card(BILLS[222], {"keep": True, "areas": [], "synopsis": "s", "impact": "i", "effective_date": ""})
    existing = [dict(card, first_seen="2025-04-22")]  # SB68, already carded earlier
    updated_card = dict(card, synopsis="amended synopsis")
    merged, added, updated = L.merge_cards(existing, [updated_card, c1])
    check("merge adds a genuinely new card", added == 1)
    check("merge updates an existing card in place", updated == 1)
    check("merge preserves the original first_seen on an update",
          next(c for c in merged if c["bill_id"] == 111)["first_seen"] == "2025-04-22")
    check("merge sorts newest status_date first", merged[0]["status_date"] >= merged[-1]["status_date"])

    # --- courtesy pacer (fake clock; no real sleeping) ---
    import unittest.mock as _mock
    clock = [1000.0]
    slept = []

    class _FakeTime:
        @staticmethod
        def monotonic():
            return clock[0]

        @staticmethod
        def sleep(s):
            slept.append(s)
            clock[0] += s

    with _mock.patch.object(L, "time", _FakeTime), _mock.patch.object(L, "MIN_INTERVAL", 1.0):
        L._last_call[0] = clock[0]           # a call just happened at t=1000
        clock[0] = 1000.3                    # 0.3s later
        L._pace()
        check("pacer waits the remainder of the interval", slept and abs(slept[-1] - 0.7) < 1e-6)
        slept.clear()
        L._last_call[0] = 0.0                # last call far in the past
        clock[0] = 5000.0
        L._pace()
        check("pacer does not wait once the interval has already elapsed", slept == [])
    with _mock.patch.object(L, "time", _FakeTime), _mock.patch.object(L, "MIN_INTERVAL", 0.0):
        slept.clear()
        L._last_call[0] = clock[0]
        L._pace()
        check("pacer is disabled at LEGISCAN_MIN_INTERVAL=0", slept == [])

    # --- LegiScan timing guard (page-7 min-resolution table): never spend a cache-hit query ---
    # LegiScan flags a poll faster than an operation's data-change resolution as a "cache hit": it
    # serves cached JSON but STILL debits a query. The guard persists the last-poll time per op and
    # skips a re-poll inside its window, so re-runs / a tightened schedule can't burn the quota.
    import datetime as _dt
    now0 = _dt.datetime(2026, 7, 17, 12, 0, 0)
    today0 = _dt.date(2026, 7, 17)

    check("_fresh is False for a never-polled op (must poll)", not L._fresh({}, "session:GA", 3600, now0))
    check("_fresh is True inside the window (a re-poll would be a cache hit)",
          L._fresh({"session:GA": (now0 - _dt.timedelta(minutes=30)).isoformat()}, "session:GA", 3600, now0))
    check("_fresh is False past the window (data may have changed -- poll)",
          not L._fresh({"session:GA": (now0 - _dt.timedelta(hours=2)).isoformat()}, "session:GA", 3600, now0))
    check("_fresh is False for a future timestamp (clock skew -> poll, never trust it)",
          not L._fresh({"session:GA": (now0 + _dt.timedelta(hours=1)).isoformat()}, "session:GA", 3600, now0))
    check("_fresh is False for an unparseable timestamp (poll)",
          not L._fresh({"session:GA": "not-a-date"}, "session:GA", 3600, now0))

    # First discover polls both operations, records their timestamps, and caches the session list.
    ps = {}
    ops1 = []
    c1 = L.discover("GOODKEY", state="GA", fetch=counting_fetch(ops1), today=today0,
                    seen={}, pollstate=ps, now=now0)
    check("first discover polls getSessionList and getMasterList",
          "getSessionList" in ops1 and "getMasterList" in ops1)
    check("first discover records the session and master poll timestamps",
          ps["polls"].get("session:GA") and any(k.startswith("master:") for k in ps["polls"]))
    check("first discover caches the raw session list for a later skipped poll", ps["sessioncache"].get("GA"))
    check("first discover still returns the moved candidates", bool(c1))

    # A minute later, same pollstate: both windows are open, so NO LegiScan call is made at all.
    ops2 = []
    c2 = L.discover("GOODKEY", state="GA", fetch=counting_fetch(ops2), today=today0,
                    seen={}, pollstate=ps, now=now0 + _dt.timedelta(minutes=1))
    check("a re-run inside both windows makes ZERO LegiScan calls (no cache-hit spend)", ops2 == [])
    check("a fully-skipped re-run yields no candidates (getMasterList never re-polled)", c2 == [])

    # After 90 minutes getSessionList is still cached (24h window) but getMasterList re-polls (1h).
    ops3 = []
    L.discover("GOODKEY", state="GA", fetch=counting_fetch(ops3), today=today0,
               seen={}, pollstate=ps, now=now0 + _dt.timedelta(minutes=90))
    check("after 90m getSessionList stays cached but getMasterList re-polls",
          "getSessionList" not in ops3 and "getMasterList" in ops3)

    # After 25 hours the session-list window has also closed, so getSessionList re-polls.
    ops4 = []
    L.discover("GOODKEY", state="GA", fetch=counting_fetch(ops4), today=today0,
               seen={}, pollstate=ps, now=now0 + _dt.timedelta(hours=25))
    check("after 24h getSessionList re-polls", "getSessionList" in ops4)

    # run() threads the guard end to end: a rapid re-run spends zero queries and drafts nothing.
    rps = {}
    L.run(key="GOODKEY", fetch=counting_fetch([]), ai=ai, states=["GA"],
          today=today0, pollstate=rps, now=now0)
    runops2 = []
    c_re, _, _ = L.run(key="GOODKEY", fetch=counting_fetch(runops2), ai=ai, states=["GA"],
                       today=today0, pollstate=rps, now=now0 + _dt.timedelta(minutes=2))
    check("run() threads the timing guard: a rapid re-run makes zero LegiScan calls", runops2 == [])
    check("run()'s guarded re-run drafts no cards", c_re == [])

    # --- batched write pass (LEGISLATION_BATCH): screen stays synchronous, the Opus writes go as ONE
    #     Message Batches job. Stub batch.run so no network; assert the verdict space matches the sync
    #     path (keep -> card+seen, decline -> seen, per-request error / whole-batch defer -> un-seen). ---
    import batch as _B
    _real_run = _B.run
    screen_keep = make_ai({"leg-screen": screen_router})   # drops the appropriations bill (444), keeps 111/222

    def _fake_batch(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        # custom_id is the bill_id: 111 kept, 222 declined, anything else an errored line.
        out = {}
        for r in reqs:
            cid = r["custom_id"]
            if cid == "111":
                out[cid] = {"ok": True, "text": json.dumps(
                    {"keep": True, "areas": ["damages"], "synopsis": "Changes apportionment.",
                     "impact": "It matters.", "effective_date": ""})}
            elif cid == "222":
                out[cid] = {"ok": True, "text": json.dumps({"keep": False})}
            else:
                out[cid] = {"ok": False, "type": "errored"}
        return out

    _B.run = _fake_batch
    try:
        bcards, bnotes, bseen = L.run(key="GOODKEY", fetch=fake_fetch, ai=screen_keep, states=["GA"],
                                      today=__import__("datetime").date(2026, 7, 17), batch_enabled=True)
    finally:
        _B.run = _real_run
    bids = {c["bill_id"] for c in bcards}
    check("batch write cards the kept bill", 111 in bids)
    check("batch write does not card the declined bill", 222 not in bids)
    check("batch write records the carded bill seen", bseen.get("111") == "h-sb68-v1")
    check("batch write records the declined bill seen (definitive)", "222" in bseen)
    check("batch write records the screen-dropped appropriations bill seen", "444" in bseen)
    check("batch run announces the batch", any("batching" in n for n in bnotes))

    # A whole-batch timeout defers EVERY write (all un-seen, retry next run); only the screen drop stays seen.
    def _timeout_batch(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        raise _B.BatchTimeout("bid", "still running")

    _B.run = _timeout_batch
    try:
        tcards, _, tseen = L.run(key="GOODKEY", fetch=fake_fetch, ai=screen_keep, states=["GA"],
                                 today=__import__("datetime").date(2026, 7, 17), batch_enabled=True)
    finally:
        _B.run = _real_run
    check("batch timeout drafts no cards", tcards == [])
    check("batch timeout leaves the writes un-seen (retry); only the screen drop is seen",
          "111" not in tseen and "222" not in tseen and "444" in tseen)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
