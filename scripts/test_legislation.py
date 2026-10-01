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
import tempfile
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


def test_recall(check, L, make_ai, fake_fetch, BILLS):
    """The RECALL check over screen drops, and the invariant it exists to hold.

    THE BUG. The screen is one cheap Haiku roll and its verdict was final AND permanent: a dropped
    bill went into `seen` with its change_hash, and legislation.py never re-screens a bill whose
    hash has not moved -- which an enacted bill's never does. One bad roll removed a law from the
    feed forever, silently, the reason going only to _dbg.

    It happened to HB945 (2026): Title 7 account holds for suspected exploitation of eligible
    adults AND cease-and-desist against unregistered litigation financiers plus registrant
    disclosures. Carded by the 2026-09-13 run, dropped by the 2026-09-17 run -- same bill, same
    change_hash 1ea86c7e5c71103ba59778b541e8bb45, opposite verdicts -- and the drop is the one that
    would have stood. It contradicts the Georgia screen's own instruction ("be PERMISSIVE ... DROP
    only what is clearly unrelated") for a bill whose caption says "litigation finance". Nothing in
    the watch noticed; it surfaced only because a stale review branch forced two passes.

    Fixture 222 is that bill's shape ("Litigation financing"), and 444 is a drop that should stand
    ("General appropriations"), so the pair below is the real miss and its control."""
    import datetime, json, os, tempfile

    # --- the instruction, pinned. A prompt fix can only be guarded by asserting the prompt. ---
    ga = L._recall_system("GA")
    check("recall prompt audits the DECISION, not the bill", "audit a triage DECISION" in ga)
    check("recall prompt carries Georgia's permissive bar",
          "CLEARLY unrelated" in ga and "permissive" in ga)
    check("recall prompt says one covered provision is enough",
          "one covered provision is enough" in ga)
    check("recall prompt refuses thinness as a ground",
          "Do NOT answer SUSPECT merely because" in ga and "the brief is thin" in ga)
    us = L._recall_system("US")
    check("recall prompt applies the STRICTER federal bar for US", "DIRECTLY changes" in us)
    check("the federal bar is not the Georgia one", "CLEARLY unrelated" not in us)

    # --- recall_drop: verdicts, and a failed audit that is NOT one ---
    drop = {"state": "GA", "number": "HB 945", "reason": "banking regulation, not civil litigation",
            "brief": "Title: Banking and finance; litigation finance registration"}
    sus, note = L.recall_drop(drop, make_ai({"leg-recall": {"suspect": True, "note": "brief names litigation finance"}}))
    check("recall_drop reports a suspect verdict", sus and "litigation finance" in note)
    ok, _ = L.recall_drop(drop, make_ai({"leg-recall": {"suspect": False, "note": "stands"}}))
    check("recall_drop reports a clean verdict", ok is False)
    # An auditor error used to come back (False, "") -- the same as a clean audit -- so run() logged
    # the drop "ok" and locked it in seen on no audit at all.
    def boom(_body):
        raise RuntimeError("auditor down")
    failed, why = L.recall_drop(drop, make_ai({"leg-recall": boom}))
    check("recall_drop reports a failed audit as None, not as a clean verdict", failed is None)
    check("recall_drop carries the failure's reason", "auditor down" in why, why)
    check("recall_drop treats a response with no boolean verdict as a failed audit",
          L.recall_drop(drop, make_ai({"leg-recall": {"note": "stands"}}))[0] is None
          and L.recall_drop(drop, make_ai({"leg-recall": {"suspect": "false"}}))[0] is None)
    import update
    def dead_key(_body):
        raise update.ConfigError("invalid x-api-key")
    try:
        L.recall_drop(drop, make_ai({"leg-recall": dead_key}))
        check("recall_drop re-raises a ConfigError (it is not about this drop)", False)
    except update.ConfigError:
        check("recall_drop re-raises a ConfigError (it is not about this drop)", True)

    # --- end to end: the HB945 miss, and its control, in one run ---
    # The screen drops BOTH. The auditor clears the appropriations bill and flags the litigation
    # one. 222 must reach the writer and be carded; 444 must stay dropped and recorded seen.
    def screen(body):
        txt = body["messages"][0]["content"]
        return {"relevant": False, "areas": [], "reason":
                "budget" if "appropriations" in txt.lower() else "banking regulation, not civil litigation"}
    def recall(body):
        txt = body["messages"][0]["content"]
        suspect = "litigation financing" in txt.lower()
        return {"suspect": suspect, "note": "brief names litigation financing" if suspect else "budget bill"}
    def write(body):
        return {"keep": True, "areas": ["procedure"], "synopsis": "Regulates third-party litigation financing.",
                "impact": "Funded-claim disclosure duties.", "effective_date": ""}
    ai = make_ai({"leg-screen": screen, "leg-recall": recall, "leg-write": write})

    real = L.DROPS_PATH
    with tempfile.TemporaryDirectory() as d:
        L.DROPS_PATH = os.path.join(d, "drops.jsonl")
        try:
            cards, notes, seen = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai, states=["GA"],
                                       today=datetime.date(2026, 7, 17))
            ids = {c["bill_id"] for c in cards}
            check("a SUSPECT screen drop is escalated and carded (the HB945 recovery)", 222 in ids)
            check("a drop that stands is not carded", 444 not in ids)
            check("the escalation is announced", any("recall ESCALATED" in n for n in notes))
            check("the run reports the audit", any("recall audited" in n for n in notes))

            # THE INVARIANT: never both dropped and locked out. 444's drop stood, so it is settled
            # and correctly locked; 222 was escalated and carded, so it is seen on the WRITER's
            # verdict, not the screen's.
            check("a standing drop is recorded seen (settled, never re-screened)",
                  seen.get("444") == BILLS[444]["change_hash"])
            check("the escalated bill is seen on the writer's verdict", seen.get("222") == BILLS[222]["change_hash"])

            # --- the log: every drop recorded, with the brief the screen read and the verdict ---
            recs = [json.loads(ln) for ln in open(L.DROPS_PATH, encoding="utf-8") if ln.strip()]
            by = {r["number"]: r for r in recs}
            # The fixture master list carries more than the two bills of interest, so assert on the
            # bills rather than a count: every drop logged, and every one carrying a verdict.
            check("the litigation bill and the appropriations bill are both logged",
                  "SB 69" in by and "HB 900" in by)
            check("every logged drop carries a recall verdict",
                  all(r.get("recall") for r in recs), str([r.get("recall") for r in recs]))
            check("the log keeps the screen's reason", "banking" in by["SB 69"]["reason"])
            check("the log keeps the BRIEF the screen actually read -- so an audit sees the same evidence",
                  "Litigation financing" in by["SB 69"]["brief"])
            check("the log records the recall verdict", by["SB 69"]["recall"] == "escalated"
                  and by["HB 900"]["recall"] == "ok")
            check("the log carries no private plumbing keys",
                  not any(k.startswith("_") for r in recs for k in r))

            # Append-only: a second run's drops are added, never a rewrite of the file. This is why
            # the log cannot conflict the way opinions_rejections.jsonl did on a review branch (#336).
            n_before = len(open(L.DROPS_PATH, encoding="utf-8").read().splitlines())
            L.log_drops([{"bill_id": "999", "number": "HB 1", "recall": "ok"}])
            after = open(L.DROPS_PATH, encoding="utf-8").read().splitlines()
            check("the drop log is APPEND-ONLY (earlier lines untouched)",
                  len(after) == n_before + 1 and "SB 69" in after[0] + after[1])

            # --- the kill switch restores the exact prior behaviour ---
            L.RECALL = False
            try:
                _, _, seen_off = L.run(key="GOODKEY", fetch=fake_fetch,
                                       ai=make_ai({"leg-screen": screen, "leg-write": write}),
                                       states=["GA"], today=datetime.date(2026, 7, 17))
                check("LEGISLATION_RECALL=off locks every drop again, as before",
                      seen_off.get("222") == BILLS[222]["change_hash"]
                      and seen_off.get("444") == BILLS[444]["change_hash"])
            finally:
                L.RECALL = True

            def logged():
                return [json.loads(ln) for ln in open(L.DROPS_PATH, encoding="utf-8") if ln.strip()]

            # --- a FAILED audit: logged "error" with its reason, counted, and left UN-SEEN ---
            # The auditor errors on the appropriations bill only; the other drops audit normally.
            def flaky(body):
                if "appropriations" in body["messages"][0]["content"].lower():
                    raise RuntimeError("auditor timed out")
                return recall(body)
            L.DROPS_PATH = os.path.join(d, "failed.jsonl")
            _, fnotes, fseen = L.run(key="GOODKEY", fetch=fake_fetch,
                                     ai=make_ai({"leg-screen": screen, "leg-recall": flaky, "leg-write": write}),
                                     states=["GA"], today=datetime.date(2026, 7, 17))
            f444 = [r for r in logged() if r["number"] == "HB 900"]
            check("a failed audit is logged recall 'error', never 'ok'",
                  f444 and all(r["recall"] == "error" for r in f444), str([r.get("recall") for r in f444]))
            check("the failed audit's reason is on the log record",
                  all("auditor timed out" in r.get("recall_error", "") for r in f444))
            check("a failed audit is NOT recorded seen, so the bill is re-screened next run", "444" not in fseen)
            check("the drops that did audit still settle or escalate",
                  fseen.get("111") == "h-sb68-v1" and fseen.get("222") == BILLS[222]["change_hash"])
            check("the run reports the failures",
                  any("recall audited" in n and " %d failed" % len(f444) in n for n in fnotes), str(fnotes))

            # --- a ConfigError stops the pass: no further audit calls, every drop left un-seen ---
            calls = []
            def dead_auditor(_body):
                calls.append(1)
                raise update.ConfigError("invalid x-api-key")
            L.DROPS_PATH = os.path.join(d, "config.jsonl")
            ccards, cnotes, cseen = L.run(key="GOODKEY", fetch=fake_fetch,
                                          ai=make_ai({"leg-screen": screen, "leg-recall": dead_auditor,
                                                      "leg-write": write}),
                                          states=["GA"], today=datetime.date(2026, 7, 17))
            crecs = logged()
            check("a ConfigError aborts the recall pass after one call", len(calls) == 1, str(len(calls)))
            check("a ConfigError leaves every drop un-seen and escalates none", not cseen and not ccards, str(cseen))
            check("a ConfigError logs every drop recall 'error' with the reason",
                  crecs and all(r["recall"] == "error" and "ConfigError" in r.get("recall_error", "")
                                for r in crecs))
            check("the abort is announced and every drop counted failed",
                  any("recall ABORTED" in n for n in cnotes)
                  and any("recall audited %d screen drop(s), %d failed" % (len(crecs), len(crecs)) in n
                          for n in cnotes), str(cnotes))

            # --- the retry cap: after RECALL_MAX_FAILS failed audits of the SAME drop, the writer
            #     decides it without another audit. A failure on an older change_hash does not count. ---
            L.DROPS_PATH = os.path.join(d, "capped.jsonl")
            L.log_drops([{"bill_id": "444", "number": "HB 900", "change_hash": BILLS[444]["change_hash"],
                          "recall": "error"}] * L.RECALL_MAX_FAILS
                        + [{"bill_id": "111", "number": "SB 68", "change_hash": "h-sb68-OLD",
                            "recall": "error"}] * L.RECALL_MAX_FAILS)
            audited = []
            def watched(body):
                audited.append(body["messages"][0]["content"].lower())
                return recall(body)
            kcards, _, kseen = L.run(key="GOODKEY", fetch=fake_fetch,
                                     ai=make_ai({"leg-screen": screen, "leg-recall": watched, "leg-write": write}),
                                     states=["GA"], today=datetime.date(2026, 7, 17))
            k444 = [r for r in logged() if r["number"] == "HB 900" and r.get("recall") != "error"]
            check("a drop at the failure cap is not audited again", not any("appropriations" in t for t in audited))
            check("a drop at the failure cap goes to the writer and is settled on its verdict",
                  444 in {c["bill_id"] for c in kcards} and kseen.get("444") == BILLS[444]["change_hash"])
            check("the capped escalation says why on the log",
                  k444 and all(r["recall"] == "escalated" and "earlier audits failed" in r.get("recall_note", "")
                               for r in k444), str(k444))
            check("failures on an older change_hash do not count toward the cap",
                  any("tort reform" in t for t in audited) and kseen.get("111") == "h-sb68-v1")
        finally:
            L.DROPS_PATH = real
    print("  ok   recall check over screen drops (HB945 miss + control, log, invariant, kill switch, "
          "failed audits, ConfigError, retry cap)")


