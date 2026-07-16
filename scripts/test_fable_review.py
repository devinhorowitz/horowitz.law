#!/usr/bin/env python3
"""Hermetic unit tests for fable_review.review_held (no network, no API key).

The Fable senior review clears a held card ONLY on the triple of is_false_positive + high
confidence + accept, on adequate opinion text; everything else is fail-closed (stays held). These
tests stub the `call_json` callable, so no Anthropic call is made -- they pin exactly when a held
case may and may not be auto-published.

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


def main():
    print("fable_review.review_held:")
    test_clears_only_on_the_full_triple()
    test_stringy_boolean_is_fail_closed()
    test_fail_closed()
    test_verdict_fields_normalized()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
