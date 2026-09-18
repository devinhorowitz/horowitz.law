#!/usr/bin/env python3
"""Every workflow that commits via a PR must commit the state file its own script writes.

THE BUG THIS EXISTS TO CATCH.

Commit 1807fba (2026-07-25) regenerated every `add-paths` list from render's OUTPUT_PATHS,
so each PR-opening workflow would commit exactly the files render owns. That is right for
render outputs and wrong for anything else on the list, and `treatment_state.json` -- the
treatment sweep's progress -- is not a render output. It fell off.

Nothing failed. The sweep kept running on its schedule and kept reporting success, because
it does its work, writes its state to disk, and then hands a fixed list of paths to
create-pull-request, which stages that list and nothing else. The state file was written and
thrown away on every run.

Two weeks later the damage was: 17 consecutive green runs that committed no progress; the
same 3 never-swept cards taking a full-history CourtListener crawl every 6 hours with the
results discarded each time; and 12 cards added after 2026-07-25 that were never swept at
all, because a card deferred by TREATMENT_FIRST_PER_RUN is "left untouched so it sorts first
again next run" -- which only works if next run can see that the others were done.

That is the worst shape a failure can take here: no error, no alert, a green check, and
silent loss. The funnel's own state is safe by a different mechanism (it commits straight to
main through push_main.sh rather than through an add-paths list), so no existing check
covered this one.

THE RULE. For each workflow that opens a PR with an `add-paths` list, find the scripts it
runs, read the `*_state.json` filenames those scripts name, and require every one to be on
the list. Derived from the source both times rather than hardcoded, so a new watch with a
new state file is covered the day it is written, with nothing to remember.

Run directly: `python scripts/test_workflow_state_paths.py`.
"""
import glob
import os
import re
import sys

try:
    import yaml
except ImportError:                                   # pragma: no cover
    print("pyyaml not available; skipping")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, "..", ".github", "workflows")

FAILS = []
CHECKS = [0]

STATE_JSON = re.compile(r"\b([a-z_]+_state\.json)\b")
RUNS_SCRIPT = re.compile(r"python3? +scripts/([a-z_]+)\.py")


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def state_files_written_by(script_name):
    """The state filenames a script names. Read as text, not imported: importing pulls in
    update.py and the whole render stack for what is a one-line question."""
    path = os.path.join(HERE, script_name + ".py")
    if not os.path.exists(path):
        return set()
    found = set()
    for line in open(path, encoding="utf-8"):
        # Only the definition, so a comment mentioning another watch's file cannot create a
        # phantom requirement.
        if "STATE_PATH" in line and "=" in line:
            found |= set(STATE_JSON.findall(line))
    return found


def pr_workflows():
    """(workflow, add-paths list, scripts it runs) for every workflow opening a PR."""
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            steps = job.get("steps") or []
            scripts = set()
            for st in steps:
                scripts |= set(RUNS_SCRIPT.findall(st.get("run") or ""))
            for st in steps:
                paths = (st.get("with") or {}).get("add-paths")
                if paths:
                    yield (os.path.basename(path),
                           [p.strip() for p in str(paths).split("\n") if p.strip()],
                           scripts)


def persisted_logs_written_by(script_name):
    """The persisted .jsonl logs a script REWRITES wholesale (not just appends to).

    The STATE_JSON rule above only sees `*_state.json`, so a `.jsonl` a script rewrites is
    outside it -- and that is exactly where the next instance of this bug landed."""
    path = os.path.join(HERE, script_name + ".py")
    if not os.path.exists(path):
        return set()
    src = open(path, encoding="utf-8").read()
    found = set()
    # A wholesale rewrite is the dangerous shape: an append is merge-friendly, a full rewrite
    # is a point-in-time snapshot that goes stale on a branch.
    if "atomic_write_text" in src and "REJECT_PATH" in src:
        found.add("opinions_rejections.jsonl")
    # The legislative watch's recall log. It is APPEND-only (log_drops), so it is safe on a review
    # branch where the opinions log is not -- but it still has to reach main by one route or the
    # other, which is what this check is for.
    if "atomic_write_text" in src and "DROPS_PATH" in src:
        found.add("legislation_rejections.jsonl")
    return found


def commits_outside_add_paths(wf_name, filename):
    """True when the workflow explicitly commits `filename` and pushes it to main itself.

    That is the OTHER legitimate way to persist a file -- the funnel's own mechanism, and what
    smell.yml does with this very log -- so it must count, or the check below would force a
    file back onto a review branch where it does not belong."""
    doc = yaml.safe_load(open(os.path.join(WORKFLOWS, wf_name), encoding="utf-8"))
    for job in (doc.get("jobs") or {}).values():
        for st in (job.get("steps") or []):
            run = st.get("run") or ""
            if filename in run and "git add" in run and "push_main.sh" in run:
                return True
    return False


def main():
    print("workflow add-paths vs the state files their scripts write:")
    seen_any, checked = 0, 0
    for wf, paths, scripts in pr_workflows():
        seen_any += 1

        # A `|` block scalar has no comments: a '#' line is a path pattern, and the action
        # would look for a file literally named "# ...". Cheap to get wrong while adding the
        # explanation that belongs above the key.
        leaked = [p for p in paths if p.startswith("#")]
        check("%s: add-paths carries no comment lines" % wf, not leaked, str(leaked[:2]))

        wanted = set()
        for s in sorted(scripts):
            wanted |= state_files_written_by(s)
        for state in sorted(wanted):
            checked += 1
            check("%s commits %s (written by a script it runs)" % (wf, state),
                  state in paths,
                  "add-paths has %s" % [p for p in paths if p.endswith(".json")])

        # The 2026-09-17 instance, one file-type over. queue_cases.stamp_audits rewrites
        # opinions_rejections.jsonl wholesale; on 2026-09-12 that file was added to this
        # workflow's add-paths so the stamp would travel. It travelled -- onto a review branch,
        # where it became a snapshot of a file the 4-hourly funnel appends to. PR #336 sat three
        # days, main gained 131 records, and the merge conflicted: the queue.txt drain and two
        # audit verdicts could not land until the log was reconciled by hand. The file now goes
        # straight to main instead. Either route is fine; NEITHER is not.
        wanted_logs = set()
        for s_ in sorted(scripts):
            wanted_logs |= persisted_logs_written_by(s_)
        for log in sorted(wanted_logs):
            checked += 1
            check("%s persists %s (add-paths, or its own commit+push_main)" % (wf, log),
                  log in paths or commits_outside_add_paths(wf, log),
                  "not in add-paths and no git add/push_main.sh step names it")

    check("PR-opening workflows were actually found", seen_any >= 3, str(seen_any))
    # Without this the suite passes trivially the day the regexes stop matching -- which is
    # the same silent-green failure the whole file is about.
    check("and at least one state file requirement was derived", checked >= 3, str(checked))

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
