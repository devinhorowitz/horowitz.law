#!/usr/bin/env python3
"""Detect workflow failures that reported themselves to nobody.

Every unattended workflow in this repo ends with a `Report a failed run` step guarded by
`if: failure()`, so a broken run opens or comments on a tracking issue instead of failing
silently. That guard has a hole, and on 2026-08-01 the weekly treatment sweep fell through
it: the hosted runner was reclaimed mid-job (exit 143, SIGTERM, "The runner has received a
shutdown signal"), and because `if: failure()` steps run ON the runner, the reporting step
never executed. Its conclusion was `skipped`, not `failure`. Sixteen minutes of work was
lost and the repo said nothing -- zero open issues, no PR, no comment.

A step cannot report the death of the machine it runs on. So this runs somewhere else: a
`workflow_run` watchdog on a fresh runner, which reads the failed run's job steps back out
of the API and decides whether anything got a chance to speak.

THE RULE. Walk each failed job's steps. Find the first step that actually failed. If every
step after it was `skipped`, nothing downstream ran -- including the reporting step -- so
the failure was silent and this watchdog files it. If any later step ran, the workflow's
own handler had its turn and files a better report than this one could, so stay quiet.

Two exclusions matter, both learned from real runs rather than guessed:

  * `Complete job` is dropped before the test. It reports `success` even when the runner
    is torn down, so leaving it in would make the "all skipped" test never fire.

  * Post-steps are NOT treated as evidence that the runner lived. In the healthy-runner
    failure of opinions-maintenance run 30375455076, `Post Run actions/setup-python` was
    itself `skipped` while checkout's and harden-runner's post-steps succeeded. A rule
    keyed on "some post-step was skipped" would have called that silent when it was not:
    its `Report maintenance findings` step ran fine. Only the reporting steps decide.

Validated against both observed shapes -- see test_run_watchdog.py, which carries the two
runs above as fixtures:

  runner death   (treatment 30704271089)     -> silent, file it
  normal failure (maintenance 30375455076)   -> not silent, the workflow spoke for itself

  python scripts/run_watchdog.py decide --jobs jobs.json --body body.md
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safeio  # noqa: E402  (sys.path shim must run first)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Trailing bookkeeping step the runner records even when it was torn down mid-job. It is
# not work, and counting it as work would defeat the whole check.
IGNORED_TRAILING_STEPS = ("Complete job",)

# Conclusions that mean "this step did not complete its work", so it cannot have reported
# anything. `skipped` was the obvious one. `cancelled` was not, and production found it:
# the daily funnel's 2026-08-04 death (run 30875514401) killed the runner WHILE its
# `Report a failed run` step was executing, so that step's conclusion was `cancelled`, the
# all-skipped test came back false, this watchdog concluded the workflow had spoken for
# itself -- and nothing was filed for the failure at all. A step that was cut off mid-run
# is exactly as silent as one that never started.
#
# `None` covers a step the API recorded without a conclusion, which is the same situation
# seen from a slightly different angle.
DID_NOT_RUN = frozenset(("skipped", "cancelled", None))

# A step that exists to file the alert. If one of these FAILED, the job is just as
# unreported as if it had never run -- and unlike the two conclusions above, this is a case
# the reporting step reaches under its own power and still loses.
#
# It is the residual hole in scripts/gh_retry.sh. That wrapper retries a degraded
# api.github.com, but it is a file in the repo, so a step whose job died AT CHECKOUT cannot
# execute it: the `if: failure()` alert step still runs, dies on the missing file, and
# concludes `failure`. Every later step then also concludes `failure`, the all-skipped test
# comes back false, and the watchdog would decide the workflow had spoken for itself. It had
# not. The one moment a checkout fails is a GitHub outage, which is the one moment the alert
# matters -- so the rule has to look at what the step was FOR, not only whether it ran.
#
# Matched on names, because the jobs API returns a step's name and conclusion and nothing
# else -- the `if: failure()` guard that actually defines a reporting step is not in the
# payload. test_run_watchdog.py closes that gap from the other side: it reads the workflow
# files and asserts every `if: failure()` step that files an issue matches one of these, so
# the list cannot drift away from the steps it is supposed to describe.
#
# `issue` is deliberately broad. It sweeps in the `Close the ... issue` recovery steps, but
# those are `if: success()` and so are already `skipped` after a failure; and it would sweep
# in an unrelated step with "issue" in its name, whose only cost is this watchdog filing a
# report that some other handler had covered. A duplicate report is a nuisance. A missed one
# is the entire failure mode this file exists for.
REPORTING_STEP_HINTS = ("report", "alert", "notify", "issue")


def is_reporting_step(step):
    name = (step.get("name") or "").lower()
    return any(h in name for h in REPORTING_STEP_HINTS)


def step_was_silent(step):
    """True when this step cannot have delivered an alert -- it did not run, or it was a
    reporting step that ran and failed."""
    if step.get("conclusion") in DID_NOT_RUN:
        return True
    return is_reporting_step(step) and step.get("conclusion") == "failure"


def _steps(job):
    """A job's steps, tolerating a shape the API did not give us."""
    steps = job.get("steps")
    return steps if isinstance(steps, list) else []


