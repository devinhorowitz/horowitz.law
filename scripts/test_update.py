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


def main():
    print("crosscheck guardrails:")
    for c in CASES:
        run_case(*c)
    print("completeness guardrails:")
    for c in CASES_COMP:
        run_comp_case(*c)
    print("helpers:")
    test_substantiation_helper()
    print("\nALL TESTS PASSED (%d cases)" % (len(CASES) + len(CASES_COMP) + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
