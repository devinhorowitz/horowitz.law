#!/usr/bin/env python3
"""Hermetic unit tests for queue_cases.parse_line (no network, no API key).

parse_line classifies each queue.txt line into blank / comment / entry and, for an
entry, resolves the cluster id from a CourtListener URL, a bare cluster id, or a
cluster:court pair -- honoring the trailing `!` force marker and inline `#` comments,
and rejecting look-alike hosts. This is the parse that decides which cases the manual
queue funnel will pull, so a regression here silently mis-queues or drops a request.

The record-step tests at the bottom run queue.yml's real "Record audit verdicts on main"
script against local bare repositories (the local `git` binary; still no network).

Run directly: `python scripts/test_queue_cases.py`.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import queue_cases    # noqa: E402  (sys.path shim must run first)

QUEUE_YML = os.path.join(HERE, "..", ".github", "workflows", "queue.yml")

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def entry(raw):
    kind, payload = queue_cases.parse_line(raw)
    assert kind == "entry", "expected entry, got %s for %r" % (kind, raw)
    return payload


def test_blank_and_comment():
    check("empty line -> blank", queue_cases.parse_line("") == ("blank", ""))
    check("whitespace-only -> blank", queue_cases.parse_line("   \n")[0] == "blank")
    check("full-line comment -> comment", queue_cases.parse_line("# a note")[0] == "comment")
    check("comment preserves the raw text", queue_cases.parse_line("# a note\n")[1] == "# a note")
    check("line that is only an inline comment -> comment",
          queue_cases.parse_line("   # trailing only")[0] == "comment")


def test_url_forms():
    e = entry("https://www.courtlistener.com/opinion/12345/smith-v-jones/")
    check("full CourtListener URL -> cid", e["cid"] == 12345)
    e = entry("www.courtlistener.com/opinion/67890/foo/")
    check("scheme-less CourtListener paste -> cid", e["cid"] == 67890)
    # A look-alike host must NOT be treated as CourtListener.
    e = entry("courtlistener.com.evil.tld/opinion/999/")
    check("look-alike host is rejected (cid None)", e["cid"] is None)


def test_bare_and_pair():
    check("bare cluster id -> cid", entry("12345")["cid"] == 12345)
    e = entry("12345:ctapp")
    check("cluster:court pair -> cid", e["cid"] == 12345)
    check("cluster:court pair -> lowercased court", e["court"] == "ctapp")
    check("cluster:court pair uppercase court is normalized", entry("77:CTAPP")["court"] == "ctapp")
    check("non-token entry -> cid None", entry("not-a-cluster")["cid"] is None)


def test_force_and_inline_comment():
    e = entry("12345!")
    check("trailing ! sets force", e["force"] is True and e["cid"] == 12345)
    e = entry("12345")
    check("no ! -> force False", e["force"] is False)
    e = entry("12345  # already carded, re-pull")
    check("inline comment is stripped before parsing", e["cid"] == 12345)
    e = entry("12345 !  # forced re-pull")
    check("force survives an inline comment", e["force"] is True and e["cid"] == 12345)
    check("entry keeps the original raw line", entry("12345 # note")["raw"] == "12345 # note")


def test_rewrite_queue():
    # A representative queue: header comment, blank, and four entries fated to each
    # outcome -- carded (remove), parked (unresolved), kept (deferred), and untouched
    # (no recorded outcome, e.g. resolve-only). The park and keep paths crashed in
    # production (2026-07-25, issue #186) because the entry payload is the parse dict,
    # not a string; this pins the payload["raw"] handling for every branch.
    raw_lines = [
        "# curated queue",
        "",
        "11111  # carded last run",
        "22222:ca11 !  # forced entry that failed to resolve",
        "33333  # deferred until text is up",
        "44444",
    ]
    parsed = [queue_cases.parse_line(l) for l in raw_lines]
    outcomes = {
        2: ("remove", None),
        3: ("park", "could not resolve cluster 22222: timeout"),
        4: ("keep", None),
        # index 5 intentionally absent: default outcome must keep the line verbatim
    }
    text = queue_cases.rewrite_queue(parsed, outcomes)
    lines = text.splitlines()
    check("comment survives verbatim", lines[0] == "# curated queue")
    check("blank line survives", lines[1] == "")
    check("removed (carded) line is gone", all("11111" not in l for l in lines))
    check("parked line becomes an annotated comment",
          lines[2] == "# 22222:ca11 !  # forced entry that failed to resolve   "
                      "-- could not resolve cluster 22222: timeout",
          detail=repr(lines[2]))
    check("kept (deferred) line survives verbatim", lines[3] == "33333  # deferred until text is up")
    check("line with no recorded outcome survives verbatim", lines[4] == "44444")
    check("text ends with exactly one newline", text.endswith("\n") and not text.endswith("\n\n"))
    check("all lines removed -> empty text",
          queue_cases.rewrite_queue([queue_cases.parse_line("11111")], {0: ("remove", None)}) == "")


def test_stamp_audits(tmpdir=None):
    """A forced queue read must RECORD what it established, so the effort accumulates.

    Queue run 26 (2026-09-12) read cluster 10956827 on the full opinion and the summarizer declined
    it. The drop record afterwards still said smell_outcome "deferred" with no audit, because
    stamp_audits did not exist and queue.yml's add-paths did not list the log. smell_check.py
    re-audits "deferred" records and skips ones carrying a full_opinion audit, so an unstamped
    decline comes back round and is escalated to the editor again -- the loop audit_log.py was
    written to end. These checks pin the write, the matching, and the two refusals."""
    import io, json, tempfile
    import update
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rej.jsonl")
        rows = [
            {"cluster_id": 10956827, "stage": "triage", "name": "Wells Fargo", "reason": "x",
             "smell": "suspect", "smell_outcome": "deferred"},
            {"cluster_id": 999, "stage": "triage", "name": "Other", "reason": "y",
             "audit": {"verdict": "recovered", "depth": "full_opinion", "by": "someone", "ts": "z"}},
        ]
        io.open(path, "w", encoding="utf-8").write(
            "".join(json.dumps(r) + "\n" for r in rows))
        real = update.REJECT_PATH
        try:
            update.REJECT_PATH = path
            n = queue_cases.stamp_audits([(10956827, "confirmed", "summarizer declined")])
            check("a declined forced read stamps one record", n == 1, "changed=%r" % n)
            out = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
            rec = next(r for r in out if r["cluster_id"] == 10956827)
            a = rec.get("audit") or {}
            check("verdict recorded", a.get("verdict") == "confirmed", repr(a))
            check("depth is full_opinion -- the summarizer read the whole opinion",
                  a.get("depth") == "full_opinion", repr(a))
            check("by names the queue path and the model", str(a.get("by", "")).startswith("queue-forced/"),
                  repr(a.get("by")))
            check("the other record is untouched",
                  next(r for r in out if r["cluster_id"] == 999)["audit"]["by"] == "someone")
            # audited_to_depth is what makes smell_check skip it next time; that is the whole point.
            check("smell_check will now skip it", update.audited_to_depth(rec, "full_opinion"))

            # A cluster with no rejection record (never dropped) is silently skipped, not an error.
            check("a cluster with no record changes nothing",
                  queue_cases.stamp_audits([(5555555, "confirmed", "n/a")]) == 0)
            # record_audit refuses to weaken a stronger prior claim, so a re-run cannot downgrade.
            check("re-stamping the same verdict is a no-op",
                  queue_cases.stamp_audits([(10956827, "confirmed", "summarizer declined")]) == 0)
            check("no stamps at all is a no-op", queue_cases.stamp_audits([]) == 0)
        finally:
            update.REJECT_PATH = real


LOG = "opinions_rejections.jsonl"
TS = "2026-09-27T12:00:00Z"
STAMPS = [(10956827, "confirmed", "queue-forced read: summarizer declined")]


def _rec(cid, name, **extra):
    r = {"ts": "2026-09-20T12:17:00Z", "stage": "triage", "cluster_id": cid, "name": name,
         "reason": "not relevant"}
    r.update(extra)
    return r


def _line(r):
    """A log line exactly as update._log_rejections appends it."""
    return json.dumps(r, separators=(",", ":"), ensure_ascii=False)


# The stamped record is LAST: a forced read is usually of a recent drop, and the last line is the
# one a funnel append lands right after.
BASE = [_rec(1, "Café Co. v. Müller"), _rec(2, "Other"),
        _rec(10956827, "Wells Fargo", smell_outcome="deferred")]


def test_stamp_audits_writes_compact():
    """The log is compact everywhere else it is written (update._log_rejections, audit_log.save,
    smell_check), and stamp_audits wrote it with json.dumps' spaced default -- a rewrite of every
    one of ~2,700 lines for a one-record stamp, so the commit collided with any funnel append in
    flight. A stamp must leave every line it did not touch byte-identical."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, LOG)
        before = [_line(r) for r in BASE]
        open(path, "w", encoding="utf-8").write("\n".join(before) + "\n")
        n = queue_cases.stamp_audits(STAMPS, path=path, ts=TS)
        text = open(path, encoding="utf-8").read()
        after = text.splitlines()
        check("the stamp lands in the log it is pointed at", n == 1, "changed=%r" % n)
        check("every unstamped line is byte-identical (non-ASCII included)",
              after[:2] == before[:2], repr(after[:2]))
        check("the stamped line is written compactly too",
              after[2] == _line(json.loads(after[2])) and '"audit":{' in after[2], after[2][:120])
        check("and carries the pinned timestamp", json.loads(after[2])["audit"]["ts"] == TS)
        check("one line per record, newline-terminated", len(after) == 3 and text.endswith("}\n"))


