#!/usr/bin/env python3
"""Hermetic end-to-end tests for the funnel's batch carry-over (update.py + batch.py).

Until 2026-10 a batch that missed its deadline was carried to the next run, and the next run's
batch.run(resume_id=...) returned the CARRIED batch's results in place of the current requests.
That stamped one run's smell verdicts onto another run's drops (positional "smell-<k>" ids), left
current summarize candidates undrafted without a log line, made a stale guard carry report a new
card's guards "unavailable" while skipping the synchronous fallback, and a run that never reached
a phase deleted that phase's carry. A timed-out batch was also never cancelled, so it was paid for
on top of the synchronous fallback.

They also pin what happens to a carried summarize draft whose cluster is not pending in the run that
collects it: a smell escalation (Benedetto 10987874, whose cluster was marked seen at its triage drop)
is refetched and finished, and its earlier drop record settled; any other draft is kept for a later
run unless its case is carded or seen. A carry survives a transport failure and is dropped only when
the API says it is gone (404/410).

These tests drive the REAL batch.run / batch.fetch and update's phase functions against a fake,
in-memory Message Batches API swapped in at batch._send (the module's single network seam), and stub
update.anthropic_json for the synchronous fallbacks. No network, no API key, no repo data is read or
written: state lives in dicts and every module global touched is restored.

Run directly: `python scripts/test_batch_resume.py`.
"""
import contextlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch    # noqa: E402  (sys.path shim must run first)
import update   # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    # sys.__stdout__: the harness captures the code under test's stdout to assert on its log lines.
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""),
          file=sys.__stdout__, flush=True)
    if not cond:
        FAILS.append(name)


class FakeBatchAPI:
    """An in-memory Message Batches API. A batch submitted while `hold` is True stays in_progress
    until finish_all(); otherwise it has ended by the first status read. `errored` names custom_ids
    whose line comes back errored. Every call is recorded."""

    def __init__(self):
        self.n = 0
        self.batches = {}
        self.calls = []
        self.hold = False
        self.errored = set()
        self.unreachable = set()   # batch ids whose status read fails in transport (no HTTP status)

    @staticmethod
    def respond(cid, params):
        user = params["messages"][0]["content"]
        if cid.startswith("smell-"):
            names = re.findall(r"^\d+\. \[[^\]]*\] (.*?) -- REASON", user, re.M)
            return json.dumps({"verdicts": [{"i": i + 1, "verdict": "suspect" if "SUS" in n else "ok",
                                             "note": "judged " + n} for i, n in enumerate(names)]})
        if cid.startswith("guards-"):
            return json.dumps({"verdict": "match" if cid.endswith("fidelity") else "complete"})
        if cid.startswith("triage-"):
            return json.dumps({"relevant": True, "significance": "high", "note": "batch " + cid})
        m = re.search(r"Case name: (.*)", user)
        return json.dumps({"drafted_for": m.group(1) if m else "?"})

    def line(self, cid, params):
        if cid in self.errored:
            return {"custom_id": cid, "result": {"type": "errored", "error": {"type": "overloaded"}}}
        return {"custom_id": cid, "result": {"type": "succeeded", "message": {
            "content": [{"type": "text", "text": self.respond(cid, params)}]}}}

    def __call__(self, method, url, body=None, label="batch"):
        self.calls.append((method, url, label, body))
        if method == "POST" and url == batch.API:
            self.n += 1
            bid = "msgbatch_%d" % self.n
            self.batches[bid] = {"status": "in_progress" if self.hold else "ended",
                                 "lines": [self.line(r["custom_id"], r["params"]) for r in body["requests"]],
                                 "ids": [r["custom_id"] for r in body["requests"]]}
            return json.dumps({"id": bid, "processing_status": "in_progress"})
        if url.startswith("results://"):
            return "\n".join(json.dumps(x) for x in self.batches[url[len("results://"):]]["lines"])
        if method == "POST" and url.endswith("/cancel"):
            bid = url.split("/")[-2]
            self.batches[bid]["status"] = "canceling"
            self.batches[bid]["cancelled"] = True
            return json.dumps({"id": bid, "processing_status": "canceling"})
        bid = url.rsplit("/", 1)[-1]
        if bid in self.unreachable:
            raise batch.BatchError("GET %s -> <urlopen error [Errno 104] Connection reset by peer>" % bid)
        if bid not in self.batches:
            raise batch.BatchError("GET %s -> HTTP 404: not_found_error" % bid, status=404)
        b = self.batches[bid]
        if b["status"] == "in_progress" and b.get("end_after") is not None:
            b["end_after"] -= 1
            if b["end_after"] < 0:
                b["status"] = "ended"
        return json.dumps({"id": bid, "processing_status": b["status"], "results_url": "results://" + bid})

    def finish_all(self):
        for b in self.batches.values():
            if b["status"] == "in_progress":
                b["status"] = "ended"

    def posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == batch.API]

    def cancels(self):
        return [c[1].split("/")[-2] for c in self.calls if c[0] == "POST" and c[1].endswith("/cancel")]

    def gets_of(self, bid):
        return [c for c in self.calls if c[0] == "GET" and c[1].endswith("/" + bid)]


