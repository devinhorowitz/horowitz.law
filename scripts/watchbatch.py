#!/usr/bin/env python3
"""Run budget and batch carry for the Legislative & Regulatory Watch (legislation.yml).

Each of the four watches -- legislation.py, regulations.py, courtrules.py, ethics.py -- sends its
Opus pass to Anthropic as one Message Batch and then waits for the batch to finish. Batch latency
ranges from minutes to over an hour. On 2026-09-27 (run 36318024609) two slow batches together ran
past the 60-minute job timeout. The job was killed before render, the review PR or the bookkeeping
could run, so nothing was saved. The paid batch ids were only printed to the log, so the next run
would have screened the same bills again and paid for the same writes a second time.

This module provides two things.

BUDGET. `Budget.for_step(watch)` ends WATCH_STEP_MARGIN_SEC before the watch's own workflow step
times out (siteconfig.WATCH_STEP_MIN, the same number as the step's `timeout-minutes`).
  * `budget.deadline(own_sec)` is the deadline a batch wait uses: the watch's own limit
    (*_BATCH_SEC) or the end of the step, whichever comes first.
  * `budget.low()` tells a synchronous model loop to stop starting new calls.
  * The step limits plus the reserve fit inside the job timeout (test_watchbatch.py checks this).
    So a slow watch reaches its own deadline and carries its batch, and the steps after it still
    run and save their state.

CARRY. A batch still running at its deadline is not abandoned. `CarryBook` records it in
watch_batches.json with:
  * the batch id,
  * a timestamp,
  * the item each custom_id stands for: a bill at one change_hash, a Federal Register document, or
    a page at one content hash.
The id is written the moment the batch is submitted, before the wait, so a step that is killed
during the wait still leaves the record behind.

The next run calls `collect()` first. The watch then applies a result only to the exact item the
carry recorded for that custom_id. A result is never used for different current work: the opinions
funnel's carry had that flaw, where a resumed batch could replace the run's own requests. A result
the watch cannot use is "re-queued", meaning the item goes through the normal pass again. That
covers an expired, errored or unparseable result, and an item that has changed since the batch was
submitted. A carry older than WATCH_CARRY_MAX_AGE_DAYS is dropped with a log line, because
Anthropic keeps batch results for only 29 days.

watch_batches.json is one file for all four watches, keyed by watch. It is a separate file from
the watches' *_state.json files because a carry has to reach main on every run. On a run that
drafts cards, the state files travel only on the unmerged bot/legislation-review branch. A carry
stored there would be invisible to the next run, which starts from main, and the paid work would
be lost again. The workflow commits this file straight to main on every non-dry run.

The batch transport lives in batch.py. This module adds no network calls of its own. Tests stub
batch.status / batch.collect / batch.run and set `clock`.
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import siteconfig  # noqa: E402

CARRY_PATH = os.path.join(REPO, "watch_batches.json")


def clock():
    """The wall clock. A module-level seam so tests can move time without sleeping."""
    return time.time()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Budget.                                                                      #
# --------------------------------------------------------------------------- #
class Budget:
    """The time a watch has left in its workflow step. `end` is an epoch second, or None for
    unbounded (a direct or test call that sets no budget behaves exactly as before)."""

    def __init__(self, end=None):
        self.end = end

    @classmethod
    def for_step(cls, watch):
        mins = (siteconfig.WATCH_STEP_MIN or {}).get(watch)
        if not mins:
            return cls(None)
        return cls(clock() + mins * 60 - siteconfig.WATCH_STEP_MARGIN_SEC)

    def left(self):
        return float("inf") if self.end is None else self.end - clock()

    def deadline(self, own_sec):
        """A batch wait's deadline: the watch's own limit, or the end of the step if sooner."""
        own = clock() + own_sec
        return own if self.end is None else min(own, self.end)

    def low(self, floor=None):
        """True once a synchronous model loop should stop starting calls."""
        floor = siteconfig.WATCH_SYNC_FLOOR_SEC if floor is None else floor
        return self.left() < floor


# --------------------------------------------------------------------------- #
# Carry file.                                                                  #
# --------------------------------------------------------------------------- #
def _load_all(path=None):
    path = path or CARRY_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        # A corrupt file must not stop the watch; it is rewritten on the next save. The carries in it
        # are lost, which costs at most one re-paid batch -- the pre-carry behavior.
        print("  ! %s unreadable (%s); starting with no carried batches"
              % (os.path.basename(path), e), flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def _valid(rec):
    return (isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]
            and isinstance(rec.get("items"), dict))


def load_carries(watch, path=None):
    recs = _load_all(path).get(watch)
    return [r for r in recs if _valid(r)] if isinstance(recs, list) else []


def save_carries(watch, carries, path=None):
    """Rewrite this watch's entry, leaving the other watches' entries as they are. The file is
    always written (as `{}` when empty) so the workflow's `git add` always finds it."""
    import safeio
    path = path or CARRY_PATH
    data = _load_all(path)
    if carries:
        data[watch] = carries
    else:
        data.pop(watch, None)
    safeio.atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