def test_carry(check, L, make_ai, fake_fetch):
    """A write batch still running at its deadline is CARRIED, not abandoned (run 36318024609 paid for
    9 writes it never recorded). The next run collects it first and applies each result ONLY to the
    bill and version its custom_id names; an unusable or mismatched result re-queues that bill."""
    import contextlib
    import datetime
    import io
    import time
    import batch as B
    import watchbatch as W
    print("carried write batches:")
    today = datetime.date(2026, 7, 17)
    cid111, cid222 = L._carry_cid("111", "h-sb68-v1"), L._carry_cid("222", "h-sb69-v1")
    check("the custom_id names the bill AND its version", cid111 == "111-hsb68v1")
    check("the custom_id is a valid batch id even for an odd hash",
          B.CUSTOM_ID_RE.match(L._carry_cid("9", "a:b/c" * 30)) is not None)
    screened = []

    def screen(body):
        txt = body["messages"][0]["content"]
        screened.append(txt)
        relevant = "appropriations" not in txt.lower() and "budget" not in txt.lower()
        return {"relevant": relevant, "areas": ["damages"], "reason": "x"}

    ai = make_ai({"leg-screen": screen, "leg-recall": {"suspect": False, "note": "drop stands"}})
    real = (B.run, B.status, B.collect, L._load_seen)

    def keep(syn):
        return {"ok": True, "text": json.dumps({"keep": True, "areas": ["damages"], "synopsis": syn,
                                                "impact": "i", "effective_date": ""})}

    def go(book, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = L.run(key="GOODKEY", fetch=fake_fetch, ai=ai, states=["GA"], today=today,
                        batch_enabled=True, carry=book, **kw)
        return out, buf.getvalue()

    def screened_bills():
        return {t for t in screened if "SB 68" in t or "SB 69" in t}

    try:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "watch_batches.json")
            # ---- 1. the write batch is still running at the deadline: carry it ----
            sent = []

            def slow(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                sent.extend(r["custom_id"] for r in reqs)
                on_submit("msgbatch_A")
                raise B.BatchTimeout("msgbatch_A", "batch msgbatch_A still in_progress at deadline")

            B.run = slow
            book = W.CarryBook.load("legislation", persist=True, path=path)
            (cards, _notes, seen), out = go(book)
            book.save()
            disk = W.load_carries("legislation", path)
            check("deferral: no cards, and the paid batch is carried",
                  cards == [] and [r["id"] for r in disk] == ["msgbatch_A"])
            check("deferral: the carry names each bill and version it covers",
                  set(disk[0]["items"]) == {cid111, cid222} == set(sent))
            item = disk[0]["items"][cid111]
            check("deferral: the carried item holds what a card needs, and the prior seen hash",
                  item["bid"] == "111" and item["ch"] == "h-sb68-v1" and item["prev"] is None
                  and item["detail"]["title"].startswith("Tort reform") and disk[0]["at"])
            check("deferral: the carried bills stay un-seen; the settled screen drop is seen",
                  "111" not in seen and "222" not in seen and "444" in seen)
            check("deferral: the carrying line is logged",
                  "  . carrying 1 batch(es) to next run: msgbatch_A" in out, out)

            # ---- 2. next run, the batch is STILL running: leave its bills alone ----
            B.status = lambda bid, label="batch": {"id": bid, "processing_status": "in_progress"}

            def no_batch(*_a, **_kw):
                raise AssertionError("nothing should be submitted")

            B.run = no_batch
            screened.clear()
            book = W.CarryBook.load("legislation", persist=True, path=path)
            (cards, notes, seen), out = go(book)
            check("in flight: the carried bills are not screened again", not screened_bills(), str(screened))
            check("in flight: nothing is re-submitted, nothing carded, nothing marked seen",
                  cards == [] and "111" not in seen and "222" not in seen)
            check("in flight: the carry is kept", [r["id"] for r in book.carries] == ["msgbatch_A"])
            check("in flight: the run says so", any("still in a carried write batch" in n for n in notes))

            # ---- 3. next run, the batch has ENDED: apply only the matching ids ----
            B.status = lambda bid, label="batch": {"id": bid, "processing_status": "ended", "results_url": "u"}
            B.collect = lambda obj, label="batch": {
                cid111: keep("CARRIED synopsis."),
                cid222: {"ok": False, "type": "expired"},
                "999-hzzz": keep("A stray result for a bill this carry never covered."),
            }
            fresh = []

            def fresh_batch(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                fresh.extend(r["custom_id"] for r in reqs)
                return {r["custom_id"]: {"ok": True, "text": json.dumps({"keep": False})} for r in reqs}

            B.run = fresh_batch
            screened.clear()
            book = W.CarryBook.load("legislation", persist=True, path=path)
            (cards, notes, seen), out = go(book)
            by_id = {c["bill_id"]: c for c in cards}
            check("collected: the carried card is applied to its own bill",
                  by_id.get(111, {}).get("synopsis") == "CARRIED synopsis.")
            check("collected: the applied bill is neither screened nor written again",
                  not any("SB 68" in t for t in screened) and cid111 not in fresh, str(screened))
            check("collected: the expired result's bill is re-queued (screened and written fresh)",
                  any("SB 69" in t for t in screened) and set(fresh) == {cid222}, str(fresh))
            check("collected: both bills reach a definitive outcome and are seen",
                  seen.get("111") == "h-sb68-v1" and seen.get("222") == "h-sb69-v1")
            check("collected: a stray id in the results is never applied", 999 not in by_id)
            check("collected: the per-batch line reports applied and re-queued",
                  "  . collected carried batch msgbatch_A (1 results applied, 1 re-queued)" in out, out)
            check("collected: the carry is gone once collected", book.carries == [])

            # ---- 4. the bill MOVED after the batch was sent: the old card is not applied ----
            stale_cid = L._carry_cid("111", "h-sb68-OLD")
            stale = [{"id": "msgbatch_S", "at": W._iso(time.time()), "items": {stale_cid: {
                "bid": "111", "ch": "h-sb68-OLD", "state": "GA", "areas": [], "prev": None,
                "detail": {"bill_id": 111, "number": "SB 68", "title": "old"}}}}]
            B.collect = lambda obj, label="batch": {stale_cid: keep("STALE synopsis.")}

            def write_batch(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                fresh.extend(r["custom_id"] for r in reqs)
                return {r["custom_id"]: keep("Fresh synopsis.") for r in reqs}

            B.run = write_batch
            fresh.clear()
            screened.clear()
            (cards, _notes, seen), out = go(W.CarryBook("legislation", stale))
            syn = {c["bill_id"]: c["synopsis"] for c in cards}
            check("moved: the stale carried card is not applied", "STALE synopsis." not in syn.values())
            check("moved: the current version is screened and written as usual",
                  syn.get(111) == "Fresh synopsis." and cid111 in fresh and any("SB 68" in t for t in screened),
                  "%r %r %r" % (syn, fresh, screened))
            check("moved: reported as re-queued",
                  "msgbatch_S (0 results applied, 1 re-queued)" in out, out)

            # ---- 5. settled since the batch was sent (seen moved): not applied ----
            L._load_seen = lambda: {"111": "h-sb68-v0"}
            settled = [{"id": "msgbatch_P", "at": W._iso(time.time()), "items": {cid111: {
                "bid": "111", "ch": "h-sb68-v1", "state": "GA", "areas": [], "prev": "h-sb68-older",
                "detail": {"bill_id": 111, "number": "SB 68", "title": "t"}}}}]
            B.collect = lambda obj, label="batch": {cid111: keep("PREV-MISMATCH synopsis.")}
            fresh.clear()
            (cards, _notes, _seen), out = go(W.CarryBook("legislation", settled))
            check("settled since: a carry whose prior seen hash no longer matches is re-queued",
                  "PREV-MISMATCH synopsis." not in {c["synopsis"] for c in cards}
                  and cid111 in fresh and "msgbatch_P (0 results applied, 1 re-queued)" in out, out)
            L._load_seen = real[3]

            # ---- 5b. REGRESSION: an ENDED carry A holds bill 111 at an old hash while an IN-FLIGHT
            #      carry B holds 111 at its current hash. The in-flight check used to run first and
            #      `continue`, leaving A's result standing, so the run carded A's stale synopsis and
            #      recorded seen['111'] = 'h-OLD'. A carried result is applied only at the bill's
            #      current hash; A must be discarded (logged) and B left in flight. ----
            old_cid = L._carry_cid("111", "h-OLD")
            pair = [
                {"id": "msgbatch_A_ended", "at": W._iso(time.time()), "items": {old_cid: {
                    "bid": "111", "ch": "h-OLD", "state": "GA", "areas": [], "prev": None,
                    "detail": {"bill_id": 111, "number": "SB 68", "title": "old", "change_hash": "h-OLD"}}}},
                {"id": "msgbatch_B_running", "at": W._iso(time.time()), "items": {cid111: {
                    "bid": "111", "ch": "h-sb68-v1", "state": "GA", "areas": [], "prev": None,
                    "detail": {"bill_id": 111, "number": "SB 68", "title": "current"}}}},
            ]
            B.status = lambda bid, label="batch": (
                {"id": bid, "processing_status": "ended", "results_url": "u"} if bid == "msgbatch_A_ended"
                else {"id": bid, "processing_status": "in_progress"})
            B.collect = lambda obj, label="batch": {old_cid: keep("STALE A synopsis.")}
            fresh.clear()
            screened.clear()
            book = W.CarryBook("legislation", pair)
            (cards, notes, seen), out = go(book)
            check("stale ended + current in flight: the stale result is NOT carded",
                  "STALE A synopsis." not in {c["synopsis"] for c in cards} and 111 not in
                  {c["bill_id"] for c in cards}, str(cards))
            check("stale ended + current in flight: the stale hash is NOT recorded seen",
                  seen.get("111") != "h-OLD" and "111" not in seen, str(seen))
            check("stale ended + current in flight: the stale result is discarded with a log line",
                  "discarding carried result for bill 111 from batch msgbatch_A_ended" in out
                  and "msgbatch_A_ended (0 results applied, 1 re-queued)" in out, out)
            check("stale ended + current in flight: the bill is held for the running batch, not "
                  "screened or written again",
                  not any("SB 68" in t for t in screened) and cid111 not in fresh
                  and any("still in a carried write batch" in n for n in notes), str(screened))
            check("stale ended + current in flight: only the running batch stays carried",
                  [r["id"] for r in book.carries] == ["msgbatch_B_running"])

            # ---- 5c. a carried result for a bill discovery did not list this run cannot be checked
            #      against the bill's current hash: it is not applied, and it stays carried ----
            lost_cid = L._carry_cid("555", "h-555")
            unlisted = [{"id": "msgbatch_U", "label": "legislation-write", "at": W._iso(time.time()),
                         "items": {lost_cid: {"bid": "555", "ch": "h-555", "state": "GA", "areas": [],
                                              "prev": None, "detail": {"bill_id": 555, "number": "HB 5",
                                                                       "title": "t"}}}}]
            B.status = lambda bid, label="batch": {"id": bid, "processing_status": "ended", "results_url": "u"}
            B.collect = lambda obj, label="batch": {lost_cid: keep("UNCONFIRMED synopsis.")}
            book = W.CarryBook("legislation", unlisted)
            (cards, _notes, seen), out = go(book)
            check("unlisted: the unconfirmed result is not applied",
                  555 not in {c["bill_id"] for c in cards} and "555" not in seen)
            check("unlisted: it stays carried (same batch, same timestamp) for the next run",
                  [(r["id"], list(r["items"]), r["at"]) for r in book.carries]
                  == [("msgbatch_U", [lost_cid], unlisted[0]["at"])], str(book.carries))
            check("unlisted: reported as kept carried",
                  "msgbatch_U (0 results applied, 0 re-queued, 1 kept carried)" in out, out)

            # ---- 5d. past the card cap, discovery still matches carried results ----
            B.collect = lambda obj, label="batch": {cid111: keep("CAPPED-RUN synopsis.")}
            capped = [{"id": "msgbatch_C", "at": W._iso(time.time()), "items": {cid111: {
                "bid": "111", "ch": "h-sb68-v1", "state": "GA", "areas": [], "prev": None,
                "detail": {"bill_id": 111, "number": "SB 68", "title": "t"}}}}]
            book = W.CarryBook("legislation", capped)
            (cards, notes, seen), out = go(book, max_run=0)
            check("capped run: the carried result is still matched and applied",
                  {c["bill_id"]: c["synopsis"] for c in cards}.get(111) == "CAPPED-RUN synopsis."
                  and seen.get("111") == "h-sb68-v1" and any("LEGISLATION_MAX" in n for n in notes),
                  "%r %r" % (cards, notes))

            # ---- 6. a carry too old to collect is dropped and its bills processed again ----
            polled = []
            B.status = lambda bid, label="batch": polled.append(bid) or {"processing_status": "ended"}
            old = [{"id": "msgbatch_OLD", "at": W._iso(time.time() - 30 * 86400), "items": {
                cid111: {"bid": "111", "ch": "h-sb68-v1", "prev": None, "detail": {"bill_id": 111}}}}]
            fresh.clear()
            book = W.CarryBook("legislation", old)
            (cards, _notes, _seen), out = go(book)
            check("expired: the old carry is dropped with a log line, never polled",
                  "dropping carried batch msgbatch_OLD" in out and polled == [] and book.carries == [], out)
            check("expired: its bill is processed again as usual", cid111 in fresh)

            # ---- 7. the step budget shortens the wait and stops the screen ----
            seen_deadline = []

            def capture(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                seen_deadline.append(deadline)
                return {r["custom_id"]: keep("ok.") for r in reqs}

            B.run = capture
            end = time.time() + 900
            go(W.CarryBook("legislation"), budget=W.Budget(end))
            check("budget: the batch deadline is the step's end, not the watch's own 30 minutes",
                  seen_deadline and seen_deadline[0] == end and L.BATCH_SEC > 900, str(seen_deadline))
            seen_deadline.clear()
            screened.clear()
            (cards, notes, seen), _out = go(W.CarryBook("legislation"), budget=W.Budget(time.time() + 5))
            check("budget: with the step nearly out of time, nothing is screened or sent",
                  screened == [] and seen_deadline == [] and cards == [] and seen == {})
            check("budget: and the run says why", any("running low" in n for n in notes))
    finally:
        B.run, B.status, B.collect, L._load_seen = real


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
    # Same hygiene for the recall check's drop log: every L.run() below screens the fixtures and
    # drops most of them, and log_drops WRITES. Unstubbed, a test run leaves a 60-line
    # legislation_rejections.jsonl in the working tree -- caught exactly that way. Redirect it once
    # for the whole process; test_recall points it at its own temp file for the assertions it makes.
    _drops_tmp = tempfile.mkdtemp(prefix="legtest-")
    L.DROPS_PATH = os.path.join(_drops_tmp, "legislation_rejections.jsonl")

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

    # The recall pass audits every screen drop, so a run with drops needs an auditor. This one clears
    # them all, settling each drop seen; without it the audit fails and the drop is left un-seen.
    clean_recall = {"suspect": False, "note": "drop stands"}

    ai = make_ai({"leg-screen": screen_router, "leg-recall": clean_recall, "leg-write": write_router})
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

    us_ai = make_ai({"leg-screen": us_screen, "leg-recall": clean_recall, "leg-write": write_router})
    fed_cards, fed_notes, _ = L.run(key="GOODKEY", fetch=fake_fetch, ai=us_ai, states=["US"],
                                    today=__import__("datetime").date(2026, 7, 17))
    fed_ids = {c["bill_id"] for c in fed_cards}
    check("federal run cards the FAAAA / motor-carrier statute", 5001 in fed_ids)
    check("federal run drops the NDAA / appropriations statute", 5002 not in fed_ids)
    check("federal card carries state US", all(c["state"] == "US" for c in fed_cards))

    # Both jurisdictions in one run, into one card set keyed on the globally-unique bill_id.
    both_ai = make_ai({"leg-screen": lambda b: (us_screen(b) if "u.s. congress" in b["messages"][0]["content"].lower()
                                                else screen_router(b)),
                       "leg-recall": clean_recall, "leg-write": write_router})
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
    # drops the appropriations bill (444), keeps 111/222; the audit clears the drop
    screen_keep = make_ai({"leg-screen": screen_router, "leg-recall": clean_recall})

    def _fake_batch(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        # custom_id is "<bill_id>-<change_hash>": 111 kept, 222 declined, anything else errored.
        out = {}
        for r in reqs:
            cid = r["custom_id"]
            if cid.split("-")[0] == "111":
                out[cid] = {"ok": True, "text": json.dumps(
                    {"keep": True, "areas": ["damages"], "synopsis": "Changes apportionment.",
                     "impact": "It matters.", "effective_date": ""})}
            elif cid.split("-")[0] == "222":
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


    # ---- the writer never sees the bill text -------------------------------------------------
    # HB625 said the bill "does not identify the appointing authority... the length of the initial
    # term". The Act names both, and adds TWO judgeships where the singular caption ("provide
    # additional judge") implied one. _bill_brief shows a title and LegiScan's description and
    # explicitly never fetches bill text, so that clause was a claim about an unread document --
    # the same failure the opinion screen had, in its most persuasive form, because a gap reported
    # as a gap reads as candour.
    check("the brief still does not include bill text",
          "bill text" not in L._bill_brief({"number": "HB1", "title": "t", "description": "d"}).lower())
    sysm = L._write_system("GA")
    check("prompt says the bill text is NOT shown", "NOT shown" in sysm)
    check("prompt forbids characterizing the bill's silence", "is silent on" in sysm)
    check("prompt says omit, do not report as missing", "OMIT" in sysm)
    check("prompt guards counts and quantities", "COUNTS AND QUANTITIES" in sysm)
    check("prompt warns that captions are singular where acts are plural",
          "provide additional judge" in sysm)
    # "on a full read" promised a read that never happens; a keep/decline must not rest on it.
    check("prompt no longer claims a full read", "on a full read" not in sysm)

    # The discriminator, calibrated on the live cards: WHAT the silence is pinned on. Pinning it on
    # the material actually read is honest and must survive; pinning it on the bill is the defect.
    for text, want in (
        ("The bill text as described does not identify the appointing authority", True),
        ("The bill text does not specify the new reference date", True),
        ("The act is silent on the compensation figure", True),
        ("The statute does not spell out which circumstances trigger it", True),
        ("the provided title and description do not specify the claim filing deadlines", False),
        ("The description does not identify which proceedings may be digital", False),
        ("The text supplied does not identify which specific records fall within", False),
        ("the provided text does not identify which code sections change", False),
        # Substantive negation is the point of many of these cards and must never be flagged.
        ("The act does not apply to ride share drivers or ride share network services", False),
        ("The statute does not preempt local ordinances", False),
        ("The bill does not take effect until January 1", False),
        ("", False),
    ):
        got = bool(L.unsourced_silence_claim(text))
        check("silence lint %s: %s" % ("flags" if want else "clears", text[:44] or "(empty)"),
              got == want, "got %r" % (L.unsourced_silence_claim(text),))
    # The two guards below are each reachable only by a case that trips the OTHER check first, so
    # they need their own fixtures -- the first pass of these tests exercised neither, and two
    # mutations (removing the provided-exemption, removing the clause trim) went undetected.
    #
    # (a) the provided-exemption: honest attribution that DOES name the bill. Without the exemption
    #     this flags, and every card that politely says "the provided bill description" is noise.
    check("an honest attribution naming the bill is still cleared",
          not L.unsourced_silence_claim(
              "the provided bill description does not specify the filing deadline"))
    # (b) the clause trim: an earlier clause names the Act, the nearest one attributes honestly and
    #     carries no 'provided' word, so only the trim can clear it.
    check("an earlier mention of the Act does not taint a later honest attribution",
          not L.unsourced_silence_claim(
              "This Act amends Title 40; the excerpt does not state an effective date"))
    check("but a bill-silence claim in that same shape is still caught",
          bool(L.unsourced_silence_claim(
              "This Act amends Title 40; the bill does not state an effective date")))

    # The reviewer sees it at the moment of review, or it may as well not exist.
    body = L._pr_body(2, 0, [
        {"number": "HB625", "status": "enacted", "areas": [], "title": "t", "url": "u",
         "synopsis": "Adds judgeships. The bill text does not identify the appointing authority."},
        {"number": "HB295", "status": "enacted", "areas": [], "title": "t2", "url": "u2",
         "synopsis": "Creates a mechanism. The provided description does not specify deadlines."}])
    check("review PR warns on a bill-silence card", "Check these against the bill first" in body)
    check("review PR names the offending bill", "**HB625**" in body)
    check("review PR does not flag an honest attribution", "**HB295**" not in body.split("Check these")[-1])
    clean = L._pr_body(1, 0, [{"number": "HB1", "status": "enacted", "areas": [], "title": "t",
                               "url": "u", "synopsis": "The act does not apply to rideshare."}])
    check("no warning block when nothing is flagged", "Check these against the bill" not in clean)

    # --- the recall check over screen drops ---
    test_recall(check, L, make_ai, fake_fetch, BILLS)

    # --- carried write batches and the step budget ---
    test_carry(check, L, make_ai, fake_fetch)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
