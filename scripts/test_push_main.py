#!/usr/bin/env python3
"""Hermetic tests for scripts/push_main.sh -- the helper that writes main.

Standard library plus the local `git` binary; no network. Each test builds a real bare
repository in a temp dir to stand in for origin, so these exercise git's actual
fast-forward and rebase behavior rather than a mock of it.

Why this file exists: push_main.sh is 32 lines that push to main from four workflows in
independent concurrency groups (the 4-hourly funnel, review-apply on a PR merge, the daily
keepalive, the golden-set build). GitHub does not serialize across concurrency groups, so
two of them landing in the same window is routine, and with a plain `git push` the loser's
committed work is silently dropped. That is the failure this script prevents and the
property these tests pin. Two write-to-main incidents in this project came from untested
shell; publish.py was extracted to fix one of them, and this covers what was left.

Run directly: `python scripts/test_push_main.py`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PUSH_MAIN = os.path.join(HERE, "push_main.sh")

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def git(*args, cwd, check_rc=True, env=None):
    e = dict(os.environ)
    e.update({"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "T",
              "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    if env:
        e.update(env)
    r = subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True, env=e)
    if check_rc and r.returncode != 0:
        raise AssertionError("git %s failed in %s: %s%s" % (" ".join(args), cwd, r.stdout, r.stderr))
    return r


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def run_push_main(cwd, backoff="0", outage_backoff="0", extra_env=None):
    """Invoke the real script the way a workflow does.

    Both backoffs default to 0 so the exhaustion paths cost no wall-clock. With real
    defaults the outage ladder alone sleeps 15+30+60+120+240s.
    """
    e = dict(os.environ)
    e.update({"PUSH_MAIN_BACKOFF": backoff, "PUSH_MAIN_OUTAGE_BACKOFF": outage_backoff,
              "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
              "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
              "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    if extra_env:
        e.update(extra_env)
    return subprocess.run(["bash", PUSH_MAIN], cwd=cwd, capture_output=True, text=True,
                          env=e, timeout=120)


def classify(message):
    """Run the script's own is_collision() against a message.

    Sourcing push_main.sh would run its loop, so the function is lifted out by its
    literal text. That keeps this pinned to the real code rather than a restatement of
    it: edit the classifier and this test sees the edit.
    """
    src = open(PUSH_MAIN, encoding="utf-8").read()
    start = src.index("is_collision()")
    end = src.index("\n}\n", start) + 3
    prog = src[start:end] + '\nis_collision "$1" && echo COLLISION || echo OUTAGE\n'
    r = subprocess.run(["bash", "-c", prog, "_", message], capture_output=True, text=True)
    return r.stdout.strip()


def reject_hook(origin, message):
    """Make every push to `origin` fail, with `message` relayed back to the client.

    git prefixes hook stderr with "remote: " and appends its own "(pre-receive hook
    declined)" line, so the phrase lands in the client-side output the script classifies
    -- which is what lets one helper drive both ladders.
    """
    hooks = os.path.join(origin, "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-receive")
    write(path, "#!/bin/sh\necho '%s' >&2\nexit 1\n" % message)
    os.chmod(path, 0o755)


def setup(tmp):
    """A bare 'origin' with one commit on main, plus a clone that mimics a workflow checkout."""
    origin = os.path.join(tmp, "origin.git")
    git("init", "--bare", "--initial-branch=main", origin, cwd=tmp)
    seed = os.path.join(tmp, "seed")
    git("clone", origin, seed, cwd=tmp)
    write(os.path.join(seed, "data.json"), "base\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "base", cwd=seed)
    git("push", "origin", "HEAD:main", cwd=seed)
    return origin, seed


def clone(tmp, origin, name):
    path = os.path.join(tmp, name)
    git("clone", origin, path, cwd=tmp)
    return path


def log_subjects(repo, ref="HEAD"):
    return git("log", "--format=%s", ref, cwd=repo).stdout.split()


def test_clean_push():
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "runA")
        write(os.path.join(run, "card.json"), "one\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "cardA", cwd=run)
        r = run_push_main(run)
        check("uncontested push exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("uncontested push lands on origin/main",
              "cardA" in git("log", "--format=%s", "main", cwd=origin).stdout)


def test_concurrent_push_preserves_both():
    """THE property: two runs commit independently, both survive."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        slow = clone(tmp, origin, "slow")     # this run committed first...
        fast = clone(tmp, origin, "fast")     # ...but this one pushes first

        write(os.path.join(slow, "cards.json"), "slow work\n")
        git("add", "-A", cwd=slow)
        git("commit", "-m", "slowwork", cwd=slow)

        write(os.path.join(fast, "keepalive.txt"), "tick\n")
        git("add", "-A", cwd=fast)
        git("commit", "-m", "keepalive", cwd=fast)
        git("push", "origin", "HEAD:main", cwd=fast)   # main advances under `slow`

        # A plain push from `slow` would now be rejected non-fast-forward and its work lost.
        plain = git("push", "origin", "HEAD:main", cwd=slow, check_rc=False)
        check("a plain push would indeed be rejected here", plain.returncode != 0)

        r = run_push_main(slow)
        check("push_main resolves the collision (exit 0)", r.returncode == 0, r.stdout + r.stderr)
        subjects = git("log", "--format=%s", "main", cwd=origin).stdout.split()
        check("the other run's commit survives", "keepalive" in subjects)
        check("this run's commit survives too", "slowwork" in subjects, str(subjects))
        check("no work was dropped", subjects.count("slowwork") == 1 and subjects.count("keepalive") == 1)