class CarryBook:
    """One watch's carried batches for one run.

    `persist` is True only for an --apply run. A dry run still reads and collects carries, so its
    preview includes carried results, but it never writes the file."""

    def __init__(self, watch, carries=None, persist=False, path=None):
        self.watch = watch
        self.carries = list(carries or [])
        self.persist = persist
        self.path = path

    @classmethod
    def load(cls, watch, persist=False, path=None):
        return cls(watch, load_carries(watch, path), persist=persist, path=path)

    # ---- the start of a run: collect what earlier runs paid for ----
    def collect(self, label):
        """Check every carried batch once. Returns (ready, inflight).

        `ready` lists the batches that have ended, each {"id", "items", "results"}, with `results`
        restricted to the custom_ids the carry recorded. An id the carry did not record is ignored,
        never matched to other work.

        `inflight` maps custom_id -> item for batches still running. The watch must leave those
        items alone: not re-submit them, not mark them seen. They stay carried.

        A carry that is too old, or that the API no longer knows (a 4xx), is dropped with a log
        line, and its items are processed again as usual. A transport failure (5xx, network) keeps
        the carry for the next run."""
        now = clock()
        max_age = siteconfig.WATCH_CARRY_MAX_AGE_DAYS * 86400
        ready, inflight, keep = [], {}, []
        import batch
        for rec in self.carries:
            bid, items = rec["id"], rec["items"]
            at = _parse_iso(rec.get("at"))
            if at is None or now - at > max_age:
                print("  . %s: dropping carried batch %s (submitted %s, past the %d-day limit for "
                      "collecting results); its %d item(s) are processed again"
                      % (label, bid, rec.get("at") or "?", siteconfig.WATCH_CARRY_MAX_AGE_DAYS,
                         len(items)), flush=True)
                continue
            try:
                obj = batch.status(bid, label)
                if obj.get("processing_status") != "ended":
                    print("  . %s: carried batch %s still %s; its %d item(s) stay carried"
                          % (label, bid, obj.get("processing_status") or "?", len(items)), flush=True)
                    keep.append(rec)
                    inflight.update(items)
                    continue
                results = batch.collect(obj, label)
            except Exception as e:   # BatchError, or a garbled response body (ValueError)
                if isinstance(e, batch.BatchError) and "HTTP 4" in str(e):
                    print("  . %s: dropping carried batch %s (%s); its %d item(s) are processed again"
                          % (label, bid, e, len(items)), flush=True)
                else:
                    print("  . %s: could not check carried batch %s (%s); keeping it for next run"
                          % (label, bid, e), flush=True)
                    keep.append(rec)
                    inflight.update(items)
                continue
            stray = [c for c in results if c not in items]
            if stray:
                print("  . %s: carried batch %s returned %d result(s) for ids it did not carry; ignored"
                      % (label, bid, len(stray)), flush=True)
            ready.append({"id": bid, "items": items,
                          "results": {c: results[c] for c in items if c in results}})
        self.carries = keep
        return ready, inflight

    @staticmethod
    def report(rec, applied, requeued):
        print("  . collected carried batch %s (%d results applied, %d re-queued)"
              % (rec["id"], applied, requeued), flush=True)

    # ---- this run's batch ----
    def run(self, reqs, items, deadline, label):
        """Submit `reqs` and wait until `deadline`. `items` maps each request's custom_id to the item
        it stands for. Returns {custom_id: result} once the batch ends, or None when it did not
        finish. A batch submitted but not finished is carried, and the carry is written to disk at
        submit time (an --apply run), so a step killed during the wait still leaves the record."""
        import batch
        held = {}

        def on_submit(bid):
            rec = {"id": bid, "label": label, "at": _iso(clock()), "n": len(items), "items": items}
            held["rec"] = rec
            self.carries.append(rec)
            if self.persist:
                # Append to what is ON DISK, not this book's in-memory list: carries collected at the
                # start of this run are already gone from memory, but their results are not saved
                # until the run ends, so the disk copy has to keep them until then.
                save_carries(self.watch, load_carries(self.watch, self.path) + [rec], self.path)

        try:
            results = batch.run(reqs, deadline=deadline, label=label, on_submit=on_submit)
        except batch.BatchTimeout as e:
            if "rec" not in held:
                on_submit(e.batch_id)
            print("  ! %s: batch %s still running at the deadline; carried to next run (%d item(s))"
                  % (label, e.batch_id, len(items)), flush=True)
            return None
        except batch.BatchError as e:
            if "rec" in held:
                print("  ! %s: lost track of batch %s (%s); carried to next run (%d item(s))"
                      % (label, held["rec"]["id"], e, len(items)), flush=True)
            else:
                print("  ! %s: batch failed (%s); %d item(s) retry next run" % (label, e, len(items)),
                      flush=True)
            return None
        if "rec" in held:
            self.carries.remove(held["rec"])
        return results

    # ---- the end of a run ----
    def announce(self):
        if self.carries:
            print("  . carrying %d batch(es) to next run: %s"
                  % (len(self.carries), ", ".join(r["id"] for r in self.carries)), flush=True)

    def save(self):
        if self.persist:
            save_carries(self.watch, self.carries, self.path)
