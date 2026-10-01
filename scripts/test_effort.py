#!/usr/bin/env python3
"""Hermetic tests for the per-tier reasoning effort the funnel sends (no network, no API key).

From 2026-09-23 model-watch judged an Opus candidate whose API default effort is `medium`
against an incumbent whose default is `high`, because no request named an effort at all, and
reported the gap as a regression. The fix is to send siteconfig.MODEL_EFFORT explicitly, in the
documented shape (`output_config: {"effort": ...}`, top-level, no beta header), from the request
builders production and the golden check share -- and only to a model documented to accept it,
since an effort sent to Haiku 4.5 is a 400.

  python scripts/test_effort.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import siteconfig  # noqa: E402
import update  # noqa: E402
import batch  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Pins:
    """Point the four tier pins at the given ids for one test, restoring afterwards."""
    def __init__(self, **pins):
        self.pins = pins

    def __enter__(self):
        self.saved = {k: getattr(update, k) for k in self.pins}
        for k, v in self.pins.items():
            setattr(update, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(update, k, v)
        return False


def test_support_rule():
    print("which models take an effort (the documented rule)")
    for mid in ("claude-opus-5", "claude-opus-5-5", "claude-opus-4-5", "claude-opus-4-8",
                "claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4-5-20251101"):
        check("%s accepts effort high" % mid, update.effort_supported(mid, "high"))
    for mid in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5", "claude-opus-4-1",
                "claude-fable-5", "claude-3-5-sonnet-20241022", "", None, "claude-opus-5-preview"):
        check("%r gets no effort (documented no, or unconfirmed)" % (mid,), not update.effort_supported(mid, "high"))
    check("xhigh needs Opus 4.7+", update.effort_supported("claude-opus-4-7", "xhigh")
          and not update.effort_supported("claude-opus-4-6", "xhigh"))
    check("xhigh needs Sonnet 5+", update.effort_supported("claude-sonnet-5", "xhigh")
          and not update.effort_supported("claude-sonnet-4-6", "xhigh"))
    check("Opus 4.5 is documented for low/medium/high only",
          update.effort_supported("claude-opus-4-5", "high")
          and not update.effort_supported("claude-opus-4-5", "max")
          and not update.effort_supported("claude-opus-4-5", "xhigh"))
    check("an unknown level is never supported", not update.effort_supported("claude-opus-5", "turbo"))
    check("documented models answer True/False; older ones are a documented no",
          update.effort_documented("claude-opus-5") is True
          and update.effort_documented("claude-haiku-4-5") is False
          and update.effort_documented("claude-sonnet-4-5") is False)
    for mid in ("claude-sonnet-5-5", "claude-opus-6", "claude-haiku-5", "claude-fable-5", "claude-mystery-1"):
        check("%s is not covered by the docs (None), never extrapolated" % mid,
              update.effort_documented(mid) is None)


class Caps:
    """Stub the Models API capabilities lookup (hermetic: no network even with a key set)."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __enter__(self):
        self.saved = (update.fetch_capabilities, dict(update._MODEL_CAPS), set(update._EFFORT_WARNED))
        update._MODEL_CAPS.clear()
        update._EFFORT_WARNED.clear()

        def fake(model):
            self.calls.append(model)
            return self.answers.get(model)
        update.fetch_capabilities = fake
        return self

    def __exit__(self, *exc):
        update.fetch_capabilities = self.saved[0]
        update._MODEL_CAPS.clear()
        update._MODEL_CAPS.update(self.saved[1])
        update._EFFORT_WARNED.clear()
        update._EFFORT_WARNED.update(self.saved[2])
        return False