@contextlib.contextmanager
def harness():
    """Swap in the fake API and a recording sync stub; restore every global on the way out."""
    api = FakeBatchAPI()
    sync = []

    def fake_json(body, label, *a, **k):
        sync.append(label)
        user = body["messages"][0]["content"]
        if label == "smell":
            names = re.findall(r"^\d+\. \[[^\]]*\] (.*?) -- REASON", user, re.M)
            return {"verdicts": [{"i": i + 1, "verdict": "ok", "note": "sync " + n} for i, n in enumerate(names)]}
        return {"relevant": True, "significance": "high", "note": "sync"}

    saved = (batch._send, batch.time.sleep, update.anthropic_json, update.SMELL_BATCH,
             update.CROSSCHECK_MODEL, update.COMPLETENESS_MODEL, update.crosscheck, update.completeness_check,
             dict(update._PENDING_BATCHES), dict(update._RESUME_BATCHES))
    batch._send = api
    batch.time.sleep = lambda *_a, **_k: None
    update.anthropic_json = fake_json
    update.SMELL_BATCH = True
    update.CROSSCHECK_MODEL = update.CROSSCHECK_MODEL or "guard-model"
    update.COMPLETENESS_MODEL = update.COMPLETENESS_MODEL or "guard-model"
    update._PENDING_BATCHES.clear(); update._RESUME_BATCHES.clear()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield api, sync, buf
    finally:
        (batch._send, batch.time.sleep, update.anthropic_json, update.SMELL_BATCH,
         update.CROSSCHECK_MODEL, update.COMPLETENESS_MODEL, update.crosscheck, update.completeness_check,
         pend, res) = saved
        update._PENDING_BATCHES.clear(); update._PENDING_BATCHES.update(pend)
        update._RESUME_BATCHES.clear(); update._RESUME_BATCHES.update(res)


def new_run(state):
    """What main() does at startup: adopt the carried batches from state."""
    return update.adopt_pending_batches(state)


def pend(cid, name):
    return {"cid": cid, "r": {}, "name": name, "court_id": "gactapp", "docket": "A%d" % cid,
            "date_filed": "2026-09-25", "text": "opinion text", "note": "", "cl_status": ""}


def drop(cid, name, reason="r"):
    return {"cid": cid, "name": name, "court": "gactapp", "date": "2026-09-25", "reason": reason}


# ---- 1. smell: a previous run's verdicts can never land on this run's drops ----------------------

def test_smell_no_contamination():
    print("smell: positional-id contamination is impossible")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        v1 = update.smell_reasons([drop(1, "Earnshaw SUS"), drop(2, "PrevCase B")], deadline=0)
        state = update.stamp_pending_batches(state)
        check("run 1: the timed-out smell batch is cancelled", api.cancels() == ["msgbatch_1"], str(api.cancels()))
        check("run 1: the synchronous fallback judged run 1's drops",
              sync == ["smell"] and v1.get(0, {}).get("note") == "sync Earnshaw SUS", repr(v1))
        check("run 1: no smell carry is recorded", "pending_batches" not in state, repr(state))

        api.finish_all(); api.hold = False; sync.clear()
        new_run(state)
        v2 = update.smell_reasons([drop(10982990, "Gary Jones"), drop(3, "Other Drop")], deadline=0)
        notes = json.dumps(v2)
        check("run 2: Gary Jones is judged on his own reason, not Earnshaw's",
              v2.get(0) == {"verdict": "ok", "note": "judged Gary Jones"}, repr(v2))
        check("run 2: no verdict mentions run 1's cases", "Earnshaw" not in notes and "PrevCase" not in notes)
        check("run 2: run 1's batch was never read again", api.gets_of("msgbatch_1")[1:] == [] and
              not any(c[1] == "results://msgbatch_1" for c in api.calls))


# ---- 2. summarize: partial overlap drafts everything, logs the rest ------------------------------

