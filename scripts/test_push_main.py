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


def run_push_main(cwd, backoff="0"):
    """Invoke the real script the way a workflow does."""
    e = dict(os.environ)
    e.update({"PUSH_MAIN_BACKOFF": backoff, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
              "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
              "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    return subprocess.run(["bash", PUSH_MAIN], cwd=cwd, capture_output=True, text=True, env=e)


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
        state = git("status", "--porcelain=v2", "--branch", cwd=a).stdout
        check("no rebase is left in progress (the tree is not wedged)", "REBASE" not in state.upper())


def test_retry_exhaustion_is_bounded_and_loud():
    """A permanently rejecting origin must stop after 5 attempts, not loop forever."""
    with tempfile.TemporaryDirectory() as tmp:
        origin, _ = setup(tmp)
        run = clone(tmp, origin, "run")
        # A pre-receive hook that always rejects: stands in for an origin that keeps moving.
        hooks = os.path.join(origin, "hooks")
        os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "pre-receive")
        write(hook, "#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
        os.chmod(hook, 0o755)

        write(os.path.join(run, "x.txt"), "x\n")
        git("add", "-A", cwd=run)
        git("commit", "-m", "mine", cwd=run)

        r = run_push_main(run, backoff="0")
        out = r.stdout + r.stderr
        check("exhausted retries exit nonzero", r.returncode == 1, out[-400:])
        check("exhausted retries are annotated", "could not fast-forward main after 5 attempts" in out)
        check("it retried exactly 5 times", out.count("rejected (main advanced under us)") == 5,
              str(out.count("rejected (main advanced under us)")))


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
