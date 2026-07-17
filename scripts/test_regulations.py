#!/usr/bin/env python3
"""Hermetic unit tests for the Federal Regulatory Watch (scripts/regulations.py). No network, no key.

Every network call is an injected `fetch` seam and every model call an injected `ai` seam, so the
whole funnel -- the Federal Register query, pagination, the seen-by-document_number dedup, the
relevance screen, the writer, CFR formatting, and card assembly -- runs against canned JSON and
canned model verdicts. Load-bearing invariants: a document_number is immutable so once seen it never
returns; the screen fails OPEN (a model error keeps the rule for the writer); the writer fails CLOSED
(an error or a decline yields no card); a substantive safety/insurance rule is kept while a fee /
administrative rule is dropped; the Federal Register needs no key, so a run just works.

Run directly: `python scripts/test_regulations.py`.
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regulations as R  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


FMCSA = [{"slug": "federal-motor-carrier-safety-administration",
          "name": "Federal Motor Carrier Safety Administration"}]

HOS = {"document_number": "2025-11111", "type": "Rule",
       "title": "Hours of Service of Drivers", "action": "Final rule.",
       "abstract": "FMCSA amends the hours-of-service rules governing driver on-duty time.",
       "publication_date": "2025-06-10", "effective_on": "2025-08-10",
       "html_url": "https://www.federalregister.gov/documents/2025/06/10/2025-11111/hours",
       "cfr_references": [{"title": 49, "part": 395}, {"title": 49, "part": 396}, {"title": 49, "part": 395}],
       "agencies": FMCSA, "agency_names": ["Federal Motor Carrier Safety Administration"],
       "regulation_id_numbers": ["2126-AC01"]}
FEE = {"document_number": "2025-22222", "type": "Rule",
       "title": "Unified Registration System Fee Adjustments", "action": "Final rule.",
       "abstract": "FMCSA adjusts registration filing fees for the coming year.",
       "publication_date": "2025-06-11", "effective_on": "2025-07-11",
       "html_url": "https://www.federalregister.gov/documents/2025/06/11/2025-22222/fees",
       "cfr_references": [{"title": 49, "part": 390}], "agencies": FMCSA,
       "agency_names": ["Federal Motor Carrier Safety Administration"]}
INS = {"document_number": "2025-33333", "type": "Rule",
       "title": "Minimum Levels of Financial Responsibility for Motor Carriers", "action": "Final rule.",
       "abstract": "FMCSA raises the minimum insurance a motor carrier must maintain.",
       "publication_date": "2025-06-12", "effective_on": "2025-09-01",
       "html_url": "https://www.federalregister.gov/documents/2025/06/12/2025-33333/insurance",
       "cfr_references": [{"title": 49, "part": 387}], "agencies": FMCSA,
       "agency_names": ["Federal Motor Carrier Safety Administration"]}

PAGE = {"count": 3, "total_pages": 1, "next_page_url": None, "results": [HOS, FEE, INS]}


def fake_fetch(url):
    """Single-page fixture: all three FMCSA rules on one page."""
    return json.dumps(PAGE)


def paginated_fetch(url):
    """Two-page fixture (HOS on page 1, INS on page 2) to exercise pagination follow-through."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    page = int((q.get("page", ["1"]) or ["1"])[0])
    if page == 1:
        return json.dumps({"count": 2, "total_pages": 2, "next_page_url": "https://x/api?page=2", "results": [HOS]})
    return json.dumps({"count": 2, "total_pages": 2, "next_page_url": None, "results": [INS]})


def make_ai(script):
    def ai(body, label="call"):
        h = script.get(label)
        if h is None:
            raise AssertionError("unexpected ai label %r" % label)
        return h(body) if callable(h) else h
    return ai


def boom(body):
    raise RuntimeError("model down")