def test_summarize_partial_overlap():
    print("summarize: a carry is applied by cluster, the rest is drafted fresh, undrafted is logged")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        finished = []
        d1 = update._draft_pending([pend(1, "One v. A"), pend(2, "Two v. B")], 0,
                                   lambda v, p: finished.append((p["cid"], v)))
        state = update.stamp_pending_batches(state)
        carry = (state.get("pending_batches") or {}).get("msgbatch_1") or {}
        check("run 1: a deferred summarize batch is carried, not cancelled",
              d1 == set() and api.cancels() == [] and carry.get("label") == "funnel-summarize", repr(state))
        check("run 1: the carry records the exact clusters it covers", carry.get("cids") == [1, 2], repr(carry))
        check("run 1: both deferred candidates are logged undrafted",
              "! undrafted: 1 One v. A" in buf.getvalue() and "! undrafted: 2 Two v. B" in buf.getvalue(),
              buf.getvalue())

        # Run 2: the carry has ended. Gresham is new; cluster 1 is still eligible; cluster 2 is not
        # (it was seen since: permanently rejected).
        api.finish_all(); api.hold = False; finished.clear()
        buf.seek(0); buf.truncate()
        new_run(state)
        d2 = update._draft_pending([pend(10983223, "Gresham v. FEC Highway"), pend(1, "One v. A")], 0,
                                   lambda v, p: finished.append((p["cid"], v)), closed={2})
        log = buf.getvalue()
        check("run 2: the carried draft and the fresh draft are both finished", d2 == {1, 10983223}, repr(d2))
        check("run 2: cluster 1 gets ITS OWN carried draft",
              (1, {"drafted_for": "One v. A"}) in finished, repr(finished))
        check("run 2: Gresham is drafted fresh in the same run",
              (10983223, {"drafted_for": "Gresham v. FEC Highway"}) in finished, repr(finished))
        posted = [c[3]["requests"] for c in api.posts()[1:]]
        check("run 2: only the uncovered candidate is requested fresh",
              [[r["custom_id"] for r in reqs] for reqs in posted] == [["summarize-10983223"]], repr(posted and
              [[r["custom_id"] for r in reqs] for reqs in posted]))
        check("run 2: the draft for no-longer-eligible cluster 2 is logged and dropped",
              "draft for cluster 2 dropped" in log and all(c != 2 for c, _ in finished), log)
        state = update.stamp_pending_batches(state)
        check("run 2: the collected carry is gone from state", "pending_batches" not in state, repr(state))


def test_summarize_running_carry():
    print("summarize: a carry still running keeps its clusters waiting and stays carried")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: None)
        state = update.stamp_pending_batches(state)
        at0 = state["pending_batches"]["msgbatch_1"]["at"]
        # Run 2: the carry is still running; Gresham is new and its batch ends promptly.
        api.hold = False
        buf.seek(0); buf.truncate()
        new_run(state)
        finished = []
        d = update._draft_pending([pend(1, "One v. A"), pend(10983223, "Gresham v. FEC Highway")], 0,
                                  lambda v, p: finished.append(p["cid"]))
        log = buf.getvalue()
        check("Gresham is drafted from a fresh request", d == {10983223} and finished == [10983223], repr(d))
        check("cluster 1 is not re-requested while its carry runs",
              all("summarize-1" not in [r["custom_id"] for r in c[3]["requests"]] for c in api.posts()[1:]))
        check("cluster 1 is logged undrafted with the reason",
              "! undrafted: 1 One v. A (carried batch msgbatch_1 still running" in log, log)
        state = update.stamp_pending_batches(state)
        rec = state.get("pending_batches", {}).get("msgbatch_1", {})
        check("the running carry stays in state with its original timestamp (it can expire)",
              rec.get("at") == at0 and rec.get("cids") == [1], repr(state))


def test_summarize_legacy_carry():
    print("summarize: a legacy carry (bare numeric ids, no recorded clusters) still maps by cluster")
    with harness() as (api, sync, buf):
        # A batch submitted by the old code: custom_ids were str(cid), and state kept no cids.
        api.batches["msgbatch_old"] = {"status": "ended", "ids": ["5", "6"], "lines": [
            {"custom_id": "5", "result": {"type": "succeeded", "message": {"content": [
                {"type": "text", "text": json.dumps({"drafted_for": "Five v. E"})}]}}},
            {"custom_id": "6", "result": {"type": "succeeded", "message": {"content": [
                {"type": "text", "text": json.dumps({"drafted_for": "Six v. F"})}]}}}]}
        import time as _t
        state = {"pending_batches": {"funnel-summarize": {"id": "msgbatch_old", "at": _t.time() - 3600, "n": 2}}}
        new_run(state)
        finished = []
        d = update._draft_pending([pend(5, "Five v. E"), pend(7, "Seven v. G")], 0,
                                  lambda v, p: finished.append((p["cid"], v["drafted_for"])))
        check("the legacy carry's line for cluster 5 drafts cluster 5", (5, "Five v. E") in finished, repr(finished))
        check("cluster 7 is drafted fresh", (7, "Seven v. G") in finished and d == {5, 7}, repr(finished))
        check("cluster 6's draft is not applied to anyone", all(n != "Six v. F" for _, n in finished))


# ---- 3. carries survive runs that do not reach the phase ----------------------------------------

