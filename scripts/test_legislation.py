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


def fake_fetch(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    op = q.get("op", [""])[0]
    if q.get("key", [""])[0] == "BADKEY":
        return json.dumps({"status": "ERROR", "alert": {"message": "Invalid API Key"}})
    if op == "getSessionList":
        return json.dumps(SESSIONS)
    if op == "getMasterList":
        return json.dumps(MASTERLIST)
    if op == "getBill":
        bid = int(q.get("id", ["0"])[0])
        return json.dumps({"status": "OK", "bill": BILLS.get(bid, {})})
    return json.dumps({"status": "ERROR", "alert": {"message": "unknown op"}})


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

    # --- api envelope handling ---
    raised = False
    try:
        L.api("getSessionList", "BADKEY", fetch=fake_fetch, state="GA")
    except L.LegiScanError:
        raised = True
    check("api raises LegiScanError on a non-OK envelope (bad key)", raised)
    payload = L.api("getSessionList", "GOODKEY", fetch=fake_fetch, state="GA")
    check("api returns the parsed OK payload", payload.get("status") == "OK" and "sessions" in payload)

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

    # --- run honors LEGISLATION_MAX ---
    capped, cnotes, _ = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai, max_run=1,
                              today=__import__("datetime").date(2026, 7, 17))
    check("run honors max_run and says remaining bills retry",
          len(capped) == 1 and any("LEGISLATION_MAX" in n for n in cnotes))

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

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
