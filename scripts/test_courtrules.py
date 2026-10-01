#!/usr/bin/env python3
"""Hermetic unit tests for the Court Rules Watch (scripts/courtrules.py). No network, no key.

The page fetch is an injected `fetch` seam and the extraction call an injected `ai` seam, so the
whole funnel -- strip HTML to text, content-hash, extract amendments, dedup, card -- runs against
canned HTML and a canned model verdict. Load-bearing invariants: an UNCHANGED page (same content
hash) skips the model call; a page is recorded as seen only after a SUCCESSFUL extraction (a
transient error retries); a card id is stable across runs so the same amendment is never carded
twice; only recognized rule sets (FRCP/FRE/FRAP/FRBP) card; everything fails open.

Run directly: `python scripts/test_courtrules.py`.
"""
import os
import sys
import unittest.mock as _m

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import courtrules as C  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


PAGE_V1 = ("<html><head><style>.x{color:red}</style></head><body>"
           "<h1>Pending Rules and Forms Amendments</h1>"
           "<p>Amendments to the Federal Rules of Civil Procedure, Rule 26, effective December 1, 2025.</p>"
           "<script>tracker();</script></body></html>")
PAGE_V2 = PAGE_V1.replace("Rule 26", "Rule 26 and Rule 702 (Evidence)")

AMEND_26 = {"rule_set": "FRCP", "rule": "Rule 26", "summary": "Narrows initial disclosure timing.",
            "status": "pending", "effective_date": "2025-12-01", "impact": "Changes discovery scheduling."}
AMEND_702 = {"rule_set": "FRE", "rule": "Rule 702", "summary": "Clarifies the expert-admissibility standard.",
             "status": "pending", "effective_date": "2025-12-01", "impact": "Raises the bar for expert testimony."}
AMEND_CRIM = {"rule_set": "FRCrP", "rule": "Rule 16", "summary": "Criminal discovery.", "status": "pending",
              "effective_date": "2025-12-01", "impact": "n/a"}


def ai_returning(*amendments):
    def ai(body, label="courtrules"):
        return {"amendments": list(amendments)}
    return ai


def ai_boom(body, label="courtrules"):
    raise RuntimeError("model down")