def test_carry_survives_unreached_phase():
    print("pending_batches: a run that never reaches summarize keeps the carry")
    with harness() as (api, sync, buf):
        import time as _t
        now = _t.time()
        state = {"pending_batches": {"msgbatch_9": {"label": "funnel-summarize", "id": "msgbatch_9",
                                                    "at": now - 600, "n": 1, "cids": [10987874]}}}
        new_run(state)
        # This run reaches triage only.
        update._triage_batch([{"cid": 42, "name": "T v. U", "docket": "A42", "text": "t"}], "", deadline=0)
        out = update.stamp_pending_batches(dict(state))
        check("the summarize carry (Benedetto 10987874's re-read) is written back untouched",
              out.get("pending_batches") == state["pending_batches"], repr(out.get("pending_batches")))
        check("and triage recorded no carry of its own", set(out["pending_batches"]) == {"msgbatch_9"})
        check("and the carried batch was never polled", api.gets_of("msgbatch_9") == [])


# ---- 4. legacy triage / smell / guard carries are cleared, never resumed -------------------------

def test_legacy_carries_cleared():
    print("legacy triage/smell/guard carries are cleared, not resumed")
    with harness() as (api, sync, buf):
        import time as _t
        now = _t.time()
        # The stale guard batch the old code would have resumed for the next card.
        api.batches["msgbatch_stale"] = {"status": "ended", "ids": ["111-fidelity"], "lines": [
            {"custom_id": "111-fidelity", "result": {"type": "succeeded", "message": {"content": [
                {"type": "text", "text": '{"verdict": "match"}'}]}}}]}
        state = {"pending_batches": {lab: {"id": "msgbatch_stale", "at": now - 60, "n": 2}
                                     for lab in ("funnel-triage", "funnel-smell", "funnel-guards")}}
        new_run(state)
        log = buf.getvalue()
        check("each legacy carry is logged as cleared",
              all("clearing legacy %s carry" % lab in log for lab in ("funnel-triage", "funnel-smell", "funnel-guards")),
              log)
        check("none of them is resumable", update._RESUME_BATCHES == {}, repr(update._RESUME_BATCHES))
        cc, cp = {}, {}
        left = update.guard_cards_batch([{"cid": 222, "name": "New v. Card", "text": "opinion",
                                          "entry": {"name": "New v. Card", "synopsis": "s", "why": "w", "areas": ["coverage"],
                                                    "disposition": "affirmed"}}], cc, cp, deadline=0)
        ids = [r["custom_id"] for r in api.posts()[0][3]["requests"]] if api.posts() else []
        check("the new card submits its own guard requests",
              sorted(ids) == ["guards-222-completeness", "guards-222-fidelity"], repr(ids))
        check("and gets its own verdicts", cc.get(222, {}).get("verdict") == "match"
              and cp.get(222, {}).get("verdict") == "complete" and left == [], repr((cc, cp, left)))
        check("the stale batch is never read", api.gets_of("msgbatch_stale") == [])
        out = update.stamp_pending_batches(state)
        check("and the legacy carries are gone from state", "pending_batches" not in out, repr(out))


# ---- 5. a guard batch with unavailable verdicts falls back to the synchronous guards -------------

def test_guard_unavailable_falls_back():
    print("guards: an unavailable verdict triggers the synchronous guard for that card")
    with harness() as (api, sync, buf):
        calls = []
        update.crosscheck = lambda name, text, entry: calls.append(("fidelity", name)) or {"verdict": "match", "reason": "sync"}
        update.completeness_check = lambda name, text, entry: calls.append(("completeness", name)) or {"verdict": "complete", "reason": "sync"}
        api.errored = {"guards-222-completeness"}
        items = [{"cid": c, "name": "Card %d" % c, "text": "opinion",
                  "entry": {"name": "Card %d" % c, "synopsis": "s", "why": "w", "areas": ["coverage"],
                            "disposition": "affirmed"}}
                 for c in (111, 222)]
        cc, cp = {}, {}
        left = update.guard_cards_batch(items, cc, cp, deadline=0)
        check("the errored guard is reported as not guarded", [(i["cid"], k) for i, k in left] == [(222, "completeness")],
              repr(left))
        check("and is not stamped 'unavailable' as if guarded", 222 not in cp, repr(cp))
        update.guard_sync(left, cc, cp)
        check("the synchronous fallback runs for exactly that guard", calls == [("completeness", "Card 222")], repr(calls))
        check("and every guard now has a real verdict",
              all(d.get(c, {}).get("verdict") not in (None, "unavailable") for d in (cc, cp) for c in (111, 222)),
              repr((cc, cp)))

        # A timed-out guard batch is cancelled and every guard falls back.
        calls.clear(); api.errored = set(); api.hold = True
        cc2, cp2 = {}, {}
        left2 = update.guard_cards_batch(items, cc2, cp2, deadline=0)
        check("a timed-out guard batch is cancelled", api.cancels() == ["msgbatch_2"], repr(api.cancels()))
        update.guard_sync(left2, cc2, cp2)
        check("and all four guards run synchronously", len(calls) == 4 and len(cc2) == 2 and len(cp2) == 2, repr(calls))
        check("no guard carry is recorded", update._PENDING_BATCHES == {}, repr(update._PENDING_BATCHES))