def main():
    print("regulatory watch:")

    # --- query URL ---
    u = R._query_url(["federal-motor-carrier-safety-administration"], ["RULE", "PRORULE"], "2026-06-01", page=2)
    check("query encodes the agency condition",
          "conditions%5Bagencies%5D%5B%5D=federal-motor-carrier-safety-administration" in u)
    check("query encodes both type conditions",
          u.count("conditions%5Btype%5D%5B%5D=") == 2)
    check("query encodes the publication-date floor and page",
          "publication_date" in u and "page=2" in u and "order=newest" in u)

    # --- pagination ---
    docs = R.fetch_documents(since="2025-06-01", fetch=paginated_fetch)
    check("fetch_documents follows pagination across pages",
          {d["document_number"] for d in docs} == {"2025-11111", "2025-33333"})
    single = R.fetch_documents(since="2025-06-01", fetch=fake_fetch)
    check("fetch_documents returns all results on a single page",
          {d["document_number"] for d in single} == {"2025-11111", "2025-22222", "2025-33333"})

    # --- seen dedup ---
    fresh = R.new_documents(single, seen={"2025-11111": "2025-06-10"})
    check("new_documents drops an already-seen document_number",
          "2025-11111" not in {d["document_number"] for d in fresh})
    check("new_documents keeps unseen documents",
          {"2025-22222", "2025-33333"} <= {d["document_number"] for d in fresh})

    # --- CFR + agency + type formatting ---
    check("cfr_label groups and dedups parts under a CFR title", R.cfr_label(HOS) == "49 CFR 395, 396")
    check("cfr_label sorts multiple CFR titles ascending",
          R.cfr_label({"cfr_references": [{"title": 49, "part": 387}, {"title": 40, "part": 1}]})
          == "40 CFR 1; 49 CFR 387")
    check("cfr_label is empty with no references", R.cfr_label({"cfr_references": []}) == "")
    check("agency_label maps the FMCSA slug to a short badge", R.agency_label(HOS) == "FMCSA")
    check("agency_label falls back to the agency name for an unknown slug",
          R.agency_label({"agencies": [{"slug": "x", "name": "Some Agency"}],
                          "agency_names": ["Some Agency"]}) == "Some Agency")
    check("type label normalizes 'Rule' to 'Final Rule'", R._type_label(HOS) == "Final Rule")
    check("type label passes 'Proposed Rule' through", R._type_label({"type": "Proposed Rule"}) == "Proposed Rule")

    # --- screen: fail-open, area filter, drop ---
    keep, areas, _ = R.screen_doc(HOS, make_ai({"reg-screen": {"relevant": True, "areas": ["auto", "bogus"], "reason": "hos"}}))
    check("screen keeps a substantive safety rule", keep)
    check("screen filters invalid area codes", areas == ["auto"])
    drop_keep, _, _ = R.screen_doc(FEE, make_ai({"reg-screen": {"relevant": False, "areas": [], "reason": "fees"}}))
    check("screen drops an administrative fee rule", not drop_keep)
    open_keep, _, reason = R.screen_doc(HOS, make_ai({"reg-screen": boom}))
    check("screen FAILS OPEN on a model error", open_keep and reason == "screen-error-kept")

    # --- writer: keep / decline / transient error ---
    good = {"keep": True, "areas": ["auto"], "synopsis": "Amends HOS.", "impact": "Bears on driver-fatigue claims.",
            "effective_date": "2025-08-10"}
    check("writer returns a verdict on a good rule", R.write_card(HOS, make_ai({"reg-write": good})) == good)
    check("writer declines (None) on keep=false",
          R.write_card(HOS, make_ai({"reg-write": {"keep": False}})) is None)
    check("writer FAILS CLOSED (None) on an empty synopsis",
          R.write_card(HOS, make_ai({"reg-write": {"keep": True, "synopsis": " "}})) is None)
    check("writer signals a TRANSIENT error distinctly (WRITER_ERROR)",
          R.write_card(HOS, make_ai({"reg-write": boom})) is R.WRITER_ERROR)

    # --- card assembly ---
    card = R.build_card(HOS, good, today=__import__("datetime").date(2026, 7, 17))
    check("card is keyed on the document_number", card["document_number"] == "2025-11111")
    check("card carries the normalized type and agency", card["type"] == "Final Rule" and card["agency"] == "FMCSA")
    check("card carries the CFR label and RIN", card["cfr"] == "49 CFR 395, 396" and card["rin"] == "2126-AC01")
    check("card keeps the writer's effective_date", card["effective_date"] == "2025-08-10")
    fallback = R.build_card(INS, {"keep": True, "areas": [], "synopsis": "s", "impact": "i", "effective_date": ""},
                            today=__import__("datetime").date(2026, 7, 17))
    check("card falls back to the endpoint effective_on when the writer omits it",
          fallback["effective_date"] == "2025-09-01")

    # --- full run: screen drops the fee rule, cards the safety + insurance rules ---
    def screen_router(body):
        txt = body["messages"][0]["content"].lower()
        rel = "fee" not in txt and "registration filing" not in txt
        return {"relevant": rel, "areas": ["auto"], "reason": "x"}

    def write_router(body):
        return {"keep": True, "areas": ["auto"], "synopsis": "Synopsis.", "impact": "Matters.", "effective_date": ""}

    ai = make_ai({"reg-screen": screen_router, "reg-write": write_router})
    cards, notes, seen = R.run(fetch=fake_fetch, ai=ai, today=__import__("datetime").date(2026, 7, 17))
    got = {c["document_number"] for c in cards}
    check("run cards the hours-of-service and insurance rules", "2025-11111" in got and "2025-33333" in got)
    check("run drops the fee rule", "2025-22222" not in got)
    check("run records a carded rule as seen", seen.get("2025-11111") == "2025-06-10")
    check("run records a screen-dropped rule as seen", seen.get("2025-22222") == "2025-06-11")

    err_ai = make_ai({"reg-screen": {"relevant": True, "areas": [], "reason": "x"}, "reg-write": boom})
    _, _, seen_err = R.run(fetch=fake_fetch, ai=err_ai, today=__import__("datetime").date(2026, 7, 17))
    check("a transient writer error leaves the rule un-seen (retries next run)", seen_err == {})

    capped, cnotes, _ = R.run(fetch=fake_fetch, ai=ai, max_run=1, today=__import__("datetime").date(2026, 7, 17))
    check("run honors max_run and says remaining rules retry",
          len(capped) == 1 and any("REGULATION_MAX" in n for n in cnotes))

    # --- merge_cards ---
    c_ins = R.build_card(INS, good, today=__import__("datetime").date(2026, 7, 17))
    existing = [dict(card, first_seen="2026-06-01")]
    merged, added, updated = R.merge_cards(existing, [dict(card, synopsis="amended"), c_ins])
    check("merge adds a new card", added == 1)
    check("merge updates an existing card in place", updated == 1)
    check("merge preserves the original first_seen",
          next(c for c in merged if c["document_number"] == "2025-11111")["first_seen"] == "2026-06-01")
    check("merge sorts newest publication_date first",
          merged[0]["publication_date"] >= merged[-1]["publication_date"])

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
