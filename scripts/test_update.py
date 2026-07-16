#!/usr/bin/env python3
"""Self-tests for scripts/update.py. Standard library only; no network and no API key.

Currently covers crosscheck()'s two false-flag guardrails, added after the 2026-06-28
maintenance run failed on a cross-check flag whose premise the model invented (the
Barnor-Cooper card, cluster 10862301): a "temporary substitute car because of mechanical
issues" rationale the card never stated. The fixture below mirrors that card's shape so the
regression is pinned without depending on the live opinions.json.

It stubs update.anthropic_json, so it makes no Anthropic and no CourtListener calls. CI runs
it in the smoke job; run it directly to check a change to crosscheck:

  python scripts/test_update.py        # prints each case; exits nonzero on any failure
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update  # after sys.path, mirroring the other scripts' import-by-sibling-name pattern


# A synthetic card mirroring the 2026-06-28 Barnor-Cooper false positive. Built here on purpose,
# not loaded from opinions.json, so the regression stays pinned even if that card is edited or pruned.
CARD = {
    "name": "Insurer v. Claimant (fixture mirroring the 2026-06-28 Barnor-Cooper false positive)",
    "areas": ["coverage", "auto"],
    "disposition": "reversed",
    "synopsis": (
        "The Court of Appeals reversed the denial of summary judgment to the insurers in a UM claim, "
        "holding the claimant was not entitled to UM benefits: the borrowed vehicle was not a "
        "\"temporary substitute car\" because the scheduled vehicle it would have replaced had been "
        "sold, and the named-insured LLC is a separate legal entity from its members and managers."
    ),
    "why": (
        "Reads the temporary-substitute and named-insured provisions strictly: a borrowed vehicle is "
        "not a temporary substitute once the scheduled car it would replace has been sold."
    ),
}
OPINION = "FULL OPINION TEXT (stubbed; the test never sends it to a model)."

# A span copied verbatim from the fixture synopsis: a flag quoting this is substantiated.
REAL_QUOTE = "the named-insured LLC is a separate legal entity from its members and managers"
# The invented rationale from the real failure: absent from the summary, so a flag quoting it is dismissed.
FAB_QUOTE = "the borrowed vehicle was a temporary substitute car because of mechanical issues with the Dodge Caravan"


class Stub:
    """Scripted stand-in for update.anthropic_json. Each call returns the next item; an Exception
    item is raised; a call past the end fails the test, which catches an unexpected re-ask."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self, body, label=None):
        i = self.calls
        self.calls += 1
        if i >= len(self.seq):
            raise AssertionError("crosscheck over-called: attempt %d, only %d scripted" % (i + 1, len(self.seq)))
        item = self.seq[i]
        if isinstance(item, Exception):
            raise item
        return item


def match():
    return {"verdict": "match", "quote": "", "reason": "ok"}


def flag(quote, reason="misstates the holding"):
    return {"verdict": "flag", "quote": quote, "reason": reason}


# (label, scripted responses, OPINIONS_CROSSCHECK_TRIES, expected verdict, expected calls,
#  expected flag_count, substring the reason must contain). None means "do not assert".
CASES = [
    # Clean card: every roll matches. Verdict match; early-exit once a majority has cleared.
    ("clean_match", [match()] * 5, 3, "match", 2, 0, None),
    # The 2026-06-28 regression: a flag whose quoted premise is not in the summary is dismissed.
    ("fabricated_premise_dismissed", [flag(FAB_QUOTE)] * 3, 3, "match", 2, 0, None),
    # A genuine, substantiated flag confirmed by consensus: verdict flag, quote folded into the reason.
    ("substantiated_unanimous", [flag(REAL_QUOTE)] * 3, 3, "flag", 2, 2, REAL_QUOTE),
    # A substantiated flag on only a minority of rolls is noise: cleared.
    ("substantiated_minority", [flag(REAL_QUOTE), match(), match()], 3, "match", 3, 1, None),
    # A substantiated flag on a majority of rolls stands.
    ("substantiated_majority", [flag(REAL_QUOTE), match(), flag(REAL_QUOTE)], 3, "flag", 3, 2, None),
    # Every attempt errors: fail-open to unavailable so the card still surfaces for a manual look.
    ("all_errors_unavailable", [RuntimeError("boom")] * 3, 3, "unavailable", 3, None, None),
    # Consensus off (tries=1) but grounding on: a substantiated flag stands.
    ("tries1_substantiated_flag", [flag(REAL_QUOTE)], 1, "flag", 1, 1, REAL_QUOTE),
    # Consensus off, fabricated quote: still dismissed by grounding.
    ("tries1_fabricated_dismissed", [flag(FAB_QUOTE)], 1, "match", 1, 0, None),
    # A too-short quote does not substantiate a flag.
    ("short_quote_dismissed", [flag("is")] * 3, 3, "match", 2, 0, None),
    # An error then two substantiated flags: the two attempts actually made carry the majority.
    ("error_then_two_flags", [RuntimeError("x"), flag(REAL_QUOTE), flag(REAL_QUOTE)], 3, "flag", 3, 2, None),
]


