#!/usr/bin/env python3
"""Golden-set regression guard for the Georgia Appellate Watch funnel.

A small curated set of opinions with known expected verdicts, used to catch a prompt or
model change that quietly starts dropping cases the funnel used to keep (or starts keeping
controls it used to drop). It does not chase recall or measure how much the funnel misses;
it only guards against silent regression on a fixed set.

Two modes:
  build  fetch and cache each case's opinion text once (the only CourtListener spend).
         Re-run only when adding cases; entries that already have text are left alone.
  check  re-run the real screen and triage tiers against the cached text and compare to
         each case's expected verdict. Makes no CourtListener calls, so it is cheap to run
         on every prompt change or on spare model budget. Exits nonzero on any regression.

It reuses the funnel's own tiers and fetch path (imported from update.py), so it tests the
actual pipeline rather than a copy. It cards nothing and never writes opinions.json.

  python scripts/golden_check.py build   # needs COURTLISTENER_TOKEN
  python scripts/golden_check.py check   # needs ANTHROPIC_API_KEY
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # the real tiers (screen, triage) and fetch path (opinion_text_full), so the guard tests production

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_set.json")
SNIPPET_CHARS = 1500  # screen reads an opening excerpt; mirror its own [:1500] slice


def _load():
    return json.load(open(GOLDEN_PATH, encoding="utf-8"))


def _save(cases):
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build():
    """Fill in cached opinion text for any entry missing it. One-time CourtListener cost,
    bounded by the size of the set; entries that already have text are untouched."""
    cases = _load()
    filled = 0
    for c in cases:
        if c.get("text"):
            continue
        cid = c["cluster_id"]
        try:
            text = update.opinion_text_full({"cluster_id": cid})
        except Exception as e:
            print("  ! fetch failed for %s (%s): %s" % (cid, c.get("name", "")[:40], e))
            continue
        if not text:
            print("  ! no text for %s (%s); leaving empty" % (cid, c.get("name", "")[:40]))
            continue
        c["text"] = text[:update.MAXCHARS]   # store exactly what triage would read
        filled += 1
        print("  + cached %d chars for %s (%s)" % (len(c["text"]), cid, c.get("name", "")[:40]))
    _save(cases)
    print("build: cached %d case(s); %d total in the set" % (filled, len(cases)))


def _kept(c):
    """Run the real screen and triage tiers on a cached case. Returns (kept, detail), where
    kept mirrors the funnel's own pass conditions: relevant at screen, and relevant and not
    low-significance at triage."""
    name = c.get("name", "")
    docket = c.get("docket", "") or ""
    text = c.get("text") or ""
    s = update.screen(name, docket, text[:SNIPPET_CHARS])
    if not s.get("pass"):
        return False, "screen dropped: %s" % (s.get("reason") or "not a fit")
    t = update.triage(name, docket, text)
    if not t.get("relevant"):
        return False, "triage dropped: %s" % (t.get("reason") or "not relevant")
    if (t.get("significance") or "").lower() == "low":
        return False, "triage dropped: low significance"
    return True, "kept"


def check():
    """Re-run screen+triage on each cached case and compare to its expected verdict. No
    CourtListener calls. Exits nonzero if any case regresses, so a run shows red."""
    cases = _load()
    regressions, uncached, ok = [], [], 0
    for c in cases:
        if not c.get("text"):
            uncached.append(c.get("name", "?"))
            continue
        kept, detail = _kept(c)
        expect = bool(c.get("expect_relevant", True))
        if kept == expect:
            ok += 1
            print("  ok   %-55s %s" % (c.get("name", "")[:55], detail))
        else:
            regressions.append((c.get("name", "?"), expect, kept, detail))
            print("  FAIL %-55s expected %s, got %s (%s)"
                  % (c.get("name", "")[:55], "keep" if expect else "drop",
                     "keep" if kept else "drop", detail))
    print("\ngolden check: %d ok, %d regression(s), %d uncached" % (ok, len(regressions), len(uncached)))
    if uncached:
        print("uncached (run `build` first): %s" % ", ".join(uncached))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Golden-set check\n\n- %d ok, %d regression(s), %d uncached\n"
                        % (ok, len(regressions), len(uncached)))
                for nm, exp, got, det in regressions:
                    f.write("- FAIL %s: expected %s, got %s (%s)\n"
                            % (nm, "keep" if exp else "drop", "keep" if got else "drop", det))
        except Exception as e:
            print("  . summary write skipped: %s" % e)

    return 1 if regressions else 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "build":
        build()
        return 0
    if mode == "check":
        return check()
    print("usage: golden_check.py [build|check]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