def test_saved_stamps_reapply():
    """queue.yml re-applies the run's saved verdicts to a fresh checkout of main via the CLI entry."""
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, LOG)
        open(log, "w", encoding="utf-8").write("".join(_line(r) + "\n" for r in BASE))
        stamps = os.path.join(d, "stamps.json")
        real = queue_cases.STAMPS_PATH
        try:
            queue_cases.STAMPS_PATH = stamps
            queue_cases.save_stamps(STAMPS, TS)
        finally:
            queue_cases.STAMPS_PATH = real
        with contextlib.redirect_stdout(io.StringIO()):
            rc = queue_cases.main(["--apply-stamps", stamps, log])
        a = json.loads(open(log, encoding="utf-8").read().splitlines()[2]).get("audit") or {}
        check("--apply-stamps stamps the log it names", rc == 0 and a.get("verdict") == "confirmed", repr(a))
        check("with the run's timestamp, not the re-apply's", a.get("ts") == TS, repr(a.get("ts")))
        # A retried push whose first attempt did land re-applies onto a main that already has the
        # stamp; a fresh timestamp there would push a second, timestamp-only commit.
        same = open(log, encoding="utf-8").read()
        check("re-applying the same run's stamps is a no-op",
              queue_cases.apply_saved_stamps(stamps, log) == 0
              and open(log, encoding="utf-8").read() == same)
        check("no saved stamps is zero, not an error",
              queue_cases.apply_saved_stamps(os.path.join(d, "none.json"), log) == 0)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                queue_cases.main(["--bogus"])
            code = None
        except SystemExit as e:
            code = e.code
        check("an unknown argument exits instead of starting a paid run", code == 2, repr(code))


