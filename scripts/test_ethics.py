#!/usr/bin/env python3
"""Hermetic unit tests for the ethics watch (scripts/ethics.py). No network, no key.

The fetch is an injected `fetch` seam and the extraction an injected `ai` seam, so the whole
path -- strip HTML, content-hash, extract, card, merge -- runs against canned input.

Two invariants carry the design and are why this file exists:

  IDENTITY IS THE CANONICAL NUMBER. Court rules shipped the same bug for a month: the id hashed
  the extractor's raw designation, the extractor relabelled between runs, and FRE 707 reached
  THREE cards on the public page. An FAO is cited even more loosely ("Formal Advisory Opinion
  No. 24-1", "FAO 24-1", "24-1"), so the number is parsed out before anything is keyed on it.

  A CARD UPDATES, BUT ONLY ON REAL NEWS. An FAO's status is its whole life -- proposed, filed
  with the Court, approved -- so unlike a court rule it must not be skipped once seen. The other
  half of that: re-reading a page must NOT count an unchanged opinion as updated, or every run
  opens a PR that changes nothing and the watch gets ignored.

Run directly: `python scripts/test_ethics.py`.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ethics as E  # noqa: E402

FAILS = []
TODAY = datetime.date(2026, 8, 18)


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


PAGE = ("<html><body><h1>Formal Advisory Opinions</h1>"
        "<p>The State Bar of Georgia Formal Advisory Opinion Board issues opinions on the "
        "Georgia Rules of Professional Conduct.</p>"
        "<p>Formal Advisory Opinion No. 24-1 concerns third-party vendors.</p>"
        "<script>x()</script></body></html>")
# NOT an empty page: strip_html would reduce that to "" and it would take the unreachable branch
# instead. This is the shape the guard actually exists for -- a real page with real text that is
# not the advisory-opinion content (a redesign, a moved section, a login wall).
SHELL = ("<html><body><h1>Member Login</h1><p>Please sign in to continue to your member "
         "dashboard. Membership dues, CLE transcripts and section rosters are available after "
         "you authenticate.</p></body></html>")

OP_241 = {"number": "Formal Advisory Opinion No. 24-1", "status": "pending",
          "subject": "vendors that obtain medical records",
          "summary": "Rule 5.3 applies to a vendor hired to obtain records.",
          "rules": ["5.3"], "impact": "Supervise the vendor or do not use it."}


def ai_ok(body, label=None):
    return {"opinions": [OP_241]}


def ai_boom(body, label=None):
    raise RuntimeError("model should not have been called")


def main():
    print("ethics watch:")

    # ---- identity ---------------------------------------------------------------------------
    print("\nidentity is the canonical number:")
    for raw in ("Formal Advisory Opinion No. 24-1", "FAO 24-1", "Advisory Opinion 24-1",
                "No. 24-1", "24-1", "  24 - 1  ", "Formal Advisory Opinion 24‑1"):
        check("%r -> '24-1'" % raw, E.canonical_number(raw) == "24-1",
              "got %r" % E.canonical_number(raw))
    check("every spelling shares one id",
          len({E.card_id(r) for r in ("FAO 24-1", "Formal Advisory Opinion No. 24-1", "24-1")}) == 1)
    check("a different opinion keeps a different id", E.card_id("24-2") != E.card_id("24-1"))
    check("a different year keeps a different id", E.card_id("05-1") != E.card_id("24-1"))
    check("text with no number yields no id", E.card_id("Formal Advisory Opinion") == "")
    check("and a card cannot be built without one",
          E.build_card({"number": "no number here"}, "u", today=TODAY) is None)

    # ---- the funnel -------------------------------------------------------------------------
    print("\nrun:")
    cards, notes, upd = E.run(fetch=lambda url: PAGE, ai=ai_ok, today=TODAY,
                              sources=[("FAO", "u")])
    check("a changed page cards the opinion it names", len(cards) == 1 and cards[0]["id"] == "24-1")
    check("the card carries the parsed number", cards[0]["number"] == "24-1")
    check("status is kept when recognised", cards[0]["status"] == "pending")
    check("rules are carried", cards[0]["rules"] == ["5.3"])
    check("the page is hashed seen after a successful extraction", upd["pages"].get("u"))

    # an unrecognised status degrades rather than being trusted
    odd = E.build_card(dict(OP_241, status="ratified-ish"), "u", today=TODAY)
    check("an unrecognised status falls back to 'proposed'", odd["status"] == "proposed")

    # an unchanged page must skip the model entirely -- that is what makes a run cheap
    real_seen = E._load_seen
    E._load_seen = lambda: {"pages": {"u": E.courtrules.page_hash(E.courtrules.strip_html(PAGE))},
                            "cards": {}}
    try:
        ucards, unotes, uupd = E.run(fetch=lambda url: PAGE, ai=ai_boom, today=TODAY,
                                     sources=[("FAO", "u")])
    finally:
        E._load_seen = real_seen
    check("an unchanged page cards nothing and never calls the model", ucards == [])
    check("and says it is unchanged", any("unchanged" in n for n in unotes))
    check("while keeping the page hashed", uupd["pages"].get("u"))

    # An ALREADY-SEEN opinion must still be carded. This is the one place the ethics watch
    # deliberately departs from courtrules.run(), which skips `cid in seen_cards` -- there a rule's
    # status barely moves, here the status IS the news. Skipping would mean an opinion that goes
    # pending -> approved never updates, and the page keeps showing the stale status forever.
    real_seen2 = E._load_seen
    E._load_seen = lambda: {"pages": {}, "cards": {"24-1": "2026-07-01"}}
    try:
        kcards, _kn, kupd = E.run(fetch=lambda url: PAGE, ai=ai_ok, today=TODAY,
                                  sources=[("FAO", "u")])
    finally:
        E._load_seen = real_seen2
    check("a known opinion is STILL carded, so a status change can land",
          len(kcards) == 1 and kcards[0]["id"] == "24-1", "got %d card(s)" % len(kcards))
    check("and keeps its original first_seen, not today's",
          kcards and kcards[0]["first_seen"] == "2026-07-01",
          "got %r" % (kcards[0]["first_seen"] if kcards else None))
    check("the state carries the original first_seen forward too",
          kupd["cards"].get("24-1") == "2026-07-01")

    # ---- the shell guard --------------------------------------------------------------------
    print("\nshell guard:")
    scards, snotes, supd = E.run(fetch=lambda url: SHELL, ai=ai_boom, today=TODAY,
                                 sources=[("FAO", "u")])
    check("a contentless shell cards nothing", scards == [])
    check("and is NOT hashed seen (or the shell hash sticks forever)", supd["pages"] == {})
    check("and says so", any("markers" in n for n in snotes))
    check("the guard never called the model (ai_boom would have raised)", True)

    # ---- failure modes ----------------------------------------------------------------------
    print("\nfails open:")
    fcards, fnotes, fupd = E.run(fetch=lambda url: "", ai=ai_boom, today=TODAY,
                                 sources=[("FAO", "u")])
    check("an unreachable page cards nothing and is not hashed",
          fcards == [] and fupd["pages"] == {})
    check("and says it will retry", any("retry" in n for n in fnotes))

    def ai_err(body, label=None):
        raise RuntimeError("boom")
    ecards, enotes, eupd = E.run(fetch=lambda url: PAGE, ai=ai_err, today=TODAY,
                                 sources=[("FAO", "u")])
    check("an extraction error cards nothing and leaves the page un-hashed",
          ecards == [] and eupd["pages"] == {})

    # ---- merge: the status is the news ------------------------------------------------------
    print("\nmerge:")
    first = E.build_card(OP_241, "u", today=TODAY)
    merged, added, updated, changes = E.merge_cards([], [first])
    check("a new opinion is added", added == 1 and updated == 0 and len(merged) == 1)

    same = E.build_card(OP_241, "u", today=datetime.date(2026, 9, 1))
    merged2, added2, updated2, changes2 = E.merge_cards(merged, [same])
    check("re-reading an UNCHANGED opinion is not an update",
          added2 == 0 and updated2 == 0 and changes2 == [], "changes=%r" % (changes2,))
    check("and does not disturb first_seen", merged2[0]["first_seen"] == TODAY.isoformat())

    approved = E.build_card(dict(OP_241, status="approved"), "u", today=datetime.date(2026, 9, 1))
    merged3, added3, updated3, changes3 = E.merge_cards(merged2, [approved])
    check("a status change IS an update", added3 == 0 and updated3 == 1)
    check("the change is reported for the PR body",
          changes3 and changes3[0][:2] == ("24-1", "status")
          and changes3[0][2:] == ("pending", "approved"), "got %r" % (changes3,))
    check("first_seen still survives the update", merged3[0]["first_seen"] == TODAY.isoformat())
    check("the opinion is not duplicated", len(merged3) == 1)

    # a moved URL is not news
    moved = E.build_card(dict(OP_241, status="approved"), "https://gabar.org/moved",
                         today=datetime.date(2026, 9, 2))
    _m4, added4, updated4, _c4 = E.merge_cards(merged3, [moved])
    check("a moved source URL alone is not an update", added4 == 0 and updated4 == 0)

    # ---- ordering ---------------------------------------------------------------------------
    print("\nordering:")
    cs = [E.build_card({"number": n}, "u", today=TODAY) for n in ("05-13", "24-9", "24-10", "24-1")]
    order = [c["number"] for c in E.merge_cards([], cs)[0]]
    check("newest opinion first, compared numerically", order == ["24-10", "24-9", "24-1", "05-13"],
          "got %r" % order)

    # ---- the PR body ------------------------------------------------------------------------
    print("\npr body:")
    body = E._pr_body(1, 1, [approved], changes3)
    check("names the opinion", "24-1" in body)
    check("shows the status transition", "pending" in body and "approved" in body)
    check("says every card is held", "held for your review" in body)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