def run_case(label, seq, tries, verdict, calls, flag_count, reason_has):
    update.anthropic_json = Stub(seq)
    update.CROSSCHECK_TRIES = tries
    r = update.crosscheck(CARD["name"], OPINION, CARD)
    made = update.anthropic_json.calls
    assert isinstance(r, dict), "%s: result is not a dict" % label
    assert r["verdict"] in ("match", "flag", "unavailable"), "%s: bad verdict %r" % (label, r["verdict"])
    assert isinstance(r.get("reason", ""), str), "%s: reason is not a string" % label
    assert r["verdict"] == verdict, "%s: verdict %r != %r (%r)" % (label, r["verdict"], verdict, r)
    if calls is not None:
        assert made == calls, "%s: made %d calls, expected %d" % (label, made, calls)
    if flag_count is not None:
        assert r.get("flag_count") == flag_count, "%s: flag_count %r != %r" % (label, r.get("flag_count"), flag_count)
    if reason_has is not None:
        assert reason_has in r.get("reason", ""), "%s: reason lacks %r (%r)" % (label, reason_has, r.get("reason"))
    print("  ok  %-32s verdict=%-11s calls=%s flag_count=%s" % (label, r["verdict"], made, r.get("flag_count")))


def test_substantiation_helper():
    drafted = "Holding 1 (areas: coverage, auto)\nSynopsis: %s\nWhy it matters: %s" % (CARD["synopsis"], CARD["why"])
    assert update._quote_substantiated(REAL_QUOTE, drafted), "verbatim span should substantiate"
    assert update._quote_substantiated(
        '  "The Named-Insured LLC   is a SEPARATE legal entity from its members and managers"  ', drafted
    ), "wrapping quotes, case, and extra whitespace should still substantiate"
    assert not update._quote_substantiated(FAB_QUOTE, drafted), "an invented premise should not substantiate"
    assert not update._quote_substantiated("", drafted), "empty quote should not substantiate"
    assert not update._quote_substantiated("   ", drafted), "whitespace quote should not substantiate"
    print("  ok  substantiation helper (presence, normalization, empties)")


# --- completeness_check: the same guardrails, with the grounding source flipped to the opinion ---
# The opinion fixture states a separate material holding verbatim; a completeness flag must quote it.
COMP_OPINION = (
    "The trial court granted summary judgment to the defendant and we affirm. The record does not "
    "show the premises were in a hazardous condition. The court further held that the plaintiff's "
    "claim was independently barred because the statute of limitations had run before suit was filed."
)
COMP_REAL = "the plaintiff's claim was independently barred because the statute of limitations had run before suit was filed"
COMP_FAB = "the court awarded treble damages and attorney fees under the civil RICO count"


def complete():
    return {"verdict": "complete", "quote": "", "reason": "ok"}


def cflag(quote, reason="omits a material holding"):
    return {"verdict": "flag", "quote": quote, "reason": reason}


CASES_COMP = [
    # Clean card: every roll says complete. Verdict complete; early-exit once a majority has cleared.
    ("comp_clean_complete", [complete()] * 5, 3, "complete", 2, 0, None),
    # An invented omission whose quoted holding is not in the opinion is dismissed.
    ("comp_fabricated_omission_dismissed", [cflag(COMP_FAB)] * 3, 3, "complete", 2, 0, None),
    # A real omission quoting the opinion, confirmed by consensus: verdict flag, quote folded into reason.
    ("comp_substantiated_unanimous", [cflag(COMP_REAL)] * 3, 3, "flag", 2, 2, COMP_REAL),
    # A substantiated flag on only a minority of rolls is noise: cleared.
    ("comp_substantiated_minority", [cflag(COMP_REAL), complete(), complete()], 3, "complete", 3, 1, None),
    # A substantiated flag on a majority of rolls stands.
    ("comp_substantiated_majority", [cflag(COMP_REAL), complete(), cflag(COMP_REAL)], 3, "flag", 3, 2, None),
    # Every attempt errors: fail-open to unavailable so the card still surfaces.
    ("comp_all_errors_unavailable", [RuntimeError("boom")] * 3, 3, "unavailable", 3, None, None),
    # Consensus off (tries=1) but grounding on: a substantiated flag stands.
    ("comp_tries1_substantiated_flag", [cflag(COMP_REAL)], 1, "flag", 1, 1, COMP_REAL),
    # Consensus off, invented omission: still dismissed by grounding.
    ("comp_tries1_fabricated_dismissed", [cflag(COMP_FAB)], 1, "complete", 1, 0, None),
]