# ---------------------------------------------------------------------------
# queue.yml's record step, run for real. The step committed the stamped log from the run's own
# tree and called push_main.sh, whose rebase git refuses while queue.txt and opinions.json sit
# modified for the PR step ("cannot rebase: You have unstaged changes"). The step failed, the PR
# step behind it was skipped, and the verdict, the queue drain and the card were all discarded.
# ---------------------------------------------------------------------------


def _queue_steps():
    import yaml
    return yaml.safe_load(open(QUEUE_YML, encoding="utf-8"))["jobs"]["queue"]["steps"]


def test_pr_step_survives_the_record_step():
    """With no `if`, the PR step took an implicit success(): a failed verdict push skipped it."""
    try:
        steps = _queue_steps()
    except ImportError:
        print("  .    pyyaml not available; skipped")
        return
    screen = next((s for s in steps if (s.get("run") or "").strip() == "python scripts/queue_cases.py"), {})
    names = [s.get("name") for s in steps]
    pr = next((s for s in steps if "create-pull-request" in str(s.get("uses"))), {})
    cond = str(pr.get("if") or "")
    check("the screen step has an id to key on", bool(screen.get("id")), repr(screen.get("name")))
    check("the PR step runs whatever the record step did (always())", "always()" in cond, cond)
    check("but only on the screen step's success",
          "steps.%s.outcome == 'success'" % screen.get("id") in cond, cond)
    check("the record step still runs before it",
          "Record audit verdicts on main" in names
          and names.index("Record audit verdicts on main") < names.index(pr.get("name")))


