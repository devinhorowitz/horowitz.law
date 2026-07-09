#!/usr/bin/env python3
"""Hermetic unit test for the Batch API transport (scripts/batch.py).

Drives the whole submit -> poll -> collect state machine with no network: it stubs
the single `batch._send` seam with a scripted fake, and no-ops time.sleep so polling
runs instantly. Covers: the happy path result mapping, a per-request errored line,
the request() cache-wrap, a poll that ends after a couple of in-progress checks, the
BatchTimeout deferral path, and the _send retry-on-429 (the one test that exercises
the real urllib path with a fake urlopen).

Run directly: `python scripts/test_batch.py`.
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch          # noqa: E402  (sys.path shim must run first)

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Scripted:
    """A fake batch._send: pops a canned text reply per call and records the calls,
    so a test can assert the submit/poll/collect sequence without a network."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, body=None, label="batch"):
        self.calls.append((method, url, body))
        if not self.replies:
            raise AssertionError("unexpected extra _send call: %s %s" % (method, url))
        r = self.replies.pop(0)
        return r() if callable(r) else r


def _succeeded(cid, text):
    return {"custom_id": cid, "result": {"type": "succeeded",
            "message": {"content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10, "output_tokens": 3},
                        "stop_reason": "end_turn"}}}


def _errored(cid, etype="invalid_request"):
    return {"custom_id": cid, "result": {"type": "errored", "error": {"type": etype}}}


def with_send(fake, fn):
    orig = batch._send
    batch._send = fake
    try:
        return fn()
    finally:
        batch._send = orig


def main():
    # No real sleeping during polls.
    _orig_sleep = batch.time.sleep
    batch.time.sleep = lambda *_a, **_k: None

    # 1. run(): submit -> one in_progress -> ended -> collect two lines (one ok, one errored).
    results_url = "https://api.anthropic.com/v1/messages/batches/batch_1/results"
    jsonl = "\n".join(json.dumps(x) for x in [_succeeded("a", '{"ok":1}'), _errored("b")])
    fake = Scripted([
        json.dumps({"id": "batch_1", "processing_status": "in_progress"}),          # submit POST
        json.dumps({"id": "batch_1", "processing_status": "in_progress"}),          # poll #1
        json.dumps({"id": "batch_1", "processing_status": "ended", "results_url": results_url}),  # poll #2
        jsonl,                                                                       # collect GET
    ])
    out = with_send(fake, lambda: batch.run([batch.request("a", "m", "sys", [{"role": "user", "content": "x"}], 16)],
                                            interval=0.01, label="t"))
    check("run maps succeeded custom_id to text", out.get("a", {}).get("text") == '{"ok":1}', repr(out.get("a")))
    check("run marks a succeeded line ok=True", out.get("a", {}).get("ok") is True)
    check("run surfaces usage on success", out.get("a", {}).get("usage", {}).get("output_tokens") == 3)
    check("run marks an errored line ok=False", out.get("b", {}).get("ok") is False)
    check("run passes the errored type through", out.get("b", {}).get("type") == "errored")
    check("submit POSTed to the batches endpoint", fake.calls[0][0] == "POST" and fake.calls[0][1] == batch.API)
    check("collect GETs the results_url", fake.calls[-1][1] == results_url)

    # 2. The POST body carried the request line with the system cache-wrap applied.
    posted = json.loads(json.dumps(fake.calls[0][2]))  # deep copy of the recorded body
    line = posted["requests"][0]
    sys_wrap = line["params"]["system"]
    check("submit sends a requests[] body", isinstance(posted.get("requests"), list) and line["custom_id"] == "a")
    check("request() wraps a str system with cache_control",
          isinstance(sys_wrap, list) and sys_wrap[0]["cache_control"] == {"type": "ephemeral"}
          and sys_wrap[0]["text"] == "sys")

    # 3. request(): a list system passes through unwrapped; extra params pass through.
    req = batch.request("c", "m", [{"type": "text", "text": "s"}], [{"role": "user", "content": "u"}], 32,
                        output_config={"effort": "low"})
    check("request() leaves a list system unwrapped", req["params"]["system"] == [{"type": "text", "text": "s"}])
    check("request() passes extra params through", req["params"]["output_config"] == {"effort": "low"})

    # 4. collect() rejects a batch that has not ended.
    try:
        batch.collect({"id": "b", "processing_status": "in_progress"})
        check("collect rejects a non-ended batch", False, "no error raised")
    except batch.BatchError:
        check("collect rejects a non-ended batch", True)

    # 5. poll() past its deadline on a still-running batch raises BatchTimeout(batch_id).
    stuck = Scripted([json.dumps({"id": "batch_9", "processing_status": "in_progress"})])
    def _poll_past_deadline():
        return batch.poll("batch_9", deadline=batch.time.time() - 1, interval=0.01, label="t")
    try:
        with_send(stuck, _poll_past_deadline)
        check("poll raises BatchTimeout at the deadline", False, "no timeout raised")
    except batch.BatchTimeout as e:
        check("poll raises BatchTimeout at the deadline", True)
        check("BatchTimeout carries the batch id", e.batch_id == "batch_9", e.batch_id)

    # 6. _send retries a 429 then succeeds (exercises the real urllib path via a fake urlopen).
    batch.time.sleep = lambda *_a, **_k: None
    state = {"n": 0}

    class _OK:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"id":"batch_retry","processing_status":"in_progress"}'

    def fake_urlopen(req, timeout=0):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, io.BytesIO(b'{"error":"slow down"}'))
        return _OK()

    orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        bid = batch.submit([batch.request("z", "m", "s", [{"role": "user", "content": "x"}], 8)], label="t")
        check("_send retries 429 then succeeds", bid == "batch_retry" and state["n"] == 2, "n=%d" % state["n"])
    finally:
        urllib.request.urlopen = orig_urlopen
        batch.time.sleep = _orig_sleep

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (15 cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