# ---- 6. a timed-out batch is cancelled -----------------------------------------------------------

def test_timed_out_batch_cancelled():
    print("triage: a timed-out batch is cancelled before the synchronous fallback")
    with harness() as (api, sync, buf):
        api.hold = True
        items = [{"cid": 7, "name": "A v. B", "docket": "A7", "text": "t"}]
        v = update._triage_batch(items, "", deadline=0)
        check("the timed-out triage batch is cancelled", api.cancels() == ["msgbatch_1"], repr(api.cancels()))
        check("and the cancel is logged", "cancelled batch msgbatch_1" in buf.getvalue(), buf.getvalue())
        check("the candidate is triaged synchronously", sync == ["triage"] and v.get(7, {}).get("note") == "sync",
              repr(v))
        check("no triage carry is recorded", update._PENDING_BATCHES == {}, repr(update._PENDING_BATCHES))


# ---- 7. a carried smell escalation is finished, not dropped (Benedetto 10987874) -----------------

BENEDETTO = 10987874


def escalation(cid, name, ts="2026-09-28T12:00:00Z"):
    """An in-run smell escalation as main() queues it: a pending item carrying its drop record."""
    p = pend(cid, name)
    p["r"] = {"cluster_id": cid, "court_id": "gactapp", "pdf_url": "https://storage.example/%d.pdf" % cid,
              "absolute_url": "/opinion/%d/x/" % cid}
    p["url"] = "https://www.courtlistener.com/opinion/%d/x/" % cid
    p["note"] = "smell: keep-shaped reason"
    p["rej"] = {"ts": ts, "stage": "triage", "cluster_id": cid, "name": name}
    p["skip_i"] = 0
    return p


def test_smell_escalation_carry():
    print("summarize: a carried smell-escalation draft is finished on a later run (Benedetto)")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        d1 = update._draft_pending([escalation(BENEDETTO, "Benedetto v. Benedetto")], 0, lambda v, p: None)
        # The state file is JSON: round-trip it, as the commit and the next checkout do.
        state = json.loads(json.dumps(update.stamp_pending_batches(state)))
        carry = state.get("pending_batches", {}).get("msgbatch_1", {})
        meta = (carry.get("smell") or {}).get(str(BENEDETTO)) or {}
        check("run 1: the escalation's batch is carried with its cluster", d1 == set() and carry.get("cids") == [BENEDETTO],
              repr(state))
        check("run 1: the carry records what it takes to finish the escalation",
              meta.get("name") == "Benedetto v. Benedetto" and meta.get("rej_ts") == "2026-09-28T12:00:00Z"
              and meta.get("pdf_url", "").endswith("%d.pdf" % BENEDETTO) and meta.get("court_id") == "gactapp",
              repr(meta))

        # Run 2: the batch has ended. Benedetto was marked seen at its triage drop, so it is NOT
        # pending (and it is in `closed`); nothing else is pending either.
        api.finish_all(); api.hold = False
        buf.seek(0); buf.truncate()
        new_run(state)
        revived, finished = [], []

        def revive(cid, m):
            revived.append((cid, m.get("name"), m.get("rej_ts")))
            return dict(pend(cid, m["name"]), rej_ts=m["rej_ts"], text="refetched opinion text")
        d2 = update._draft_pending([], 0, lambda v, p: finished.append((p["cid"], v, p["text"])),
                                   carded=set(), closed={BENEDETTO}, revive=revive)
        check("run 2: the carried escalation is revived from the carry's record",
              revived == [(BENEDETTO, "Benedetto v. Benedetto", "2026-09-28T12:00:00Z")], repr(revived))
        check("run 2: it is finished with ITS OWN paid draft and the refetched text",
              finished == [(BENEDETTO, {"drafted_for": "Benedetto v. Benedetto"}, "refetched opinion text")],
              repr(finished))
        check("run 2: it counts as drafted (so its outcome resolves as read)", d2 == {BENEDETTO}, repr(d2))
        check("run 2: no fresh request is paid for it", len(api.posts()) == 1, repr(len(api.posts())))
        out = update.stamp_pending_batches(dict(state))
        check("run 2: the collected carry is gone", "pending_batches" not in out, repr(out))