def job_is_silent(job):
    """True when no step after the failure completed its work, so no handler reported it.

    A job that did not fail is never silent: there is nothing to report. A job that failed
    with NO steps at all is treated as silent -- that is the runner dying before it could
    record anything, which is exactly the case this exists to catch.
    """
    if job.get("conclusion") != "failure":
        return False

    steps = [s for s in _steps(job) if s.get("name") not in IGNORED_TRAILING_STEPS]
    first_failure = None
    for i, s in enumerate(steps):
        if s.get("conclusion") == "failure":
            first_failure = i
            break

    # Failed with nothing recorded, or failed at the job level with no step marked failed:
    # either way no reporting step can have run.
    if first_failure is None:
        return True

    after = steps[first_failure + 1:]
    return all(step_was_silent(s) for s in after)


def silent_jobs(payload):
    """The failed jobs in a `gh api .../jobs` payload whose failure went unreported."""
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return []
    return [j for j in jobs if job_is_silent(j)]


def _last_executed(job):
    """The last step that actually ran -- the best available hint at where a killed job
    got to, since no log tail survives a torn-down runner."""
    ran = [s for s in _steps(job)
           if s.get("conclusion") not in (None, "skipped")
           and s.get("name") not in IGNORED_TRAILING_STEPS]
    return ran[-1].get("name") if ran else "(no step completed)"


def issue_body(workflow, run_url, jobs):
    """The tracking-issue body. Deliberately short on diagnosis and long on pointers: the
    run log is the only place the real story survives, and this watchdog cannot see it."""
    lines = [
        "A scheduled run failed **and its own failure-reporting step never delivered an "
        "alert**, so it would otherwise have failed silently.",
        "",
        "Usually that means the hosted runner was reclaimed mid-job (exit 143 / SIGTERM / "
        "\"The runner has received a shutdown signal\") and the reporting step never ran: "
        "an `if: failure()` step runs on the runner, so it cannot report the runner's own "
        "death -- which is why this watchdog runs separately.",
        "",
        "The other shape is a reporting step that ran and failed. That points at GitHub "
        "itself rather than the runner: a checkout that failed leaves the alert step with "
        "no `scripts/gh_retry.sh` to call, and an `api.github.com` degraded for longer "
        "than that wrapper's retry budget leaves it with nowhere to file. Check the run "
        "log for a `gh_retry` warning before assuming the workflow is at fault.",
        "",
        "| workflow | %s |" % (workflow or "(unknown)"),
        "| --- | --- |",
        "| run | %s |" % (run_url or "(unknown)"),
    ]
    for j in jobs:
        lines.append("| job `%s` last ran | %s |" % (j.get("name") or "?", _last_executed(j)))
    lines += [
        "",
        "**Work from this run was almost certainly lost** -- a job killed before its "
        "commit or PR step leaves nothing behind. Check whether the run needs "
        "re-dispatching rather than waiting for the next schedule.",
    ]
    return "\n".join(lines) + "\n"


def _inside_repo(path, default=""):
    """Resolve a caller-supplied path and confine it to the repository.

    Same rule as link_suspects: every path this CLI touches is a file in the Actions
    workspace, so a value like ../../../etc/shadow is refused rather than followed.
    """
    raw = path or default
    if not raw:
        return ""
    full = os.path.realpath(os.path.join(REPO, raw))
    root = os.path.realpath(REPO)
    if full != root and not full.startswith(root + os.sep):
        raise SystemExit("run_watchdog: refusing a path outside the repository: %r" % raw)
    return full


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("decide", help="decide whether a failed run went unreported")
    d.add_argument("--jobs", required=True, help="JSON from the run's jobs endpoint")
    d.add_argument("--body", default="", help="where to write the issue body when silent")
    d.add_argument("--workflow", default="", help="workflow name, for the issue body")
    d.add_argument("--run-url", default="", help="run URL, for the issue body")
    a = p.parse_args(argv)

    try:
        with open(_inside_repo(a.jobs), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        # Fail loud, not closed. If the watchdog cannot read the payload it must not
        # conclude "nothing to see"; that is the exact failure mode it exists to prevent.
        print("watchdog_error=%s" % e, file=sys.stderr)
        print("silent=unknown")
        return 2

    jobs = silent_jobs(payload)
    print("silent=%s" % ("true" if jobs else "false"))
    print("silent_jobs=%d" % len(jobs))
    if jobs and a.body:
        safeio.atomic_write_text(_inside_repo(a.body), issue_body(a.workflow, a.run_url, jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
