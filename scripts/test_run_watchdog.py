#!/usr/bin/env python3
"""Hermetic tests for run_watchdog.

The headline fixtures are not invented. They are real step sequences, trimmed to the
fields the rule reads:

  RUNNER_DEATH        treatment sweep 30704271089, 2026-08-01 -- runner reclaimed at 16
                      minutes, exit 143. `Report a failed run` was SKIPPED. Nothing filed.
  CANCELLED_MID_REPORT daily funnel 30875514401, 2026-08-04 -- runner reclaimed WHILE the
                      reporting step was running, so it is `cancelled`, not `skipped`. The
                      first version of the rule called this self-reported and stayed quiet,
                      so the failure went entirely unrecorded. Production found the bug.
  NORMAL_FAILURE      opinions-maintenance 30375455076, 2026-07-28 -- a genuine finding,
                      exit 1. `Report maintenance findings` ran and filed. Note that
                      `Post Run actions/setup-python` is `skipped` here on a perfectly
                      healthy runner, which is why the rule must not read post-steps as
                      liveness.

The two failure fixtures pull in opposite directions from NORMAL_FAILURE, which is the
point. If a future change makes NORMAL_FAILURE report as silent, the watchdog has started
duplicating every ordinary failure onto its own issue -- the pile-up the two-strike link
rule was built to avoid. If it makes either of the others report as NOT silent, a dead
runner goes unrecorded again.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_watchdog as rw  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print("  ok   %s" % label)
    else:
        FAILURES.append(label)
        print("  FAIL %s%s" % (label, ("  <- %s" % detail) if detail else ""))


def _steps(pairs):
    return [{"name": n, "conclusion": c} for n, c in pairs]


# --- the two real runs -----------------------------------------------------
RUNNER_DEATH = {
    "name": "treatment",
    "conclusion": "failure",
    "steps": _steps([
        ("Set up job", "success"),
        ("Pre Harden the runner", "success"),
        ("Harden the runner", "success"),
        ("Run actions/checkout@3d3c42e5", "success"),
        ("Run actions/setup-python@5fda3b95", "success"),
        ("Install dependencies", "success"),
        ("Sweep citations, classify treatment, flag adversely-treated cards", "failure"),
        ("Open pull request", "skipped"),
        ("Report a failed run", "skipped"),
        ("Post Run actions/setup-python@5fda3b95", "skipped"),
        ("Post Run actions/checkout@3d3c42e5", "skipped"),
        ("Post Harden the runner", "skipped"),
        ("Complete job", "success"),
    ]),
}

# Run 30875514401, the daily funnel on 2026-08-04. The runner was reclaimed WHILE the
# reporting step was executing, so that step is `cancelled` rather than `skipped`. Nothing
# was filed for this failure -- not by the workflow (its handler was cut off) and not by
# this watchdog (the all-skipped test came back false). It is the exact case the module
# exists for, and the first rule missed it.
CANCELLED_MID_REPORT = {
    "name": "update",
    "conclusion": "failure",
    "steps": _steps([
        ("Set up job", "success"),
        ("Harden the runner", "success"),
        ("Install dependencies", "success"),
        ("Fetch, screen, triage, summarize, render", "failure"),
        ("Publish auto cards and bookkeeping to main", "skipped"),
        ("Stage held cases in the review PR", "skipped"),
        ("Publish scan status", "skipped"),
        ("Report a failed run", "cancelled"),          # started, then the runner went away
        ("Post Run actions/setup-python@5fda3b95", "skipped"),
        ("Post Run actions/checkout@3d3c42e5", "skipped"),
        ("Post Harden the runner", "skipped"),
        ("Complete job", "success"),
    ]),
}

NORMAL_FAILURE = {
    "name": "maintain",
    "conclusion": "failure",
    "steps": _steps([
        ("Set up job", "success"),
        ("Pre Harden the runner", "success"),
        ("Harden the runner", "success"),
        ("Run actions/checkout@3d3c42e5", "success"),
        ("Keepalive marker", "success"),
        ("Run actions/setup-python@5fda3b95", "success"),
        ("Validate court registry", "success"),
        ("Court registry drift flag", "skipped"),
        ("Court registry clean", "success"),
        ("Feed-shape canary", "success"),
        ("Feed-shape drift flag", "skipped"),
        ("Feed-shape check clean", "success"),
        ("Run maintenance", "failure"),
        ("Run summary", "skipped"),
        ("Report maintenance findings", "success"),
        ("Close the findings issue on a healthy run", "skipped"),
        ("Post Run actions/setup-python@5fda3b95", "skipped"),
        ("Post Run actions/checkout@3d3c42e5", "success"),
        ("Post Harden the runner", "success"),
        ("Complete job", "success"),
    ]),
}


def test_the_two_real_runs():
    print("the two runs this module was built from")
    check("a reclaimed runner reads as silent",
          rw.job_is_silent(RUNNER_DEATH) is True)
    check("an ordinary failure whose handler ran does NOT read as silent",
          rw.job_is_silent(NORMAL_FAILURE) is False)
    check("a handler CANCELLED mid-run reads as silent, same as one never started",
          rw.job_is_silent(CANCELLED_MID_REPORT) is True,
          "run 30875514401 filed nothing and must be caught")
    check("...and that is decided by the handler, not by post-steps",
          any(s["name"].startswith("Post ") and s["conclusion"] == "skipped"
              for s in NORMAL_FAILURE["steps"]),
          "fixture no longer exercises the post-step trap")


def test_the_rule():
    print("the rule itself")
    ok = {"conclusion": "success", "steps": _steps([("a", "success")])}
    check("a successful job is never silent", rw.job_is_silent(ok) is False)
    check("a cancelled job is never silent",
          rw.job_is_silent({"conclusion": "cancelled",
                            "steps": _steps([("a", "failure")])}) is False)

    check("a cancelled later step counts as not having run",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("report", "cancelled")])}) is True)
    check("a later step with no conclusion at all counts as not having run",
          rw.job_is_silent({"conclusion": "failure", "steps": [
              {"name": "work", "conclusion": "failure"}, {"name": "report"}]}) is True)
    check("a mix of skipped and cancelled is still silent",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("a", "skipped"), ("report", "cancelled")])}) is True)
    check("but one step that SUCCEEDED among them is not silent",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("a", "cancelled"), ("report", "success")])}) is False)
    check("a failure with one later step that RAN is not silent",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("report", "success")])}) is False)
    check("a failure with one later step that skipped IS silent",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("report", "skipped")])}) is True)
    check("a later FAILED step also counts as having run",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("report", "failure")])}) is False)

    check("Complete job alone after the failure does not count as running",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("Complete job", "success")])}) is True)

    check("a failed job with no steps at all is silent",
          rw.job_is_silent({"conclusion": "failure", "steps": []}) is True)
    check("a failed job with no step marked failed is silent",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("a", "success")])}) is True)
    check("a malformed steps field does not crash",
          rw.job_is_silent({"conclusion": "failure", "steps": "nonsense"}) is True)

    check("only the FIRST failure anchors the window",
          rw.job_is_silent({"conclusion": "failure", "steps": _steps([
              ("work", "failure"), ("report", "success"), ("later", "failure")])}) is False)


def test_selection():
    print("picking failed jobs out of a payload")
    payload = {"jobs": [RUNNER_DEATH, NORMAL_FAILURE, CANCELLED_MID_REPORT,
                        {"conclusion": "success", "steps": []}]}
    got = rw.silent_jobs(payload)
    check("both silent jobs are selected, the self-reported one is not",
          [j["name"] for j in got] == ["treatment", "update"],
          str([j.get("name") for j in got]))
    check("a bare list payload works too",
          [j["name"] for j in rw.silent_jobs([RUNNER_DEATH])] == ["treatment"])
    check("a junk payload yields nothing rather than crashing",
          rw.silent_jobs({"jobs": "nope"}) == [] and rw.silent_jobs(None) == [])


def test_body():
    print("the issue body")
    body = rw.issue_body("Georgia Appellate Watch treatment sweep",
                         "https://github.com/o/r/actions/runs/30704271089",
                         [RUNNER_DEATH])
    check("it names the workflow",
          any(ln.startswith("| workflow | Georgia Appellate Watch treatment sweep |")
              for ln in body.splitlines()), body)
    check("it links the run",
          any(ln.startswith("| run | https://github.com/o/r/actions/runs/30704271089 |")
              for ln in body.splitlines()), body)
    check("it names the last step that actually ran",
          "Sweep citations, classify treatment, flag adversely-treated cards"
          in rw._last_executed(RUNNER_DEATH), rw._last_executed(RUNNER_DEATH))
    check("it says the failure was unreported",
          "never ran" in body, body)
    check("it warns that work was lost",
          "lost" in body, body)
    check("a job that ran nothing still renders",
          rw._last_executed({"steps": []}) == "(no step completed)")


def test_cli():
    print("the CLI")
    with tempfile.TemporaryDirectory(dir=HERE) as d:
        jobs = os.path.join(d, "jobs.json")
        body = os.path.join(d, "body.md")

        with open(jobs, "w", encoding="utf-8") as f:
            json.dump({"jobs": [RUNNER_DEATH]}, f)
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_watchdog.py"), "decide",
             "--jobs", jobs, "--body", body, "--workflow", "W", "--run-url", "U"],
            capture_output=True, text=True)
        check("a silent run reports silent=true", "silent=true" in out.stdout, out.stdout)
        check("and exits 0", out.returncode == 0, str(out.returncode))
        check("and writes the body", os.path.exists(body))

        with open(jobs, "w", encoding="utf-8") as f:
            json.dump({"jobs": [NORMAL_FAILURE]}, f)
        body2 = os.path.join(d, "body2.md")
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_watchdog.py"), "decide",
             "--jobs", jobs, "--body", body2],
            capture_output=True, text=True)
        check("a self-reported run reports silent=false",
              "silent=false" in out.stdout, out.stdout)
        check("and writes no body at all", not os.path.exists(body2))

        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_watchdog.py"), "decide",
             "--jobs", os.path.join(d, "missing.json")],
            capture_output=True, text=True)
        check("an unreadable payload exits non-zero rather than saying 'nothing wrong'",
              out.returncode == 2, str(out.returncode))
        check("and never prints silent=false",
              "silent=false" not in out.stdout, out.stdout)


def test_path_confinement():
    print("path confinement")
    try:
        rw._inside_repo("../../../etc/passwd")
        check("a path outside the repo is refused", False, "no SystemExit")
    except SystemExit:
        check("a path outside the repo is refused", True)
    check("an empty path resolves to nothing", rw._inside_repo("") == "")


def test_matches_the_workflow():
    """The workflow reads `silent=` off stdout and the watchdog must be in nobody's
    watch list but its own absence -- a watchdog that watches itself loops forever."""
    print("agreement with the workflow")
    path = os.path.join(HERE, "..", ".github", "workflows", "watchdog.yml")
    with open(path, encoding="utf-8") as f:
        y = f.read()
    check("the workflow greps the marker this script prints", "'^silent='" in y)
    check("and gates the issue steps on the value it captured",
          "steps.decide.outputs.silent == 'true'" in y)
    check("the workflow only acts on failures",
          "workflow_run.conclusion == 'failure'" in y, "guard missing")
    name = [ln for ln in y.splitlines() if ln.startswith("name:")][0].split("name:")[1].strip()
    watched_raw = y.split("workflows:")[1].split("types:")[0]
    check("the watchdog does not watch itself", name not in watched_raw, name)

    # A watched name is matched against the workflow's `name:`, so a typo or a rename
    # silently un-watches that workflow -- no error, no coverage, which is precisely the
    # silent failure this module exists to end. Pin every name to a real file.
    wdir = os.path.join(HERE, "..", ".github", "workflows")
    real = {}
    for fn in sorted(os.listdir(wdir)):
        if not fn.endswith(".yml"):
            continue
        with open(os.path.join(wdir, fn), encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("name:"):
                    real[ln.split("name:")[1].strip()] = fn
                    break
    watched = [ln.strip()[2:].strip() for ln in watched_raw.splitlines()
               if ln.strip().startswith("- ")]
    unknown = [w for w in watched if w not in real]
    check("every watched name matches a real workflow", not unknown, str(unknown))

    # The exclusions are a decision, not an oversight: these run against a PR, where a
    # failure is already visible in the checks list.
    expected_unwatched = {"CI", "Ruff lint", "opinions-diagnose", "opinions-review-veto"}
    actual_unwatched = set(real) - set(watched) - {name}
    check("only the PR-time workflows are left unwatched",
          actual_unwatched == expected_unwatched,
          "unwatched=%s" % sorted(actual_unwatched))


def main():
    for t in (test_the_two_real_runs, test_the_rule, test_selection, test_body,
              test_cli, test_path_confinement, test_matches_the_workflow):
        t()
    n = len(FAILURES)
    print()
    if n:
        print("%d CHECK(S) FAILED" % n)
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