def test_smell_escalation_carry_kept_or_dropped():
    print("summarize: a carried escalation is kept when its text is unavailable, dropped once carded")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        update._draft_pending([escalation(BENEDETTO, "Benedetto v. Benedetto")], 0, lambda v, p: None)
        state = json.loads(json.dumps(update.stamp_pending_batches(state)))
        api.finish_all(); api.hold = False

        # Run 2: the text cannot be fetched -> revive says "keep" (None).
        new_run(state)
        finished = []
        d = update._draft_pending([], 0, lambda v, p: finished.append(p["cid"]), closed={BENEDETTO},
                                  revive=lambda cid, m: None)
        state = json.loads(json.dumps(update.stamp_pending_batches(state)))
        carry = state.get("pending_batches", {}).get("msgbatch_1", {})
        check("no text: nothing is finished", d == set() and finished == [], repr((d, finished)))
        check("no text: the carry (and its smell record) is kept for a later run",
              carry.get("cids") == [BENEDETTO] and str(BENEDETTO) in (carry.get("smell") or {}), repr(state))

        # Run 3: the case was carded meanwhile (say, under the queue's force flag) -> dropped.
        buf.seek(0); buf.truncate()
        new_run(state)
        called = []
        d = update._draft_pending([], 0, lambda v, p: finished.append(p["cid"]), carded={BENEDETTO},
                                  closed={BENEDETTO}, revive=lambda cid, m: called.append(cid))
        out = update.stamp_pending_batches(dict(state))
        check("carded: the draft is dropped without reviving it", called == [] and finished == [] and d == set(),
              repr((called, finished)))
        check("carded: the drop is logged", "already carded or staged" in buf.getvalue(), buf.getvalue())
        check("carded: the carry is gone", "pending_batches" not in out, repr(out))


def test_settle_logged_smell():
    print("rejection log: a carried escalation's outcome is stamped on the earlier run's drop record")
    import tempfile
    saved = update.REJECT_PATH
    with tempfile.TemporaryDirectory() as td:
        update.REJECT_PATH = os.path.join(td, "rejections.jsonl")
        rows = [{"ts": "2026-09-28T12:00:00Z", "stage": "triage", "cluster_id": BENEDETTO, "name": "Benedetto",
                 "smell": "suspect", "smell_outcome": "deferred"},
                {"ts": "2026-09-20T12:00:00Z", "stage": "triage", "cluster_id": BENEDETTO, "name": "Benedetto (older)"},
                {"ts": "2026-09-28T12:00:00Z", "stage": "screen", "cluster_id": BENEDETTO, "name": "Benedetto (screen)"},
                {"ts": "2026-09-28T12:00:00Z", "stage": "triage", "cluster_id": 5, "name": "Other"}]
        with open(update.REJECT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                n = update._settle_logged_smell([(BENEDETTO, "2026-09-28T12:00:00Z", "carded")])
            with open(update.REJECT_PATH, encoding="utf-8") as f:
                got = [json.loads(ln) for ln in f.read().splitlines() if ln.strip()]
        finally:
            update.REJECT_PATH = saved
    check("exactly the escalated drop record is updated", n == 1 and got[0].get("smell_outcome") == "carded",
          repr(got[0]))
    check("the other records are untouched", got[1:] == rows[1:], repr(got[1:]))


def test_benedetto_end_to_end():
    print("smell: Benedetto end to end -- carried escalation draft refetched, finished, drop record settled")
    import tempfile
    saved = (update.pdf_text, update.opinion_text_full, update.REJECT_PATH)
    long_text = "The Court of Appeals holds that the trial court erred. " * 40
    try:
        with harness() as (api, sync, buf), tempfile.TemporaryDirectory() as td:
            update.REJECT_PATH = os.path.join(td, "rejections.jsonl")
            ts = "2026-09-28T12:00:00Z"
            with open(update.REJECT_PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "stage": "triage", "cluster_id": BENEDETTO, "name": "Benedetto",
                                    "smell": "suspect", "smell_outcome": "deferred"}) + "\n")
            # Run 1: the escalation's summarize batch misses its deadline and is carried.
            state = {}
            new_run(state)
            api.hold = True
            update._draft_pending([escalation(BENEDETTO, "Benedetto v. Benedetto", ts)], 0, lambda v, p: None)
            state = json.loads(json.dumps(update.stamp_pending_batches(state)))
            api.finish_all(); api.hold = False

            # Run 2: the PDF is unreachable and the REST fallback fails -> the draft is kept.
            update.pdf_text = lambda url, deadline=None: ""
            update.opinion_text_full = lambda r, deadline=None: (_ for _ in ()).throw(OSError("cl down"))
            new_run(state)
            fetched = []
            d = update._draft_pending([], 0, lambda v, p: fetched.append(p), closed={BENEDETTO},
                                      revive=lambda c, m: update.revive_smell_escalation(c, m, [], 0))
            state = json.loads(json.dumps(update.stamp_pending_batches(state)))
            check("no text: kept for a later run", d == set() and fetched == [] and
                  state.get("pending_batches", {}).get("msgbatch_1", {}).get("cids") == [BENEDETTO], repr(state))

            # Run 3: the PDF reads. The draft is finished through the real reviver.
            update.pdf_text = lambda url, deadline=None: long_text if url.endswith("%d.pdf" % BENEDETTO) else ""
            new_run(state)
            dedup = []
            d = update._draft_pending([], 0, lambda v, p: fetched.append((p, v)), closed={BENEDETTO},
                                      revive=lambda c, m: update.revive_smell_escalation(c, m, dedup, 0))
            p, v = fetched[0] if fetched else ({}, {})
            check("run 3: finished with its own draft and the refetched text",
                  d == {BENEDETTO} and v == {"drafted_for": "Benedetto v. Benedetto"} and p.get("text") == long_text,
                  repr((d, v)))
            check("run 3: the rebuilt candidate carries what finish_card needs",
                  p.get("r", {}).get("pdf_url", "").endswith("%d.pdf" % BENEDETTO) and p.get("court_id") == "gactapp"
                  and p.get("url", "").endswith("/opinion/%d/x/" % BENEDETTO) and p.get("rej_ts") == ts, repr(p))
            check("run 3: the case joins the in-run dedup index", [n for _, n in dedup] == ["Benedetto v. Benedetto"],
                  repr(dedup))
            check("run 3: the carry is gone", "pending_batches" not in update.stamp_pending_batches(dict(state)))
            # main() then settles the earlier run's drop record (here: carded).
            update._settle_logged_smell([(BENEDETTO, p.get("rej_ts"), "carded")])
            with open(update.REJECT_PATH, encoding="utf-8") as f:
                rec = json.loads(f.read().splitlines()[0])
            check("the earlier run's drop record is settled: carded, no longer deferred",
                  rec.get("smell_outcome") == "carded", repr(rec))

            # A twin of a case already in the dedup index is dropped, not finished.
            twin = update.revive_smell_escalation(BENEDETTO, update._norm_smell(
                {str(BENEDETTO): update._carry_smell_meta(escalation(BENEDETTO, "Benedetto v. Benedetto", ts))}
            )[str(BENEDETTO)], dedup, 0)
            check("an in-run twin is dropped (False)", twin is False, repr(twin))
        # main() wires it: the reviver and the carded/closed sets go to _draft_pending, a run with
        # nothing pending still settles a carry, and the settled outcomes reach the rejection log.
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.py"), encoding="utf-8").read()
        check("main() passes revive, carded and closed to _draft_pending",
              "revive=_revive" in src and "carded=have | pending_review, closed=seen | evaluated" in src)
        check("main() settles a carry even when nothing is pending",
              "if FUNNEL_BATCH and (pending or _RESUME_BATCHES) and not cfg_error:" in src)
        check("main() writes the settled outcomes before logging the run's rejections",
              "_settle_logged_smell(smell_settled)\n    _log_rejections(rejections)" in src)
    finally:
        update.pdf_text, update.opinion_text_full, update.REJECT_PATH = saved


