#!/usr/bin/env python3
"""Hermetic unit test for golden_check's fail-closed exit codes (no network).

golden_check.check()/summarize_check()/recall() gate whether a model change is safe
(model-watch.yml treats exit 0 as "safe to bump"). If every golden case is uncached --
or the set is empty -- the gate verified NOTHING and must exit nonzero, not silently pass.
Uncached cases skip the model calls (`if not c.get("text"): continue`), so this test drives
the fail-closed paths without any Anthropic/CourtListener calls.

Run directly: `python scripts/test_golden_check.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden_check    # noqa: E402
import update          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
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


def main():
    print("golden_check fail-closed gate:")
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

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (7 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
