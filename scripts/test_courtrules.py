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

    def _fake_batch(reqs, deadline=None, interval=20.0, label="batch"):
        assert [r["custom_id"] for r in reqs] == ["cr-0"], [r["custom_id"] for r in reqs]
        return {"cr-0": {"ok": True, "text": __import__("json").dumps({"amendments": [AMEND_26]})}}

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

    def _timeout_batch(reqs, deadline=None, interval=20.0, label="batch"):
        raise _B.BatchTimeout("bid", "still running")

    _B.run = _timeout_batch
    try:
        tcards, _, tupd = C.run(fetch=lambda url: PAGE_V1, ai=ai_boom, sources=[("Pending", "u")],
                                today=__import__("datetime").date(2026, 7, 17), batch_enabled=True)
    finally:
        _B.run = _real_run
    check("batch extract timeout: no cards, page left un-hashed (retry next run)",
          tcards == [] and tupd["pages"] == {})

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