# ---- 8. a carry is kept on a transport error, dropped only when it is definitively gone ----------

def test_carry_transport_error_kept():
    print("summarize: a transport failure keeps the carry; a 404 drops it and re-requests")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: None)
        state = update.stamp_pending_batches(state)

        # Run 2: the carry's status read fails in transport (after _send's retries).
        api.finish_all(); api.hold = False
        api.unreachable = {"msgbatch_1"}
        buf.seek(0); buf.truncate()
        new_run(state)
        finished = []
        d = update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: finished.append(p["cid"]))
        log = buf.getvalue()
        state = update.stamp_pending_batches(state)
        check("transport error: the carry is kept", state.get("pending_batches", {}).get("msgbatch_1", {}).get("cids") == [1],
              repr(state))
        check("transport error: its cluster is not paid for again", len(api.posts()) == 1 and d == set(),
              repr(len(api.posts())))
        check("transport error: the undrafted reason says so",
              "! undrafted: 1 One v. A (carried batch msgbatch_1 unreachable" in log, log)

        # Run 3: reachable again -> collected, applied, gone.
        api.unreachable = set()
        new_run(state)
        d = update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: finished.append(p["cid"]))
        state = update.stamp_pending_batches(state)
        check("run 3: the kept carry is collected and applied", d == {1} and finished == [1] and len(api.posts()) == 1,
              repr((d, finished)))
        check("run 3: and is gone from state", "pending_batches" not in state, repr(state))

        # A carry the API no longer knows (404) is dropped and its cluster requested fresh.
        import time as _t
        state = {"pending_batches": {"msgbatch_gone": {"label": "funnel-summarize", "id": "msgbatch_gone",
                                                       "at": _t.time() - 60, "n": 1, "cids": [2]}}}
        buf.seek(0); buf.truncate()
        new_run(state)
        finished.clear()
        d = update._draft_pending([pend(2, "Two v. B")], 0, lambda v, p: finished.append(p["cid"]))
        out = update.stamp_pending_batches(dict(state))
        check("404: the carry is dropped", "pending_batches" not in out and "is gone" in buf.getvalue(),
              repr(out) + buf.getvalue())
        check("404: its cluster is drafted fresh in the same run", d == {2} and finished == [2]
              and [r["custom_id"] for r in api.posts()[-1][3]["requests"]] == ["summarize-2"], repr(d))


