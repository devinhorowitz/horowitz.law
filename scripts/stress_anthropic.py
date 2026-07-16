#!/usr/bin/env python3
"""Fault-injection stress harness for update.anthropic_json -- the SYNCHRONOUS model-call path used
by screen/pretriage/triage/summarize and the guards/audits. No network, no key.

Stubs the single urllib.request.urlopen seam (and no-ops time.sleep so retries don't wait) and drives
anthropic_json against adversarial HTTP behavior and model output, asserting the contract every caller
relies on:

  - a valid response returns the parsed model JSON;
  - a max_tokens stop is caught and raised (never a truncated verdict acted on);
  - auth / model-not-found / credit HTTP errors raise ConfigError (which aborts the run cleanly);
  - a retryable status (429/5xx) or a transient network error retries and then succeeds, or -- once
    the attempts are spent -- raises;
  - malformed MODEL json (fenced, prose-wrapped, truncated, empty) either parses via parse_json's
    salvage or raises RuntimeError -- never returns a silently-wrong value;
  - nothing hangs and nothing leaks an exception type a caller would not expect.

Run directly: `python scripts/stress_anthropic.py [iterations]`. Exits nonzero on any failure.
"""
import io
import json
import os
import random
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name + (("  -- " + detail) if detail else ""))
        print("  FAIL " + name + (("  -- " + detail) if detail else ""))


class FakeResp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def http_error(code, body='{"error":{"type":"x"}}', retry_after=None):
    hdrs = {"retry-after": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", code, "err", hdrs,
                                  io.BytesIO(body.encode("utf-8")))


def ok_body(text, stop="end_turn"):
    return json.dumps({"content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": 5, "output_tokens": 3}, "stop_reason": stop})


class ScriptedURLOpen:
    """Pops one action per urlopen call: a bytes/str body -> FakeResp; an Exception -> raise it."""
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def __call__(self, req, timeout=0):
        self.calls += 1
        if not self.actions:
            raise AssertionError("urlopen called more times than scripted")
        a = self.actions.pop(0)
        if isinstance(a, Exception):
            raise a
        return FakeResp(a)


def with_urlopen(actions, body):
    real = update.urllib.request.urlopen
    update.urllib.request.urlopen = ScriptedURLOpen(actions)
    try:
        return body()
    finally:
        update.urllib.request.urlopen = real


BODY = {"model": "m", "max_tokens": 64, "system": "s", "messages": [{"role": "user", "content": "u"}]}


def call(actions):
    """Run anthropic_json under a scripted urlopen; return ('ok', value) or ('exc', ExceptionClass)."""
    try:
        return ("ok", with_urlopen(actions, lambda: update.anthropic_json(dict(BODY), "stress")))
    except Exception as e:
        return ("exc", type(e))


