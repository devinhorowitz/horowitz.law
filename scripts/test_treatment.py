#!/usr/bin/env python3
"""Hermetic unit tests for treatment.py's pure logic (no network, no API key).

Covers the citation-window text extractor (passage) and, most importantly, the
full-history-vs-incremental sweep decision (sweep_since / swept_full) -- the state
machine behind the fix for a card whose oid is resolved but whose citer search is
cut short by a budget stop: it must NOT be marked fully swept, so the next run
redoes the full-history search instead of dropping to the 200-day window.

Run directly: `python scripts/test_treatment.py`.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import treatment      # noqa: E402  (sys.path shim must run first)

FAILS = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def test_passage():
    check("empty text -> empty", treatment.passage("", "Smith v. Jones") == "")
    # No distinctive party name present -> the WIDE fallback (not just the opening). A short body is
    # returned whole (it is shorter than the wide cap).
    body = "a plain paragraph with no matching party names in it at all"
    check("no-match returns the (short) body via the wide fallback",
          treatment.passage(body, "Smith v. Jones") == body)
    # Distinctive surname present -> a window around it that includes the surname, capped at MAXCHARS.
    doc = ("x" * 3000) + " Smith " + ("y" * 3000)
    p = treatment.passage(doc, "Smith v. Jones")
    check("distinctive surname returns a window containing it", "Smith" in p)
    check("located window never exceeds MAXCHARS", len(p) <= treatment.MAXCHARS)
    # The not-located fallback is bounded by WIDE_MAXCHARS (wider than the old opening-only cap), so a
    # 'neutral' read off it saw far more of the opinion than just the caption.
    check("wide fallback is wider than the old MAXCHARS cap", treatment.WIDE_MAXCHARS > treatment.MAXCHARS)
    check("wide fallback is capped at WIDE_MAXCHARS",
          len(treatment.passage("z" * (treatment.WIDE_MAXCHARS + 5000), "No v. Match")) == treatment.WIDE_MAXCHARS)

    # The core bug: a caption whose first token is a ubiquitous word ('State') must NOT anchor on the
    # opinion's first everyday 'state' -- with no distinctive party token present, it is not located.
    noise = "the state of the record on this matter " * 400
    check("does not anchor on the caption stopword 'state'",
          not treatment.located(noise, "State Farm Mut. Ins. Co. v. Barnor-Cooper"))
    # ...and the distinctive second-party surname IS located even deep in the text (past MAXCHARS),
    # exactly where the old opening-only fallback silently missed the overruling.
    deep = ("state " * 4000) + " Barnor-Cooper overruled that holding. " + ("q" * 100)
    check("locates the distinctive party name deep in the text (past MAXCHARS)",
          treatment.located(deep, "State Farm Mut. Ins. Co. v. Barnor-Cooper"))
    check("the deep distinctive-name window carries the treatment discussion",
          "Barnor-Cooper" in treatment.passage(deep, "State Farm Mut. Ins. Co. v. Barnor-Cooper"))


def test_sweep_since():
    card = {"date": "2024-01-15"}
    today = datetime.date(2026, 7, 10)
    # Until a full pass has completed, search from the card's own date (nothing older missed).
    check("not-yet-full searches from card date",
          treatment.sweep_since(card, False, today=today) == "2024-01-15")
    # After a full pass, only the cheap incremental window.
    expect = (today - datetime.timedelta(days=treatment.LOOKBACK_DAYS)).isoformat()
    check("full uses the LOOKBACK_DAYS window",
          treatment.sweep_since(card, True, today=today) == expect)
    check("incremental window is strictly newer than the card date",
          treatment.sweep_since(card, True, today=today) > card["date"])


def test_swept_full():
    # The core of the fix: a stop on a not-yet-full card must leave it not-full.
    check("completed pass on a fresh card -> full", treatment.swept_full(False, "") is True)
    check("stopped pass on a fresh card -> NOT full (redo next run)",
          treatment.swept_full(False, "rest budget") is False)
    check("already-full card stays full after a stop", treatment.swept_full(True, "time budget") is True)
    check("already-full card stays full on a clean run", treatment.swept_full(True, "") is True)
    # Truncation (claim-2): a completed-but-truncated first sweep (search not exhausted, or a cap cut
    # the collect loop) must NOT mark the card full, or its older citers are orphaned by the next
    # run's incremental window.
    check("truncated first sweep (no global stop) -> NOT full", treatment.swept_full(False, "", True) is False)
    check("clean, untruncated first sweep -> full", treatment.swept_full(False, "", False) is True)
    check("already-full card stays full even if a later incremental run truncates",
          treatment.swept_full(True, "", True) is True)


def test_classify_batch():
    """The per-card classify-batch orchestration (TREATMENT_BATCH, treatment._classify_batch):
    custom_id round-trip, per-result ok / errored / unparseable handling, and -- the correctness-
    critical part -- a whole-batch failure returning ok=False so the card is NOT marked fully-swept
    (its citer history is re-searched next run rather than silently skipped)."""
    card = {"name": "Landmark v. State", "synopsis": "holds X", "why": "matters because Y"}
    collect = [{"ccid": c, "cname": "Citer %d" % c, "cdate": "2026-07-0%d" % i, "ccourt": "ga",
                "ctext": "Landmark v. State opinion text. " * 40}
               for i, c in enumerate([501, 502, 503], 1)]
    real_run = treatment.batch.run

    def mixed_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        check("classify batch custom_ids are str(ccid)",
              sorted(rq["custom_id"] for rq in reqs) == ["501", "502", "503"])
        return {"501": {"ok": True, "stop_reason": "end_turn",
                        "text": '{"treatment": "negative", "kind": "overruled", "affects_proposition": true, "confidence": "high"}'},
                "502": {"ok": False, "type": "errored", "error": "x"},
                "503": {"ok": True, "text": "not json {{{", "stop_reason": "end_turn"}}
    treatment.batch.run = mixed_run
    try:
        verdicts, ok = treatment._classify_batch(card, collect, deadline=123.0)
    finally:
        treatment.batch.run = real_run
    check("classify batch: completed job -> ok True", ok is True)
    check("classify batch: only the ok+parseable citer yields a verdict",
          set(verdicts) == {501} and verdicts[501].get("kind") == "overruled")
    check("classify batch: errored + unparseable citers omitted (retry next run, stay unseen)",
          502 not in verdicts and 503 not in verdicts)

    for label, exc in (("timeout", treatment.batch.BatchTimeout("bid", "still running")),
                       ("transport error", treatment.batch.BatchError("submit failed"))):
        def raiser(reqs, deadline=None, interval=20.0, label="batch", _e=exc, **_kw):
            raise _e
        treatment.batch.run = raiser
        try:
            verdicts, ok = treatment._classify_batch(card, collect, deadline=123.0)
        finally:
            treatment.batch.run = real_run
        check("classify batch: whole-batch %s -> ok False (card deferred, not marked full)" % label,
              ok is False and verdicts == {})


def test_pending_rec():
    """The per-citer pending record (option b) trims a search result to just what a later run needs to
    re-fetch its text and re-classify it: cluster id, name, date, court, sub-opinion ids + PDF urls,
    and the _tries counter. _pending_key gives an order-independent identity for change detection."""
    r = {"cluster_id": 4242, "caseName": "Later v. Earlier", "dateFiled": "2026-06-01", "court_id": "ga",
         "opinions": [{"id": 91, "download_url": "https://x/91.pdf", "junk": "drop me"},
                      {"id": 92, "download_url": None}, "not-a-dict"],
         "html_with_citations": "HUGE TEXT " * 1000}
    rec = treatment._pending_rec(r, 2)
    check("pending rec keeps the identity + tries", rec["cluster_id"] == 4242 and rec["_tries"] == 2)
    check("pending rec keeps name/date/court", rec["caseName"] == "Later v. Earlier"
          and rec["dateFiled"] == "2026-06-01" and rec["court_id"] == "ga")
    check("pending rec keeps only id + download_url per sub-opinion (drops bulk text)",
          rec["opinions"] == [{"id": 91, "download_url": "https://x/91.pdf"}, {"id": 92, "download_url": None}]
          and "html_with_citations" not in rec)
    # A trimmed rec is itself a valid `r` for cluster_id_of / citer_text on the next run.
    check("a pending rec round-trips as an r (cluster_id_of resolves it)",
          treatment.update.cluster_id_of(rec) == 4242)
    check("missing name falls back, missing opinions -> empty list",
          treatment._pending_rec({"cluster_id": 7}, 0) == {"cluster_id": 7, "caseName": "(unnamed)",
                                                            "dateFiled": None, "court_id": None,
                                                            "opinions": [], "_tries": 0})
    # _pending_key is order-independent and reflects (id, tries), so a reordered list compares equal.
    a = [treatment._pending_rec({"cluster_id": 1}, 1), treatment._pending_rec({"cluster_id": 2}, 3)]
    check("pending key is order-independent",
          treatment._pending_key(a) == treatment._pending_key(list(reversed(a))))
    check("pending key changes when tries change",
          treatment._pending_key(a) != treatment._pending_key([treatment._pending_rec({"cluster_id": 1}, 2),
                                                               treatment._pending_rec({"cluster_id": 2}, 3)]))


def test_first_time_budget():
    """The backlog cap. Three sweeps died mid-crawl on 2026-08-01 with nine never-swept
    cards queued ahead of everything else, each entitled to an unbounded full-history
    crawl. The cap bounds how many of those one run attempts."""
    check("under the cap, another full crawl is allowed",
          treatment.first_time_allowed(0, cap=3) is True)
    check("the last slot is still allowed",
          treatment.first_time_allowed(2, cap=3) is True)
    check("at the cap, the next never-swept card defers",
          treatment.first_time_allowed(3, cap=3) is False)
    check("past the cap it stays closed",
          treatment.first_time_allowed(9, cap=3) is False)
    check("a cap of 0 defers every full crawl (forces an incremental-only run)",
          treatment.first_time_allowed(0, cap=0) is False)
    check("the default cap is a positive number, not an accidental 0",
          treatment.FIRST_PER_RUN > 0, str(treatment.FIRST_PER_RUN))
    check("with no cap argument it reads the module default",
          treatment.first_time_allowed(treatment.FIRST_PER_RUN - 1) is True
          and treatment.first_time_allowed(treatment.FIRST_PER_RUN) is False)


def test_full_history_ceiling():
    """The depth cap. `max_pages=None` used to mean genuinely unbounded paging -- `out` grew
    by a page of results per iteration with no stop but CourtListener's own `next` and the
    5h deadline. On a 16 GB runner that is a process the supervisor can SIGTERM, which is
    what exit 143 is. The cap must be finite, and must never mark a capped card as swept."""
    check("the full-history ceiling is finite and positive",
          isinstance(treatment.FIRST_PAGES, int) and treatment.FIRST_PAGES > 0,
          str(treatment.FIRST_PAGES))
    check("it is deep enough to be a backstop, not a routine truncation",
          treatment.FIRST_PAGES >= 50, str(treatment.FIRST_PAGES))
    check("it is far deeper than the incremental window",
          treatment.FIRST_PAGES > treatment.PAGES)
    # The exhausted contract is what makes capping safe. citing_results returns
    # exhausted=False whenever a cap stopped the walk; the caller computes
    # `truncated = cap_truncated or not exhausted` and swept_full must then refuse to mark
    # the card done, or its unexamined older history is lost for good.
    check("a capped walk (exhausted=False -> truncated) does NOT mark the card fully swept",
          treatment.swept_full(False, "", True) is False)
    check("a walk that reached the end does",
          treatment.swept_full(False, "", False) is True)
    check("and a card already full is not un-marked by a capped walk",
          treatment.swept_full(True, "", True) is True)


def test_crawl_is_actually_bounded():
    """Exercise citing_results against the pathological case that motivated the ceiling: a
    CourtListener that never stops handing back a `next` cursor. Before the cap this walked
    forever, growing `out` by a page each time -- the shape that gets a process SIGTERMed on
    a 16 GB runner. Asserting on the constant alone did not catch a cap set to a billion."""
    calls = {"n": 0}
    # Fail fast, do not hang. If the ceiling is ever removed, an endless `next` would spin
    # this test until CI killed the job -- a timeout reads as infrastructure trouble, not as
    # "the cap is gone". The runaway guard turns that into an immediate, legible failure.
    RUNAWAY = treatment.FIRST_PAGES + 50

    def endless_cl_get(url, deadline=None):
        calls["n"] += 1
        if calls["n"] > RUNAWAY:
            raise AssertionError("citation walk ran past %d pages -- the ceiling is not "
                                 "bounding it" % RUNAWAY)
        return {"results": [{"cluster_id": calls["n"]}], "next": "https://next.example/page"}

    real_get, real_sleep = treatment.update.cl_get, treatment.time.sleep
    treatment.update.cl_get = endless_cl_get
    treatment.time.sleep = lambda _s: None
    try:
        out, exhausted = treatment.citing_results(1, "2020-01-01", None, max_pages=3)
        check("an explicit page cap stops the walk", calls["n"] == 3, str(calls["n"]))
        check("and reports NOT exhausted, so the card is retried",
              exhausted is False)
        check("results are still returned up to the cap", len(out) == 3, str(len(out)))

        calls["n"] = 0
        out, exhausted = treatment.citing_results(1, "2020-01-01", None, max_pages=None)
        check("a full-history walk is bounded by FIRST_PAGES, not unbounded",
              calls["n"] == treatment.FIRST_PAGES, str(calls["n"]))
        check("and it too reports NOT exhausted", exhausted is False)

        calls["n"] = 0
        treatment.update.cl_get = lambda url, deadline=None: {"results": [{"cluster_id": 1}],
                                                              "next": None}
        out, exhausted = treatment.citing_results(1, "2020-01-01", None, max_pages=None)
        check("a walk that genuinely runs out reports exhausted", exhausted is True)
    finally:
        treatment.update.cl_get, treatment.time.sleep = real_get, real_sleep


def test_rss():
    """A diagnostic must never break the run it is diagnosing."""
    mb = treatment.rss_mb()
    check("rss reads as a positive number on this platform",
          mb is None or (isinstance(mb, int) and mb > 0), str(mb))
    note = treatment.rss_note()
    check("the note is empty or a formatted fragment",
          note == "" or note.startswith("; rss "), repr(note))
    # Patch where the lookup happens: rss_note lives in update and resolves rss_mb in
    # update's namespace, so patching treatment.rss_mb would be a no-op that silently
    # tested nothing.
    real = treatment.update.rss_mb
    treatment.update.rss_mb = lambda: None
    try:
        check("an unreadable rss degrades to an empty fragment, it does not raise",
              treatment.rss_note() == "")
    finally:
        treatment.update.rss_mb = real
    check("treatment re-exports the shared implementation, it does not keep a copy",
          treatment.rss_mb is treatment.update.rss_mb
          and treatment.rss_note is treatment.update.rss_note)


def test_page_logging():
    """A crawl that prints nothing is a crawl you cannot debug -- the whole reason three
    dead runs could not name the card they were on."""
    check("the first page always prints, so a started crawl is visible",
          treatment.log_this_page(1, every=10) is True)
    check("quiet between milestones", treatment.log_this_page(4, every=10) is False)
    check("every Nth page prints", treatment.log_this_page(20, every=10) is True)
    check("a zero cadence does not divide by zero, and still prints page 1",
          treatment.log_this_page(1, every=0) is True
          and treatment.log_this_page(7, every=0) is False)
    check("the default cadence is positive",
          treatment.PAGE_LOG_EVERY > 0, str(treatment.PAGE_LOG_EVERY))


def main():
    print("treatment pure logic:")
    test_passage()
    test_sweep_since()
    test_swept_full()
    test_pending_rec()
    print("treatment backlog + progress:")
    test_first_time_budget()
    test_full_history_ceiling()
    test_crawl_is_actually_bounded()
    test_page_logging()
    test_rss()
    print("treatment classify batch:")
    test_classify_batch()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
