#!/usr/bin/env python3
"""Hermetic tests for the Legislative & Regulatory Watch's run budget and batch carry
(scripts/watchbatch.py), and for the legislation.yml wiring that depends on them.

THE FAILURE THIS EXISTS FOR. Run 36318024609 (2026-09-27). Four watches ran in one 60-minute job,
each with its own 30-minute batch wait. The statutes batch used all 30 minutes, then the court-rules
batch waited another 25, and the job was killed. Render, the review PR and the bookkeeping never ran,
so the finished statutes and regulations state was thrown away along with everything else. The
deferred statutes batch, already paid for, was recorded nowhere.

What is pinned here:
  * Budget: a batch wait ends at the watch's own limit or the step's, whichever comes first, and a
    synchronous loop is told to stop when the step is nearly out of time.
  * CarryBook.collect: an ended batch's results come back only for the custom_ids the carry
    recorded. A running batch stays carried and its items are reported as in flight. A too-old
    carry is dropped with a log line. A 4xx drops the carry; a transport failure keeps it.
  * CarryBook.run: the id is written to disk at SUBMIT (before the wait) on --apply. A timeout
    carries the batch, and a finished batch leaves the book. A dry run never writes.
  * legislation.yml: each watch step's timeout-minutes equals siteconfig, and the steps plus setup
    plus reserve fit inside the job timeout. When a watch step fails, the later watches, the
    results step, the bookkeeping, render and the review PR still run (the `if:` conditions are
    evaluated the way GitHub does). The bookkeeping commits watch_batches.json on every non-dry
    run, and every state file on a quiet one.

The per-watch carry behavior (apply only matching ids, re-queue mismatches) is tested beside each
watch: test_legislation.py, test_regulations.py, test_courtrules.py, test_ethics.py.

Run directly: `python scripts/test_watchbatch.py`.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import batch          # noqa: E402
import siteconfig     # noqa: E402
import watchbatch as W  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@contextlib.contextmanager
def patched(obj, **kw):
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


NOW = 1_790_000_000.0


def test_budget():
    print("budget:")
    clk = Clock(NOW)
    with patched(W, clock=clk):
        b = W.Budget.for_step("legislation")
        want_end = NOW + siteconfig.WATCH_STEP_MIN["legislation"] * 60 - siteconfig.WATCH_STEP_MARGIN_SEC
        check("a step budget ends a margin before the step's timeout-minutes", b.end == want_end)
        check("the watch's own limit wins while it is the sooner", b.deadline(600) == NOW + 600)
        clk.t = NOW + 30 * 60
        check("later in the step, the step's end caps the watch's own 30-minute wait",
              b.deadline(1800) == want_end, "%s vs %s" % (b.deadline(1800), want_end))
        check("plenty of time left: a sync loop is not told to stop", not b.low())
        clk.t = want_end - siteconfig.WATCH_SYNC_FLOOR_SEC + 1
        check("inside the floor: a sync loop is told to stop", b.low())
        u = W.Budget()
        check("an unbounded budget never caps a wait", u.deadline(1800) == clk.t + 1800)
        check("an unbounded budget never says low", not u.low())
        check("an unknown watch gets an unbounded budget", W.Budget.for_step("nope").end is None)


def _rec(bid, at, items):
    return {"id": bid, "label": "x", "at": at, "n": len(items), "items": items}


def test_collect():
    print("collect:")
    clk = Clock(NOW)
    iso_now = W._iso(NOW)
    old = W._iso(NOW - (siteconfig.WATCH_CARRY_MAX_AGE_DAYS + 1) * 86400)
    carries = [
        _rec("b_ended", iso_now, {"a-1": {"k": "A"}, "b-1": {"k": "B"}}),
        _rec("b_running", iso_now, {"c-1": {"k": "C"}}),
        _rec("b_old", old, {"d-1": {"k": "D"}}),
        _rec("b_gone", iso_now, {"e-1": {"k": "E"}}),
        _rec("b_flaky", iso_now, {"f-1": {"k": "F"}}),
    ]
    status_calls = []

    def status(bid, label="batch"):
        status_calls.append(bid)
        if bid == "b_ended":
            return {"id": bid, "processing_status": "ended", "results_url": "u"}
        if bid == "b_running":
            return {"id": bid, "processing_status": "in_progress"}
        if bid == "b_gone":
            raise batch.BatchError("x GET -> HTTP 404: not found")
        raise batch.BatchError("x GET -> <urlopen error timed out>")

    def collect(obj, label="batch"):
        return {"a-1": {"ok": True, "text": "{}"}, "b-1": {"ok": False, "type": "expired"},
                "zz-9": {"ok": True, "text": "{\"stray\": 1}"}}

    book = W.CarryBook("legislation", carries)
    with patched(W, clock=clk), patched(batch, status=status, collect=collect):
        (ready, inflight), out = quiet(book.collect, "lbl")
    check("a too-old carry is never polled", "b_old" not in status_calls)
    check("a too-old carry is dropped with a log line", "dropping carried batch b_old" in out
          and "25-day" in out.replace("%d" % siteconfig.WATCH_CARRY_MAX_AGE_DAYS, "25"), out)
    check("an ended batch is ready", [r["id"] for r in ready] == ["b_ended"])
    check("its results are restricted to the ids the carry recorded",
          set(ready[0]["results"]) == {"a-1", "b-1"})
    check("a result for an id the carry did not record is ignored, and said so",
          "zz-9" not in ready[0]["results"] and "did not carry" in out, out)
    check("a running batch's items are in flight", set(inflight) == {"c-1", "f-1"})
    check("a 4xx drops the carry (its items are processed again)",
          "dropping carried batch b_gone" in out and "e-1" not in inflight)
    check("a transport failure keeps the carry", "keeping it for next run" in out)
    check("the book keeps only the running and the unreadable batches",
          [r["id"] for r in book.carries] == ["b_running", "b_flaky"])
    _, out2 = quiet(W.CarryBook.report, ready[0], 1, 1)
    check("the collected line is the documented one",
          "  . collected carried batch b_ended (1 results applied, 1 re-queued)" in out2, out2)


def test_run_and_persist():
    print("run + persistence:")
    clk = Clock(NOW)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "watch_batches.json")
        W.save_carries("ethics", [_rec("other_watch", W._iso(NOW), {"x": {}})], path)
        W.save_carries("legislation", [_rec("collected_earlier", W._iso(NOW), {"c-1": {}}),
                                       _rec("kept", W._iso(NOW), {"k-1": {}})], path)
        # This run already collected "collected_earlier" (it is gone from the book), but its results
        # are not saved until the run ends -- so the submit-time write must not drop it from disk.
        book = W.CarryBook("legislation", [_rec("kept", W._iso(NOW), {"k-1": {}})], persist=True,
                           path=path)
        seen_on_disk = {}

        def slow(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
            on_submit("msgbatch_new")
            # The id must be on disk BEFORE the wait that might not survive.
            seen_on_disk.update(json.load(open(path)))
            raise batch.BatchTimeout("msgbatch_new", "still running")

        items = {"1-h": {"bid": "1"}}
        with patched(W, clock=clk), patched(batch, run=slow):
            res, out = quiet(book.run, [{"custom_id": "1-h"}], items, NOW + 5, "lbl")
        check("a batch still running at the deadline returns None", res is None)
        check("its id was on disk at submit, before the wait, beside what was already there",
              [r["id"] for r in seen_on_disk.get("legislation", [])]
              == ["collected_earlier", "kept", "msgbatch_new"])
        check("the submit-time write leaves the other watches' carries alone",
              [r["id"] for r in seen_on_disk.get("ethics", [])] == ["other_watch"])
        check("the carry records its items and a timestamp",
              book.carries[-1]["items"] == items and book.carries[-1]["at"] == W._iso(NOW))
        check("the deferral is logged", "carried to next run" in out, out)
        _, out = quiet(book.announce)
        check("the carrying line names every batch carried",
              "  . carrying 2 batch(es) to next run: kept, msgbatch_new" in out, out)
        book.save()
        on_disk = json.load(open(path))
        check("save writes this watch's carries", [r["id"] for r in on_disk["legislation"]]
              == ["kept", "msgbatch_new"])

        # A batch that finishes leaves the book (the disk copy keeps it until the run's save).
        def fast(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
            on_submit("msgbatch_fast")
            return {"1-h": {"ok": True, "text": "{}"}}

        book2 = W.CarryBook("legislation", [], persist=True, path=path)
        with patched(W, clock=clk), patched(batch, run=fast):
            res, _ = quiet(book2.run, [{"custom_id": "1-h"}], items, NOW + 5, "lbl")
        check("a finished batch returns its results", res == {"1-h": {"ok": True, "text": "{}"}})
        check("and is not carried", book2.carries == [])
        book2.save()
        check("an empty save drops the watch's key but keeps the file (and the other watches)",
              set(json.load(open(path))) == {"ethics"})

        # A transport failure BEFORE submit carries nothing.
        def broken(reqs, **_kw):
            raise batch.BatchError("POST -> HTTP 500")

        book3 = W.CarryBook("legislation", [], persist=False)
        with patched(batch, run=broken):
            res, out = quiet(book3.run, [{"custom_id": "1-h"}], items, NOW + 5, "lbl")
        check("a failed submit returns None and carries nothing", res is None and book3.carries == [])

        # A dry run (persist=False) never writes, even at submit.
        before = open(path).read()
        book4 = W.CarryBook("legislation", [], persist=False, path=path)
        with patched(W, clock=clk), patched(batch, run=slow):
            quiet(book4.run, [{"custom_id": "1-h"}], items, NOW + 5, "lbl")
            book4.save()
        check("a dry run's book never writes the file", open(path).read() == before)

        # A corrupt file reads as no carries rather than crashing the watch.
        with open(path, "w") as f:
            f.write("{nope")
        recs, out = quiet(W.load_carries, "legislation", path)
        check("a corrupt carry file reads as empty, loudly", recs == [] and "unreadable" in out)
        with open(path, "w") as f:
            json.dump({"legislation": [{"id": "ok", "items": {}}, {"no": "id"}, "junk"]}, f)
        check("malformed carry records are skipped", [r["id"] for r in W.load_carries("legislation", path)]
              == ["ok"])


# ---- legislation.yml ---------------------------------------------------------------------------
WF = os.path.join(HERE, "..", ".github", "workflows", "legislation.yml")
WATCH_SCRIPTS = {"legislation": "legislation.py", "regulations": "regulations.py",
                 "courtrules": "courtrules.py", "ethics": "ethics.py"}


def _steps():
    import yaml
    doc = yaml.safe_load(open(WF, encoding="utf-8"))
    job = doc["jobs"]["legislation"]
    return doc, job, job["steps"]


def _step_running(steps, script):
    for i, st in enumerate(steps):
        if re.search(r"python3? +scripts/%s\b" % re.escape(script), st.get("run") or ""):
            return i, st
    return None, None


def _eval_if(expr, ctx):
    """Evaluate a step `if:` the way GitHub does, for the subset this workflow uses: status
    functions, `steps.<id>.outputs.<name>`, `inputs.<name>`, ==, !=, !, &&, ||, quoted strings,
    true/false. An expression with no status function is implicitly `success() && (...)`."""
    if expr is None:
        expr = "success()"
    expr = str(expr).strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    if not re.search(r"\b(success|failure|always|cancelled)\(\)", expr):
        expr = "success() && (%s)" % expr
    py = expr
    py = re.sub(r"steps\.([A-Za-z_][\w-]*)\.outputs\.([\w-]+)",
                lambda m: "S(%r,%r)" % (m.group(1), m.group(2)), py)
    py = re.sub(r"inputs\.([\w-]+)", lambda m: "I(%r)" % m.group(1), py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = re.sub(r"\btrue\b", "True", py)
    py = re.sub(r"\bfalse\b", "False", py)
    ns = {
        "S": lambda sid, name: ctx["outputs"].get(sid, {}).get(name, ""),
        "I": lambda name: ctx["inputs"].get(name),
        "success": lambda: not ctx["failed"],
        "failure": lambda: ctx["failed"],
        "always": lambda: True,
        "cancelled": lambda: False,
    }
    return bool(eval(py, {"__builtins__": {}}, ns))   # noqa: S307 -- fixed, local expression


def _simulate(steps, fail_ids=(), changed="0", dry_run=None):
    """Walk the steps in order: which ran. A step listed in fail_ids fails when it runs (and, with
    no continue-on-error, turns the job red); `changed` is the results step's output."""
    ctx = {"failed": False, "outputs": {}, "inputs": {"dry_run": dry_run}}
    ran = []
    for st in steps:
        sid = st.get("id") or st.get("name")
        if not _eval_if(st.get("if"), ctx):
            continue
        ran.append(sid)
        if sid in fail_ids and not st.get("continue-on-error"):
            ctx["failed"] = True
        if st.get("id") == "run":
            ctx["outputs"]["run"] = {"changed": changed}
    return ran