def test_undocumented_models():
    """A model the docs do not cover (the Sonnet 5.5 candidate) gets effort ONLY when the Models API
    confirms it. Docs silent and API silent: no effort, and a logged warning that the comparison
    may be unmatched -- never an extrapolation from "Sonnet 4.6+"."""
    print("models the docs do not cover")
    yes = {"effort": {"supported": True, "high": {"supported": True}}}
    with Caps({"claude-sonnet-5-5": yes}) as c:
        check("the API confirms effort: it is sent", update.effort_supported("claude-sonnet-5-5", "high"))
        update.effort_supported("claude-sonnet-5-5", "high")
        check("...and the API is asked once per process", c.calls == ["claude-sonnet-5-5"], str(c.calls))
        check("documented models never hit the API", update.effort_supported("claude-opus-5", "high")
              and c.calls == ["claude-sonnet-5-5"], str(c.calls))
    with Caps({"claude-sonnet-5-5": {"effort": {"supported": True, "high": {"supported": False}}}}):
        check("the API denies the level: none sent", not update.effort_supported("claude-sonnet-5-5", "high"))
    with Caps({"claude-sonnet-5-5": {"effort": {"supported": False}}}):
        check("the API denies effort: none sent", not update.effort_supported("claude-sonnet-5-5", "high"))
    import contextlib
    import io
    for label, answer in (("no capabilities object", None), ("capabilities without effort", {"vision": {}})):
        with Caps({"claude-sonnet-5-5": answer}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = update.effort_supported("claude-sonnet-5-5", "high")
                update.effort_supported("claude-sonnet-5-5", "high")
            check("%s: no effort is sent" % label, got is False)
            log = buf.getvalue()
            check("%s: it is logged once, naming the unmatched comparison" % label,
                  log.count("claude-sonnet-5-5") == 1 and "unmatched" in log, log)
    with Caps({}), Pins(TRIAGE_MODEL="claude-sonnet-5-5"):
        t = update.triage_request("A v. B", "S1", "text")
    check("an unconfirmed triage candidate is sent no output_config", "output_config" not in t, str(t.keys()))
    with Caps({"claude-sonnet-5-5": yes}), Pins(TRIAGE_MODEL="claude-sonnet-5-5"):
        t = update.triage_request("A v. B", "S1", "text")
    check("a confirmed one is sent the tier's effort", t.get("output_config") == {"effort": "high"},
          str(t.get("output_config")))
    with Caps({}):
        update.remember_capabilities("claude-sonnet-5-5", yes)
        check("capabilities model_watch already listed are reused", update.effort_supported("claude-sonnet-5-5"))
    saved_key = update.KEY
    try:
        update.KEY = ""
        check("with no API key the lookup is skipped (None)", update.fetch_capabilities("claude-sonnet-5-5") is None)
    finally:
        update.KEY = saved_key


def test_config():
    print("siteconfig.MODEL_EFFORT")
    check("every funnel tier has an entry",
          set(siteconfig.MODEL_EFFORT) == {"summarize", "triage", "pretriage", "screen"},
          str(sorted(siteconfig.MODEL_EFFORT)))
    check("summarize and triage pin high (the incumbents' documented default: no change today)",
          siteconfig.MODEL_EFFORT["summarize"] == "high" and siteconfig.MODEL_EFFORT["triage"] == "high")
    check("the Haiku tiers send none", not siteconfig.MODEL_EFFORT["screen"]
          and not siteconfig.MODEL_EFFORT["pretriage"])
    check("keyed by tier, never by a model id (a bump would leave an id key behind)",
          not any(k.startswith("claude-") for k in siteconfig.MODEL_EFFORT))


def test_request_shapes():
    print("the request builders")
    with Pins(MODEL="claude-opus-5-5", TRIAGE_MODEL="claude-sonnet-5",
              SCREEN_MODEL="claude-haiku-4-5", PRETRIAGE_MODEL="claude-haiku-4-5"):
        s = update.summarize_request("ga", "A v. B", "S1", "2026-01-01", "text", "note")
        t = update.triage_request("A v. B", "S1", "text")
        sc = update.screen_request("A v. B", "S1", "text")
        pt = update.pretriage_request("A v. B", "S1", "text")
    check("summarize sends output_config.effort high", s.get("output_config") == {"effort": "high"}, str(s.get("output_config")))
    check("triage sends output_config.effort high", t.get("output_config") == {"effort": "high"}, str(t.get("output_config")))
    check("no top-level `effort` key (not the documented shape)", "effort" not in s and "effort" not in t)
    check("the Haiku screen sends no output_config", "output_config" not in sc, str(sc.keys()))
    check("the Haiku pretriage sends no output_config", "output_config" not in pt, str(pt.keys()))
    check("no thinking, temperature or other sampling field is added",
          not ({"thinking", "temperature", "top_p", "top_k"} & (set(s) | set(t) | set(sc) | set(pt))))

    with Caps({}), Pins(MODEL="claude-mystery-1", TRIAGE_MODEL="claude-haiku-4-5"):
        s = update.summarize_request("ga", "A v. B", "S1", "2026-01-01", "text", "note")
        t = update.triage_request("A v. B", "S1", "text")
    check("an undocumented model gets no effort (its default, the old behaviour)",
          "output_config" not in s and "output_config" not in t)

    saved = dict(siteconfig.MODEL_EFFORT)
    try:
        siteconfig.MODEL_EFFORT["screen"] = "high"
        with Pins(SCREEN_MODEL="claude-haiku-4-5"):
            sc = update.screen_request("A v. B", "S1", "text")
        check("even if configured, Haiku 4.5 is never sent an effort (it 400s)", "output_config" not in sc)
    finally:
        siteconfig.MODEL_EFFORT.clear()
        siteconfig.MODEL_EFFORT.update(saved)


def test_batch_carries_it():
    print("the batch path")
    with Pins(MODEL="claude-opus-5", TRIAGE_MODEL="claude-sonnet-5"):
        line = batch.from_body("c1", update.summarize_request("ga", "A", "S", "d", "t", "n"))
        tline = batch.from_body("c2", update.triage_request("A", "S", "t"))
    check("a batched summarize carries the same output_config", line["params"].get("output_config") == {"effort": "high"})
    check("a batched triage carries the same output_config", tline["params"].get("output_config") == {"effort": "high"})


def test_sync_path_sends_it():
    print("the synchronous path")
    sent = []
    orig = update.anthropic_json
    update.anthropic_json = lambda body, label="call": sent.append((label, body)) or {"areas": []}
    try:
        with Pins(MODEL="claude-opus-5", TRIAGE_MODEL="claude-sonnet-5", SCREEN_MODEL="claude-haiku-4-5",
                  PRETRIAGE_MODEL="claude-haiku-4-5"):
            update.summarize("ga", "A", "S", "d", "t", "n")
            update.triage("A", "S", "t")
            update.screen("A", "S", "t")
            update.pretriage("A", "S", "t")
    finally:
        update.anthropic_json = orig
    got = {label: body.get("output_config") for label, body in sent}
    check("summarize() and triage() post the effort; screen() and pretriage() do not",
          got == {"summarize": {"effort": "high"}, "triage": {"effort": "high"},
                  "screen": None, "pretriage": None}, str(got))


def main():
    print("effort:")
    # Hermetic: no test may reach the Models API, even when ANTHROPIC_API_KEY is set.
    update.fetch_capabilities = lambda model: None
    for t in (test_support_rule, test_undocumented_models, test_config, test_request_shapes, test_batch_carries_it,
              test_sync_path_sends_it):
        t()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
