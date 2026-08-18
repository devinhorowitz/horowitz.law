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
    check("request uses the tunable output budget", body["max_tokens"] == update.SMELL_TOKENS)
    check("the budget clears ~200 tokens per chunk item",
          update.SMELL_TOKENS >= update.SMELL_CHUNK * 200)
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

        def fake_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            calls["batch"] += 1
            check("batch gets exactly one request", len(reqs) == 1)
            check("batch request is keyed 'smell-0'", reqs[0]["custom_id"] == "smell-0")
            return {"smell-0": {"ok": True, "text": payload}}
        batch.run = fake_run
        update.anthropic_json = lambda body, label=None: calls.__setitem__("sync", calls["sync"] + 1) or {}
        out = update.smell_reasons(ITEMS)
        check("batch path parses the verdict", out.get(1, {}).get("verdict") == "suspect")
        check("batch path never calls the sync API", calls["sync"] == 0)

        def broken_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            raise batch.BatchError("boom")
        batch.run = broken_run
        update.anthropic_json = lambda body, label=None: {"verdicts": [{"i": 1, "verdict": "suspect", "note": "n"}]}
        out = update.smell_reasons(ITEMS)
        check("batch error falls back to the synchronous call", out.get(0, {}).get("verdict") == "suspect")

        def failed_line_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            return {"smell-0": {"ok": False, "type": "errored", "error": "x"}}
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
    check("an un-judged drop gets NO annotation (never a fabricated 'ok')", 0 not in annot_missing)
    esc_none, annot_none = update.smell_select(drops, {})
    check("with no verdicts at all, only the empty-reason drop is annotated",
          list(annot_none) == [2] and esc_none == [2])


def test_chunking():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        many = [{"name": "N%d" % i, "court": "ctapp", "date": "2026-07-01", "reason": "r%d" % i}
                for i in range(update.SMELL_CHUNK * 2 + 5)]
        update.SMELL_BATCH = True

        def fake_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            check("chunked batch: one request per SMELL_CHUNK slice", len(reqs) == 3)
            check("chunk custom_ids are smell-<k>",
                  [r["custom_id"] for r in reqs] == ["smell-0", "smell-1", "smell-2"])
            # chunk 1 judges its second item (global index SMELL_CHUNK+1); chunk 2's line fails
            return {"smell-0": {"ok": True, "text": json.dumps({"verdicts": []})},
                    "smell-1": {"ok": True, "text": json.dumps(
                        {"verdicts": [{"i": 2, "verdict": "suspect", "note": "x"}]})},
                    "smell-2": {"ok": False, "type": "errored", "error": "boom"}}
        sync_calls = []

        def fake_sync(body, label=None):
            sync_calls.append(body)
            return {"verdicts": [{"i": 1, "verdict": "suspect", "note": "fallback"}]}
        batch.run, update.anthropic_json = fake_run, fake_sync
        out = update.smell_reasons(many)
        check("chunk-local item numbers map to global indices",
              out.get(update.SMELL_CHUNK + 1, {}).get("verdict") == "suspect")
        check("only the failed chunk falls back to a synchronous call", len(sync_calls) == 1)
        check("the fallback chunk's verdict lands at its global index",
              out.get(update.SMELL_CHUNK * 2, {}).get("note") == "fallback")

        def broken_sync(body, label=None):
            raise RuntimeError("api down")
        def dead_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            raise batch.BatchError("dead")
        batch.run, update.anthropic_json = dead_run, broken_sync
        check("total failure returns an EMPTY map (nothing judged, nothing invented)",
              update.smell_reasons(ITEMS) == {})

        def cfg_sync(body, label=None):
            raise update.ConfigError("bad model")
        update.anthropic_json = cfg_sync
        try:
            update.smell_reasons(ITEMS)
            check("ConfigError propagates out of smell_reasons", False)
        except update.ConfigError:
            check("ConfigError propagates out of smell_reasons", True)
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


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
    deferred = [json.dumps({"stage": "triage", "reason": "x", "smell": "suspect",
                            "smell_outcome": "deferred"})]
    picked_def, _ = smell_check.select_records(deferred, stages=["triage"], all_flag=False, limit=10)
    check("a deferred in-run escalation is re-audited by the retro pass", len(picked_def) == 1)