def test_workflow():
    print("legislation.yml:")
    try:
        import yaml  # noqa: F401
    except ImportError:                                 # pragma: no cover
        print("  pyyaml not available; skipping the workflow checks")
        return
    doc, job, steps = _steps()
    check("the job timeout is siteconfig.WATCH_JOB_TIMEOUT_MIN",
          job.get("timeout-minutes") == siteconfig.WATCH_JOB_TIMEOUT_MIN, str(job.get("timeout-minutes")))
    total = 0
    idx = {}
    for watch, script in WATCH_SCRIPTS.items():
        i, st = _step_running(steps, script)
        check("%s runs in a step of its own" % script, st is not None and
              all(s not in (st.get("run") or "") for s in WATCH_SCRIPTS.values() if s != script))
        if st is None:
            continue
        idx[watch] = i
        check("%s's step timeout-minutes is siteconfig.WATCH_STEP_MIN[%r]" % (script, watch),
              st.get("timeout-minutes") == siteconfig.WATCH_STEP_MIN[watch],
              "%s vs %s" % (st.get("timeout-minutes"), siteconfig.WATCH_STEP_MIN[watch]))
        check("%s's step has no continue-on-error (a failure must turn the run red)" % script,
              not st.get("continue-on-error"))
        check("%s's step passes the run mode, and its output reaches run.log" % script,
              '"$WATCH_MODE"' in st["run"] and 'tee -a "$RUNNER_TEMP/run.log"' in st["run"]
              and "set -o pipefail" in st["run"])
        total += st.get("timeout-minutes") or 0
    check("the watch steps + setup + reserve fit inside the job timeout",
          total + siteconfig.WATCH_SETUP_MIN + siteconfig.WATCH_RESERVE_MIN
          <= siteconfig.WATCH_JOB_TIMEOUT_MIN,
          "%d + %d + %d > %d" % (total, siteconfig.WATCH_SETUP_MIN, siteconfig.WATCH_RESERVE_MIN,
                                 siteconfig.WATCH_JOB_TIMEOUT_MIN))
    check("siteconfig names a step budget for exactly the four watches",
          set(siteconfig.WATCH_STEP_MIN) == set(WATCH_SCRIPTS))
    check("the watches run in the old order (statutes first, ethics last)",
          [idx.get(w) for w in WATCH_SCRIPTS] == sorted(idx.values()))
    check("WATCH_MODE is --json on a dry run and --apply otherwise",
          "inputs.dry_run == true && '--json' || '--apply'" in str((job.get("env") or {}).get("WATCH_MODE")))
    # The first watch truncates run.log; every later one appends.
    first = steps[min(idx.values())]["run"]
    check("the first watch step starts run.log fresh", '| tee "$RUNNER_TEMP/run.log"' in first)

    ids = [st.get("id") or st.get("name") for st in steps]
    leg, reg, crc, eth = (steps[idx[w]].get("id") for w in ("legislation", "regulations", "courtrules", "ethics"))
    book = next((st.get("id") or st.get("name") for st in steps if "bookkeeping" in (st.get("name") or "")), None)
    render = next((st.get("id") or st.get("name") for st in steps if "render.py" in (st.get("run") or "")), None)
    pr = next((st.get("id") or st.get("name") for st in steps
               if "create-pull-request" in (st.get("uses") or "")), None)
    report = next((st.get("id") or st.get("name") for st in steps if "Report" in (st.get("name") or "")), None)
    check("every step the simulation needs is found", all([leg, reg, crc, eth, book, render, pr, report]),
          str(ids))

    quiet_ok = _simulate(steps, changed="0")
    check("a clean quiet run: every watch, the results, bookkeeping; no render/PR/report",
          all(s in quiet_ok for s in (leg, reg, crc, eth, "run", book))
          and render not in quiet_ok and pr not in quiet_ok and report not in quiet_ok, str(quiet_ok))
    cards_ok = _simulate(steps, changed="1")
    check("a clean run with cards: bookkeeping (the carry), render and the PR all run",
          all(s in cards_ok for s in (book, render, pr)) and report not in cards_ok, str(cards_ok))
    for failing in (leg, crc):
        r0 = _simulate(steps, fail_ids=(failing,), changed="0")
        check("after %s fails: the later watches still run" % failing,
              all(s in r0 for s in (reg, crc, eth)), str(r0))
        check("after %s fails: the results and the bookkeeping still run (state is saved)" % failing,
              "run" in r0 and book in r0, str(r0))
        check("after %s fails: the failure report fires" % failing, report in r0, str(r0))
        r1 = _simulate(steps, fail_ids=(failing,), changed="1")
        check("after %s fails on a run with cards: render and the review PR still run" % failing,
              render in r1 and pr in r1 and book in r1, str(r1))
    dry = _simulate(steps, changed="0", dry_run=True)
    check("a dry run commits nothing", book not in dry and pr not in dry, str(dry))
    check("the eval helper is not trivially true (a plain step is skipped after a failure)",
          not _eval_if(None, {"failed": True, "outputs": {}, "inputs": {}}))

    # What the bookkeeping commits.
    brun = next(st["run"] for st in steps if "bookkeeping" in (st.get("name") or ""))
    lines = [ln.strip() for ln in brun.splitlines() if ln.strip().startswith("git add")]
    check("the bookkeeping has a cards-run add and a quiet-run add", len(lines) == 2, str(lines))
    if len(lines) == 2:
        cards_add, quiet_add = lines
        check("a run with cards still commits the carry file to main", "watch_batches.json" in cards_add)
        check("a run with cards leaves the seen-state to the review PR",
              "_state.json" not in cards_add)
        check("a quiet run commits the carry file too", "watch_batches.json" in quiet_add)
        add_paths = []
        for st in steps:
            ap = (st.get("with") or {}).get("add-paths")
            if ap:
                add_paths = [p.strip() for p in str(ap).splitlines() if p.strip()]
        for p in add_paths:
            if re.match(r"^[a-z_]+_(state\.json|log\.jsonl|rejections\.jsonl)$", p):
                check("a quiet run commits %s (it rides the PR on a run with cards)" % p,
                      re.search(r"(^|\s)%s(\s|$)" % re.escape(p), quiet_add) is not None)
        check("the carry file does not ride the review PR (it must reach main every run)",
              "watch_batches.json" not in add_paths)
    check("watch_batches.json is tracked (seeded), so `git add` always finds it",
          os.path.exists(os.path.join(HERE, "..", "watch_batches.json")))
    check("watch_batches.json is the path watchbatch writes",
          os.path.basename(W.CARRY_PATH) == "watch_batches.json")


def main():
    test_budget()
    test_collect()
    test_run_and_persist()
    test_workflow()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
