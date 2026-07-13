#!/usr/bin/env python3
"""Hermetic unit test for render's as-of reference date (scripts/render.py), no network.

The CI render-idempotency gate re-renders and asserts the committed pages are byte-identical. Its
one false-positive was the live clock drifting past a card's rolling-2-year edge (or the Jan-1
footer year) after the last funnel render but before render-sync reconciled -- an unrelated PR
would then go spuriously red. The fix is `_asof()`: render's time reference, overridable by
OPINIONS_RENDER_ASOF so CI can render AS OF the last render (status.json scanned_at) and reproduce
the committed pages. This pins that behavior: the override drives the window cutoff and the footer
year, it is inert/graceful by default, and a card sitting just past its 2-year edge is KEPT when
as-of is the last render date (the exact case that used to fail) yet DROPPED as of real today.

Run directly: `python scripts/test_render.py`.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def with_asof(value):
    """Set/unset OPINIONS_RENDER_ASOF, returning a restore callable."""
    prev = os.environ.get("OPINIONS_RENDER_ASOF")
    if value is None:
        os.environ.pop("OPINIONS_RENDER_ASOF", None)
    else:
        os.environ["OPINIONS_RENDER_ASOF"] = value

    def restore():
        if prev is None:
            os.environ.pop("OPINIONS_RENDER_ASOF", None)
        else:
            os.environ["OPINIONS_RENDER_ASOF"] = prev
    return restore


def main():
    print("render as-of reference date:")
    restore = with_asof(None)
    try:
        check("_asof default is real today", render._asof() == datetime.date.today())

        with_asof("2024-06-15")
        check("_asof honors the override", render._asof() == datetime.date(2024, 6, 15))
        check("_cutoff_iso is WINDOW_YEARS before the as-of date",
              render._cutoff_iso() == datetime.date(2024 - render.WINDOW_YEARS, 6, 15).isoformat())
        check("_stamp_year rewrites the footer to the as-of year",
              render._stamp_year("&copy; 2019 footer") == "&copy; 2024 footer")

        with_asof("not-a-date")
        check("_asof falls back to today on a bad value", render._asof() == datetime.date.today())

        with_asof("")
        check("_asof falls back to today on an empty value", render._asof() == datetime.date.today())

        # The load-bearing case: a card whose date is exactly at the 2-year edge relative to a
        # fixed "now". As of real-now it has aged out (excluded); as of the last render (a day
        # before it aged out) it is still kept -- so CI, rendering as-of scanned_at, reproduces the
        # committed page that still had it, instead of failing.
        now = datetime.date(2026, 7, 13)
        card_date = datetime.date(2024, 7, 12).isoformat()  # ages out 2026-07-12 (WINDOW_YEARS=2)
        with_asof(now.isoformat())
        cutoff_now = render._cutoff_iso()
        with_asof((now - datetime.timedelta(days=2)).isoformat())  # the last render, before it aged out
        cutoff_lastrender = render._cutoff_iso()
        check("card is aged out as of real now (excluded)", not (card_date >= cutoff_now))
        check("card is kept as of the last render date (included)", card_date >= cutoff_lastrender)
    finally:
        restore()

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