def test_retro_persistence():
    import tempfile
    real = (update.REJECT_PATH, update.KEY, update.SMELL_MODEL, smell_check.CHUNK,
            smell_check.OUT, smell_check.DRY_RUN, update.smell_reasons)
    tmp = tempfile.mkdtemp(prefix="smelltest")
    try:
        update.REJECT_PATH = os.path.join(tmp, "rej.jsonl")
        smell_check.OUT = os.path.join(tmp, "suspects.md")
        update.KEY, update.SMELL_MODEL = "test-key", "test-model"
        smell_check.CHUNK, smell_check.DRY_RUN = 2, False
        recs = [{"ts": "t", "stage": "triage", "cluster_id": 100 + i, "name": "Case %d" % i,
                 "court": "ctapp", "docket": "", "date": "2026-07-01", "url": "",
                 "reason": "reason %d" % i} for i in range(4)]
        with open(update.REJECT_PATH, "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")

        calls = [0]
        def scripted(items, deadline=None):
            calls[0] += 1
            if calls[0] == 1:   # chunk 1: one suspect, one ok
                return {0: {"verdict": "suspect", "note": "keep-shaped"},
                        1: {"verdict": "ok", "note": ""}}
            raise RuntimeError("api died mid-run")   # chunk 2: transport failure
        update.smell_reasons = scripted
        rc = smell_check.main()
        check("retro run survives a mid-run failure (exit 0)", rc == 0)
        lines = [json.loads(l) for l in open(update.REJECT_PATH) if l.strip()]
        check("chunk 1's verdicts persisted despite the later failure",
              lines[0].get("smell") == "suspect" and lines[1].get("smell") == "ok")
        check("failed chunk's records stay un-audited",
              "smell" not in lines[2] and "smell" not in lines[3])
        check("suspect carries the review outcome", lines[0].get("smell_outcome") == "review")
        check("suspects report exists with the queue line",
              os.path.exists(smell_check.OUT) and "100 !" in open(smell_check.OUT).read())
    finally:
        (update.REJECT_PATH, update.KEY, update.SMELL_MODEL, smell_check.CHUNK,
         smell_check.OUT, smell_check.DRY_RUN, update.smell_reasons) = real


def test_stage_config():
    """Screen drops were the blindest and the only unwatched gate: of 1,502 logged rejections the
    1,163 screen drops had never had a reason checked, because this audit read "triage" alone on
    the premise that screen reasons are category labels and so safe by construction. Twelve
    "In re: A v. B" captions dropped on the prefix alone disproved it.

    Two things are pinned. The stage list must come from the CONFIG FILE, not a hardcoded default
    or a GitHub Variable, with the env var demoted to a one-run override; and the audit must treat
    a reason the case NAME contradicts as suspect, which is what actually catches these -- the
    reasons name a recognized disqualifier ("dependency or juvenile proceeding") and are only
    detectable as wrong against a caption naming State Farm as a party."""
    import importlib, siteconfig
    check("screen is audited by default", "screen" in siteconfig.SMELL_STAGES)
    check("triage is still audited", "triage" in siteconfig.SMELL_STAGES)
    check("the default comes from the config file, not a literal in the script",
          'os.environ.get("SMELL_STAGES", "triage")' not in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "smell_check.py"), encoding="utf-8").read())

    saved = os.environ.get("SMELL_STAGES")
    try:
        os.environ["SMELL_STAGES"] = "pretriage"
        mod = importlib.reload(importlib.import_module("smell_check"))
        check("the env var still overrides for one run", mod.STAGES == ["pretriage"],
              "got %r" % (mod.STAGES,))
        os.environ.pop("SMELL_STAGES")
        mod = importlib.reload(importlib.import_module("smell_check"))
        check("and falls back to the config file when unset",
              mod.STAGES == list(siteconfig.SMELL_STAGES), "got %r" % (mod.STAGES,))
    finally:
        if saved is None:
            os.environ.pop("SMELL_STAGES", None)
        else:
            os.environ["SMELL_STAGES"] = saved
        importlib.reload(importlib.import_module("smell_check"))

    sysp = update.SMELL_SYSTEM
    check("a reason contradicted by the caption is suspect", "contradicted by the case" in sysp)
    check("the wrapper prefixes are named as carrying no subject",
          "'In re'" in sysp and "Ex parte" in sysp)
    check("the request header no longer claims every drop came from triage",
          "CASES DROPPED AT TRIAGE" not in update.smell_request(ITEMS)["messages"][0]["content"])


def main():
    print("smell prompt + parsing:")
    test_prompt_shape()
    test_verdict_parsing()
    test_batch_path_and_fallback()
    print("smell selection:")
    test_select()
    print("chunking:")
    test_chunking()
    print("retro record selection:")
    test_retro_selection()
    print("retro persistence:")
    test_retro_persistence()
    test_stage_config()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