def _git(*args, cwd, check_rc=True):
    e = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T",
             GIT_COMMITTER_EMAIL="t@e", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    r = subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True, env=e)
    if check_rc and r.returncode != 0:
        raise AssertionError("git %s failed in %s: %s%s" % (" ".join(args), cwd, r.stdout, r.stderr))
    return r


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _record_fixture(tmp):
    """A bare origin holding the log, queue.txt and opinions.json, and `ws`: its checkout as a
    finished queue run leaves it -- queue.txt drained, a card added, the log stamped locally, all
    uncommitted for the PR step -- plus the saved stamps and the two scripts the step calls."""
    origin = os.path.join(tmp, "origin.git")
    _git("init", "--bare", "--initial-branch=main", origin, cwd=tmp)
    seed = os.path.join(tmp, "seed")
    _git("clone", origin, seed, cwd=tmp)
    _write(os.path.join(seed, LOG), "".join(_line(r) + "\n" for r in BASE))
    _write(os.path.join(seed, "queue.txt"), "10956827 !\n")
    _write(os.path.join(seed, "opinions.json"), "[]\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "base", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)

    ws = os.path.join(tmp, "ws")
    _git("clone", origin, ws, cwd=tmp)
    _write(os.path.join(ws, "queue.txt"), "")
    _write(os.path.join(ws, "opinions.json"), '[{"cluster_id": 77}]\n')
    queue_cases.stamp_audits(STAMPS, path=os.path.join(ws, LOG), ts=TS)
    os.makedirs(os.path.join(ws, "scripts"))
    for f in ("queue_cases.py", "push_main.sh"):
        os.symlink(os.path.join(HERE, f), os.path.join(ws, "scripts", f))
    real = queue_cases.STAMPS_PATH
    try:
        queue_cases.STAMPS_PATH = os.path.join(ws, "scripts", "queue_audit_stamps.json")
        queue_cases.save_stamps(STAMPS, TS)
    finally:
        queue_cases.STAMPS_PATH = real
    return origin, ws


def _commit_append(tmp, origin, name, rec, push=True):
    """Another job (the funnel) appending one drop to main's log."""
    path = os.path.join(tmp, name)
    _git("clone", origin, path, cwd=tmp)
    with open(os.path.join(path, LOG), "a", encoding="utf-8") as f:
        f.write(_line(rec) + "\n")
    _git("commit", "-am", "funnel: " + name, cwd=path)
    if push:
        _git("push", "origin", "HEAD:main", cwd=path)
    return path


def _run_record_step(tmp, ws, extra_env=None):
    """queue.yml's own "Record audit verdicts on main" script, run the way Actions runs it."""
    run = next(s["run"] for s in _queue_steps() if s.get("name") == "Record audit verdicts on main")
    script = os.path.join(tmp, "record_step.sh")
    _write(script, run)
    shim = os.path.join(tmp, "bin")
    if not os.path.isdir(shim):
        os.makedirs(shim)
        _write(os.path.join(shim, "python"), '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
        os.chmod(os.path.join(shim, "python"), 0o755)
    runner_temp = os.path.join(tmp, "runner")
    os.makedirs(runner_temp, exist_ok=True)
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update({"PATH": shim + os.pathsep + os.environ.get("PATH", ""), "RUNNER_TEMP": runner_temp,
              "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
              "PUSH_MAIN_BACKOFF": "0", "PUSH_MAIN_OUTAGE_BACKOFF": "0"})
    e.update(extra_env or {})
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", script], cwd=ws,
                          capture_output=True, text=True, env=e, timeout=120)


def _main_log(origin):
    return _git("show", "main:" + LOG, cwd=origin).stdout.splitlines()


def _local_log(ws):
    return open(os.path.join(ws, LOG), encoding="utf-8").read().splitlines()


def _worktrees(ws):
    return _git("worktree", "list", "--porcelain", cwd=ws).stdout.count("worktree ")


def _state(ws):
    return (_git("rev-parse", "HEAD", cwd=ws).stdout, _git("status", "--porcelain", cwd=ws).stdout)


def test_record_step_dirty_tree_and_prior_append():
    """The common case: the funnel appended to the log while the queue ran, and the run's tree is
    dirty. The verdict must land on top of that append, and the tree must be left for the PR."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, ws = _record_fixture(tmp)
        f1 = _rec(20000001, "Funnel drop while the queue ran")
        _commit_append(tmp, origin, "funnel", f1)
        before = _state(ws)

        r = _run_record_step(tmp, ws)
        out = r.stdout + r.stderr
        check("record step exits 0 with queue.txt and opinions.json still dirty",
              r.returncode == 0, out[-800:])
        check("main = this run's stamped log + the funnel's append, every other line byte-identical",
              _main_log(origin) == _local_log(ws) + [_line(f1)], "\n".join(_main_log(origin))[-600:])
        check("the verdict commit carries the log and nothing else",
              _git("show", "--name-only", "--format=", "main", cwd=origin).stdout.split() == [LOG])
        check("the run's tree is untouched for the PR step (HEAD and changes)", _state(ws) == before)
        check("the throwaway worktree is removed", _worktrees(ws) == 1)

        tip = _git("rev-parse", "main", cwd=origin).stdout
        r2 = _run_record_step(tmp, ws)
        check("a second run finds the verdict already on main and pushes nothing",
              r2.returncode == 0 and _git("rev-parse", "main", cwd=origin).stdout == tip,
              (r2.stdout + r2.stderr)[-400:])


def test_record_step_race_adjacent_to_the_stamp():
    """A funnel append landing between the step's fetch and its push, directly after the stamped
    line. Rebasing the stamp commit over it conflicts -- git will not merge an edit to one line
    with an append on the next -- so push_main's rebase would have lost the verdict. The step
    regenerates the stamp on the new main instead."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, ws = _record_fixture(tmp)
        f2 = _rec(20000002, "Funnel drop in the push window")
        racer = _commit_append(tmp, origin, "racer", f2, push=False)
        mark = os.path.join(tmp, "raced")
        # pre-push runs after the remote's refs are read and before the push is sent: the racer
        # lands there, so this push is rejected exactly as a real race would reject it.
        hook = os.path.join(ws, ".git", "hooks", "pre-push")
        _write(hook, "#!/bin/sh\n"
                     "if [ ! -f '%s' ]; then\n"
                     "  touch '%s'\n"
                     "  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR\n"
                     "  cd '%s' && git push -q origin HEAD:main >/dev/null 2>&1\n"
                     "fi\n"
                     "exit 0\n" % (mark, mark, racer))
        os.chmod(hook, 0o755)

        r = _run_record_step(tmp, ws)
        out = r.stdout + r.stderr
        check("the race actually happened (first push refused, retried)",
              os.path.exists(mark) and "attempt 1" in out, out[-800:])
        check("record step exits 0 through the race", r.returncode == 0, out[-800:])
        check("main = the stamped log + the racing append, adjacent to the stamp",
              _main_log(origin) == _local_log(ws) + [_line(f2)], "\n".join(_main_log(origin))[-600:])
        subjects = _git("log", "--format=%s", "main", cwd=origin).stdout.splitlines()
        check("exactly one verdict commit reached main",
              sum("audit verdicts" in s for s in subjects) == 1, str(subjects))

        # The premise, shown rather than asserted: the same stamp, rebased over the same append.
        demo = os.path.join(tmp, "demo")
        _git("clone", origin, demo, cwd=tmp)
        _git("checkout", "-q", "-b", "stamp", "origin/main~2", cwd=demo)
        _write(os.path.join(demo, LOG), "\n".join(_local_log(ws)) + "\n")
        _git("commit", "-qam", "stamp", cwd=demo)
        rb = _git("rebase", "origin/main~1", cwd=demo, check_rc=False)
        check("a rebase of that stamp over that append does conflict", rb.returncode != 0,
              rb.stdout + rb.stderr)


def test_record_step_failure_leaves_the_tree_for_the_pr():
    """When the push cannot land at all the step fails loudly (the failure issue opens), but the
    tree the PR step commits from is exactly as the queue run left it."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, ws = _record_fixture(tmp)
        hooks = os.path.join(origin, "hooks")
        os.makedirs(hooks, exist_ok=True)
        _write(os.path.join(hooks, "pre-receive"), "#!/bin/sh\necho 'Internal Server Error' >&2\nexit 1\n")
        os.chmod(os.path.join(hooks, "pre-receive"), 0o755)
        tip = _git("rev-parse", "main", cwd=origin).stdout
        before = _state(ws)

        r = _run_record_step(tmp, ws, {"PUSH_MAIN_OUTAGE_TRIES": "2"})
        check("an unpushable verdict fails the step", r.returncode != 0, (r.stdout + r.stderr)[-400:])
        check("main is untouched", _git("rev-parse", "main", cwd=origin).stdout == tip)
        check("the run's tree is untouched for the PR step", _state(ws) == before)
        check("the throwaway worktree is removed even on failure", _worktrees(ws) == 1)


def test_record_step_without_stamps():
    with tempfile.TemporaryDirectory() as tmp:
        origin, ws = _record_fixture(tmp)
        os.remove(os.path.join(ws, "scripts", "queue_audit_stamps.json"))
        tip = _git("rev-parse", "main", cwd=origin).stdout
        r = _run_record_step(tmp, ws)
        check("no saved stamps: exit 0, nothing pushed, no worktree",
              r.returncode == 0 and "no audit verdicts to record" in r.stdout
              and _git("rev-parse", "main", cwd=origin).stdout == tip and _worktrees(ws) == 1,
              (r.stdout + r.stderr)[-400:])


def main():
    print("queue_cases.parse_line:")
    test_blank_and_comment()
    test_url_forms()
    test_bare_and_pair()
    test_force_and_inline_comment()
    print("queue_cases.rewrite_queue:")
    test_rewrite_queue()
    print("queue_cases.stamp_audits:")
    test_stamp_audits()
    test_stamp_audits_writes_compact()
    test_saved_stamps_reapply()
    print("queue.yml record + PR steps:")
    test_pr_step_survives_the_record_step()
    try:
        import yaml    # noqa: F401  (the record-step tests read the step out of queue.yml)
        have_yaml = True
    except ImportError:
        have_yaml = False
    if shutil.which("git") and have_yaml:
        test_record_step_dirty_tree_and_prior_append()
        test_record_step_race_adjacent_to_the_stamp()
        test_record_step_failure_leaves_the_tree_for_the_pr()
        test_record_step_without_stamps()
    else:
        print("  .    git or pyyaml not available; record-step tests skipped")
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
