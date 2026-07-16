#!/usr/bin/env python3
"""State-machine stress harness for the treatment sweep. Drives the REAL treatment.main() in a
stubbed sandbox and asserts the correctness-critical invariant:

    a card is NEVER marked fully-swept when its citation search was cut short.

If it were -- a rate-budget stop, the model breaker, a config error, or a failed classify batch
truncates the sweep, yet the card is flagged `full` -- the next run drops to the incremental
LOOKBACK window and an overruling decision filed in the card's older history is missed FOREVER. That
is the worst failure mode in the pipeline (a lawyer relies on a precedent that's been overruled).

Everything is stubbed: no network, no key, no repo writes. lead_opinion_id / citing_results /
citer_text / classify / batch.run are replaced, faults are injected at each, and the state
treatment.main() writes is read back to check every card's `full` flag -- for both the synchronous
and the TREATMENT_BATCH path.

Run directly: `python scripts/stress_treatment.py`. Exits nonzero on any failure.
"""
import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch      # noqa: E402
import cl_rate     # noqa: E402
import treatment  # noqa: E402
import update     # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


SCOPE_COURT = update.COURTS_ALL[0]
CARDS = [{"cluster_id": 700 + i, "date": "2020-01-0%d" % (i + 1), "name": "Card%d v. State" % i,
          "synopsis": "Holds proposition %d." % i, "why": "It matters.", "areas": ["auto"],
          "court": "gactapp", "dockets": ["A20A%04d" % i], "disposition": "affirmed",
          "url": "https://x/%d" % (700 + i)} for i in range(3)]


def citer(ccid):
    return {"cluster_id": ccid, "caseName": "Citer %d" % ccid, "dateFiled": "2026-06-01",
            "court_id": SCOPE_COURT}


@contextlib.contextmanager
def sandbox(cards, batch_mode, fault):
    """Redirect treatment's paths to a tempdir and stub every network seam; inject `fault`. Yields
    the tempdir. `fault` in: '', 'rate_citers', 'rate_text', 'breaker', 'batch_fail', 'config'."""
    tmp = tempfile.mkdtemp(prefix="treat-stress-")
    saved = {}

    def sv(obj, name, val):
        saved[(id(obj), name)] = (obj, name, getattr(obj, name))
        setattr(obj, name, val)

    sv(treatment, "JSON_PATH", os.path.join(tmp, "opinions.json"))
    sv(treatment, "STATE_PATH", os.path.join(tmp, "treatment_state.json"))
    sv(treatment, "PR_PATH", os.path.join(tmp, "treatment_pr_body.md"))
    with open(treatment.JSON_PATH, "w") as f:
        json.dump(cards, f)

    sv(treatment, "KEY", "test-key")
    sv(treatment, "BATCH", batch_mode)
    sv(treatment.time, "sleep", lambda *a, **k: None)
    sv(update, "anthropic_status", lambda: ("operational", "ok"))
    sv(treatment, "lead_opinion_id", lambda cid, dl: int(cid) * 10)

    def citing_results(oid, since, deadline):
        if fault == "rate_citers":
            raise cl_rate.RateBudgetExceeded("budget")
        # Enough citers that an all-classify-fail run actually TRIPS the breaker (>= BREAKER
        # consecutive failures) -- i.e. a genuine global stop, which is the precondition the
        # not-fully-swept invariant is about.
        return [citer(oid * 100 + k) for k in range(treatment.BREAKER + 2)]
    sv(treatment, "citing_results", citing_results)

    def citer_text(r, deadline):
        if fault == "rate_text":
            raise cl_rate.RateBudgetExceeded("budget")
        return "Opinion body discussing the cited case. " * 30
    sv(treatment, "citer_text", citer_text)

    def classify(card, cname, ctext):
        if fault == "breaker":
            raise RuntimeError("model down")
        if fault == "config":
            raise update.ConfigError("credit exhausted")
        return {"treatment": "neutral"}
    sv(treatment, "classify", classify)

    def fake_run(reqs, deadline=None, interval=20.0, label="batch"):
        if fault == "batch_timeout":
            raise batch.BatchTimeout("bid", "still running")
        if fault == "batch_error":
            raise batch.BatchError("submit failed")
        if fault == "config":
            raise update.ConfigError("credit exhausted")
        return {rq["custom_id"]: {"ok": True, "text": '{"treatment": "neutral"}', "stop_reason": "end_turn"}
                for rq in reqs}
    sv(batch, "run", fake_run)

    try:
        yield tmp
    finally:
        for obj, name, val in saved.values():
            setattr(obj, name, val)


def run_sweep(cards, batch_mode, fault):
    """Run treatment.main() once; return {str(cid): full_flag_or_None} from the written state."""
    with sandbox(cards, batch_mode, fault):
        try:
            treatment.main()
        except SystemExit:
            pass
        sp = treatment.STATE_PATH
        state = json.load(open(sp)) if os.path.exists(sp) else {}
    return {k: v.get("full") for k, v in state.items()}


def main():
    print("treatment state-machine stress (real main(), stubbed I/O):")

    for mode_name, batch_mode in (("sync", False), ("batch", True)):
        # Clean run: every card is fully swept -> full == True.
        st = run_sweep(CARDS, batch_mode, "")
        full_cards = [k for k, v in st.items() if v is True]
        check("%s clean: every swept card is marked full" % mode_name, len(full_cards) == len(CARDS),
              "full=%r" % st)

        # Every global-stop fault must leave EVERY card NOT fully-swept (the load-bearing invariant).
        # breaker is a synchronous per-citer thing; the batch path's equivalents are a timeout /
        # transport error / config error surfacing from the one job.
        if batch_mode:
            faults = ["rate_citers", "rate_text", "batch_timeout", "batch_error", "config"]
        else:
            faults = ["rate_citers", "rate_text", "breaker", "config"]
        for fault in faults:
            st = run_sweep(CARDS, batch_mode, fault)
            leaked = [k for k, v in st.items() if v is True]
            check("%s fault=%s: NO card marked full on a cut-short sweep" % (mode_name, fault),
                  not leaked, "cards wrongly marked full: %r" % leaked)

    # Parity: a clean sync run and a clean batch run reach the same fully-swept set.
    s_sync = {k for k, v in run_sweep(CARDS, False, "").items() if v is True}
    s_bat = {k for k, v in run_sweep(CARDS, True, "").items() if v is True}
    check("clean sync and batch mark the same cards full (parity)", s_sync == s_bat, "%r vs %r" % (s_sync, s_bat))

    # An already-full card stays full even under a stop (swept_full must never REGRESS a flag).
    prefull = [dict(CARDS[0])]
    with sandbox(prefull, False, "rate_citers"):
        key = str(int(prefull[0]["cluster_id"]))
        with open(treatment.STATE_PATH, "w") as f:
            json.dump({key: {"oid": 7000, "seen": [1, 2], "full": True}}, f)
        try:
            treatment.main()
        except SystemExit:
            pass
        after = json.load(open(treatment.STATE_PATH)) if os.path.exists(treatment.STATE_PATH) else {key: {"full": True}}
    check("an already-full card stays full through a stop (no regression)", after.get(key, {}).get("full") is True)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TREATMENT STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