def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    update.time.sleep = lambda *a, **k: None    # no real backoff waits

    # --- fixed contract cases -------------------------------------------------------------------
    kind, val = call([ok_body('{"relevant": true, "significance": "high"}')])
    check("valid response -> parsed dict", kind == "ok" and val == {"relevant": True, "significance": "high"}, repr((kind, val)))

    kind, val = call([ok_body('{"relevant": true}', stop="max_tokens")])
    check("max_tokens stop -> RuntimeError (truncation guard)", kind == "exc" and val is RuntimeError, repr((kind, val)))

    kind, val = call([ok_body('```json\n{"a": 1}\n```')])
    check("fenced model json -> parsed", kind == "ok" and val == {"a": 1}, repr((kind, val)))

    kind, val = call([ok_body('here you go: {"a": 2} thanks')])
    check("prose-wrapped model json -> salvaged", kind == "ok" and val == {"a": 2}, repr((kind, val)))

    for bad in ("not json at all", "", "   ", "{unclosed", '{"a": '):
        kind, val = call([ok_body(bad)])
        check("unparseable model json -> RuntimeError (bad=%r)" % bad, kind == "exc" and val is RuntimeError, repr((kind, val)))

    for code in (401, 403):
        kind, val = call([http_error(code)])
        check("auth HTTP %d -> ConfigError" % code, kind == "exc" and val is update.ConfigError, repr((kind, val)))
    kind, val = call([http_error(404, '{"error":{"message":"model not_found"}}')])
    check("model-not-found -> ConfigError", kind == "exc" and val is update.ConfigError, repr((kind, val)))
    kind, val = call([http_error(400, '{"error":{"message":"insufficient credit balance"}}')])
    check("credit 400 -> ConfigError", kind == "exc" and val is update.ConfigError, repr((kind, val)))

    # Retryable then success (each retryable status), and exhaustion.
    for code in sorted(update.RETRY_STATUS):
        kind, val = call([http_error(code, retry_after="1"), ok_body('{"ok": 1}')])
        check("HTTP %d then success -> parsed" % code, kind == "ok" and val == {"ok": 1}, repr((kind, val)))
    kind, val = call([http_error(429)] * 5)
    check("429 x5 (exhausted) -> raises", kind == "exc", repr((kind, val)))
    kind, val = call([urllib.error.URLError("reset")] * 4 + [ok_body('{"ok": 2}')])
    check("network errors then success -> parsed", kind == "ok" and val == {"ok": 2}, repr((kind, val)))
    kind, val = call([TimeoutError("slow")] * 5)
    check("timeouts (exhausted) -> raises", kind == "exc", repr((kind, val)))

    # Malformed TOP-LEVEL body (a truncated/corrupt HTTP response, not JSON). Whatever it raises, it
    # must be a clean exception the callers already handle, not a hang or a wrong value.
    kind, val = call(['{"content": [{"type":"text","text":"'])
    check("truncated top-level body -> raises (no hang, no wrong value)", kind == "exc", repr((kind, val)))

    # Content-shape edge cases: empty content, no text blocks, mixed block types.
    kind, val = call([json.dumps({"content": [], "stop_reason": "end_turn"})])
    check("empty content -> RuntimeError (nothing to parse)", kind == "exc" and val is RuntimeError, repr((kind, val)))
    kind, val = call([json.dumps({"content": [{"type": "tool_use", "id": "x"}], "stop_reason": "end_turn"})])
    check("no text block -> RuntimeError", kind == "exc" and val is RuntimeError, repr((kind, val)))
    kind, val = call([json.dumps({"content": [{"type": "text", "text": '{"a":'}, {"type": "text", "text": '3}'}],
                                  "stop_reason": "end_turn"})])
    check("split text blocks concatenate then parse", kind == "ok" and val == {"a": 3}, repr((kind, val)))

    # --- randomized fuzz: random retry sequences + random model output, never hangs/leaks ---------
    rng = random.Random(99887766)
    ALLOWED = (RuntimeError, update.ConfigError)
    for _ in range(iters):
        seq = []
        for _ in range(rng.randint(0, 6)):
            r = rng.random()
            if r < 0.4:
                seq.append(http_error(rng.choice([429, 500, 502, 503, 529]), retry_after=rng.choice([None, "0", "1", "2"])))
            elif r < 0.55:
                seq.append(urllib.error.URLError("net"))
            elif r < 0.65:
                seq.append(TimeoutError("to"))
        # terminal action: a body (valid/garbage/truncated) or a fatal HTTP error
        term = rng.random()
        if term < 0.4:
            seq.append(ok_body(rng.choice(['{"ok": 1}', 'x', '', '```{"a":1}```', 'prose {"b":2} end',
                                           '{"trunc":', json.dumps({"deep": {"n": rng.randint(0, 9)}})]),
                                stop=rng.choice(["end_turn", "max_tokens"])))
        elif term < 0.6:
            seq.append(rng.choice([http_error(401), http_error(404, '{"error":"model not found"}'),
                                   http_error(400, '{"error":"credit"}')]))
        elif term < 0.8:
            seq.append(http_error(rng.choice([429, 500])))   # may exhaust
            seq += [http_error(429)] * 5
        else:
            seq.append('{"truncated response body')          # corrupt top-level
        kind, val = call(seq if seq else [ok_body('{"ok": 1}')])
        if kind == "ok":
            if not isinstance(val, (dict, list)):
                check("fuzz: ok result is parsed JSON", False, repr(val))
        else:
            if not (issubclass(val, ALLOWED) or issubclass(val, (json.JSONDecodeError, ValueError))):
                check("fuzz: exception is a clean/expected type", False, repr(val))

    if FAILS:
        print("\nFAILED (%d):" % len(set(FAILS)))
        for f in sorted(set(FAILS))[:40]:
            print("  - " + f)
        return 1
    print("anthropic_json stress: %d fixed + %d fuzz iterations" % (0, iters))
    print("ALL ANTHROPIC STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
