#!/usr/bin/env python3
"""Guard against a shell shape that fails a workflow step while every command succeeded.

A `run:` block exits with the status of its LAST command. When that last command is a bare
conditional -- `[ -f x ] && { ... }`, or a loop whose final iteration is one -- a false test
is not "nothing to do", it is exit 1, and the step fails with nothing wrong.

That is exactly what broke the Georgia Legislative & Regulatory Watch on 2026-08-02
(run 30740163015). The step ended with:

    for b in pr_body_legislation pr_body_regulations pr_body_courtrules; do
      [ -f "scripts/$b.md" ] && { cat ... ; echo ...; }
    done

A for loop's status is its last iteration's, the last name checked is pr_body_courtrules,
and courtrules.py writes that file only when something changed. So a quiet week in the
federal rules failed the run -- after all three watches had done their work correctly and
written their state. The log tail in the failure issue showed only success, because the
report tails run.log and the failing line is shell that never writes there.

Note what it is NOT: `set -e`. A failing command inside a `&&` list is exempt from that.
This is only ever "the script exits with its last command's status", which is why it needs
its own check rather than being caught by shell strictness.

Run directly: `python scripts/test_workflow_shell.py`.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:                                   # pragma: no cover
    print("pyyaml not available; skipping")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, "..", ".github", "workflows")

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# A bare conditional: a test or a `[ ... ]` chained into an action with && (or ||), with no
# unconditional command after it. `if ... fi` is the safe spelling and is deliberately not
# matched -- an `if` whose condition is false returns 0.
BARE_CONDITIONAL = re.compile(r"^(\[\[?|test\s|grep\s|\[\s).*(&&|\|\|)")
BLOCK_CLOSERS = ("fi", "done", "esac", "else", "}")


def last_command(run_block):
    """The command whose status becomes the step's, ignoring block closers and comments."""
    lines = [ln.strip() for ln in run_block.strip().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    for ln in reversed(lines):
        if ln in BLOCK_CLOSERS:
            continue
        return ln
    return ""


def steps():
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        for jname, job in (doc.get("jobs") or {}).items():
            for st in (job.get("steps") or []):
                if st.get("run"):
                    yield os.path.basename(path), (st.get("name") or jname), st["run"]


def test_the_shape_is_actually_dangerous():
    """Demonstrate the failure mode rather than asserting it from memory -- the whole check
    rests on this being true of real bash."""
    print("the shape itself")
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.sh")
        good = os.path.join(d, "good.sh")
        with open(bad, "w") as f:
            f.write('for b in a b; do\n  [ -f "%s/$b" ] && { echo hit; }\ndone\n' % d)
        with open(good, "w") as f:
            f.write('for b in a b; do\n  if [ -f "%s/$b" ]; then echo hit; fi\ndone\n' % d)
        rc_bad = subprocess.run(["bash", "-e", bad], capture_output=True).returncode
        rc_good = subprocess.run(["bash", "-e", good], capture_output=True).returncode
        check("a trailing `[ ] && { }` loop exits non-zero when the test is false",
              rc_bad == 1, "rc=%d" % rc_bad)
        check("the same loop written with `if` exits 0",
              rc_good == 0, "rc=%d" % rc_good)


def test_no_step_ends_on_a_bare_conditional():
    print("every workflow step")
    offenders = []
    seen = 0
    for wf, name, run in steps():
        seen += 1
        tail = last_command(run)
        if BARE_CONDITIONAL.match(tail):
            offenders.append("%s / %s: %s" % (wf, name, tail[:70]))
    check("at least one workflow step was actually scanned", seen > 0, str(seen))
    check("no step's exit status is a bare conditional",
          not offenders, " | ".join(offenders))


def test_relaxed_egress_is_justified_and_marked_temporary():
    """Every workflow that is not on `egress-policy: block` must say why, next to the policy.

    Two are permanently relaxed by design -- links.yml and lighthouse.yml crawl arbitrary
    third-party URLs, so no allowlist can be written in advance. opinions.yml is relaxed
    only as a running experiment into the exit-143 runner kills, and an experiment with no
    end date is just a weakened posture nobody remembers choosing. Requiring the word
    TEMPORARY there means the day someone restores `block`, this check keeps passing with
    nothing else to remember; the day someone leaves it relaxed and silent, it fails.
    """
    print("egress policy")
    PERMANENT = ("links.yml", "lighthouse.yml")   # crawl arbitrary third-party URLs by design
    hardened, relaxed, unjustified, untemporary = [], [], [], []
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
        raw = open(path, encoding="utf-8").read()
        lines = raw.splitlines()
        doc = yaml.safe_load(raw)
        for job in (doc.get("jobs") or {}).values():
            for st in (job.get("steps") or []):
                if "harden-runner" not in str(st.get("uses", "")):
                    continue
                name = os.path.basename(path)
                hardened.append(name)
                if (st.get("with") or {}).get("egress-policy") == "block":
                    continue
                relaxed.append(name)
                # The justification must sit next to the policy, not in a commit message
                # nobody reads while editing. Require a comment in the lines just above it.
                # Deliberately not keyword-matched: links.yml says "audit, not block" and
                # never uses the word "egress", and a check that dictates vocabulary would
                # fail good prose while a missing explanation slipped past on a lucky word.
                # Counting '#' across the whole file would pass anything -- these workflows
                # are heavily commented -- so the window is what makes it mean something.
                idx = next((i for i, ln in enumerate(lines)
                            if ln.strip().startswith("egress-policy:")), None)
                window = lines[max(0, (idx or 0) - 30):(idx or 0)]
                if not any(ln.strip().startswith("#") for ln in window):
                    unjustified.append(name)
                if name not in PERMANENT and "TEMPORARY" not in raw:
                    untemporary.append(name)

    check("harden-runner steps were actually found and parsed",
          len(hardened) >= 10, "%d found" % len(hardened))
    check("every relaxed workflow explains itself in a comment beside the policy",
          not unjustified, str(unjustified))
    check("a relaxed workflow that is not one of the two permanent crawlers is marked TEMPORARY",
          not untemporary, str(untemporary))
    # reclaim-probe.yml is parameterised (`${{ matrix.egress }}`), not relaxed: one arm is
    # block, the other audit, and that contrast IS the experiment. It still has to explain
    # itself and carry TEMPORARY, which the checks above enforce -- a probe that outlives its
    # question is the same problem as a forgotten policy change.
    check("the relaxed/parameterised set is still just the two crawlers plus marked experiments",
          set(relaxed) <= {"links.yml", "lighthouse.yml", "opinions.yml", "reclaim-probe.yml"},
          str(sorted(relaxed)))


def test_the_legislation_step_specifically():
    """The step that broke. Pinned by name because it is the one with three optional body
    files, so it is the one most likely to regrow the shape."""
    print("the step that broke on 2026-08-02")
    run = ""
    for wf, name, r in steps():
        if wf == "legislation.yml" and "watches" in name:
            run = r
    check("the step is still present under a recognisable name", bool(run))
    check("it assembles the combined PR body with `if`, not a trailing `&&`",
          "if [ -f \"scripts/$b.md\" ]; then" in run, last_command(run))
    check("and it still writes every watch's body into the combined one",
          all(b in run for b in ("pr_body_legislation", "pr_body_regulations",
                                 "pr_body_courtrules")))


def main():
    for t in (test_the_shape_is_actually_dangerous,
              test_no_step_ends_on_a_bare_conditional,
              test_relaxed_egress_is_justified_and_marked_temporary,
              test_the_legislation_step_specifically):
        t()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