def test_conflict_fails_loudly_without_force():
    """An unresolvable rebase must fail, and must NOT force-push over the other run."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        a = clone(tmp, origin, "a")
        b = clone(tmp, origin, "b")

        write(os.path.join(b, "data.json"), "theirs\n")
        git("add", "-A", cwd=b)
        git("commit", "-m", "theirs", cwd=b)
        git("push", "origin", "HEAD:main", cwd=b)
        before = git("rev-parse", "main", cwd=origin).stdout.strip()

        write(os.path.join(a, "data.json"), "mine\n")     # same file, same region
        git("add", "-A", cwd=a)
        git("commit", "-m", "mine", cwd=a)

        r = run_push_main(a)
        check("a rebase conflict exits nonzero", r.returncode == 1, r.stdout + r.stderr)
        check("the conflict is annotated for the run log", "::error::" in (r.stdout + r.stderr))
        after = git("rev-parse", "main", cwd=origin).stdout.strip()
        check("origin/main is left untouched on conflict", before == after)
        check("the other run's commit is still the tip",
              "theirs" in git("log", "--format=%s", "-1", "main", cwd=origin).stdout)
        # Checked against the on-disk state dirs, not `git status`. The obvious spelling
        # -- grepping porcelain=v2 for "REBASE" -- is vacuous: that format never emits the
        # word, so the assertion passed no matter what, and mutation testing found it by
        # deleting the `git rebase --abort` line without a single check noticing.
        gitdir = os.path.join(a, git("rev-parse", "--git-dir", cwd=a).stdout.strip())
        leftovers = [d for d in ("rebase-merge", "rebase-apply")
                     if os.path.exists(os.path.join(gitdir, d))]
        check("no rebase is left in progress (the tree is not wedged)", not leftovers, str(leftovers))
        check("and HEAD is back on a branch, not left detached mid-rebase",
              "(detached)" not in git("status", "--porcelain=v2", "--branch", cwd=a).stdout)


def test_retry_exhaustion_is_bounded_and_loud():
    """A permanently rejecting origin must stop after 5 attempts, not loop forever."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "run")
        # Stands in for an origin that keeps moving: rejects with git's own collision wording,
        # so the script takes the fast ladder exactly as it would against a real race.
        reject_hook(origin, "fetch first")

        write(os.path.join(run, "x.txt"), "x\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "mine", cwd=run)

        r = run_push_main(run)
        out = r.stdout + r.stderr
        check("exhausted retries exit nonzero", r.returncode == 1, out[-400:])
        check("exhausted retries are annotated", "could not fast-forward main after 5 attempts" in out)
        check("it retried exactly 5 times", out.count("rejected (main advanced under us)") == 5,
              str(out.count("rejected (main advanced under us)")))
        check("a collision never spends the outage budget",
              "may be degraded" not in out, out[-400:])


# ---------------------------------------------------------------------------
# Outages. Everything above is about losing a race with another run, which resolves in
# seconds. Everything below is about GitHub being unwell, which does not -- and which
# costs strictly more, because the push is the last thing a run does: by the time it
# fails, the funnel's model calls, the sweep's classifications and the golden set's
# rebuild have all been paid for, committed, and are about to be thrown away.
# ---------------------------------------------------------------------------


def test_real_git_rejection_wording_is_classified_as_a_collision():
    """The classifier is matched against what git actually prints, not what I remember it
    printing. Captured from a genuine non-fast-forward, not hand-written."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        stale = clone(tmp, origin, "stale")
        other = clone(tmp, origin, "other")

        write(os.path.join(other, "o.txt"), "o\n")
        git("add", "-A", cwd=other)
        git("commit", "-m", "other", cwd=other)
        git("push", "origin", "HEAD:main", cwd=other)

        write(os.path.join(stale, "s.txt"), "s\n")
        git("add", "-A", cwd=stale)
        git("commit", "-m", "stale", cwd=stale)
        rejected = git("push", "origin", "HEAD:main", cwd=stale, check_rc=False)
        real = rejected.stdout + rejected.stderr

        check("the captured output is a genuine rejection", rejected.returncode != 0, real)
        check("git's real rejection wording classifies as a collision",
              classify(real) == "COLLISION", real)


def test_unrecognised_failures_are_treated_as_outages():
    """The safe default. An unfamiliar error is far more likely to be GitHub having a bad
    day than a collision, and guessing wrong in this direction only costs time."""
    for msg in ("fatal: unable to access 'https://github.com/x.git/': The requested URL "
                "returned error: 503",
                "fatal: unable to access 'https://github.com/x.git/': Could not resolve host",
                "error: RPC failed; curl 56 GnuTLS recv error (-54)",
                "remote: Internal Server Error",
                ""):
        check("classified as an outage: %r" % (msg[:48] or "<empty stderr>"),
              classify(msg) == "OUTAGE")


def test_outage_exhaustion_uses_the_long_ladder():
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "run")
        reject_hook(origin, "Internal Server Error")

        write(os.path.join(run, "x.txt"), "x\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "mine", cwd=run)

        r = run_push_main(run)
        out = r.stdout + r.stderr
        check("an outage that outlasts the budget exits nonzero", r.returncode == 1, out[-400:])
        check("it is annotated as an outage, not as a collision",
              "outlasted the retry budget" in out and "could not fast-forward" not in out,
              out[-500:])
        check("it tried 6 times, not 5", out.count("the remote may be degraded") == 6,
              str(out.count("the remote may be degraded")))
        check("the lost work is called out so the run log says what to do",
              "re-run once GitHub recovers" in out)


def test_a_failed_fetch_is_an_outage_not_a_conflict():
    """The regression this rewrite exists for.

    During an outage the fetch fails before the push does. Swallow that error and the next
    line -- `git rebase FETCH_HEAD` -- dies with "invalid upstream", which looks exactly
    like a rebase conflict and exits at once. The retry budget would then never be reached
    in the only situation it was written for.
    """
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "run")
        write(os.path.join(run, "x.txt"), "x\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "mine", cwd=run)
        # origin vanishes after the checkout, exactly as an unreachable github.com would.
        git("remote", "set-url", "origin", os.path.join(tmp, "gone.git"), cwd=run)

        r = run_push_main(run)
        out = r.stdout + r.stderr
        check("an unreachable origin exits nonzero", r.returncode == 1, out[-400:])
        check("it is NOT misreported as a rebase conflict",
              "manual resolution needed" not in out, out[-500:])
        check("the fetch failure went down the outage ladder",
              out.count("could not fetch origin/main") == 6,
              str(out.count("could not fetch origin/main")))
        check("and it is annotated as an outage", "outlasted the retry budget" in out)


def test_the_outage_ladder_actually_waits():
    """Backoff of 0 everywhere else keeps the suite fast, which would also let a ladder that
    silently never sleeps pass every other test. Prove the sleeps are real and geometric."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "run")
        reject_hook(origin, "Internal Server Error")
        write(os.path.join(run, "x.txt"), "x\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "mine", cwd=run)

        started = time.time()
        # base 1 -> 1+2+4+8+16 = 31s of sleeping across the five waits before the sixth
        # attempt gives up. Slow for a unit test, but it is the only way to show the wait
        # exists at all, and 31s is the whole cost of knowing a ten-minute budget is real.
        r = run_push_main(run, outage_backoff="1")
        elapsed = time.time() - started
        out = r.stdout + r.stderr
        check("the ladder waited roughly the geometric sum, not zero",
              29 <= elapsed < 90, "%.1fs" % elapsed)
        check("and the waits it logged are geometric",
              all(("waiting %ds" % w) in out for w in (1, 2, 4, 8, 16)), out[-600:])


def test_no_force_push_anywhere():
    # Read the CODE, not the prose: the header comment discusses `git push` and would
    # otherwise be counted as a second push site.
    code = "\n".join(ln for ln in open(PUSH_MAIN, encoding="utf-8").read().splitlines()
                     if ln.strip() and not ln.strip().startswith("#"))
    pushes = [ln.strip() for ln in code.splitlines() if "git push" in ln]
    check("the script has exactly one push", len(pushes) == 1, str(pushes))
    push = pushes[0] if pushes else ""
    check("that push targets main", "HEAD:main" in push, push)
    check("that push carries no force flag",
          not any(flag in push.split() for flag in ("--force", "-f", "--force-with-lease")), push)


def main():
    print("push_main.sh:")
    test_clean_push()
    test_concurrent_push_preserves_both()
    test_conflict_fails_loudly_without_force()
    test_retry_exhaustion_is_bounded_and_loud()
    test_real_git_rejection_wording_is_classified_as_a_collision()
    test_unrecognised_failures_are_treated_as_outages()
    test_outage_exhaustion_uses_the_long_ladder()
    test_a_failed_fetch_is_an_outage_not_a_conflict()
    test_the_outage_ladder_actually_waits()
    test_no_force_push_anywhere()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    if not shutil.which("git"):
        print("git not available; skipping")
        sys.exit(0)
    sys.exit(main())
