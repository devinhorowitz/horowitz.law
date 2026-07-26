#!/usr/bin/env python3
"""Hermetic unit tests for golden_check -- the gate that decides a model change is safe (no network).

Three groups:

  fail-closed exit codes  golden_check.check()/summarize_check()/recall() gate whether a model
      change is safe (model-watch.yml treats exit 0 as "safe to bump"). If every golden case is
      uncached -- or the set is empty -- the gate verified NOTHING and must exit nonzero, not
      silently pass. Uncached cases skip the model calls (`if not c.get("text"): continue`), so
      these drive the fail-closed paths without any Anthropic/CourtListener calls.

  verdict comparison      the gate's actual decision: which tier outcomes count as a regression.
      The tiers are stubbed, so this pins the comparison itself -- a dropped keeper is red in
      EITHER direction, including the low-significance drop and the too-permissive control keep.
      Without this, only the fail-closed paths were covered and the red/green logic was not.

  set integrity           the committed scripts/golden_set.json is data that four model tiers are
      judged against, and nothing validated it. A misspelled practice area in expect_areas would
      make summarize mode permanently red for a reason no output explains.

Run directly: `python scripts/test_golden_check.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden_check    # noqa: E402
import update          # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def run_with_cases(cases, pretriage_model="claude-haiku-4-5"):
    """Point golden_check._load at a synthetic set and enable pretriage, restoring after."""
    orig_load, orig_pt = golden_check._load, update.PRETRIAGE_MODEL
    golden_check._load = lambda: cases
    update.PRETRIAGE_MODEL = pretriage_model
    try:
        return golden_check.check(), golden_check.summarize_check(), golden_check.recall()
    finally:
        golden_check._load = orig_load
        update.PRETRIAGE_MODEL = orig_pt


class Tiers:
    """Replace the real model tiers with deterministic stubs for one test.

    Each tier is given as a plain value or a callable of the case name, so a test can make the
    funnel behave one way for a keeper and another for a control within the same run.
    """
    def __init__(self, screen=True, pretriage=True, relevant=True, significance="medium",
                 summarize=None, pretriage_model="claude-haiku-4-5"):
        self.spec = dict(screen=screen, pretriage=pretriage, relevant=relevant,
                         significance=significance, summarize=summarize)
        self.pretriage_model = pretriage_model
        self.summarize_calls = []

    def _v(self, key, name):
        v = self.spec[key]
        return v(name) if callable(v) else v

    def __enter__(self):
        self.saved = {k: getattr(update, k) for k in
                      ("screen", "pretriage", "triage", "summarize", "PRETRIAGE_MODEL")}
        update.screen = lambda name, docket, snip: {"pass": self._v("screen", name), "reason": "stub"}
        update.pretriage = lambda name, docket, text: {"pass": self._v("pretriage", name), "reason": "stub"}
        update.triage = lambda name, docket, text: {"relevant": self._v("relevant", name),
                                                    "significance": self._v("significance", name),
                                                    "reason": "stub"}

        def _sum(*a, **kw):
            name = a[1] if len(a) > 1 else ""
            self.summarize_calls.append(name)
            v = self._v("summarize", name)
            if isinstance(v, Exception):
                raise v
            return v if v is not None else {"areas": [], "additional_holdings": []}
        update.summarize = _sum
        update.PRETRIAGE_MODEL = self.pretriage_model
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(update, k, v)
        return False


def with_set(cases, fn):
    """Run one golden_check mode against a synthetic set."""
    orig = golden_check._load
    golden_check._load = lambda: cases
    try:
        return fn()
    finally:
        golden_check._load = orig


def case(name, keep=True, areas=None, text="an opinion."):
    c = {"cluster_id": abs(hash(name)) % 10**7, "name": name, "docket": "A00001",
         "expect_relevant": keep, "text": text, "note": "synthetic"}
    if areas:
        c["expect_areas"] = list(areas)
    return c


# --- 1. fail-closed exit codes -------------------------------------------
def test_fail_closed():
    # Every case uncached (no "text") -> nothing verified -> all three must fail closed (exit 1).
    uncached = [{"name": "Alpha v. X", "expect_relevant": True},
                {"name": "Bravo v. Y", "expect_relevant": True}]
    c, s, r = run_with_cases(uncached)
    check("check() fails closed when every case is uncached", c == 1)
    check("summarize_check() fails closed when every case is uncached", s == 1)
    check("recall() fails closed when every keeper is uncached", r == 1)

    # An empty golden set also verifies nothing -> fail closed.
    c, s, r = run_with_cases([])
    check("check() fails closed on an empty set", c == 1)
    check("summarize_check() fails closed on an empty set", s == 1)

    # recall() is legitimately not-applicable (exit 0) when pretriage is disabled.
    _, _, r_disabled = run_with_cases(uncached, pretriage_model="")
    check("recall() is a clean no-op (exit 0) when pretriage is disabled", r_disabled == 0)


# --- 2. verdict comparison ------------------------------------------------
def test_check_verdicts():
    """The red/green decision itself, with the tiers stubbed. A regression in EITHER direction
    -- a keeper the funnel now drops, a control it now keeps -- has to come back nonzero."""
    keeper, control = case("Keep v. Me"), case("Drop v. Me", keep=False)

    with Tiers():                                   # everything passes
        check("a kept keeper is green", with_set([keeper], golden_check.check) == 0)
        check("a kept control is RED (the funnel got too permissive)",
              with_set([control], golden_check.check) == 1)

    with Tiers(screen=False):
        check("a keeper the screen drops is red", with_set([keeper], golden_check.check) == 1)
        check("a control the screen drops is green", with_set([control], golden_check.check) == 0)

    with Tiers(pretriage=False):
        check("a keeper pretriage drops is red", with_set([keeper], golden_check.check) == 1)

    # Pretriage is skipped entirely when disabled, so the same drop must NOT fail the gate.
    with Tiers(pretriage=False, pretriage_model=""):
        check("with pretriage disabled its verdict is not consulted",
              with_set([keeper], golden_check.check) == 0)

    with Tiers(relevant=False):
        check("a keeper triage calls irrelevant is red", with_set([keeper], golden_check.check) == 1)

    # The significance boundary: the funnel drops "low", so a keeper demoted to low is a
    # regression even though triage still called it relevant. This is the exact shape of the
    # 2026-07 gloss regression, where a prompt change quietly demoted a real card.
    with Tiers(significance="low"):
        check("a keeper demoted to low significance is red",
              with_set([keeper], golden_check.check) == 1)
    with Tiers(significance="LOW"):
        check("the low-significance drop is case-insensitive",
              with_set([keeper], golden_check.check) == 1)

    # Mixed set: one regression among passing cases still fails the whole gate.
    with Tiers(relevant=lambda n: n != "Keep v. Me"):
        mixed = [keeper, case("Other v. One"), control]
        check("one regression fails the run even when other cases pass",
              with_set(mixed, golden_check.check) == 1)


def test_summarize_verdicts():
    with Tiers(summarize={"areas": ["premises", "damages"], "additional_holdings": []}):
        check("produced areas covering the expectation is green",
              with_set([case("A v. B", areas=["premises"])], golden_check.summarize_check) == 0)
        check("extra produced areas are fine (coverage, not exact match)",
              with_set([case("A v. B", areas=["damages"])], golden_check.summarize_check) == 0)
        check("a dropped expected area is red",
              with_set([case("A v. B", areas=["premises", "expert"])], golden_check.summarize_check) == 1)

    # An area carried only by an additional holding still counts -- a card that reorganizes
    # coverage across holdings is not a regression.
    with Tiers(summarize={"areas": ["premises"],
                          "additional_holdings": [{"areas": ["expert"]}]}):
        check("an area from an additional holding counts toward coverage",
              with_set([case("A v. B", areas=["premises", "expert"])], golden_check.summarize_check) == 0)

    # _produced_areas directly: the taxonomy filter is what stops a hallucinated area from
    # counting as coverage, and it is invisible through the exit code alone (an expectation
    # outside the taxonomy is separately forbidden by the integrity check below), so assert
    # the returned set rather than the verdict.
    got = golden_check._produced_areas({"areas": ["premises", "not-an-area", "AUTO"],
                                        "additional_holdings": [{"areas": ["expert", "bogus"]},
                                                                "not a dict"]})
    check("_produced_areas keeps only real practice areas", got == {"premises", "expert"}, str(sorted(got)))

    # Controls and keepers without expect_areas are skipped; a run that skipped everything
    # verified nothing and must fail closed rather than report a clean summarizer.
    with Tiers(summarize={"areas": ["premises"], "additional_holdings": []}):
        only_skipped = [case("Ctrl v. X", keep=False, areas=["premises"]), case("NoAreas v. Y")]
        check("a run that skipped every case fails closed",
              with_set(only_skipped, golden_check.summarize_check) == 1)

    # temperature 1 makes a single run noisy, so the union across attempts is what counts:
    # neither attempt alone covers both areas, together they do.
    seq = {"n": 0}

    def alternating(_name):
        seq["n"] += 1
        return {"areas": ["premises"] if seq["n"] == 1 else ["expert"], "additional_holdings": []}

    os.environ["OPINIONS_GOLDEN_RETRIES"] = "1"        # tries = 2
    try:
        with Tiers(summarize=alternating) as t:
            r = with_set([case("A v. B", areas=["premises", "expert"])], golden_check.summarize_check)
            check("areas union across retries, so a noisy single run is not a regression", r == 0)
            check("and it really did take both attempts", len(t.summarize_calls) == 2,
                  str(len(t.summarize_calls)))
        # A persistent miss is still a regression, and it stops at the retry budget.
        seq["n"] = 99
        with Tiers(summarize=alternating) as t:
            r = with_set([case("A v. B", areas=["premises"])], golden_check.summarize_check)
            check("a persistent miss is red after the retry budget", r == 1)
            check("retries are bounded by OPINIONS_GOLDEN_RETRIES", len(t.summarize_calls) == 2,
                  str(len(t.summarize_calls)))
        # A summarizer that raises every time must be reported, never counted as covered.
        with Tiers(summarize=RuntimeError("api down")):
            check("a summarizer that always errors is red, not silently ok",
                  with_set([case("A v. B", areas=["premises"])], golden_check.summarize_check) == 1)
    finally:
        os.environ.pop("OPINIONS_GOLDEN_RETRIES", None)


def test_recall_verdicts():
    keeper, control = case("Keep v. Me"), case("Drop v. Me", keep=False)
    with Tiers(pretriage=True):
        check("pretriage passing every keeper is green",
              with_set([keeper, control], golden_check.recall) == 0)
    with Tiers(pretriage=lambda n: n != "Keep v. Me"):
        check("pretriage dropping a keeper is red (a recall miss)",
              with_set([keeper, control], golden_check.recall) == 1)
    # A control pretriage drops is the savings side, not a failure.
    with Tiers(pretriage=lambda n: n == "Keep v. Me"):
        check("pretriage dropping only controls is green",
              with_set([keeper, control], golden_check.recall) == 0)


# --- 3. the committed set is well-formed ---------------------------------
def test_committed_set_integrity():
    """scripts/golden_set.json is the data four model tiers are judged against. Nothing else
    validates it, and a bad entry degrades quietly: a misspelled area can never be produced, so
    summarize mode goes permanently red with no output that explains why."""
    cases = json.load(open(golden_check.GOLDEN_PATH, encoding="utf-8"))
    check("the golden set parses as a non-empty list", isinstance(cases, list) and len(cases) > 0)

    ids = [c.get("cluster_id") for c in cases]
    check("every entry has an integer cluster_id",
          all(isinstance(i, int) for i in ids), str([i for i in ids if not isinstance(i, int)]))
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    check("cluster_ids are unique (a dupe double-counts one case)", not dupes, str(dupes))

    bad_name = [c.get("cluster_id") for c in cases
                if not (isinstance(c.get("name"), str) and c.get("name").strip())]
    check("every entry has a non-empty name (the tiers are given it)", not bad_name, str(bad_name))
    no_note = [c.get("cluster_id") for c in cases if not (c.get("note") or "").strip()]
    check("every entry explains why it is in the set", not no_note, str(no_note))

    keepers = [c for c in cases if c.get("expect_relevant", True)]
    controls = [c for c in cases if not c.get("expect_relevant", True)]
    check("the set has both keepers and controls (it must detect drift both ways)",
          len(keepers) >= 1 and len(controls) >= 1, "%d/%d" % (len(keepers), len(controls)))

    bad_areas = sorted({a for c in cases for a in (c.get("expect_areas") or [])
                        if a not in update.VALID_AREAS})
    check("every expect_areas value is in the practice-area taxonomy", not bad_areas, str(bad_areas))

    # summarize_check skips controls, so expect_areas on one is dead weight that reads as coverage.
    ctrl_areas = [c.get("cluster_id") for c in controls if c.get("expect_areas")]
    check("no control carries expect_areas (summarize skips controls)", not ctrl_areas, str(ctrl_areas))

    bad_text = [c.get("cluster_id") for c in cases
                if c.get("text") is not None and not isinstance(c.get("text"), str)]
    check("cached text, where present, is a string", not bad_text, str(bad_text))
    too_long = [c.get("cluster_id") for c in cases
                if isinstance(c.get("text"), str) and len(c["text"]) > update.MAXCHARS]
    check("cached text is within the funnel's read limit", not too_long, str(too_long))

    # Not a failure -- entries added since the last `build` legitimately have no text yet -- but
    # the gate fails closed on them, so name them here rather than letting a red run be a mystery.
    uncached = [c.get("cluster_id") for c in cases if not c.get("text")]
    if uncached:
        print("  note  %d entr%s awaiting `golden_check.py build`: %s"
              % (len(uncached), "y" if len(uncached) == 1 else "ies",
                 ", ".join(str(i) for i in uncached)))


def main():
    print("golden_check:")
    test_fail_closed()
    test_check_verdicts()
    test_summarize_verdicts()
    test_recall_verdicts()
    test_committed_set_integrity()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