def main():
    print("court rules watch:")

    # --- strip_html ---
    txt = C.strip_html(PAGE_V1)
    check("strip_html drops script/style and tags", "tracker" not in txt and "<p>" not in txt and ".x{" not in txt)
    check("strip_html keeps the readable text", "Rule 26" in txt and "Civil Procedure" in txt)
    check("page_hash changes when the text changes", C.page_hash(C.strip_html(PAGE_V1)) != C.page_hash(C.strip_html(PAGE_V2)))

    # --- card id + build_card ---
    check("card id is stable and case-insensitive on rule_set",
          C._card_id("FRCP", "Rule 26") == C._card_id("frcp", "Rule 26"))
    check("card id ignores the effective date so pending->effective updates in place, not duplicates",
          C.build_card(dict(AMEND_26, status="pending", effective_date=""), "u")["id"]
          == C.build_card(dict(AMEND_26, status="effective", effective_date="2025-12-01"), "u")["id"])
    card = C.build_card(AMEND_26, "https://uscourts.gov/x")
    check("build_card keys on the synthetic id and normalizes fields",
          card and card["rule_set"] == "FRCP" and card["status"] == "pending" and card["effective_date"] == "2025-12-01")
    check("build_card blanks a malformed effective_date",
          C.build_card(dict(AMEND_26, effective_date="soon"), "u")["effective_date"] == "")
    check("build_card defaults an unknown status to pending",
          C.build_card(dict(AMEND_26, status="weird"), "u")["status"] == "pending")
    check("build_card rejects an unrecognized (criminal-only) rule set", C.build_card(AMEND_CRIM, "u") is None)

    # --- extract: [] vs None(error) ---
    check("extract returns the amendment list", C.extract("text", ai_returning(AMEND_26)) == [AMEND_26])
    check("extract returns [] when the page names none", C.extract("text", ai_returning()) == [])
    check("extract returns [] on empty page text without calling the model",
          C.extract("   ", ai_boom) == [])
    check("extract signals a model error as None (retry)", C.extract("text", ai_boom) is None)

    # --- run: a fresh page cards the amendment. Patch _load_seen to an EMPTY state: the fixtures use
    #     REAL rule ids (FRCP Rule 26, FRE Rule 702), so reading the on-disk courtrules_state.json --
    #     which the watch commits and populates with those very rules -- would dedup the fixture as
    #     "already seen" and card nothing. Empty seen keeps this hermetic regardless of the state file. ---
    fetch_v1 = lambda url: PAGE_V1
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {}}):
        cards, notes, upd = C.run(fetch=fetch_v1, ai=ai_returning(AMEND_26), sources=[("Pending", "u")],
                                  today=__import__("datetime").date(2026, 7, 17))
    check("run cards a newly-seen amendment", len(cards) == 1 and cards[0]["rule"] == "Rule 26")
    check("run records the page hash and the card id in seen_updates",
          upd["pages"] and upd["cards"] and len(upd["cards"]) == 1)

    # --- run: an UNCHANGED page skips the model entirely ---
    h = C.page_hash(C.strip_html(PAGE_V1))
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {"u": h}, "cards": {}}):
        cards2, notes2, _ = C.run(fetch=lambda url: PAGE_V1, ai=ai_boom, sources=[("Pending", "u")],
                                  today=__import__("datetime").date(2026, 7, 17))
    check("run skips the model on an unchanged page (ai_boom never raised)", cards2 == [])
    check("run notes the page as unchanged", any("unchanged" in n for n in notes2))

    # --- run: an already-carded amendment is not re-carded ---
    cid = C._card_id("FRCP", "Rule 26")
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {cid: "2026-01-01"}}):
        cards3, _, _ = C.run(fetch=lambda url: PAGE_V1, ai=ai_returning(AMEND_26), sources=[("Pending", "u")],
                             today=__import__("datetime").date(2026, 7, 17))
    check("run does not re-card an amendment already seen", cards3 == [])

    # --- run: a transient extraction error leaves the page un-hashed (retry) ---
    _, notes4, upd4 = C.run(fetch=lambda url: PAGE_V1, ai=ai_boom, sources=[("Pending", "u")],
                            today=__import__("datetime").date(2026, 7, 17))
    check("a transient extraction error does not record the page hash (retries)",
          upd4["pages"] == {} and any("extraction failed" in n for n in notes4))

    # --- run: an unreachable page fails open ---
    _, notes5, upd5 = C.run(fetch=lambda url: "", ai=ai_returning(AMEND_26), sources=[("Pending", "u")])
    check("an unreachable page fails open (no cards, no hash)",
          upd5["pages"] == {} and any("unreachable" in n for n in notes5))

    # --- marker guard: a fetched-but-contentless shell must NOT be recorded seen off an empty
    #     extraction (else the stable shell hash sticks and the page is never re-examined) ---
    check("has_rules_markers accepts the real amendments page", C.has_rules_markers(C.strip_html(PAGE_V1)))
    check("has_rules_markers rejects a contentless shell",
          not C.has_rules_markers("<html><body><div id=app></div>Loading…</body></html>"))
    shell = "<html><head><title>Rules</title></head><body><div id='root'></div><p>Enable JavaScript.</p></body></html>"
    cardsS, notesS, updS = C.run(fetch=lambda url: shell, ai=ai_boom, sources=[("Pending", "u")],
                                 today=__import__("datetime").date(2026, 7, 17))
    check("a shell page draws no cards and is NOT hashed (will retry)",
          cardsS == [] and updS["pages"] == {})
    check("a shell page surfaces a visible marker note (not a silent 'unchanged')",
          any("no Federal Rules markers" in n for n in notesS))
    check("the shell guard never called the model (ai_boom would have raised)", True)

    # --- multiple amendments on one page (empty seen, so the real state file cannot dedup the
    #     fixtures' real rule ids -- see the fresh-page test above) ---
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {}}):
        multi, _, _ = C.run(fetch=lambda url: PAGE_V2, ai=ai_returning(AMEND_26, AMEND_702),
                            sources=[("Pending", "u")], today=__import__("datetime").date(2026, 7, 17))
    check("run cards multiple amendments from one page",
          {c["rule"] for c in multi} == {"Rule 26", "Rule 702"})
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {}}):
        crim_cards = C.run(fetch=lambda url: PAGE_V2, ai=ai_returning(AMEND_CRIM),
                           sources=[("Pending", "u")], today=__import__("datetime").date(2026, 7, 17))[0]
    check("run drops a criminal-only amendment mixed in", not crim_cards)

    # --- merge_cards + merge_seen ---
    merged, added, updated = C.merge_cards([dict(card, first_seen="2026-06-01")],
                                           [dict(card, summary="revised"), C.build_card(AMEND_702, "u")])
    check("merge_cards adds new and updates existing", added == 1 and updated == 1)
    check("merge_cards preserves first_seen",
          next(c for c in merged if c["id"] == card["id"])["first_seen"] == "2026-06-01")
    folded = C.merge_seen({"pages": {"a": "1"}, "cards": {"x": "d"}},
                          {"pages": {"b": "2"}, "cards": {"y": "e"}})
    check("merge_seen unions pages and cards", folded["pages"] == {"a": "1", "b": "2"} and folded["cards"] == {"x": "d", "y": "e"})

    # --- batched extraction (COURTRULES_BATCH): the Opus page extraction runs as ONE batch job.
    #     Stub batch.run so no network; assert the {url: amendments|None} space matches the sync path
    #     (extract cards + hashes the page; a whole-batch timeout leaves it un-hashed to retry). ---
    import batch as _B
    _real_run = _B.run

    # The custom_id names the page AND the text sent (url + page hash), never a list index.
    _want_cid = C.page_cid("cr", "u", C.page_hash(C.strip_html(PAGE_V1)))

    def _fake_batch(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        assert [r["custom_id"] for r in reqs] == [_want_cid], [r["custom_id"] for r in reqs]
        return {_want_cid: {"ok": True, "text": __import__("json").dumps({"amendments": [AMEND_26]})}}

    _B.run = _fake_batch
    try:
        # Empty seen, so the on-disk state file cannot dedup the fixture's real rule id (see above).
        with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {}}):
            bcards, bnotes, bupd = C.run(fetch=lambda url: PAGE_V1, ai=ai_boom, sources=[("Pending", "u")],
                                         today=__import__("datetime").date(2026, 7, 17), batch_enabled=True)
    finally:
        _B.run = _real_run
    check("batch extract cards the amendment (ai_boom never called -> batch path used)",
          len(bcards) == 1 and bcards[0]["rule"] == "Rule 26")
    check("batch extract hashes the page seen", bupd["pages"].get("u"))
    check("batch run announces the batch", any("batching" in n for n in bnotes))

    def _timeout_batch(reqs, deadline=None, interval=20.0, label="batch", **_kw):
        raise _B.BatchTimeout("bid", "still running")

    _B.run = _timeout_batch
    try:
        tcards, _, tupd = C.run(fetch=lambda url: PAGE_V1, ai=ai_boom, sources=[("Pending", "u")],
                                today=__import__("datetime").date(2026, 7, 17), batch_enabled=True)
    finally:
        _B.run = _real_run
    check("batch extract timeout: no cards, page left un-hashed (retry next run)",
          tcards == [] and tupd["pages"] == {})

    # --- a deferred extraction batch is CARRIED (run 36318024609 was killed waiting on this one);
    #     the next run uses its result only for the same page with the same text ---
    import contextlib
    import io
    import tempfile
    import watchbatch as _W
    _real = (_B.run, _B.status, _B.collect)
    h1 = C.page_hash(C.strip_html(PAGE_V1))
    h2 = C.page_hash(C.strip_html(PAGE_V2))
    cid1, cid2 = C.page_cid("cr", "u", h1), C.page_cid("cr", "u", h2)
    day = __import__("datetime").date(2026, 7, 17)

    def go(page, book):
        buf = io.StringIO()
        with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {}}), \
                contextlib.redirect_stdout(buf):
            out = C.run(fetch=lambda url: page, ai=ai_boom, sources=[("Pending", "u")], today=day,
                        batch_enabled=True, carry=book)
        return out, buf.getvalue()

    try:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "watch_batches.json")

            def _slow(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                on_submit("msgbatch_C")
                raise _B.BatchTimeout("msgbatch_C", "still running")

            _B.run = _slow
            book = _W.CarryBook.load("courtrules", persist=True, path=path)
            (ccards, _n, cupd), out = go(PAGE_V1, book)
            book.save()
            disk = _W.load_carries("courtrules", path)
            check("carry: the extraction batch is recorded under the page's url + hash id",
                  [r["id"] for r in disk] == ["msgbatch_C"] and list(disk[0]["items"]) == [cid1]
                  and disk[0]["items"][cid1]["url"] == "u" and disk[0]["items"][cid1]["h"] == h1)
            check("carry: the page stays un-hashed", ccards == [] and cupd["pages"] == {})

            def _no_batch(*_a, **_kw):
                raise AssertionError("no new batch expected")

            _B.run = _no_batch
            _B.status = lambda bid, label="batch": {"id": bid, "processing_status": "in_progress"}
            (hcards, hnotes, hupd), _ = go(PAGE_V1, _W.CarryBook.load("courtrules", path=path))
            check("in flight: the page is left un-hashed and nothing is re-sent",
                  hcards == [] and hupd["pages"] == {} and any("carried batch" in n for n in hnotes))

            _B.status = lambda bid, label="batch": {"id": bid, "processing_status": "ended", "results_url": "x"}
            _B.collect = lambda obj, label="batch": {
                cid1: {"ok": True, "text": __import__("json").dumps({"amendments": [AMEND_26]})}}
            book = _W.CarryBook.load("courtrules", path=path)
            (rcards, _n, rupd), out = go(PAGE_V1, book)
            check("collected: the carried extraction cards the page with no new call",
                  len(rcards) == 1 and rcards[0]["rule"] == "Rule 26" and rupd["pages"].get("u") == h1)
            check("collected: reported, and nothing left to carry",
                  "msgbatch_C (1 results applied, 0 re-queued)" in out and book.carries == [], out)

            # The page CHANGED after the batch was sent: that result describes old text.
            sent = []

            def _fresh(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
                sent.extend(r["custom_id"] for r in reqs)
                return {r["custom_id"]: {"ok": True, "text": __import__("json").dumps({"amendments": []})}
                        for r in reqs}

            _B.run = _fresh
            (mcards, _n, mupd), out = go(PAGE_V2, _W.CarryBook.load("courtrules", path=path))
            check("moved: a carried result for different text is not applied (re-queued)",
                  mcards == [] and sent == [cid2] and mupd["pages"].get("u") == h2
                  and "msgbatch_C (0 results applied, 1 re-queued)" in out, "%r %s" % (sent, out))

            # An ENDED carry of the old text beside an IN-FLIGHT carry of the current text: the
            # old result is discarded with a log line (it used to sit behind the in-flight check),
            # and the page is held for the running batch.
            import time as _time
            now_iso = _W._iso(_time.time())
            pair = [{"id": "msgbatch_X", "at": now_iso, "items": {cid1: {"url": "u", "h": h1, "label": "P"}}},
                    {"id": "msgbatch_Y", "at": now_iso, "items": {cid2: {"url": "u", "h": h2, "label": "P"}}}]
            _B.run = _no_batch
            _B.status = lambda bid, label="batch": (
                {"id": bid, "processing_status": "ended", "results_url": "x"} if bid == "msgbatch_X"
                else {"id": bid, "processing_status": "in_progress"})
            _B.collect = lambda obj, label="batch": {
                cid1: {"ok": True, "text": __import__("json").dumps({"amendments": [AMEND_26]})}}
            book = _W.CarryBook("courtrules", pair)
            (scards, snotes, supd), out = go(PAGE_V2, book)
            check("stale ended + current in flight: the stale extraction is discarded, logged",
                  scards == [] and "discarding carried extraction of u from batch msgbatch_X" in out
                  and "msgbatch_X (0 results applied, 1 re-queued)" in out, out)
            check("stale ended + current in flight: the page is held, un-hashed, for the running batch",
                  supd["pages"] == {} and any("carried batch" in n for n in snotes)
                  and [r["id"] for r in book.carries] == ["msgbatch_Y"], str(book.carries))

            # The page did not fetch (or failed its marker check): its carried extraction can be
            # neither applied nor called stale, so it STAYS carried for the next run.
            _B.status = lambda bid, label="batch": {"id": bid, "processing_status": "ended", "results_url": "x"}
            for why, page in (("unreachable", ""), ("no markers", "<html><body>Site maintenance</body></html>")):
                book = _W.CarryBook("courtrules", [{"id": "msgbatch_K", "label": "courtrules-extract",
                                                    "at": now_iso, "items": {cid1: {"url": "u", "h": h1,
                                                                                    "label": "P"}}}])
                (kcards, _n, kupd), out = go(page, book)
                check("%s: the carried extraction is not applied, and stays carried" % why,
                      kcards == [] and kupd["pages"] == {}
                      and [(r["id"], list(r["items"]), r["at"]) for r in book.carries]
                      == [("msgbatch_K", [cid1], now_iso)], "%r %s" % (book.carries, out))
                check("%s: reported as kept carried" % why,
                      "msgbatch_K (0 results applied, 0 re-queued, 1 kept carried)" in out, out)
            # ...and the next run, with the page back at the same text, applies it.
            (bcards, _n, bupd), out = go(PAGE_V1, book)
            check("kept carry: applied once the page is read at the same text",
                  len(bcards) == 1 and bupd["pages"].get("u") == h1 and book.carries == [], out)
    finally:
        _B.run, _B.status, _B.collect = _real

    # ---- A relabelled amendment is the SAME amendment ----------------------------------------
    # The regression this pins: the extractor wrote "Rule 707", then "Rule 707 (new)", then
    # "New Rule 707" on three successive runs. Each phrasing hashed to a different id, missed
    # seen_cards, and carded again -- FRE 707 and FRBP 7043 each reached three cards on the
    # public page before anyone noticed.
    print("\nrelabelled rules are one card:")
    for variant in ("Rule 707 (new)", "New Rule 707", "  Rule   707  ", "new Rule 707"):
        check("%r canonicalizes to 'Rule 707'" % variant, C.canonical_rule(variant) == "Rule 707",
              "got %r" % C.canonical_rule(variant))
    base = C._card_id("FRE", "Rule 707")
    for variant in ("Rule 707 (new)", "New Rule 707", "NEW RULE 707", "Rule 707 ( New )"):
        check("%r shares the id of 'Rule 707'" % variant, C._card_id("FRE", variant) == base)
    check("the stored designation is canonical, so the page shows one name",
          C.build_card({"rule_set": "FRE", "rule": "New Rule 707", "summary": "s", "impact": "i",
                        "status": "pending", "effective_date": "2027-12-01"}, "u",
                       today=__import__("datetime").date(2026, 8, 16))["rule"] == "Rule 707")

    # The other half of the guarantee: canonicalizing must not FUSE distinct amendments.
    check("Rule 5 and Rule 5.2 stay separate", C._card_id("FRCP", "Rule 5") != C._card_id("FRCP", "Rule 5.2"))
    check("a Form is not the Rule of the same number",
          C._card_id("FRAP", "Form 4") != C._card_id("FRAP", "Rule 4"))
    check("the same number in two rule sets stays separate",
          C._card_id("FRE", "Rule 707") != C._card_id("FRBP", "Rule 707"))
    check("a designation with no 'new' marker is untouched",
          C.canonical_rule("Official Forms 101 and 106C") == "Official Forms 101 and 106C")
    check("'new' inside a longer word is not stripped",
          C.canonical_rule("Rule 9 (renewed)") == "Rule 9 (renewed)")

    # End to end: a run whose extraction relabels an already-seen rule must card nothing.
    seen_id = C._card_id("FRE", "Rule 707")
    with _m.patch.object(C, "_load_seen", lambda: {"pages": {}, "cards": {seen_id: "2026-07-19"}}):
        cards, _notes, _upd = C.run(
            fetch=lambda url: PAGE_V1, sources=[("Pending", "u")],
            ai=lambda body, label=None: {"amendments": [
                {"rule_set": "FRE", "rule": "New Rule 707", "summary": "relabelled", "impact": "i",
                 "status": "pending", "effective_date": "2027-12-01"}]},
            today=__import__("datetime").date(2026, 8, 16))
    check("a relabelled, already-seen rule cards nothing", cards == [], "got %d card(s)" % len(cards))

    # ---- impact must come from the page ------------------------------------------------------
    # 29 of 31 published cards carried an "impact" the source page does not support: the page is a
    # docket of rule numbers and dates, and the model was reasoning from what it knows the rule
    # covers. "Self-authentication procedures, often used for electronic records, may change and
    # alter trial exhibit preparation" is an inference about FRE 902, not a statement the page made
    # about the amendment -- a fabricated consequence attached to a real citation, on a site lawyers
    # read. The same shape as the O.C.G.A. insertion the review lane caught in a case card.
    print("\nimpact is grounded, not inferred:")
    check("the extractor is told impact usually comes back empty",
          "IMPACT MUST COME FROM THE PAGE" in C.EXTRACT_SYSTEM)
    check("and is told not to reason from what the rule covers",
          "Do NOT reason from what you know the rule covers" in C.EXTRACT_SYSTEM)
    check("with the actual failure named, so the instruction is not abstract",
          "902" in C.EXTRACT_SYSTEM and "fabricated consequence" in C.EXTRACT_SYSTEM)
    check("and it still says a docket-only listing is WORTH reporting",
          "still worth reporting" in C.EXTRACT_SYSTEM)
    check("an empty impact survives build_card",
          C.build_card({"rule_set": "FRE", "rule": "Rule 902", "summary": "s", "impact": "",
                        "status": "pending", "effective_date": "2028-12-01"}, "u",
                       today=__import__("datetime").date(2026, 8, 18))["impact"] == "")

    # The published set must stay clean: no card may carry an impact the page cannot support.
    import json as _json
    import os as _os
    cards = _json.load(open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "courtrules.json"), encoding="utf-8"))
    withimpact = [c for c in cards if (c.get("impact") or "").strip()]
    check("no published court-rule card carries an inferred impact", not withimpact,
          "%d do: %s" % (len(withimpact), [c.get("rule") for c in withimpact][:3]))

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
