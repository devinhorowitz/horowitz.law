#!/usr/bin/env python3
"""Hermetic unit tests for the drop-reason smell test (update.py tier 2.5 + smell_check.py).

Standard library only; no network and no API key. Stubs update.anthropic_json and batch.run the
same way test_update.py does. The smell test is the recall audit of triage-drop reasons: a wrong
parse here silently swallows the audit (fail-open), so these tests pin the request shape, the
verdict parsing, the fail-open defaults, the empty-reason rule, the escalation cap, and the retro
script's record selection.

Run directly: `python scripts/test_smell.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update       # noqa: E402  (sys.path shim must run first)
import batch        # noqa: E402
import smell_check  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


ITEMS = [
    {"name": "A v. B", "court": "ctapp", "date": "2026-07-01", "reason": "criminal appeal, out of scope"},
    {"name": "C v. D", "court": "ca11", "date": "2026-07-02", "reason": "Georgia duty-to-defend analysis"},
    {"name": "E v. F", "court": "scotga", "date": "2026-07-03", "reason": ""},
]


def test_prompt_shape():
    body = update.smell_request(ITEMS)
    check("request uses the smell model", body["model"] == update.SMELL_MODEL)
    user = body["messages"][0]["content"]
    check("items are numbered 1-based", "1. [ctapp 2026-07-01]" in user and "2. [ca11 2026-07-02]" in user)
    check("reason text reaches the prompt", "criminal appeal, out of scope" in user)
    check("an empty reason renders as (none given)", "REASON FOR THE DROP: (none given)" in user)
    check("smell system carries the triage criteria verbatim (no drift)",
          update.TRIAGE_CRITERIA in update.SMELL_SYSTEM)
    check("smell system demands the verdicts object", '"verdicts"' in update.SMELL_SYSTEM)


def _sync_stub(payload):
    """Stub anthropic_json to return `payload` and force the synchronous path."""
    update.SMELL_BATCH = False
    update.anthropic_json = lambda body, label=None: payload


def test_verdict_parsing():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        _sync_stub({"verdicts": [
            {"i": 1, "verdict": "ok", "note": ""},
            {"i": 2, "verdict": "suspect", "note": "keep-shaped label"},
            {"i": 99, "verdict": "suspect", "note": "out of range"},
            {"i": "x", "verdict": "suspect", "note": "bad index"},
            {"i": 3, "verdict": "banana", "note": "unknown verdict"},
        ]})
        out = update.smell_reasons(ITEMS)
        check("verdict 1 parses ok", out[0] == {"verdict": "ok", "note": ""})
        check("verdict 2 parses suspect with note", out[1] == {"verdict": "suspect", "note": "keep-shaped label"})
        check("out-of-range item number is ignored", all(k in (0, 1, 2) for k in out))
        check("an unknown verdict coerces to ok (fail-open)", out[2]["verdict"] == "ok")

        _sync_stub({"verdicts": []})
        check("empty verdicts list -> empty map", update.smell_reasons(ITEMS) == {})
        _sync_stub({"nonsense": True})
        check("missing verdicts key -> empty map", update.smell_reasons(ITEMS) == {})

        # First verdict for an item wins; a duplicate row cannot flip it.
        _sync_stub({"verdicts": [{"i": 1, "verdict": "ok"}, {"i": 1, "verdict": "suspect"}]})
        check("duplicate item number keeps the first verdict",
              update.smell_reasons(ITEMS)[0]["verdict"] == "ok")
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


def test_batch_path_and_fallback():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        update.SMELL_BATCH = True
        payload = json.dumps({"verdicts": [{"i": 2, "verdict": "suspect", "note": "topic label"}]})
        calls = {"batch": 0, "sync": 0}

        def fake_run(reqs, deadline=None, interval=20.0, label="batch"):
            calls["batch"] += 1
            check("batch gets exactly one request", len(reqs) == 1)
            check("batch request is keyed 'smell'", reqs[0]["custom_id"] == "smell")
            return {"smell": {"ok": True, "text": payload}}
        batch.run = fake_run
        update.anthropic_json = lambda body, label=None: calls.__setitem__("sync", calls["sync"] + 1) or {}
        out = update.smell_reasons(ITEMS)
        check("batch path parses the verdict", out.get(1, {}).get("verdict") == "suspect")
        check("batch path never calls the sync API", calls["sync"] == 0)

        def broken_run(reqs, deadline=None, interval=20.0, label="batch"):
            raise batch.BatchError("boom")
        batch.run = broken_run
        update.anthropic_json = lambda body, label=None: {"verdicts": [{"i": 1, "verdict": "suspect", "note": "n"}]}
        out = update.smell_reasons(ITEMS)
        check("batch error falls back to the synchronous call", out.get(0, {}).get("verdict") == "suspect")

        def failed_line_run(reqs, deadline=None, interval=20.0, label="batch"):
            return {"smell": {"ok": False, "type": "errored", "error": "x"}}
        batch.run = failed_line_run
        out = update.smell_reasons(ITEMS)
        check("a failed batch line falls back to the synchronous call",
              out.get(0, {}).get("verdict") == "suspect")
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


def test_select():
    drops = [{"reason": "criminal, out of scope"}, {"reason": "keep-shaped"}, {"reason": "  "},
             {"reason": "another suspect"}, {"reason": "yet another"}]
    verdicts = {0: {"verdict": "ok", "note": ""}, 1: {"verdict": "suspect", "note": "label"},
                3: {"verdict": "suspect", "note": "s2"}, 4: {"verdict": "suspect", "note": "s3"}}
    esc, annot = update.smell_select(drops, verdicts, cap=10)
    check("ok verdict stays ok", annot[0]["verdict"] == "ok")
    check("empty reason is suspect without a model verdict",
          annot[2] == {"verdict": "suspect", "note": "no reason recorded"})
    check("suspects escalate in order", esc == [1, 2, 3, 4])
    esc_capped, _ = update.smell_select(drops, verdicts, cap=2)
    check("cap bounds the escalations", esc_capped == [1, 2])
    esc_zero, annot_zero = update.smell_select(drops, verdicts, cap=0)
    check("cap 0 escalates nothing but still annotates", esc_zero == [] and len(annot_zero) == 5)
    _, annot_missing = update.smell_select([{"reason": "something"}], {})
    check("missing model verdict defaults to ok (fail-open)", annot_missing[0]["verdict"] == "ok")


def test_retro_selection():
    recs = [
        {"stage": "screen", "reason": "criminal"},
        {"stage": "triage", "reason": "topic label"},
        {"stage": "triage", "reason": "already audited", "smell": "ok"},
        {"stage": "pretriage", "reason": "immigration"},
        {"stage": "triage", "reason": "newest"},
    ]
    lines = [json.dumps(r) for r in recs] + ["", "not json {{{"]
    picked, parsed = smell_check.select_records(lines, stages=["triage"], all_flag=False, limit=10)
    check("retro picks only un-audited triage records",
          [r["reason"] for r in picked] == ["topic label", "newest"])
    check("corrupt and blank lines are dropped, valid ones parsed", len(parsed) == 5)
    check("selected records alias the parsed list (annotation reaches the rewrite)",
          picked[0] is parsed[1])
    picked_all, _ = smell_check.select_records(lines, stages=["triage"], all_flag=True, limit=10)
    check("SMELL_ALL re-audits annotated records", len(picked_all) == 3)
    picked_lim, _ = smell_check.select_records(lines, stages=["triage"], all_flag=True, limit=2)
    check("limit keeps the most recent records",
          [r["reason"] for r in picked_lim] == ["already audited", "newest"])
    ql = smell_check.queue_line({"cluster_id": 123, "name": "A v. B", "court": "ca11",
                                 "date": "2026-07-01"}, "keep-shaped")
    check("queue line is a forced bare cluster id", ql.startswith("123 !  # smell: keep-shaped"))


def main():
    print("smell prompt + parsing:")
    test_prompt_shape()
    test_verdict_parsing()
    test_batch_path_and_fallback()
    print("smell selection:")
    test_select()
    print("retro record selection:")
    test_retro_selection()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
