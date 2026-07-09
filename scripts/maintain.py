#!/usr/bin/env python3
"""Budget-gated maintenance for the opinions pipeline.

Runs on a daily schedule and spends only idle CourtListener capacity on quality and
self-healing work. Ingestion always wins. Two pieces:

  1. The golden regression check (CourtListener-free): re-runs the real screen and
     triage tiers against cached opinion text and reports any keeper that would now be
     dropped, or any control that would now be kept. It costs no CourtListener calls,
     so it runs every time, as a standing tripwire for a prompt or model change.

  2. A rotating re-validation of already-published cards (the budget-gated trickle):
     re-runs the independent cross-check and completeness check on a small, date-rotated
     slice of cards in opinions.json, to catch a card whose drafted holding reads wrong
     against its own opinion or that omits a separate material holding. It re-fetches each
     card's opinion text once and reuses it for both, so it is the only part that spends
     CourtListener calls, and it runs only when there is comfortable headroom.

Yielding to ingestion, two layers:
  * Soft gate: sum the funnel's cl_calls over the trailing 24h from the pipeline log
    and skip the re-validation when that is at or above a reserve. The log records only
    the funnel's own calls, not the other workflows, so this is a floor, and the
    reserve stays well under the daily ceiling.
  * Hard net: each text fetch runs under a short wall-clock deadline, so a full rolling
    window or a 429 raises RateBudgetExceeded and the slice defers the rest to the next
    run. Maintenance defers, it never fails the run, and it never runs the funnel.

Findings surface, they do not self-edit. A cross-check or completeness flag on a published
card, or a golden regression, goes to the run summary, and the process exits nonzero so the
workflow opens or updates a tracking issue for a person. Nothing here writes opinions.json.

Imports update, so the cross-check, completeness check, text fetch, rate budget, and paths
are the production ones, reused exactly: what runs here is what the funnel runs.
"""
import os
import sys
import json
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update          # production cross-check, text fetch, paths, model config
import cl_rate         # shared CourtListener REST budget (same singleton update uses)
import golden_check    # the regression guard; reused so it tests what the funnel runs

