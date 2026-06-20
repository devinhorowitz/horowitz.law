#!/usr/bin/env python3
"""Golden-set regression guard for the Georgia Appellate Watch funnel.

A small curated set of opinions with known expected verdicts, used to catch a prompt or
model change that quietly starts dropping cases the funnel used to keep (or starts keeping
controls it used to drop). It does not chase recall or measure how much the funnel misses;
it only guards against silent regression on a fixed set.

Three modes:
  build      fetch and cache each case's opinion text once (the only CourtListener spend).
             Re-run only when adding cases; entries that already have text are left alone.
  check      re-run the real screen and triage tiers against the cached text and compare to
             each case's expected verdict. Makes no CourtListener calls, so it is cheap to run
             on every prompt change or on spare model budget. Exits nonzero on any regression.
  summarize  re-run the real Opus summarizer on each cached keeper and assert the produced
             practice areas still cover the case's expected areas. Catches a summarizer change
             that drops a material aspect. Makes no CourtListener calls but spends model budget
             (Opus), so run it on a summarizer change rather than daily. Exits nonzero on any
             dropped area.

It reuses the funnel's own tiers and fetch path (imported from update.py), so it tests the
actual pipeline rather than a copy. It cards nothing and never writes opinions.json.

  python scripts/golden_check.py build       # needs COURTLISTENER_TOKEN
  python scripts/golden_check.py check       # needs ANTHROPIC_API_KEY
  python scripts/golden_check.py summarize   # needs ANTHROPIC_API_KEY
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # the real tiers (screen, triage) and fetch path (opinion_text_full), so the guard tests production
import safeio  # crash-safe atomic writes (golden_set.json is committed; never truncate-write it)

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_set.json")
SNIPPET_CHARS = 1500  # screen reads an opening excerpt; mirror its own [:1500] slice


def _load():
    return json.load(open(GOLDEN_PATH, encoding="utf-8"))


def _save(cases):
    # Atomic: build mode commits this file straight to main, so a truncating write
    # killed mid-flight (timeout, runner eviction) would commit a corrupt set.
    safeio.atomic_write_text(GOLDEN_PATH, json.dumps(cases, ensure_ascii=False, indent=2) + "\n")


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
    """Run the real screen, pretriage, and triage tiers on a cached case. Returns (kept, detail),
    where kept mirrors the funnel's own pass conditions: passes the excerpt screen, passes the
    full-read pretriage, and is relevant and not low-significance at triage."""
    name = c.get("name", "")
    docket = c.get("docket", "") or ""
    text = c.get("text") or ""
    s = update.screen(name, docket, text[:SNIPPET_CHARS])
    if not s.get("pass"):
        return False, "screen dropped: %s" % (s.get("reason") or "not a fit")
    if update.PRETRIAGE_MODEL:
        p = update.pretriage(name, docket, text)
        if not p.get("pass"):
            return False, "pretriage dropped: %s" % (p.get("reason") or "not a fit")
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


def _produced_areas(v):
    """The practice areas a summarize result covers: the primary holding's areas plus any
    additional holdings' areas, each filtered to the valid taxonomy, exactly as the funnel
    records them. A union, so a card that reorganizes coverage across holdings still counts
    every area it covers."""
    areas = set(a for a in (v.get("areas") or []) if a in update.VALID_AREAS)
    for h in (v.get("additional_holdings") or []):
        if isinstance(h, dict):
            areas |= set(a for a in (h.get("areas") or []) if a in update.VALID_AREAS)
    return areas


def _summarize_attempts(name, docket, text, expect, tries):
    """Run the real Opus summarizer on one cached opinion up to `tries` times, accumulating the
    union of produced areas across attempts. The summarizer runs at temperature 1 (the model
    rejects a lower value), so a single run's area set is noisy; a genuine regression is a
    persistent miss, not a one-roll drop. Returns as soon as the union covers expect. A transient
    error makes that attempt contribute nothing and the loop proceeds; a persistent error leaves
    the union short and is reported. Returns (covered, union, used, last_addl, last_error)."""
    union, last_addl, last_error, used = set(), 0, "", 0
    # 'used' is the attempt count, consumed in the return below, not in the loop body.
    for used in range(1, tries + 1):  # noqa: B007
        try:
            v = update.summarize("", name, docket, "", text, "", cl_status="")
            union |= _produced_areas(v)
            last_addl, last_error = len(v.get("additional_holdings") or []), ""
        except Exception as e:
            last_error = str(e)[:120]
        if expect <= union:
            break
    return (expect <= union), union, used, last_addl, last_error


def summarize_check():
    """Re-run the real Opus summarizer on each cached keeper and assert the produced areas
    still cover its expect_areas. Catches a summarizer prompt or model change that drops a
    material aspect, an area the card used to carry. Coverage, not exact match: extra areas
    are fine, a dropped expected area is the regression. Spends model budget (Opus) but makes
    no CourtListener calls. Each case gets up to OPINIONS_GOLDEN_RETRIES retries (default 3, four
    attempts) because the summarizer runs at temperature 1 and varies run to run; only a persistent
    miss, an expected area produced in none of the attempts, is a regression. Exits nonzero on such
    a miss. Entries with no expect_areas,
    and controls (expect_relevant false), are skipped, since a non-keeper is not summarized."""
    cases = _load()
    tries = max(1, int(os.environ.get("OPINIONS_GOLDEN_RETRIES", "3")) + 1)
    regressions, uncached, skipped, ok = [], [], 0, 0
    for c in cases:
        expect = set(c.get("expect_areas") or [])
        if not bool(c.get("expect_relevant", True)) or not expect:
            skipped += 1
            continue
        if not c.get("text"):
            uncached.append(c.get("name", "?"))
            continue
        name = c.get("name", "")
        docket = c.get("docket", "") or ""
        passed, union, used, addl, err = _summarize_attempts(name, docket, c["text"], expect, tries)
        tag = "%d tr%s" % (used, "y" if used == 1 else "ies")
        if passed:
            ok += 1
            print("  ok   %-55s areas %s (holdings %d, %s)"
                  % (name[:55], ",".join(sorted(union)) or "(none)", 1 + addl, tag))
        else:
            detail = ("missing %s after %s (produced %s)"
                      % (",".join(sorted(expect - union)), tag, ",".join(sorted(union)) or "(none)"))
            if err:
                detail += "; last error: %s" % err
            regressions.append((name, detail))
            print("  FAIL %-55s %s" % (name[:55], detail))
    print("\ngolden summarize: %d ok, %d regression(s), %d uncached, %d skipped"
          % (ok, len(regressions), len(uncached), skipped))
    if uncached:
        print("uncached (run `build` first): %s" % ", ".join(uncached))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Golden-set summarize\n\n- %d ok, %d regression(s), %d uncached, %d skipped\n"
                        % (ok, len(regressions), len(uncached), skipped))
                for nm, det in regressions:
                    f.write("- FAIL %s: %s\n" % (nm, det))
        except Exception as e:
            print("  . summary write skipped: %s" % e)
    return 1 if regressions else 0


def recall():
    """Focused recall test for the Tier 1.5 pretriage screen: run pretriage alone on each cached
    case and report whether it would drop a known keeper before the Sonnet triage ever saw it. A
    pretriage drop of an expect_relevant case is a recall failure. Controls (expect_relevant false)
    are expected to pass pretriage, which is high-recall and leaves real filtering to triage, so a
    control that passes is fine and only noted. No CourtListener calls. Exits nonzero if any expected
    keeper is dropped, so this gates enabling pretriage in production."""
    if not update.PRETRIAGE_MODEL:
        print("pretriage is disabled (OPINIONS_PRETRIAGE_MODEL=''); nothing to test")
        return 0
    cases = _load()
    missed, uncached, kept_ok, ctrl = [], [], 0, 0
    for c in cases:
        if not c.get("text"):
            uncached.append(c.get("name", "?"))
            continue
        name = c.get("name", "")
        docket = c.get("docket", "") or ""
        p = update.pretriage(name, docket, c["text"])
        passed = bool(p.get("pass"))
        keeper = bool(c.get("expect_relevant", True))
        if keeper and not passed:
            missed.append((name, p.get("reason") or ""))
            print("  MISS %-55s pretriage dropped a keeper: %s" % (name[:55], p.get("reason") or ""))
        elif keeper:
            kept_ok += 1
            print("  ok   %-55s pretriage passed" % (name[:55]))
        else:
            ctrl += 1
            print("  ctrl %-55s pretriage %s (control)" % (name[:55], "passed" if passed else "dropped"))
    n_keep = kept_ok + len(missed)
    print("\npretriage recall: %d of %d expected keepers passed, %d missed; %d control(s), %d uncached"
          % (kept_ok, n_keep, len(missed), ctrl, len(uncached)))
    if uncached:
        print("uncached (run `build` first): %s" % ", ".join(uncached))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Pretriage recall\n\n- %d of %d expected keepers passed, %d missed\n"
                        % (kept_ok, n_keep, len(missed)))
                for nm, why in missed:
                    f.write("- MISS %s: %s\n" % (nm, why))
        except Exception as e:
            print("  . summary write skipped: %s" % e)
    return 1 if missed else 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "build":
        build()
        return 0
    if mode == "check":
        return check()
    if mode == "summarize":
        return summarize_check()
    if mode == "recall":
        return recall()
    print("usage: golden_check.py [build|check|summarize|recall]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
