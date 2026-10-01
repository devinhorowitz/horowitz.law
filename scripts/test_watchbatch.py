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
    carry is dropped with a log line. A 404 (the batch is gone) drops the carry; a transport
    failure, a 401/403/429 or any other 4xx keeps it.
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
    check("a 404 drops the carry (its items are processed again)",
          "dropping carried batch b_gone" in out and "e-1" not in inflight)
    check("a transport failure keeps the carry", "keeping it for next run" in out)
    check("the book keeps only the running and the unreadable batches",
          [r["id"] for r in book.carries] == ["b_running", "b_flaky"])

    # Only "the batch is gone" justifies dropping a paid carry. A key problem (401/403), a rate
    # limit (429) or another 4xx says nothing about the batch: keep it (the age limit bounds it).
    errs = {
        "k401": ("legislation-write GET -> HTTP 401: invalid x-api-key", True),
        "k403": ("legislation-write GET -> HTTP 403: permission_error", True),
        "k429": ("legislation-write GET -> HTTP 429: rate_limit_error", True),
        "k400": ("legislation-write GET -> HTTP 400: invalid_request_error: bad header", True),
        "k409": ("legislation-write GET -> HTTP 409: conflict", True),
        "g404": ("legislation-write GET -> HTTP 404: not_found_error", False),
        "g400": ("legislation-write GET -> HTTP 400: invalid_request_error: Invalid batch id 'g400'",
                 False),
    }

    def status4(bid, label="batch"):
        raise batch.BatchError(errs[bid][0])

    book4 = W.CarryBook("legislation", [_rec(b, iso_now, {"%s-1" % b: {"k": b}}) for b in errs])
    with patched(W, clock=clk), patched(batch, status=status4):
        (_ready4, inflight4), out4 = quiet(book4.collect, "lbl")
    for b, (msg, kept) in errs.items():
        code = msg.split("HTTP ")[1][:3]
        if kept:
            check("an HTTP %s (%s) keeps the carry, its items in flight" % (code, b),
                  b in [r["id"] for r in book4.carries] and "%s-1" % b in inflight4, out4)
        else:
            check("an HTTP %s that says the batch is gone (%s) drops the carry" % (code, b),
                  b not in [r["id"] for r in book4.carries] and "dropping carried batch %s" % b in out4, out4)
    check("batch_gone ignores a transport error with no HTTP status",
          not W.batch_gone(batch.BatchError("x GET -> <urlopen error timed out>")))

    # recarry: an ended batch put back for the ids this run could not confirm keeps its id and
    # timestamp (so the age limit still applies) and only those ids.
    rec = {"id": "b_back", "label": "x", "at": iso_now, "items": {"p-1": {}, "q-1": {}},
           "results": {}}
    book4 = W.CarryBook("legislation", [])
    n = book4.recarry(rec, ["q-1", "nope"])
    check("recarry keeps only the named ids, under the same id and timestamp",
          n == 1 and book4.carries == [{"id": "b_back", "label": "x", "at": iso_now, "n": 1,
                                       "items": {"q-1": {}}}], str(book4.carries))
    book4.recarry(rec, ["p-1"])
    check("a second recarry of the same batch merges into the one record",
          len(book4.carries) == 1 and set(book4.carries[0]["items"]) == {"p-1", "q-1"}
          and book4.carries[0]["n"] == 2)
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
    functions, `steps.<id>.outputs.<name>`, `steps.<id>.outcome`, `inputs.<name>`, ==, !=, !, &&,
    ||, quoted strings, true/false. An expression with no status function is implicitly
    `success() && (...)`. Once the run is cancelled (the job timeout), success() and !cancelled()
    are false and only always() / cancelled() steps run."""
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
    py = re.sub(r"steps\.([A-Za-z_][\w-]*)\.outcome",
                lambda m: "O(%r)" % m.group(1), py)
    py = re.sub(r"inputs\.([\w-]+)", lambda m: "I(%r)" % m.group(1), py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = re.sub(r"\btrue\b", "True", py)
    py = re.sub(r"\bfalse\b", "False", py)
    cancelled = ctx.get("cancelled", False)
    ns = {
        "S": lambda sid, name: ctx["outputs"].get(sid, {}).get(name, ""),
        "O": lambda sid: ctx.get("outcomes", {}).get(sid, ""),
        "I": lambda name: ctx["inputs"].get(name),
        "success": lambda: not ctx["failed"] and not cancelled,
        "failure": lambda: ctx["failed"] and not cancelled,
        "always": lambda: True,
        "cancelled": lambda: cancelled,
    }
    return bool(eval(py, {"__builtins__": {}}, ns))   # noqa: S307 -- fixed, local expression


def _simulate(steps, fail_ids=(), changed="0", dry_run=None, cancel_at=None):
    """Walk the steps in order: which ran. A step listed in fail_ids fails when it runs (and, with
    no continue-on-error, turns the job red); `changed` is the results step's output. `cancel_at`
    names a step during which the run is cancelled (the job timeout firing): it does not succeed,
    and every later step is evaluated as GitHub evaluates it on a cancelled run."""
    ctx = {"failed": False, "cancelled": False, "outputs": {}, "outcomes": {}, "inputs": {"dry_run": dry_run}}
    ran = []
    for st in steps:
        sid = st.get("id") or st.get("name")
        if not _eval_if(st.get("if"), ctx):
            ctx["outcomes"][sid] = "skipped"
            continue
        ran.append(sid)
        if sid == cancel_at:
            ctx["cancelled"] = True
            ctx["outcomes"][sid] = "cancelled"
            continue
        if sid in fail_ids:
            ctx["outcomes"][sid] = "failure"
            if not st.get("continue-on-error"):
                ctx["failed"] = True
            continue
        ctx["outcomes"][sid] = "success"
        if st.get("id") == "run":
            ctx["outputs"]["run"] = {"changed": changed}
        if "$GITHUB_OUTPUT" in (st.get("run") or "") and st.get("id") == "setup":
            ctx["outputs"]["setup"] = {"ok": "1"}
    return ran


def _carry_save_behaves(script):
    """Run the carry-save step's script for real against a local bare "origin": the remote file is
    replaced by the runner's, nothing else in the runner's tree or its local commits is pushed, and
    a second run with the file unchanged pushes nothing."""
    import shutil
    import subprocess
    if not shutil.which("git") or not shutil.which("bash"):     # pragma: no cover
        print("  git/bash not available; skipping the carry-save execution check")
        return
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def sh(cmd, cwd):
        return subprocess.run(cmd, cwd=cwd, env=env, shell=True, check=True, capture_output=True,
                              text=True).stdout

    with tempfile.TemporaryDirectory() as td:
        origin, work = os.path.join(td, "origin.git"), os.path.join(td, "work")
        sh("git init -q --bare -b main %s" % origin, td)
        sh("git clone -q %s %s" % (origin, work), td)
        with open(os.path.join(work, "watch_batches.json"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(work, "legislation_state.json"), "w") as f:
            f.write("{\"seen\": {}}\n")
        sh("git add -A && git commit -qm seed && git push -q origin main", work)
        # The runner: a carry written, a state file changed, and an unpushed local commit.
        with open(os.path.join(work, "notes.txt"), "w") as f:
            f.write("local only\n")
        sh("git add notes.txt && git commit -qm 'local only'", work)
        with open(os.path.join(work, "watch_batches.json"), "w") as f:
            f.write('{"legislation": [{"id": "msgbatch_X", "items": {}}]}\n')
        with open(os.path.join(work, "legislation_state.json"), "w") as f:
            f.write("{\"seen\": {\"1\": \"h\"}}\n")
        renv = dict(env, GITHUB_REF_NAME="main", RUNNER_TEMP=td)
        r = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=renv, capture_output=True, text=True)
        check("carry-save (executed): the step succeeds", r.returncode == 0, r.stdout + r.stderr)
        files = sh("git ls-tree -r --name-only main", origin).split()
        remote_carry = sh("git show main:watch_batches.json", origin)
        check("carry-save (executed): main now holds the runner's watch_batches.json",
              "msgbatch_X" in remote_carry, remote_carry)
        check("carry-save (executed): nothing else reached main (no local commit, no state file)",
              sorted(files) == ["legislation_state.json", "watch_batches.json"]
              and sh("git show main:legislation_state.json", origin) == "{\"seen\": {}}\n"
              and sh("git log --format=%s main", origin).split("\n")[1] == "seed", str(files))
        head = sh("git rev-parse main", origin)
        r2 = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=renv, capture_output=True, text=True)
        check("carry-save (executed): a second run with nothing new pushes nothing",
              r2.returncode == 0 and sh("git rev-parse main", origin) == head
              and "already current" in r2.stdout, r2.stdout + r2.stderr)


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
    check("the eval helper honors cancellation (!cancelled() is false, always() true)",
          not _eval_if("${{ !cancelled() }}", {"failed": False, "cancelled": True, "outputs": {},
                                               "inputs": {}})
          and _eval_if("${{ always() }}", {"failed": False, "cancelled": True, "outputs": {}, "inputs": {}}))

    # ---- the setup gate: a failed checkout / setup-python / pip install stops every later step
    #      that needs the repo, but the failure report still fires ----
    setup_ids = [st.get("id") for st in steps[:min(idx.values())]]
    check("every setup step has an id, and the last is the setup-ok marker",
          all(setup_ids) and setup_ids[-1] == "setup"
          and 'echo "ok=1" >> "$GITHUB_OUTPUT"' in (steps[min(idx.values()) - 1].get("run") or ""),
          str(setup_ids))
    carry = next((st.get("id") for st in steps if "watch_batches.json" in (st.get("run") or "")
                  and "commit-tree" in (st.get("run") or "")), None)
    check("a carry-save step exists", carry is not None)
    for failing in ("checkout", "python", "deps"):
        rs = _simulate(steps, fail_ids=(failing,), changed="1")
        check("after %s fails: no watch, results, bookkeeping, render, PR or carry step runs" % failing,
              not any(x in rs for x in (leg, reg, crc, eth, "run", book, render, pr, carry)), str(rs))
        check("after %s fails: the failure report fires" % failing, report in rs, str(rs))

    # ---- the job timeout: every !cancelled() step is skipped, but the carry is still saved ----
    for at in (leg, crc, eth, "run", render):
        rc = _simulate(steps, changed="1", cancel_at=at)
        check("cancelled during %s: the carry-save step still runs" % at, carry in rc, str(rc))
        after = [x for x in (book, pr) if x in rc and rc.index(x) > rc.index(at)]
        check("cancelled during %s: the !cancelled() steps after it do not" % at, not after, str(rc))
    check("the carry-save step does not run on a dry run, even when cancelled",
          carry not in _simulate(steps, changed="0", dry_run=True)
          and carry not in _simulate(steps, changed="0", dry_run=True, cancel_at=crc))
    check("the carry-save step also runs on a clean run (a no-op when main already has the file)",
          carry in quiet_ok and carry in cards_ok)
    check("the carry-save step comes after the bookkeeping and the review PR",
          ids.index(carry) > ids.index(book) and ids.index(carry) > ids.index(pr))
    cst = next(st for st in steps if st.get("id") == carry)
    crun = cst.get("run") or ""
    check("the carry-save step's `if` is always() and not a dry run",
          "always()" in str(cst.get("if")) and "inputs.dry_run != true" in str(cst.get("if")))
    check("the carry-save step commits ONLY watch_batches.json, onto the freshly fetched tip",
          "git fetch" in crun and "read-tree" in crun and "update-index --add --cacheinfo" in crun
          and crun.count("watch_batches.json") >= 3 and "git add" not in crun
          and not re.search(r"git commit\b(?!-tree)", crun), crun)
    check("the carry-save step never pushes local HEAD (only the commit it built)",
          re.search(r'git push origin "\$commit:refs/heads/\$branch"', crun) is not None
          and not re.search(r"git push\s*($|\|\||;|&&)", crun, re.M), crun)
    check("the carry-save step is a no-op when the remote file already matches",
          '= "$blob" ]' in crun and "exit 0" in crun)
    _carry_save_behaves(crun)

    # ---- a failed bookkeeping push must not leak its local commit into the review PR ----
    rb = _simulate(steps, fail_ids=(book,), changed="1")
    check("bookkeeping push fails: the review PR does not run (its base would hold the unpushed commit)",
          pr not in rb, str(rb))
    check("bookkeeping push fails: the carry still reaches main through the carry-save step",
          carry in rb, str(rb))
    check("bookkeeping push fails: the failure report fires", report in rb, str(rb))
    prst = next(st for st in steps if st.get("id") == pr)
    check("the review PR requires the bookkeeping step's success",
          "steps.%s.outcome == 'success'" % book in str(prst.get("if")))

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