# Knobs, all repo-variable overridable; the defaults are conservative.
RESERVE   = int(os.environ.get("OPINIONS_MAINT_RESERVE", "50"))     # trailing-24h funnel cl_calls at or above which the CL re-validation is skipped
SLICE     = int(os.environ.get("OPINIONS_MAINT_SLICE", "3"))        # published cards to re-validate per run
FETCH_SEC = int(os.environ.get("OPINIONS_MAINT_FETCH_SEC", "180"))  # per-run wall-clock budget for the slice's fetches; a full window or 429 defers the rest
REQUIRED_FIELDS = ("cluster_id", "name", "court", "date", "dockets", "disposition",
                   "areas", "url", "synopsis", "why", "first_seen", "precedential")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp():
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(text):
    """Append to the Actions run summary, best-effort; a write failure never matters."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        print("  . summary write skipped: %s" % e)


def trailing_24h_cl_calls():
    """Sum cl_calls over funnel run records from the last 24h of the pipeline log.

    A floor on real CourtListener usage, since the log records only the funnel's calls,
    used only to decide whether to spend on maintenance, so erring low is safe with a
    conservative reserve. Returns (total_calls, n_runs)."""
    path = update.LOG_PATH
    if not os.path.exists(path):
        return 0, 0
    cutoff = _now() - datetime.timedelta(hours=24)
    total = n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = datetime.datetime.strptime(
                        rec.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.timezone.utc)
                except (ValueError, json.JSONDecodeError):
                    continue
                if ts >= cutoff:
                    total += int(rec.get("cl_calls", 0) or 0)
                    n += 1
    except Exception as e:
        print("  . pipeline log read skipped: %s" % e)
    return total, n


def rotating_slice(cards):
    """A deterministic, date-rotated slice of the cards, sorted by cluster_id so the
    order is stable as cards are added. No persisted cursor: the start advances by SLICE
    each UTC day and wraps, so every card is revisited over time. Cards without a
    cluster_id are skipped, since the text fetch needs one."""
    pool = sorted((c for c in cards if c.get("cluster_id")), key=lambda c: c["cluster_id"])
    if not pool:
        return []
    n = len(pool)
    k = min(max(1, SLICE), n)
    epoch_day = int(time.time() // 86400)
    start = (epoch_day * k) % n
    return [pool[(start + i) % n] for i in range(k)]


def revalidate(cards):
    """Re-run the per-card guards on the rotating slice. Returns (flags, checked, deferred),
    where flags is a list of (name, reason) and each reason is prefixed with the guard that
    raised it ("fidelity: ..." for the cross-check, "completeness: ..." for the completeness
    check). The opinion text is fetched once per card and reused by both guards, so adding the
    completeness guard costs model calls but no extra CourtListener calls. Defers cleanly on a
    rate-budget stop."""
    flags, checked, deferred = [], 0, 0
    if not update.CROSSCHECK_MODEL and not update.COMPLETENESS_MODEL:
        print("  . both per-card guards disabled (OPINIONS_CROSSCHECK_MODEL and "
              "OPINIONS_COMPLETENESS_MODEL empty); skipping re-validation")
        return flags, checked, deferred
    deadline = time.time() + FETCH_SEC
    for card in rotating_slice(cards):
        name = card.get("name", "(unnamed)")
        try:
            text = update.opinion_text_full({"cluster_id": card["cluster_id"]}, deadline=deadline)
        except cl_rate.RateBudgetExceeded as e:
            deferred += 1
            print("  . rate budget reached; deferring the rest of the slice (%s)" % e)
            break
        if not text:
            print("  . no opinion text fetched for %s; skipping" % name[:50])
            continue
        checked += 1
        raised = False
        try:
            if update.CROSSCHECK_MODEL:
                cc = update.crosscheck(name, text, card)
                if cc and cc.get("verdict") == "flag":
                    flags.append((name, "fidelity: " + (cc.get("reason") or "")))
                    print("  FLAG (fidelity) %s: %s" % (name[:50], cc.get("reason") or "")); raised = True
                elif cc and cc.get("verdict") == "unavailable":
                    print("  . cross-check unavailable for %s" % name[:50])
            if update.COMPLETENESS_MODEL:
                cp = update.completeness_check(name, text, card)
                if cp and cp.get("verdict") == "flag":
                    flags.append((name, "completeness: " + (cp.get("reason") or "")))
                    print("  FLAG (completeness) %s: %s" % (name[:50], cp.get("reason") or "")); raised = True
                elif cp and cp.get("verdict") == "unavailable":
                    print("  . completeness check unavailable for %s" % name[:50])
        except update.ConfigError:
            raise                     # a real misconfig must surface, not be swallowed
        except Exception as e:
            # A transient model/network error on one card must not crash the sweep:
            # maintenance defers, it never fails the run. Skip this card, keep going.
            checked -= 1
            print("  . guard error on %s; skipping card (%s)" % (name[:50], e))
            continue
        if not raised:
            print("  ok   %s" % name[:50])
    return flags, checked, deferred


def completeness(cards):
    """Field-integrity scan of published cards (CourtListener-free). Flags any card missing a
    required field, carrying an empty areas/dockets list, or holding an unparseable date. The
    funnel always populates these, so a flag means a regression, a schema drift, or a bad manual
    edit. Surfaces; does not self-edit."""
    issues = []
    for c in cards:
        nm = (c.get("name") or "?")[:60]
        missing = [k for k in REQUIRED_FIELDS if not c.get(k)]
        if missing:
            issues.append((nm, "missing " + ", ".join(missing)))
            continue
        if not isinstance(c.get("areas"), list) or not c["areas"]:
            issues.append((nm, "empty areas"))
        if not isinstance(c.get("dockets"), list) or not c["dockets"]:
            issues.append((nm, "empty dockets"))
        try:
            datetime.date.fromisoformat(c["date"])
        except Exception:
            issues.append((nm, "unparseable date %r" % c.get("date")))
    return issues


def main():
    if not update.KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    # 1. Golden regression check (CourtListener-free). Runs every time. It writes its
    #    own run-summary section and returns 0 (clean) or 1 (a keeper would now drop or
    #    a control would now keep).
    print("== golden regression check ==")
    gc_rc = golden_check.check()

    # 1b. Card completeness scan (CourtListener-free, runs every time). Catches a published
    #     card that lost a required field, or carries an empty areas/dockets list or a bad date.
    print("\n== published-card completeness ==")
    cards = json.load(open(update.JSON_PATH, encoding="utf-8")) if os.path.exists(update.JSON_PATH) else []
    comp_issues = completeness(cards)
    if comp_issues:
        print("  %d card(s) with completeness gaps:" % len(comp_issues))
        for nm, why in comp_issues:
            print("    - %s: %s" % (nm, why))
        _summary("\n### Card completeness %s\n\n" % _stamp()
                 + "".join("- GAP: %s (%s)\n" % (nm, why) for nm, why in comp_issues))
    else:
        print("  all %d card(s) complete" % len(cards))

    # 2. Budget-gated re-validation of published cards.
    print("\n== published-card re-validation ==")
    used, n_runs = trailing_24h_cl_calls()
    flags = []
    if used >= RESERVE:
        reason = ("skipped: the funnel used %d CourtListener call(s) in the last 24h across %d run(s), "
                  "at or above the reserve of %d, so the budget is left for ingestion"
                  % (used, n_runs, RESERVE))
        print("  . " + reason)
        _summary("\n### Opinions maintenance %s\n\n- re-validation %s" % (_stamp(), reason))
    else:
        flags, checked, deferred = revalidate(cards)
        line = ("re-validated %d published card(s), %d flag(s), %d deferred to the next run; "
                "funnel CourtListener calls in the last 24h: %d (reserve %d); this run: %d"
                % (checked, len(flags), deferred, used, RESERVE, cl_rate.PACER.calls))
        print("  . " + line)
        body = "\n### Opinions maintenance %s\n\n- %s\n" % (_stamp(), line)
        for nm, rs in flags:
            body += "- FLAG: %s (%s)\n" % (nm, rs)
        _summary(body)

    # Exit nonzero only when a person should look: a golden regression or a published-card
    # flag. A deferral or a budget skip is normal operation and exits clean. The workflow's
    # failure step opens or updates the maintenance issue.
    sys.exit(1 if (gc_rc or flags or comp_issues) else 0)


if __name__ == "__main__":
    main()
