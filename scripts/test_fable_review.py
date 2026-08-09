#!/usr/bin/env python3
"""Hermetic unit tests for fable_review (no network, no API key).

Two reviewers, opposite failure directions, and the tests exist to keep them that way.

The Fable senior review clears a held card ONLY on the triple of is_false_positive + high
confidence + accept, on adequate opinion text; everything else is fail-closed (stays held). These
tests stub the `call_json` callable, so no Anthropic call is made -- they pin exactly when a held
case may and may not be auto-published.

review_published() runs AFTER the card is live. It can never suppress a flag -- the caller has
already recorded it -- so its fail-safe is different: a correction is shown ONLY on a genuine
verdict whose quote is actually found in the opinion. An ungrounded quote is the dangerous case,
because a drafted edit to published legal text resting on a passage the opinion does not contain
is worse than no suggestion at all.

Run directly: `python scripts/test_fable_review.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fable_review    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


ENTRY = {"cluster_id": 1, "name": "Alpha v. Beta", "synopsis": "s", "why": "w",
         "disposition": "affirmed", "areas": ["coverage"]}
TEXT = "x" * 2000   # enough alpha to clear the MIN_TEXT_ALPHA floor


def stub(verdict):
    """A call_json that returns a fixed verdict and records that it was called."""
    calls = []
    def _call(body, label="call"):
        calls.append(body)
        return verdict
    _call.calls = calls
    return _call


def test_clears_only_on_the_full_triple():
    hi_fp_accept = {"is_false_positive": True, "confidence": "high", "recommendation": "accept", "assessment": "faithful"}
    v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT, stub(hi_fp_accept))
    check("clears on high-confidence false-positive accept", v["clear"] is True and v["available"] is True)

    for label, verdict in [
        ("medium confidence does not clear", {"is_false_positive": True, "confidence": "medium", "recommendation": "accept", "assessment": "a"}),
        ("not-a-false-positive does not clear", {"is_false_positive": False, "confidence": "high", "recommendation": "accept", "assessment": "a"}),
        ("a veto recommendation does not clear", {"is_false_positive": True, "confidence": "high", "recommendation": "veto", "assessment": "a"}),
        ("a decline recommendation does not clear", {"is_false_positive": True, "confidence": "high", "recommendation": "decline", "assessment": "a"}),
    ]:
        v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT, stub(verdict))
        check(label, v["clear"] is False)


def test_stringy_boolean_is_fail_closed():
    """A model that emits the STRING "false" for is_false_positive must NOT clear -- bool("false") is
    True in Python, so a naive cast would auto-publish a card the model actually flagged. Only a real
    True (or an unambiguous true-string) clears; anything else stays held."""
    stringy_false = {"is_false_positive": "false", "confidence": "high", "recommendation": "accept", "assessment": "a"}
    v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT, stub(stringy_false))
    check("string 'false' does not clear (fail-closed)", v["clear"] is False and v["is_false_positive"] is False)
    for raw in ("False", "no", "0", "", None, 0):
        v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT,
                                     stub({"is_false_positive": raw, "confidence": "high", "recommendation": "accept", "assessment": "a"}))
        check("falsy is_false_positive %r does not clear" % (raw,), v["clear"] is False)
    # A genuine string "true" is still honored (the triple otherwise holds).
    v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT,
                                 stub({"is_false_positive": "true", "confidence": "high", "recommendation": "accept", "assessment": "a"}))
    check("string 'true' clears when the rest of the triple holds", v["clear"] is True)


def test_fail_closed():
    # Thin/empty opinion text -> never call the model, never clear.
    s = stub({"is_false_positive": True, "confidence": "high", "recommendation": "accept", "assessment": "a"})
    v = fable_review.review_held(ENTRY, ["fidelity flag"], "", s)
    check("empty text: held, unavailable, and no model call made", v["clear"] is False and v["available"] is False and s.calls == [])
    v = fable_review.review_held(ENTRY, ["fidelity flag"], "short", s)
    check("thin text: held and unavailable", v["clear"] is False and v["available"] is False)

    # A raising call_json (network/parse error) -> fail closed, not an exception.
    def boom(body, label="call"):
        raise RuntimeError("api down")
    v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT, boom)
    check("model error: held and unavailable, no exception", v["clear"] is False and v["available"] is False)

    # A non-dict response -> held.
    v = fable_review.review_held(ENTRY, ["fidelity flag"], TEXT, lambda b, l="c": "not a dict")
    check("unexpected response shape: held", v["clear"] is False)


def test_verdict_fields_normalized():
    v = fable_review.review_held(ENTRY, ["completeness flag"], TEXT,
                                 stub({"is_false_positive": False, "confidence": "LOW",
                                       "recommendation": "nonsense", "assessment": "  needs a person  "}))
    check("confidence lowercased", v["confidence"] == "low")
    check("unknown recommendation falls back to veto", v["recommendation"] == "veto")
    check("assessment trimmed and kept", v["assessment"] == "needs a person")


# --- review_published: the post-publication reviewer -----------------------------------

GENUINE = {"verdict": "genuine", "confidence": "high",
           "quote": "we proceed under that same assumption without deciding",
           "assessment": "The card says the court applied the law it expressly declined to decide.",
           "suggested_synopsis": "Corrected synopsis.", "suggested_why": "Corrected why."}

# The quote above appears verbatim in this text; the tests that need it absent say so.
PTEXT = ("The parties agreed the question is governed by federal common law. "
         "We proceed under that same assumption without deciding the applicable law. " + "filler " * 200)


def test_published_suggests_only_on_genuine_and_grounded():
    print("review_published: when a correction is shown")
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(GENUINE))
    check("a genuine, grounded flag yields a correction",
          v["suggested_synopsis"] == "Corrected synopsis." and v["suggested_why"] == "Corrected why.")
    check("and is reported as grounded", v["grounded"] is True)
    check("and available", v["available"] is True)
    check("verdict and confidence pass through", (v["verdict"], v["confidence"]) == ("genuine", "high"))

    for verdict in ("false_positive", "uncertain"):
        v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT,
                                          stub(dict(GENUINE, verdict=verdict)))
        check("no correction on a %s verdict" % verdict,
              v["suggested_synopsis"] == "" and v["suggested_why"] == "")
        check("but the assessment survives on %s" % verdict, bool(v["assessment"]))


def test_ungrounded_quote_withholds_the_correction():
    """The load-bearing guardrail: a drafted edit to live legal text must rest on a passage the
    opinion actually contains."""
    print("review_published: quote grounding")
    absent = dict(GENUINE, quote="the court squarely held that federal common law governs")
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(absent))
    check("an invented quote is not grounded", v["grounded"] is False)
    check("and the correction is withheld",
          v["suggested_synopsis"] == "" and v["suggested_why"] == "")
    check("while the verdict and quote are still reported for the human",
          v["verdict"] == "genuine" and v["quote"] == absent["quote"])

    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(dict(GENUINE, quote="")))
    check("an empty quote is not grounded either", v["grounded"] is False)
    check("and withholds the correction", v["suggested_synopsis"] == "")

    # A trivially short quote appears in almost any opinion, so it cannot substantiate an edit.
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(dict(GENUINE, quote="the")))
    check("a trivially short quote is not grounded", v["grounded"] is False)

    # An injected checker is used in preference to the built-in, and a broken one reads as
    # NOT grounded rather than throwing or passing.
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(GENUINE),
                                      grounded=lambda q, s: (_ for _ in ()).throw(RuntimeError("boom")))
    check("a checker that raises is treated as ungrounded", v["grounded"] is False)
    check("and withholds the correction", v["suggested_synopsis"] == "")
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(GENUINE),
                                      grounded=lambda q, s: False)
    check("the injected checker overrides the built-in", v["grounded"] is False)


def test_published_never_suppresses_and_never_raises():
    print("review_published: fail-soft")
    def boom(body, label="call"):
        raise RuntimeError("model down")
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, boom)
    check("a model error returns unavailable rather than raising", v["available"] is False)
    check("with no correction", v["suggested_synopsis"] == "" and v["suggested_why"] == "")
    check("and says why", "model down" in v["assessment"])

    v = fable_review.review_published(ENTRY, ["fidelity: x"], "short", stub(GENUINE))
    check("thin opinion text yields no review", v["available"] is False)

    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(["not", "a", "dict"]))
    check("a non-dict answer yields no review", v["available"] is False)

    # Every unavailable path must still be a well-formed dict: the caller reads these keys.
    for val in (v, fable_review.review_published(ENTRY, [], "", stub(GENUINE))):
        check("unavailable results still carry every key",
              set(val) == {"available", "verdict", "confidence", "quote", "grounded",
                           "assessment", "suggested_synopsis", "suggested_why"})


def test_published_normalizes_and_bounds():
    print("review_published: normalization")
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT,
                                      stub(dict(GENUINE, verdict="DEFINITELY BROKEN", confidence="certain")))
    check("an unrecognized verdict falls back to uncertain", v["verdict"] == "uncertain")
    check("an unrecognized confidence falls back to low", v["confidence"] == "low")
    check("and an unrecognized verdict suggests nothing", v["suggested_synopsis"] == "")

    long = dict(GENUINE, suggested_synopsis="y" * 9000, suggested_why="z" * 9000)
    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(long))
    check("a runaway suggestion is bounded",
          len(v["suggested_synopsis"]) == fable_review.MAX_SUGGESTION
          and len(v["suggested_why"]) == fable_review.MAX_SUGGESTION)

    v = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT, stub(dict(GENUINE, verdict="GENUINE")))
    check("verdict matching is case-insensitive", v["verdict"] == "genuine")


def test_the_two_reviewers_fail_in_opposite_directions():
    """The distinction the module is built on, asserted rather than left to the comments."""
    print("held vs published")
    doubtful = {"is_false_positive": True, "confidence": "low", "recommendation": "accept",
                "assessment": "unsure"}
    held = fable_review.review_held(ENTRY, ["x"], TEXT, stub(doubtful))
    check("doubt before publication keeps the card held", held["clear"] is False)
    # After publication the gate is the QUOTE, not confidence. Low confidence on a verdict whose
    # quote is verified present in the opinion still yields a draft: the reader has the passage in
    # front of them and decides. Vetoing on confidence too would throw away the useful half of a
    # review whose basis is already checked, and would guard nothing the quote check does not --
    # the harm case is an edit resting on words the opinion does not contain, and that is blocked.
    pub = fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT,
                                        stub(dict(GENUINE, confidence="low")))
    check("doubt after publication still drafts, because the quote is grounded",
          pub["suggested_synopsis"] == "Corrected synopsis." and pub["grounded"] is True)
    check("and surfaces the low confidence for the reader to weigh", pub["confidence"] == "low")
    check("while an ungrounded quote at ANY confidence drafts nothing",
          fable_review.review_published(ENTRY, ["fidelity: x"], PTEXT,
                                        stub(dict(GENUINE, confidence="high",
                                                  quote="words the opinion never used")))
          ["suggested_synopsis"] == "")


def main():
    print("fable_review:")
    test_clears_only_on_the_full_triple()
    test_stringy_boolean_is_fail_closed()
    test_fail_closed()
    test_verdict_fields_normalized()
    test_published_suggests_only_on_genuine_and_grounded()
    test_ungrounded_quote_withholds_the_correction()
    test_published_never_suppresses_and_never_raises()
    test_published_normalizes_and_bounds()
    test_the_two_reviewers_fail_in_opposite_directions()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