def test_batch_error_gone():
    print("batch.gone: only HTTP 404/410 is definitive")
    check("404 is gone", batch.gone(batch.BatchError("x", status=404)))
    check("410 is gone", batch.gone(batch.BatchError("x", status=410)))
    check("a transport failure is not", not batch.gone(batch.BatchError("connection reset")))
    check("a 5xx after retries is not", not batch.gone(batch.BatchError("x", status=529)))
    check("an auth error is not", not batch.gone(batch.BatchError("x", status=401)))
    check("a timeout is not", not batch.gone(batch.BatchTimeout("b", "late")))


# ---- 9. a finished draft for a cluster this run never reached is kept ----------------------------

def test_carry_kept_for_absent_cluster():
    print("summarize: a carried draft for a cluster not pending this run is kept, unless carded or seen")
    with harness() as (api, sync, buf):
        state = {}
        new_run(state)
        api.hold = True
        update._draft_pending([pend(1, "One v. A"), pend(2, "Two v. B"), pend(3, "Three v. C")], 0,
                              lambda v, p: None)
        state = update.stamp_pending_batches(state)
        api.finish_all(); api.hold = False

        # Run 2: the CourtListener budget ran out before clusters 2 and 3 were reached; 1 is pending.
        # Cluster 3 was carded by hand meanwhile.
        buf.seek(0); buf.truncate()
        new_run(state)
        finished = []
        d = update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: finished.append(p["cid"]), carded={3})
        state = update.stamp_pending_batches(state)
        carry = state.get("pending_batches", {}).get("msgbatch_1", {})
        check("run 2: cluster 1 gets its carried draft", d == {1} and finished == [1], repr(d))
        check("run 2: the carry is kept, narrowed to cluster 2", carry.get("cids") == [2] and carry.get("n") == 1,
              repr(state))
        check("run 2: cluster 3's draft is dropped (already carded)", "draft for cluster 3 dropped" in buf.getvalue(),
              buf.getvalue())

        # Run 3: cluster 2 is reached -> its kept draft applies, nothing is requested fresh.
        new_run(state)
        finished.clear()
        d = update._draft_pending([pend(2, "Two v. B")], 0, lambda v, p: finished.append((p["cid"], v)))
        state = update.stamp_pending_batches(state)
        check("run 3: cluster 2 gets its own kept draft", finished == [(2, {"drafted_for": "Two v. B"})], repr(finished))
        check("run 3: no fresh request was paid for", len(api.posts()) == 1, repr(len(api.posts())))
        check("run 3: the carry is gone", "pending_batches" not in state, repr(state))


# ---- 10. a running carry that finishes without a line says so ------------------------------------

def test_running_carry_finished_without_line():
    print("summarize: a carry that finishes in-run without a line for a cluster says so")
    with harness() as (api, sync, buf):
        api.batches["msgbatch_7"] = {"status": "in_progress", "end_after": 1, "ids": ["summarize-1"], "lines": []}
        import time as _t
        state = {"pending_batches": {"msgbatch_7": {"label": "funnel-summarize", "id": "msgbatch_7",
                                                    "at": _t.time() - 60, "n": 1, "cids": [1]}}}
        new_run(state)
        d = update._draft_pending([pend(1, "One v. A")], 0, lambda v, p: None)
        log = buf.getvalue()
        reads = [c for c in api.gets_of("msgbatch_7") if not c[1].startswith("results://")]
        check("the carry's status was read twice (running, then ended)", len(reads) == 2, repr(reads))
        check("the undrafted reason says the carry finished without a line",
              d == set() and "! undrafted: 1 One v. A (carried batch msgbatch_7 finished without a result line" in log,
              log)
        check("and not that it is still running", "still running" not in log.split("! undrafted: 1")[-1], log)


def main():
    for t in (test_smell_no_contamination, test_summarize_partial_overlap, test_summarize_running_carry,
              test_summarize_legacy_carry, test_carry_survives_unreached_phase, test_legacy_carries_cleared,
              test_guard_unavailable_falls_back, test_timed_out_batch_cancelled, test_smell_escalation_carry,
              test_smell_escalation_carry_kept_or_dropped, test_settle_logged_smell, test_benedetto_end_to_end,
              test_carry_transport_error_kept,
              test_batch_error_gone, test_carry_kept_for_absent_cluster, test_running_carry_finished_without_line):
        t()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
