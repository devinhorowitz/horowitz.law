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
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch          # noqa: E402  (sys.path shim must run first)

FAILS = []
CASES = [0]


def check(name, cond, detail=""):
    CASES[0] += 1
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


def test_poll_progress():
    """A poll loop is where four dead runs were last seen (three treatment sweeps 2026-08-01,
    the daily funnel 2026-08-03 -- all exit 143, runner shutdown). It is also the quietest
    stretch of a run: minutes with nothing printed, so a death there looked identical to a
    death anywhere else. Every wait must leave a line, and it must carry RSS."""
    check("the first wait always logs, so a started batch is visible",
          batch._poll_log(1, every=5) is True)
    check("quiet between milestones", batch._poll_log(3, every=5) is False)
    check("every Nth wait logs", batch._poll_log(10, every=5) is True)
    check("a zero cadence neither divides by zero nor goes silent on wait 1",
          batch._poll_log(1, every=0) is True and batch._poll_log(4, every=0) is False)
    check("the default cadence is positive", batch.POLL_LOG_EVERY > 0, str(batch.POLL_LOG_EVERY))

    note = batch._rss_note()
    check("the rss fragment is empty or formatted",
          note == "" or note.startswith("; rss "), repr(note))

    import update as _u
    real = _u.rss_note
    _u.rss_note = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        check("a broken rss helper cannot break a batch poll", batch._rss_note() == "")
    finally:
        _u.rss_note = real


def test_poll_actually_prints(capture):
    """Drive poll() itself: the predicate being right is not the same as it being wired in."""
    fake = Scripted([
        json.dumps({"id": "b1", "processing_status": "in_progress"}),
        json.dumps({"id": "b1", "processing_status": "ended", "results_url": "u"}),
    ])
    out = with_send(fake, lambda: batch.poll("b1", interval=0.01, label="lbl"))
    text = capture()
    check("poll returns the ended object", out.get("processing_status") == "ended")
    check("and printed a wait line naming the label and batch",
          "lbl: waiting on batch b1" in text, text.strip()[:120] or "(no output)")


def main():
    # No real sleeping during polls.
    _orig_sleep = batch.time.sleep
    batch.time.sleep = lambda *_a, **_k: None

    print("poll progress:")
    test_poll_progress()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        test_poll_actually_prints(lambda: buf.getvalue())
    for ln in buf.getvalue().splitlines():
        if ln.startswith("  ok") or ln.startswith("  FAIL"):
            print(ln)

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

    # 3a. request()/from_body reject a custom_id the Batch API would 400 on (a colon), so a bad
    # id fails at build time in a test rather than as an HTTP 400 on the live job.
    ok = True
    for bad in ("10918352:fidelity", "has space", "a" * 65, ""):
        try:
            batch.request(bad, "m", "s", [{"role": "user", "content": "u"}], 8)
            ok = False
        except batch.BatchError:
            pass
    check("request() rejects an invalid custom_id (colon/space/too-long/empty)", ok)
    try:
        batch.from_body("10918352:fidelity", {"model": "m", "system": "s", "max_tokens": 8,
                                              "messages": [{"role": "user", "content": "u"}]})
        check("from_body rejects an invalid custom_id", False)
    except batch.BatchError:
        check("from_body rejects an invalid custom_id", True)
    check("request() accepts a hyphen custom_id", batch.request("10918352-fidelity", "m", "s",
          [{"role": "user", "content": "u"}], 8)["custom_id"] == "10918352-fidelity")

    # 3b. from_body(): adapt an update-style body dict, wrapping its system and keeping extras.
    fb = batch.from_body("d", {"model": "m2", "system": "SYS", "max_tokens": 400,
                               "messages": [{"role": "user", "content": "u"}], "output_config": {"effort": "high"}})
    check("from_body wraps the body system with cache_control",
          fb["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
          and fb["params"]["system"][0]["text"] == "SYS")
    check("from_body keeps model/max_tokens/extras",
          fb["custom_id"] == "d" and fb["params"]["model"] == "m2"
          and fb["params"]["max_tokens"] == 400 and fb["params"]["output_config"] == {"effort": "high"})

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

    # 7. collect() skips a malformed/truncated results line instead of crashing the whole batch and
    #    its caller's run -- a mid-stream network cut leaves the final JSONL line a partial object.
    #    The complete lines before AND after it must still be returned. (Stress-test regression.)
    ended = {"id": "batch_x", "processing_status": "ended", "results_url": batch.API + "/batch_x/results"}
    body = "\n".join([json.dumps(_succeeded("a", '{"ok":1}')),
                      '{"custom_id": "b", "result": {"type": "succ',   # truncated: must be skipped
                      json.dumps(_errored("c")),
                      ""])                                             # trailing blank line
    out = with_send(Scripted([body]), lambda: batch.collect(ended))
    check("collect skips a malformed results line without crashing", set(out) == {"a", "c"}, repr(set(out)))
    check("collect keeps the good line before the malformed one", out.get("a", {}).get("ok") is True)
    check("collect keeps the good line after the malformed one", out.get("c", {}).get("ok") is False)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d cases)" % CASES[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
