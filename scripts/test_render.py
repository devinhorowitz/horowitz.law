#!/usr/bin/env python3
"""Hermetic unit test for render's as-of reference date (scripts/render.py), no network.

The CI render-idempotency gate re-renders and asserts the committed pages are byte-identical. Its
one false-positive was the live clock drifting past a card's rolling-2-year edge (or the Jan-1
footer year) after the last funnel render but before render-sync reconciled -- an unrelated PR
would then go spuriously red. The fix is `_asof()`: render's time reference, overridable by
OPINIONS_RENDER_ASOF so CI can render AS OF the date the committed pages were rendered -- read
from their own commit, since both status.json timestamps track something else -- and reproduce
the committed pages. This pins that behavior: the override drives the window cutoff and the footer
year, it is inert/graceful by default, and a card sitting just past its 2-year edge is KEPT when
as-of is the pages' render date (the exact case that used to fail) yet DROPPED as of real today.

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


def check_ci_pins_asof_to_the_pages_commit():
    """The override is only as good as what CI feeds it, and CI fed it the wrong clock for
    two weeks before anyone noticed.

    On 2026-08-07 this gate went red on a PR that touched no rendered content. The pages had
    been rendered 08-05; `status.json` `scanned_at` had since advanced to 08-07 because the
    funnel scans every four hours whether or not it renders; and a card decided 2024-08-06
    aged out of the rolling 2-year window in between. Pinning to `scanned_at` reproduced the
    live clock, not the committed pages -- precisely the drift the pin exists to absorb.

    `content_updated_at` is no better: it is the last commit to opinions.json, so it lags any
    render-sync run that re-rendered pages without a data change, and 5 of the last 20 page
    renders were that shape.

    So the date must come from the pages' own commit, and CI needs unshallow history to read
    it. Both halves are asserted here, because either one alone silently reverts the fix: the
    right command against a depth-1 clone returns an empty string and falls back to real
    today, which is the unpinned behavior this whole mechanism replaced.
    """
    print("CI pins the as-of date to the pages' commit")
    try:
        import yaml
    except ImportError:                                # pragma: no cover
        print("  .. pyyaml not available; skipping")
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        ".github", "workflows", "ci.yml")
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    step, checkout = None, None
    for job in (doc.get("jobs") or {}).values():
        for st in (job.get("steps") or []):
            if "idempot" in (st.get("name") or "").lower():
                step = st.get("run") or ""
            if "actions/checkout" in str(st.get("uses", "")):
                checkout = st.get("with") or {}

    check("the render-idempotency step was found", bool(step))
    check("it derives the as-of date from the pages' commit",
          "git log -1" in (step or "") and "public/opinions.html" in (step or ""),
          (step or "")[:120])
    check("and not from either status.json timestamp",
          "scanned_at" not in (step or "") .split("#")[0]
          and "status.json" not in "\n".join(
              ln for ln in (step or "").splitlines() if not ln.strip().startswith("#")))
    check("the checkout is unshallow, or the git log returns nothing",
          str((checkout or {}).get("fetch-depth")) == "0", str(checkout))


def main():
    check_ci_pins_asof_to_the_pages_commit()
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
        # before it aged out) it is still kept -- so CI, rendering as of the pages' commit date,
        # reproduces the committed page that still had it, instead of failing.
        now = datetime.date(2026, 7, 13)
        card_date = datetime.date(2024, 7, 12).isoformat()  # ages out 2026-07-12 (WINDOW_YEARS=2)
        with_asof(now.isoformat())
        cutoff_now = render._cutoff_iso()
        with_asof((now - datetime.timedelta(days=2)).isoformat())  # the last render, before it aged out
        cutoff_lastrender = render._cutoff_iso()
        check("card is aged out as of real now (excluded)", not (card_date >= cutoff_now))
        check("card is kept as of the last render date (included)", card_date >= cutoff_lastrender)

        # The /areas/*.json slice must carry the HUMAN treatment_note when present, exactly as the
        # HTML card does -- not silently substitute the generic machine auto-note, which would erase an
        # editor's manual correction in the data feed draft-time consumers read.
        base = {"cluster_id": 1, "name": "X v. Y", "court": "ctapp", "date": "2026-01-01",
                "url": "https://cl/1/", "areas": ["auto"], "treatment": "superseded",
                "treatment_auto_note": "Possibly overruled by a later decision. Confirm on Shepard's.",
                "treatment_date": "2026-01-05"}
        human = dict(base, treatment_note="Good law for duty; overruled only on causation.")
        check("slice prefers the human treatment_note over the auto note",
              render._slice_entry(human)["treatment_note"] == "Good law for duty; overruled only on causation.")
        check("slice falls back to the auto note when there is no human note",
              render._slice_entry(base)["treatment_note"] == base["treatment_auto_note"])

        # Adversarial card text must not break the feeds or the JSON-LD.
        import re as _re
        card = {"cluster_id": 9, "name": "X v. Y", "court": "ctapp", "date": "2026-01-01",
                "dockets": ["A1"], "areas": ["auto"], "disposition": "affirmed",
                "precedential": "published", "why": "matters", "url": "https://cl/9/",
                "additional_holdings": []}

        # JSON-LD: a U+2028/U+2029 in the text is a JS line terminator; it must be \u-escaped inside
        # the <script>, never raw (which throws SyntaxError and drops the whole rich snippet).
        LS, PS = chr(0x2028), chr(0x2029)
        perma = render.permalink_html(dict(card, synopsis="holding" + LS + "then" + PS + "more <b> </script>"))
        ld = _re.search(r'<script type="application/ld\+json">(.*?)</script>', perma, _re.S).group(1)
        check("JSON-LD escapes U+2028 (no raw line-separator in the script)", LS not in ld and "\\u2028" in ld)
        check("JSON-LD escapes U+2029", PS not in ld and "\\u2029" in ld)
        check("JSON-LD still neutralizes < and > (no </script> breakout)",
              "<" not in ld and ">" not in ld)

        # RSS: an XML-1.0-illegal control char (bad PDF OCR) must be stripped, or the whole feed breaks.
        item = render.rss_item(dict(card, name="X v. Y\x0b", synopsis="a\x08b\x0bc holding", why="w\x1fy"))
        check("rss_item strips XML-illegal control chars", not _re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", item))
        check("rss_item keeps legal whitespace and real text", "holding" in item and "X v. Y" in item)
    finally:
        restore()

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
