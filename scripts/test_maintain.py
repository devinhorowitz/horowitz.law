#!/usr/bin/env python3
"""Hermetic unit test for maintain.revalidate.

Covers the wiring that runs both per-card guards on the rotating slice: the fidelity
crosscheck and the completeness check share one opinion-text fetch, each flag is labeled
with the guard that raised it, both can fire on one card, a rate-budget stop defers cleanly,
and disabling both guards skips the slice without fetching anything.

It stubs update.opinion_text_full, update.crosscheck, and update.completeness_check, so it
makes no Anthropic and no CourtListener calls. Run directly: `python scripts/test_maintain.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maintain      # noqa: E402  (sys.path shim must run first)
import update        # noqa: E402
import cl_rate       # noqa: E402

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
    print("\nALL TESTS PASSED (6 cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