def run_comp_case(label, seq, tries, verdict, calls, flag_count, reason_has):
    update.anthropic_json = Stub(seq)
    update.COMPLETENESS_TRIES = tries
    r = update.completeness_check(CARD["name"], COMP_OPINION, CARD)
    made = update.anthropic_json.calls
    assert isinstance(r, dict), "%s: result is not a dict" % label
    assert r["verdict"] in ("complete", "flag", "unavailable"), "%s: bad verdict %r" % (label, r["verdict"])
    assert isinstance(r.get("reason", ""), str), "%s: reason is not a string" % label
    assert r["verdict"] == verdict, "%s: verdict %r != %r (%r)" % (label, r["verdict"], verdict, r)
    if calls is not None:
        assert made == calls, "%s: made %d calls, expected %d" % (label, made, calls)
    if flag_count is not None:
        assert r.get("flag_count") == flag_count, "%s: flag_count %r != %r" % (label, r.get("flag_count"), flag_count)
    if reason_has is not None:
        assert reason_has in r.get("reason", ""), "%s: reason lacks %r (%r)" % (label, reason_has, r.get("reason"))
    print("  ok  %-32s verdict=%-11s calls=%s flag_count=%s" % (label, r["verdict"], made, r.get("flag_count")))


# --- docket-aware duplicate guard (pure functions; no model or network calls) ---
CASES_DEDUP = [
    # (label, courtA, dateA, docketsA, nameA, courtB, dateB, docketsB, nameB, same_case?)
    # A corrected opinion republished under a new cluster: same court, same docket, later date.
    ("revision_shared_docket_diff_date", "ctapp", "2026-03-01", ["A26A0526"], "Smith v. Jones",
     "ctapp", "2026-04-15", "A26A0526", "Smith v. Jones", True),
    # A split-docket twin of one consolidated appeal: same court and day, dockets differ, parties match.
    ("split_docket_twin_same_day", "ctapp", "2026-03-01", ["A26A0526"], "Barnor-Cooper v. Acme Roofing",
     "ctapp", "2026-03-01", "A26A0550", "Acme Roofing v. Barnor-Cooper", True),
    # A genuine repeat appearance at a higher court is NOT a duplicate: the court differs.
    ("higher_court_repeat_diff_court", "ctapp", "2026-03-01", ["A26A0526"], "Smith v. Jones",
     "scotga", "2026-06-01", "S26G0010", "Smith v. Jones", False),
    # Same court and day but only one shared distinctive token: not enough to merge.
    ("one_shared_token_same_day", "ctapp", "2026-03-01", ["X100"], "Washington v. Lincoln Apartments",
     "ctapp", "2026-03-01", ["X200"], "Washington v. Jefferson Holdings", False),
    # Same parties, no docket signal, different day: too little to merge, kept separate.
    ("same_parties_diff_day_no_docket", "ctapp", "2026-03-01", [], "Barnor-Cooper v. Acme Roofing",
     "ctapp", "2026-05-01", [], "Barnor-Cooper v. Acme Roofing", False),
    # Unrelated cases decided the same day: no shared docket or parties.
    ("unrelated_same_day", "ctapp", "2026-03-01", ["A1234"], "Alpha v. Beta",
     "ctapp", "2026-03-01", ["C5678"], "Gamma v. Delta", False),
]


def run_dedup_case(label, ca, da, ka, na, cb, db, kb, nb, expected):
    a = update._dup_sig(ca, da, ka, na)
    b = update._dup_sig(cb, db, kb, nb)
    got = update._same_case(a, b)
    assert got == expected, "%s: _same_case=%r expected %r" % (label, got, expected)
    assert update._same_case(b, a) == expected, "%s: relation is not symmetric" % label
    print("  ok  %-34s same_case=%s" % (label, got))


def test_docket_set():
    assert update._docket_set("A26A0526, A26A0550") == {"A26A0526", "A26A0550"}, "comma split"
    assert update._docket_set(["No. 21-1234"]) == {"21-1234"}, "list and No. prefix dropped"
    assert update._docket_set("") == set(), "empty string"
    assert update._docket_set(["A1", "and", "S24G0123"]) == {"S24G0123"}, "short token and noise dropped"
    print("  ok  docket-set normalization (split, noise, empties)")


