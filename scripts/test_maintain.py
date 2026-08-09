#!/usr/bin/env python3
"""Hermetic unit test for maintain.revalidate.

Covers the wiring that runs both per-card guards on the rotating slice: the fidelity
crosscheck and the completeness check share one opinion-text fetch, each flag is labeled
with the guard that raised it, both can fire on one card, a rate-budget stop defers cleanly,
and disabling both guards skips the slice without fetching anything.

It stubs update.opinion_text_full, update.crosscheck, and update.completeness_check, so it
makes no Anthropic and no CourtListener calls. Run directly: `python scripts/test_maintain.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maintain      # noqa: E402  (sys.path shim must run first)
import update        # noqa: E402
import cl_rate       # noqa: E402
import batch         # noqa: E402

MATCH = {"verdict": "match", "reason": ""}
COMPLETE = {"verdict": "complete", "reason": ""}


def cc_flag(reason):
    return {"verdict": "flag", "reason": reason}


def cp_flag(reason):
    return {"verdict": "flag", "reason": reason}


def card(cid, name):
    return {"cluster_id": cid, "name": name, "court": "ctapp", "date": "2026-03-01",
            "dockets": ["A26A%04d" % cid], "synopsis": "s", "why": "w", "areas": ["auto"]}


CARDS = [card(1, "Alpha v. X"), card(2, "Beta v. Y")]


class TextStub:
    """Stands in for update.opinion_text_full. Returns fixed text, or raises the real
    rate-budget exception on the first call to exercise the deferral path."""

    def __init__(self, text="OPINION TEXT", raise_budget=False):
        self.text = text
        self.raise_budget = raise_budget
        self.calls = 0

    def __call__(self, req, deadline=None):
        self.calls += 1
        if self.raise_budget:
            raise cl_rate.RateBudgetExceeded("rolling window full")
        return self.text


def _verdicts(by_name, default):
    def f(name, text, entry):
        return by_name.get(name, default)
    return f


def run(label, cc_map, cp_map, *, text_stub=None, cross_model="x", comp_model="x",
        exp_flags=None, exp_checked=None, exp_deferred=None, exp_fetches=None):
    text_stub = text_stub or TextStub()
    maintain.BATCH = False
    update.CROSSCHECK_MODEL = cross_model
    update.COMPLETENESS_MODEL = comp_model
    update.opinion_text_full = text_stub
    update.crosscheck = _verdicts(cc_map, MATCH)
    update.completeness_check = _verdicts(cp_map, COMPLETE)
    maintain.SLICE = len(CARDS)
    flags, checked, deferred = maintain.revalidate(CARDS)
    if exp_checked is not None:
        assert checked == exp_checked, "%s: checked %d != %d" % (label, checked, exp_checked)
    if exp_deferred is not None:
        assert deferred == exp_deferred, "%s: deferred %d != %d" % (label, deferred, exp_deferred)
    if exp_fetches is not None:
        assert text_stub.calls == exp_fetches, "%s: %d text fetches != %d" % (label, text_stub.calls, exp_fetches)
    if exp_flags is not None:
        assert sorted(flags) == sorted(exp_flags), "%s: flags %r != %r" % (label, flags, exp_flags)
    print("  ok  %-26s flags=%d checked=%d deferred=%d fetches=%d"
          % (label, len(flags), checked, deferred, text_stub.calls))


class BatchStub:
    """Stands in for batch.run: records the request count, then returns canned results
    keyed by the caller's custom_id (the sync guards never run in this path). Can instead
    raise BatchTimeout / BatchError to exercise the deferral branches."""

    def __init__(self, results, raise_timeout=False, raise_error=False):
        self.results = results       # custom_id -> verdict dict, or "UNAVAIL"
        self.raise_timeout = raise_timeout
        self.raise_error = raise_error
        self.n_requests = None

    def __call__(self, reqs, deadline=None, interval=20.0, label="batch"):
        self.n_requests = len(reqs)
        if self.raise_timeout:
            raise batch.BatchTimeout("bid_test", "not finished within budget")
        if self.raise_error:
            raise batch.BatchError("boom")
        out = {}
        for cid, v in self.results.items():
            if v == "UNAVAIL":
                out[cid] = {"ok": False, "type": "errored", "error": {"type": "invalid_request"}}
            else:
                out[cid] = {"ok": True, "text": json.dumps(v), "usage": {}, "stop_reason": "end_turn"}
        return out


def run_batch(label, results, *, raise_timeout=False, raise_error=False, text_stub=None,
              exp_flags=None, exp_checked=None, exp_deferred=None, exp_fetches=None, exp_requests=None):
    text_stub = text_stub or TextStub()
    bstub = BatchStub(results, raise_timeout=raise_timeout, raise_error=raise_error)
    maintain.BATCH = True
    update.CROSSCHECK_MODEL = "x"
    update.COMPLETENESS_MODEL = "x"
    update.opinion_text_full = text_stub
    batch.run = bstub
    maintain.SLICE = len(CARDS)
    try:
        flags, checked, deferred = maintain.revalidate(CARDS)
    finally:
        maintain.BATCH = False
    if exp_checked is not None:
        assert checked == exp_checked, "%s: checked %d != %d" % (label, checked, exp_checked)
    if exp_deferred is not None:
        assert deferred == exp_deferred, "%s: deferred %d != %d" % (label, deferred, exp_deferred)
    if exp_fetches is not None:
        assert text_stub.calls == exp_fetches, "%s: %d fetches != %d" % (label, text_stub.calls, exp_fetches)
    if exp_requests is not None:
        assert bstub.n_requests == exp_requests, "%s: %r requests != %d" % (label, bstub.n_requests, exp_requests)
    if exp_flags is not None:
        assert sorted(flags) == sorted(exp_flags), "%s: flags %r != %r" % (label, flags, exp_flags)
    print("  ok  %-30s flags=%d checked=%d deferred=%d reqs=%s"
          % (label, len(flags), checked, deferred, bstub.n_requests))


def test_finding_marker_matches_the_workflow():
    """maintain.py prints FINDING_MARKER when it FOUND something; an uncaught exception exits
    nonzero WITHOUT it. maintain.yml greps for that exact string to decide whether the issue
    gets the sticky maintenance-finding label -- and that label is the only thing stopping the
    self-heal from auto-closing a card flag the next day, when re-validation walks a different
    rotating slice and says nothing about the card that was flagged. If the two strings drift,
    every finding silently becomes auto-closeable again, which is the bug this pair prevents."""
    wf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".github", "workflows", "maintain.yml")
    text = open(wf, encoding="utf-8").read()
    marker = maintain.FINDING_MARKER
    assert ('grep -qx "%s"' % marker) in text, \
        "maintain.yml does not grep for maintain.FINDING_MARKER (%r)" % marker
    assert "\n" not in marker and " " not in marker, \
        "FINDING_MARKER must be one bare token for grep -qx to match a whole line: %r" % marker
    assert 'index(\\"maintenance-finding\\") | not' in text, \
        "the self-heal no longer filters out maintenance-finding-labelled issues"
    assert "--label \"$label\"" in text, "the report step no longer applies the label"
    print("  ok   finding marker and label wiring agree between maintain.py and maintain.yml")



def test_the_senior_review_can_never_suppress_a_flag():
    """The property the whole feature rests on: whatever the reviewer does -- disabled, errors,
    calls it a false positive, or quotes something the opinion never said -- the flag that was
    already recorded stays recorded, and `checked` is not disturbed.

    Worth an integration test rather than trusting fable_review's unit tests, because the risk
    here is in the WIRING: the sync caller runs guards inside a try/except that decrements
    `checked` and skips the card, so a review that raised in the wrong place would silently cost
    a card's re-validation while looking like a clean run.
    """
    print("senior review never suppresses a flag")
    import fable_review
    flagged = {"verdict": "flag", "reason": "misstates the holding"}
    baseline_prev = maintain.MAINT_REVIEW
    real = fable_review.review_published
    seen = []            # (card_name, reason, opinion_text) per invocation

    def recording(result):
        """Wrap a canned result so the test also proves the reviewer was REACHED, and with the
        flag's own reason and the opinion text already in hand. Without this the whole feature
        could be deleted and every flag assertion below would still pass."""
        def _r(card, reasons, text, call_json, grounded=None, model=None, out_tokens=8000):
            seen.append((card.get("name"), list(reasons), text))
            if callable(result):
                return result()
            return result
        return _r

    try:
        # Baseline: review off (the default) -- one flag.
        maintain.MAINT_REVIEW = False
        run("review off", {"Alpha v. X": flagged}, {}, exp_flags=[("Alpha v. X", "fidelity: misstates the holding")], exp_checked=2)

        maintain.MAINT_REVIEW = True
        for label, stub in (
            ("review raises", lambda: (_ for _ in ()).throw(RuntimeError("fable down"))),
            ("review unavailable", {"available": False, "verdict": "uncertain",
                                    "confidence": "low", "quote": "", "grounded": False,
                                    "assessment": "no", "suggested_synopsis": "", "suggested_why": ""}),
            ("review says false positive", {"available": True, "verdict": "false_positive",
                                    "confidence": "high", "quote": "q", "grounded": True,
                                    "assessment": "mistaken", "suggested_synopsis": "", "suggested_why": ""}),
            ("review drafts a fix", {"available": True, "verdict": "genuine",
                                    "confidence": "high", "quote": "q", "grounded": True,
                                    "assessment": "real", "suggested_synopsis": "fixed",
                                    "suggested_why": "fixed"}),
        ):
            fable_review.review_published = recording(stub)
            del seen[:]
            run(label, {"Alpha v. X": flagged}, {},
                exp_flags=[("Alpha v. X", "fidelity: misstates the holding")], exp_checked=2)
            assert len(seen) == 1, "%s: reviewer reached %d time(s), expected 1" % (label, len(seen))
            nm, reasons, text = seen[0]
            assert nm == "Alpha v. X", "%s: reviewed the wrong card (%r)" % (label, nm)
            assert reasons == ["fidelity: misstates the holding"], "%s: reason not passed (%r)" % (label, reasons)
            assert text == "OPINION TEXT", "%s: opinion text not passed (%r)" % (label, text)

        # The BATCH path is a second, independent call site -- it raises its flags in a later
        # loop where neither the card nor its text is otherwise in scope, so it is exactly the
        # one that would be missed.
        fable_review.review_published = recording({"available": True, "verdict": "genuine",
                                    "confidence": "high", "quote": "q", "grounded": True,
                                    "assessment": "real", "suggested_synopsis": "fixed",
                                    "suggested_why": "fixed"})
        del seen[:]
        # A GROUNDED flag, since the batch path runs verdicts through update.guard_verdict and
        # an unquoted one is dismissed as an invented premise before any review could happen.
        run_batch("batch path reviews too",
                  {"1-fidelity": {"verdict": "flag", "reason": "misread", "quote": "Why it matters"},
                   "1-completeness": {"verdict": "complete"},
                   "2-fidelity": {"verdict": "match"}, "2-completeness": {"verdict": "complete"}},
                  exp_flags=[("Alpha v. X",
                              'fidelity: misread (drafted text at issue: "Why it matters")')],
                  exp_checked=2)
        assert len(seen) == 1, "batch: reviewer reached %d time(s), expected 1" % len(seen)
        assert seen[0][0] == "Alpha v. X" and seen[0][2] == "OPINION TEXT", \
            "batch: card/text not carried to the reviewer (%r)" % (seen[0],)
        assert seen[0][1] == ['fidelity: misread (drafted text at issue: "Why it matters")'], \
            "batch: the flag's own reason was not passed to the reviewer (%r)" % (seen[0][1],)

        # And with the feature off, it is never reached at all.
        maintain.MAINT_REVIEW = False
        del seen[:]
        run("review off, not reached", {"Alpha v. X": flagged}, {},
            exp_flags=[("Alpha v. X", "fidelity: misstates the holding")], exp_checked=2)
        assert not seen, "reviewer was called while disabled"
    finally:
        fable_review.review_published = real
        maintain.MAINT_REVIEW = baseline_prev


def test_the_review_is_off_by_default():
    """A new model call on a production path must not arrive with a merge."""
    print("senior review default")
    import importlib
    prev = os.environ.pop("OPINIONS_MAINT_REVIEW", None)
    try:
        importlib.reload(maintain)
        assert maintain.MAINT_REVIEW is False, "OPINIONS_MAINT_REVIEW defaults on"
        print("  ok  defaults to off")
        for on in ("1", "on", "true", "YES"):
            os.environ["OPINIONS_MAINT_REVIEW"] = on
            importlib.reload(maintain)
            assert maintain.MAINT_REVIEW is True, "%r did not enable it" % on
        print("  ok  1/on/true/yes enable it (case-insensitive)")
        os.environ["OPINIONS_MAINT_REVIEW"] = "off"
        importlib.reload(maintain)
        assert maintain.MAINT_REVIEW is False
        print("  ok  off disables it")
    finally:
        os.environ.pop("OPINIONS_MAINT_REVIEW", None)
        if prev is not None:
            os.environ["OPINIONS_MAINT_REVIEW"] = prev
        importlib.reload(maintain)


def main():
    print("maintain.revalidate wiring:")
    # Both guards clean: no flags, both cards checked, one fetch per card.
    run("both_clean", {}, {}, exp_flags=[], exp_checked=2, exp_deferred=0, exp_fetches=2)
    # A fidelity flag is labeled and surfaced; completeness clean.
    run("fidelity_flag_labeled", {"Alpha v. X": cc_flag("holding misread")}, {},
        exp_flags=[("Alpha v. X", "fidelity: holding misread")], exp_checked=2)
    # A completeness flag is labeled and surfaced; fidelity clean.
    run("completeness_flag_labeled", {}, {"Beta v. Y": cp_flag("omits the SOL holding")},
        exp_flags=[("Beta v. Y", "completeness: omits the SOL holding")], exp_checked=2)
    # Both guards can fire on one card: two distinctly labeled flags, one fetch.
    run("both_flags_one_card", {"Alpha v. X": cc_flag("misread")}, {"Alpha v. X": cp_flag("omits")},
        exp_flags=[("Alpha v. X", "fidelity: misread"), ("Alpha v. X", "completeness: omits")],
        exp_checked=2)
    # A rate-budget stop on the first fetch defers the slice: nothing checked, one fetch attempted.
    run("defer_on_rate_budget", {"Alpha v. X": cc_flag("x")}, {"Alpha v. X": cp_flag("y")},
        text_stub=TextStub(raise_budget=True), exp_flags=[], exp_checked=0, exp_deferred=1, exp_fetches=1)
    # Both guards disabled: the slice is skipped entirely, with no fetch.
    run("both_guards_disabled", {}, {}, cross_model="", comp_model="",
        exp_flags=[], exp_checked=0, exp_deferred=0, exp_fetches=0)

    print("\nmaintain.revalidate batch path (MAINTAIN_BATCH=1):")
    # All guards clean via one batch: no flags, both cards checked, 4 requests (2 cards x 2 guards).
    run_batch("batch_both_clean",
              {"1-fidelity": {"verdict": "match"}, "1-completeness": {"verdict": "complete"},
               "2-fidelity": {"verdict": "match"}, "2-completeness": {"verdict": "complete"}},
              exp_flags=[], exp_checked=2, exp_deferred=0, exp_fetches=2, exp_requests=4)
    # Grounded flags on both guards, one per card, are surfaced and labeled, with the quote folded in.
    run_batch("batch_grounded_flags",
              {"1-fidelity": {"verdict": "flag", "reason": "misread", "quote": "Why it matters"},
               "1-completeness": {"verdict": "complete"},
               "2-fidelity": {"verdict": "match"},
               "2-completeness": {"verdict": "flag", "reason": "omits X", "quote": "OPINION"}},
              exp_flags=[("Alpha v. X", 'fidelity: misread (drafted text at issue: "Why it matters")'),
                         ("Beta v. Y", 'completeness: omits X (opinion text omitted: "OPINION")')],
              exp_checked=2)
    # An ungrounded flag (quote not in the grounding text) is dismissed, not surfaced.
    run_batch("batch_ungrounded_dismissed",
              {"1-fidelity": {"verdict": "flag", "reason": "x", "quote": "not in the summary zzzzz"},
               "1-completeness": {"verdict": "complete"},
               "2-fidelity": {"verdict": "match"}, "2-completeness": {"verdict": "complete"}},
              exp_flags=[], exp_checked=2)
    # A per-request failure (ok=False) yields no flag and does not sink the rest of the batch.
    run_batch("batch_unavailable_line",
              {"1-fidelity": "UNAVAIL", "1-completeness": {"verdict": "complete"},
               "2-fidelity": {"verdict": "match"}, "2-completeness": {"verdict": "complete"}},
              exp_flags=[], exp_checked=2)
    # A batch that does not finish within the budget defers the whole (already-fetched) slice.
    run_batch("batch_timeout_defers", {}, raise_timeout=True,
              exp_flags=[], exp_checked=0, exp_deferred=2, exp_fetches=2, exp_requests=4)

    test_finding_marker_matches_the_workflow()
    test_the_senior_review_can_never_suppress_a_flag()
    test_the_review_is_off_by_default()

    print("\nALL TESTS PASSED (11 cases + marker wiring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
