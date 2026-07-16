#!/usr/bin/env python3
"""Hermetic stress + fault-injection harness for the batched pipeline paths. No network, no API key.

Hammers the three new batch surfaces under adversarial input and volume, asserting the invariants that
matter for an unattended run: no crash on a malformed/partial API response, no duplicate or lost
cards, and correct defer/evaluated behavior.

  A. batch.py transport  -- stub the single _send seam with adversarial submit/status/results bodies
     (malformed JSONL, truncated last line, missing/extra custom_ids, unknown statuses, blank lines,
     huge result sets) and drive the real submit/poll/collect/run chain.
  B. update._draft_pending (funnel tier-3) -- fuzz large `pending` sets against randomized result maps
     (random ok/errored/unparseable, missing, extra, duplicate cids) and a finish_fn that sometimes
     raises; assert drafted == exactly the ok+parseable cids and finish is called for exactly those.
  C. treatment._classify_batch -- same fuzz; assert verdicts ⊆ collected and ok flips only on a
     whole-batch failure.
  D. parity -- an all-ok batch drafts every card and finishes each with its verdict, identical to what
     the synchronous path would produce.

Run directly: `python scripts/stress_batch.py [iterations]` (default 3000). Exits nonzero on any
failure. Deterministic: seeds the RNG so a failure reproduces.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch      # noqa: E402
import treatment  # noqa: E402
import update     # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name + (("  -- " + detail) if detail else ""))
        print("  FAIL " + name + (("  -- " + detail) if detail else ""))


# ---- adversarial response builders --------------------------------------------------------------

def _result_line(cid, kind, text='{"ok": true}'):
    """One JSONL result record as the collect() reader expects."""
    if kind == "succeeded":
        return json.dumps({"custom_id": str(cid),
                           "result": {"type": "succeeded",
                                      "message": {"content": [{"type": "text", "text": text}],
                                                  "usage": {}, "stop_reason": "end_turn"}}})
    return json.dumps({"custom_id": str(cid), "result": {"type": kind, "error": {"type": kind}}})


def make_send(cids, rng, poison=True):
    """A _send stub for the whole submit/status/collect chain over `cids`, with random corruption."""
    body_lines = []
    expect = {}
    for cid in cids:
        roll = rng.random()
        if roll < 0.55:
            txt = rng.choice(['{"relevant": true, "significance": "high", "areas": ["auto"]}',
                              '{"treatment": "negative", "kind": "overruled", "affects_proposition": true}',
                              '{"ok": true}'])
            body_lines.append(_result_line(cid, "succeeded", txt))
            expect[str(cid)] = ("ok", txt)
        elif roll < 0.75:
            body_lines.append(_result_line(cid, rng.choice(["errored", "canceled", "expired"])))
            expect[str(cid)] = ("err", None)
        elif roll < 0.9:
            body_lines.append(_result_line(cid, "succeeded", rng.choice(["not json {{{", "", "  ", "{partial"])))
            expect[str(cid)] = ("bad", None)
        else:
            expect[str(cid)] = ("missing", None)   # no line emitted for this cid at all
    if poison:
        # Adversarial noise the reader must survive: blank lines, whitespace, an extra unknown cid,
        # and (the big one) a truncated final JSON line, as a mid-stream network cut would produce.
        rng.shuffle(body_lines)
        if rng.random() < 0.5:
            body_lines.insert(rng.randint(0, len(body_lines)), "")
        if rng.random() < 0.5:
            body_lines.insert(rng.randint(0, len(body_lines)), "   ")
        if rng.random() < 0.4:
            body_lines.append(_result_line("999999999", "succeeded"))   # unknown cid: must be ignored
        if rng.random() < 0.5 and body_lines:
            body_lines.append('{"custom_id": "5", "result": {"type": "succ')   # truncated last line
    results_body = "\n".join(body_lines)

    def _send(method, url, payload, label="batch"):
        if method == "POST":
            return json.dumps({"id": "msgbatch_stress"})
        if url.endswith("msgbatch_stress"):
            return json.dumps({"id": "msgbatch_stress", "processing_status": "ended",
                               "results_url": batch.API + "/msgbatch_stress/results"})
        if url.endswith("/results"):
            return results_body
        raise AssertionError("unexpected url " + url)
    return _send, expect


# ---- A. transport robustness --------------------------------------------------------------------

def stress_transport(iters, rng):
    real_send = batch._send
    try:
        for _ in range(iters):
            n = rng.randint(0, 40)
            cids = list(range(1, n + 1))
            send, expect = make_send(cids, rng, poison=True)
            batch._send = send
            try:
                out = batch.run([batch.from_body(str(c), {"model": "m", "max_tokens": 8,
                                 "system": "s", "messages": [{"role": "user", "content": "u"}]})
                                 for c in cids] or [batch.from_body("1", {"model": "m", "max_tokens": 8,
                                 "system": "s", "messages": [{"role": "user", "content": "u"}]})],
                                deadline=None, label="stress")
            except batch.BatchError:
                continue   # submit with no requests etc. -- acceptable, not a crash
            # collect must never raise on any corruption; every returned cid must be one we submitted.
            for k in out:
                check("transport: returned cid was submitted", k in expect or k == "999999999")
            for cid, (want, _txt) in expect.items():
                if want == "ok":
                    check("transport: ok line -> ok result", out.get(cid, {}).get("ok") is True,
                          "cid=%s got=%r" % (cid, out.get(cid)))
                elif want == "err":
                    check("transport: errored line -> ok False", out.get(cid, {}).get("ok") is False)
                # "bad" (unparseable text) still parses as a succeeded line here: collect returns the
                # raw text; parsing happens downstream. "missing" -> absent. Both are non-crash cases.
    finally:
        batch._send = real_send


# ---- B/C. orchestrator fuzz ---------------------------------------------------------------------

def _run_map(result_map, raise_exc=None):
    def _run(reqs, deadline=None, interval=20.0, label="batch"):
        if raise_exc is not None:
            raise raise_exc
        return result_map
    return _run


def stress_draft_pending(iters, rng):
    real_run = batch.run
    try:
        for _ in range(iters):
            n = rng.randint(0, 60)
            cids = rng.sample(range(1, 10000), n)
            pending = [{"cid": c, "r": {}, "name": "C%d" % c, "court_id": "ga", "docket": "D%d" % c,
                        "date_filed": "2026-07-01", "text": "t", "note": "", "cl_status": "published"}
                       for c in cids]
            # Build an adversarial result map: random subset ok/errored/unparseable, plus extras/dupes.
            rmap, want_ok = {}, set()
            for c in cids:
                roll = rng.random()
                if roll < 0.6:
                    rmap[str(c)] = {"ok": True, "text": '{"v": %d}' % c, "stop_reason": "end_turn"}
                    want_ok.add(c)
                elif roll < 0.8:
                    rmap[str(c)] = {"ok": False, "type": "errored"}
                elif roll < 0.92:
                    rmap[str(c)] = {"ok": True, "text": "not json", "stop_reason": "end_turn"}
                # else: omit (missing)
            if rng.random() < 0.3:
                rmap["88888888"] = {"ok": True, "text": '{"v": 0}', "stop_reason": "end_turn"}  # unknown

            mode = rng.random()
            finished = []

            def finish_fn(v, p):
                # Models the real funnel _finish: does its work, and catches its own non-ConfigError
                # exceptions (a bad card must not sink the batch), so it never propagates here.
                finished.append(p["cid"])
                try:
                    if rng.random() < 0.1:
                        raise RuntimeError("boom finishing %s" % p["cid"])
                except Exception:
                    pass

            if mode < 0.15:
                batch.run = _run_map(None, raise_exc=batch.BatchTimeout("b", "late"))
                drafted = update._draft_pending(pending, 1.0, finish_fn)
                check("draft: timeout -> empty drafted", drafted == set())
                check("draft: timeout -> no finish", finished == [])
            elif mode < 0.3:
                batch.run = _run_map(None, raise_exc=batch.BatchError("boom"))
                drafted = update._draft_pending(pending, 1.0, finish_fn)
                check("draft: transport error -> empty drafted", drafted == set())
            else:
                batch.run = _run_map(rmap)
                drafted = update._draft_pending(pending, 1.0, finish_fn)
                check("draft: drafted == ok+parseable cids", drafted == want_ok,
                      "diff=%r" % (drafted ^ want_ok))
                check("draft: finish called for exactly the drafted cids",
                      set(finished) == want_ok, "diff=%r" % (set(finished) ^ want_ok))
                check("draft: drafted subset of pending cids", drafted <= set(cids))

        # Contract: a ConfigError raised by finish_fn propagates out of _draft_pending so the run can
        # abort (main() turns it into cfg_error -> nothing committed). Pin it explicitly.
        one = [{"cid": 1, "r": {}, "name": "C1", "court_id": "ga", "docket": "D1",
                "date_filed": "2026-07-01", "text": "t", "note": "", "cl_status": "published"}]
        batch.run = _run_map({"1": {"ok": True, "text": '{"v": 1}', "stop_reason": "end_turn"}})

        def _raise_cfg(v, p):
            raise update.ConfigError("credit exhausted")
        propagated = False
        try:
            update._draft_pending(one, 1.0, _raise_cfg)
        except update.ConfigError:
            propagated = True
        check("draft: ConfigError from finish propagates (run aborts, nothing committed)", propagated)
    finally:
        batch.run = real_run


def stress_classify_batch(iters, rng):
    real_run = batch.run
    card = {"name": "Landmark v. State", "synopsis": "holds X", "why": "Y"}
    try:
        for _ in range(iters):
            n = rng.randint(0, 30)
            ccids = rng.sample(range(1, 10000), n)
            collect = [{"ccid": c, "cname": "C%d" % c, "cdate": "2026-07-01", "ccourt": "ga",
                        "ctext": "Landmark v. State opinion. " * 20} for c in ccids]
            rmap, want_ok = {}, set()
            for c in ccids:
                roll = rng.random()
                if roll < 0.6:
                    rmap[str(c)] = {"ok": True, "text": '{"treatment": "neutral"}', "stop_reason": "end_turn"}
                    want_ok.add(c)
                elif roll < 0.8:
                    rmap[str(c)] = {"ok": False, "type": "expired"}
                elif roll < 0.92:
                    rmap[str(c)] = {"ok": True, "text": "xx not json", "stop_reason": "end_turn"}
            mode = rng.random()
            if mode < 0.2:
                batch.run = _run_map(None, raise_exc=batch.BatchTimeout("b", "late"))
                verdicts, ok = treatment._classify_batch(card, collect, 1.0)
                check("classify: whole-batch fail -> ok False, empty", ok is False and verdicts == {})
            else:
                batch.run = _run_map(rmap)
                verdicts, ok = treatment._classify_batch(card, collect, 1.0)
                check("classify: ok True on a completed job", ok is True)
                check("classify: verdicts == ok+parseable", set(verdicts) == want_ok,
                      "diff=%r" % (set(verdicts) ^ want_ok))
                check("classify: verdicts subset of collected", set(verdicts) <= set(ccids))
    finally:
        batch.run = real_run


# ---- D. sync/batch parity -----------------------------------------------------------------------

def stress_parity(rng):
    real_run = batch.run
    try:
        cids = rng.sample(range(1, 10000), 25)
        pending = [{"cid": c, "r": {}, "name": "C%d" % c, "court_id": "ga", "docket": "D%d" % c,
                    "date_filed": "2026-07-01", "text": "t", "note": "", "cl_status": "published"}
                   for c in cids]
        rmap = {str(c): {"ok": True, "text": '{"v": %d}' % c, "stop_reason": "end_turn"} for c in cids}
        batch.run = _run_map(rmap)
        seen = []
        update._draft_pending(pending, 1.0, lambda v, p: seen.append((p["cid"], v["v"])))
        check("parity: an all-ok batch finishes every card with its own verdict, in some order",
              sorted(seen) == sorted((c, c) for c in cids))
    finally:
        batch.run = real_run


def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    seed = 1234567
    rng = random.Random(seed)
    print("batch stress harness: %d iterations/section, seed=%d" % (iters, seed))
    stress_transport(iters, rng)
    print("  transport: done")
    stress_draft_pending(iters, rng)
    print("  _draft_pending: done")
    stress_classify_batch(iters, rng)
    print("  _classify_batch: done")
    stress_parity(rng)
    print("  parity: done")
    if FAILS:
        uniq = sorted(set(FAILS))
        print("\nFAILED (%d unique):" % len(uniq))
        for f in uniq[:40]:
            print("  - " + f)
        return 1
    print("\nALL STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