def test_draft_pending():
    """The tier-3 summarize-batch orchestration (OPINIONS_BATCH, update._draft_pending): result
    mapping by custom_id, the ok / errored-line / unparseable-body / whole-batch-defer branches, and
    the drafted (evaluated) set it returns. Stubs batch.run, so it exercises the real summarize_request
    + batch.from_body request building with no network."""
    print("tier-3 summarize batch (_draft_pending):")
    pend = [{"cid": c, "r": {}, "name": nm, "court_id": "ga", "docket": "A%d" % c,
             "date_filed": "2026-07-0%d" % i, "text": "t%d" % c, "note": "", "cl_status": "published"}
            for i, (c, nm) in enumerate([(111, "A v. B"), (222, "C v. D"), (333, "E v. F")], 1)]
    real_run = update.batch.run
    finished = []

    def finish_fn(v, p):
        finished.append((p["cid"], v))

    def mixed_run(reqs, deadline=None, interval=20.0, label="batch"):
        assert sorted(rq["custom_id"] for rq in reqs) == ["111", "222", "333"], [rq["custom_id"] for rq in reqs]
        return {"111": {"ok": True, "text": '{"relevant": true, "significance": "high"}', "stop_reason": "end_turn"},
                "222": {"ok": False, "type": "errored", "error": "x"},
                "333": {"ok": True, "text": "not json {{{", "stop_reason": "end_turn"}}
    update.batch.run = mixed_run
    try:
        drafted = update._draft_pending(pend, deadline=123.0, finish_fn=finish_fn)
    finally:
        update.batch.run = real_run
    assert drafted == {111}, drafted
    assert finished == [(111, {"relevant": True, "significance": "high"})], finished
    print("  ok  ok line drafts+finishes; errored and unparseable lines skip (retry next run)")

    for name, exc in (("timeout", update.batch.BatchTimeout("bid", "still running")),
                      ("transport error", update.batch.BatchError("submit failed"))):
        finished.clear()

        def raiser(reqs, deadline=None, interval=20.0, label="batch", _e=exc):
            raise _e
        update.batch.run = raiser
        try:
            drafted = update._draft_pending(pend, deadline=123.0, finish_fn=finish_fn)
        finally:
            update.batch.run = real_run
        assert drafted == set() and finished == [], (name, drafted, finished)
        print("  ok  batch %s defers the whole draft set (nothing evaluated)" % name)


def test_funnel_pr_body():
    """The run's PR/dry-run markdown assembly (update._funnel_pr_body), extracted verbatim from
    main(). Pure: given this run's accumulated results it must fold in each section -- the added card
    with its cross-check flag, the treatment flag on an existing card, and the dropped tail -- and, on
    an empty run, say so."""
    print("funnel PR body:")
    card = {"cluster_id": 4321, "name": "Alpha v. Beta", "court": "ctapp", "date": "2026-06-01",
            "dockets": ["A26A0001"], "areas": ["auto"], "disposition": "affirmed",
            "synopsis": "Held a thing.", "why": "It matters.", "url": "https://cl/4321/",
            "precedential": "published", "additional_holdings": []}
    body = update._funnel_pr_body(
        added=[card], flagged=[("Alpha v. Beta", ["low confidence"])],
        crosschecks={4321: {"verdict": "flag", "reason": "the synopsis overstates the holding"}},
        completeness={4321: {"verdict": "ok", "reason": ""}},
        treat_flags=[("Old Card v. State", "Alpha v. Beta", "overruled")],
        audit_notes=[], sa_events=[], skipped=[("Noise v. State", "screen: out of scope")])
    for needle in ("## Georgia Appellate Watch: 1 new opinion(s)", "Alpha v. Beta",
                   "cross-check FLAG: the synopsis overstates the holding", "review: low confidence",
                   "Treatment flags this run", "Old Card v. State", "Screened or dropped this run",
                   "Noise v. State"):
        assert needle in body, "missing %r in PR body" % needle
    assert body.endswith("\n"), "trailing newline preserved"
    print("  ok  full run folds in card, checks, treatment flag, and dropped tail")

    empty = update._funnel_pr_body([], [], {}, {}, [], [], [], [])
    assert "No new relevant opinions this run." in empty, empty
    assert empty.startswith("## Georgia Appellate Watch: 0 new opinion(s)"), empty
    print("  ok  empty run says 'no new relevant opinions'")


def main():
    print("crosscheck guardrails:")
    for c in CASES:
        run_case(*c)
    print("completeness guardrails:")
    for c in CASES_COMP:
        run_comp_case(*c)
    print("duplicate guard:")
    for c in CASES_DEDUP:
        run_dedup_case(*c)
    print("helpers:")
    test_substantiation_helper()
    test_docket_set()
    test_draft_pending()
    test_funnel_pr_body()
    print("\nALL TESTS PASSED (%d cases)" % (len(CASES) + len(CASES_COMP) + len(CASES_DEDUP) + 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
