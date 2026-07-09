#!/usr/bin/env python3
"""Message Batches transport for the opinions pipeline.

The Batch API bills every input and output token at 50% of the standard rate, so
at scale each tier's model calls cost half as much as the synchronous path in
update.anthropic_json. That is the lever that keeps the funnel cheap as coverage
grows past Georgia to more courts and states.

A batch is asynchronous by construction: you submit many Messages API requests as
one job, it runs server-side (usually minutes, up to 24h), then you collect the
results keyed by your own `custom_id`. This module gives the two shapes a caller
needs:

  * blocking -- `run(requests, deadline=...)` submits, polls until the batch ends
    (never past the deadline), and returns {custom_id: result}. On timeout it
    raises BatchTimeout carrying the batch id, so the caller can persist the id
    and collect it on a later run instead of losing the work; and
  * async    -- `submit()` returns the id now, `status()`/`collect()` finish it on
    a subsequent run once it has ended.

Pure standard library and no project imports (a safe leaf like cl_rate), so any
script can depend on it without a cycle. Auth mirrors update.anthropic_json:
ANTHROPIC_API_KEY plus anthropic-version, and a retry on 429/5xx. Batches is GA,
so no beta header. Every network call funnels through the single `_send` seam,
which the tests stub, so the submit/poll/collect logic runs with no network.
"""
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages/batches"
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
RETRY_STATUS = {429, 500, 502, 503, 529}   # same set update.anthropic_json retries


class BatchError(RuntimeError):
    """A batch API call failed (non-retryable HTTP, or retries exhausted)."""


class BatchTimeout(RuntimeError):
    """The batch had not ended by the caller's deadline. Carries `batch_id` so the
    caller can record it and collect the results on a later run rather than losing
    the (already-billed) work."""

    def __init__(self, batch_id, message):
        super().__init__(message)
        self.batch_id = batch_id


def _send(method, url, body=None, label="batch"):
    """One HTTP round trip, returning the response body as text. Retries 429/5xx
    with capped exponential backoff, exactly like update.anthropic_json. This is
    the only place the module touches the network; tests stub it."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"content-type": "application/json", "x-api-key": KEY,
                         "anthropic-version": VERSION})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last = "%s %s -> HTTP %s: %s" % (label, method, e.code, (detail[:400] or e.reason))
            if e.code in RETRY_STATUS and attempt < 4:
                time.sleep(min(2 ** attempt * 2, 30)); continue
            raise BatchError(last)
        except urllib.error.URLError as e:
            last = "%s %s -> %s" % (label, method, e)
            if attempt < 4:
                time.sleep(min(2 ** attempt * 2, 30)); continue
            raise BatchError(last)
    raise BatchError(last or "batch http failed")


def request(custom_id, model, system, messages, max_tokens, **extra):
    """Build one batch request line. Applies the same system-prompt cache wrap as
    update.anthropic_json, so a large static system prompt bills at the cache-read
    rate across the batch. `extra` passes through any other Messages params
    (thinking, output_config, tools, ...)."""
    params = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if isinstance(system, str):
        params["system"] = [{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}]
    elif system is not None:
        params["system"] = system
    params.update(extra)
    return {"custom_id": custom_id, "params": params}


def submit(requests, label="batch"):
    """POST a list of {custom_id, params} request lines. Returns the batch id."""
    if not requests:
        raise BatchError("submit: no requests")
    obj = json.loads(_send("POST", API, {"requests": requests}, label))
    return obj["id"]


def status(batch_id, label="batch"):
    """Fetch the batch object (its `processing_status`, `results_url` once ended)."""
    return json.loads(_send("GET", "%s/%s" % (API, batch_id), None, label))


def collect(batch_obj, label="batch"):
    """From an ended batch object, fetch the results and key them by custom_id.

    Each value is {"ok": True, "text", "usage", "stop_reason"} for a succeeded
    request, or {"ok": False, "type", "error"} for an errored/canceled/expired one,
    so a caller sees a per-request failure without one bad line sinking the batch."""
    if batch_obj.get("processing_status") != "ended":
        raise BatchError("collect: batch %s not ended (status=%s)"
                         % (batch_obj.get("id"), batch_obj.get("processing_status")))
    url = batch_obj.get("results_url")
    if not url:
        raise BatchError("collect: ended batch %s has no results_url" % batch_obj.get("id"))
    out = {}
    for line in _send("GET", url, None, label).splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        cid = rec.get("custom_id")
        res = rec.get("result") or {}
        if res.get("type") == "succeeded":
            msg = res.get("message") or {}
            txt = "".join(b.get("text", "") for b in (msg.get("content") or [])
                          if b.get("type") == "text")
            out[cid] = {"ok": True, "text": txt, "usage": msg.get("usage") or {},
                        "stop_reason": msg.get("stop_reason")}
        else:
            out[cid] = {"ok": False, "type": res.get("type"), "error": res.get("error")}
    return out


def poll(batch_id, deadline=None, interval=20.0, label="batch"):
    """Block until the batch ends, returning the ended batch object. Never sleeps
    past `deadline`; if the batch is still running when the deadline passes, raise
    BatchTimeout(batch_id) so the caller can defer collection to a later run."""
    while True:
        obj = status(batch_id, label)
        if obj.get("processing_status") == "ended":
            return obj
        if deadline is not None and time.time() >= deadline:
            raise BatchTimeout(batch_id, "batch %s still %s at deadline"
                               % (batch_id, obj.get("processing_status")))
        nap = interval
        if deadline is not None:
            nap = max(1.0, min(interval, deadline - time.time()))
        time.sleep(nap)


def run(requests, deadline=None, interval=20.0, label="batch"):
    """Submit, poll to completion, and collect -- the blocking convenience path.
    Returns {custom_id: result}. Raises BatchTimeout (carrying the batch id) if the
    deadline passes first, and BatchError on a transport failure."""
    return collect(poll(submit(requests, label), deadline=deadline,
                        interval=interval, label=label), label)
