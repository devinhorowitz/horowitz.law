#!/usr/bin/env python3
"""Hermetic tests for scripts/gh_retry.sh -- the wrapper around every alerting `gh` call.

No network and no real `gh`: a stub binary is put on PATH that emits a scripted stdout,
stderr and exit code per attempt, so these exercise the wrapper's actual classification and
retry loop rather than a description of it.

The property that matters most is the quiet one. Callers do

    num=$(gh issue list --repo "$R" --state open --search "..." --json number --jq '...')

so a single line of retry chatter on stdout does not just look untidy -- it becomes `$num`,
and the next line comments on issue "gh_retry: attempt 1 ...". Every log line in the wrapper
goes to stderr for that reason, and test_stdout_is_never_contaminated is what keeps it there.

Run directly: `python scripts/test_gh_retry.py`.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GH_RETRY = os.path.join(HERE, "gh_retry.sh")
WORKFLOWS = os.path.join(HERE, "..", ".github", "workflows")

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def run_with_stub(tmp, script, args=("issue", "create", "--title", "x"), backoff="0", tries=None):
    """Put a stub `gh` on PATH and invoke the wrapper.

    `script` is bash run inside the stub with $N set to the 1-based attempt number, so a
    test can make gh behave differently on each call.
    """
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    counter = os.path.join(tmp, "n")
    stub = os.path.join(bindir, "gh")
    with open(stub, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n"
                "N=$(( $(cat %s 2>/dev/null || echo 0) + 1 ))\n"
                "echo $N > %s\n" % (counter, counter) + script + "\n")
    os.chmod(stub, 0o755)

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["GH_RETRY_BACKOFF"] = backoff
    if tries is not None:
        env["GH_RETRY_TRIES"] = tries
    r = subprocess.run(["bash", GH_RETRY] + list(args), capture_output=True, text=True,
                       env=env, timeout=180)
    attempts = int(open(counter).read().strip()) if os.path.exists(counter) else 0
    return r, attempts


# gh's real wording, transcribed from its source and from run logs. Retrying any of these is
# the whole point; retrying anything in the second list is a bug.
TRANSIENT = [
    "error connecting to api.github.com",
    "HTTP 503: Service Unavailable (https://api.github.com/repos/x/y/issues)",
    "HTTP 502: Bad gateway",
    "HTTP 500: Internal Server Error",
    "API rate limit exceeded for installation",
    "You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
    "dial tcp: lookup api.github.com: no such host",
    "net/http: TLS handshake timeout",
    "read tcp 10.0.0.1:443: connection reset by peer",
    "context deadline exceeded",
    "HTTP 408: Request Timeout",
]

PERMANENT = [
    "GraphQL: Could not resolve to a Repository with the name 'x/y'. (repository)",
    "HTTP 404: Not Found (https://api.github.com/repos/x/y/issues)",
    "GraphQL: Resource not accessible by integration",
    "unknown flag: --titel",
    "could not add label: 'nonexistent' not found",
    "HTTP 422: Validation Failed",
    "gh: Not Found (HTTP 404)",
]


def test_classification():
    print("classification")
    with tempfile.TemporaryDirectory() as tmp:
        for msg in TRANSIENT:
            # tries=2 so a retried command makes exactly 2 attempts; 1 means it gave up.
            r, n = run_with_stub(tmp, 'echo %s >&2; exit 1' % _q(msg), tries="2")
            check("retried: %s" % msg[:52], n == 2, "attempts=%d" % n)
            shutil.rmtree(os.path.join(tmp, "bin")); os.remove(os.path.join(tmp, "n"))
        for msg in PERMANENT:
            r, n = run_with_stub(tmp, 'echo %s >&2; exit 1' % _q(msg), tries="5")
            check("not retried: %s" % msg[:52], n == 1, "attempts=%d" % n)
            shutil.rmtree(os.path.join(tmp, "bin")); os.remove(os.path.join(tmp, "n"))


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def test_success_passes_through():
    print("the ordinary path")
    with tempfile.TemporaryDirectory() as tmp:
        r, n = run_with_stub(tmp, 'echo "1234"; exit 0', args=("issue", "list", "--json", "number"))
        check("a successful gh exits 0", r.returncode == 0, r.stderr)
        check("its stdout is passed through verbatim", r.stdout == "1234\n", repr(r.stdout))
        check("and it is called exactly once", n == 1, str(n))


def test_recovery_mid_ladder():
    print("recovery")
    with tempfile.TemporaryDirectory() as tmp:
        r, n = run_with_stub(
            tmp,
            'if [ "$N" -lt 3 ]; then echo "HTTP 503: Service Unavailable" >&2; exit 1; fi\n'
            'echo "77"; exit 0')
        check("a blip that clears is retried until it does", n == 3, str(n))
        check("and the wrapper reports success", r.returncode == 0, r.stderr)
        check("the caller still gets clean output", r.stdout == "77\n", repr(r.stdout))


def test_stdout_is_never_contaminated():
    """The failure this would cause is silent and absurd: `$num` becomes a log line, and the
    next command comments on an issue named after it."""
    print("stdout hygiene")
    with tempfile.TemporaryDirectory() as tmp:
        r, _ = run_with_stub(
            tmp,
            'if [ "$N" -lt 3 ]; then echo "HTTP 502: Bad gateway" >&2; exit 1; fi\n'
            'echo "88"; exit 0',
            args=("issue", "list", "--json", "number"))
        check("retry chatter does not reach stdout", r.stdout == "88\n", repr(r.stdout))
        check("the chatter went to stderr instead", "retrying in" in r.stderr, r.stderr[:200])

    with tempfile.TemporaryDirectory() as tmp:
        # Exhaustion: the annotation must not reach stdout either.
        r, _ = run_with_stub(tmp, 'echo "HTTP 503: Service Unavailable" >&2; exit 1',
                             args=("issue", "list", "--json", "number"))
        check("the give-up annotation does not reach stdout either", r.stdout == "", repr(r.stdout))

    with tempfile.TemporaryDirectory() as tmp:
        # And the real shape: command substitution, exactly as the workflows write it.
        bindir = os.path.join(tmp, "bin"); os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write('#!/usr/bin/env bash\nn=$(cat %s 2>/dev/null || echo 0); n=$((n+1)); echo $n > %s\n'
                    'if [ "$n" -lt 2 ]; then echo "HTTP 503: Service Unavailable" >&2; exit 1; fi\n'
                    'echo 4321\n' % (os.path.join(tmp, "n"), os.path.join(tmp, "n")))
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        env["GH_RETRY_BACKOFF"] = "0"
        got = subprocess.run(
            ["bash", "-c", 'num=$(bash "$1" issue list --json number --jq ".[0].number" 2>/dev/null || echo ""); '
                           'echo "num=[$num]"', "_", GH_RETRY],
            capture_output=True, text=True, env=env, timeout=60)
        check("a caller's $( ) capture sees only gh's output",
              got.stdout.strip() == "num=[4321]", repr(got.stdout))


def test_exhaustion_is_loud_and_preserves_the_exit_code():
    print("exhaustion")
    with tempfile.TemporaryDirectory() as tmp:
        r, n = run_with_stub(tmp, 'echo "HTTP 503: Service Unavailable" >&2; exit 1')
        check("it stops after the default 5 attempts", n == 5, str(n))
        check("it exits with gh's status, so `|| true` still works", r.returncode == 1, str(r.returncode))
        check("giving up is a workflow annotation, not silence",
              "::warning::gh_retry:" in r.stderr, r.stderr[-300:])
        check("the annotation says the alert was lost",
              "this alert was not delivered" in r.stderr, r.stderr[-300:])
        check("and it quotes the underlying error", "Service Unavailable" in r.stderr)


def test_permanent_failure_exit_code_is_gh_s_own():
    print("exit codes")
    with tempfile.TemporaryDirectory() as tmp:
        r, n = run_with_stub(tmp, 'echo "HTTP 404: Not Found" >&2; exit 4')
        check("a permanent failure is not retried", n == 1, str(n))
        check("and the wrapper exits with gh's own code, not 1", r.returncode == 4, str(r.returncode))
        check("gh's stderr still reaches the run log", "404" in r.stderr, r.stderr[:200])


def test_the_backoff_actually_sleeps():
    """Every other test sets the backoff to 0, which would also pass if the sleep were gone."""
    print("backoff")
    with tempfile.TemporaryDirectory() as tmp:
        started = time.time()
        r, n = run_with_stub(tmp, 'echo "HTTP 503: Service Unavailable" >&2; exit 1',
                             backoff="1", tries="4")   # 1+2+4 = 7s
        elapsed = time.time() - started
        check("the ladder waits the geometric sum", 6.5 <= elapsed < 25, "%.1fs" % elapsed)
        check("and logs geometric waits",
              all(("retrying in %ds" % w) in r.stderr for w in (1, 2, 4)), r.stderr[-400:])


def test_workflows_route_alerts_through_the_wrapper():
    """A guard against the pattern regrowing. Bare `gh issue` in a workflow is an alert that
    vanishes the next time api.github.com has a bad ten minutes."""
    print("workflow call sites")
    try:
        import yaml
    except ImportError:                                # pragma: no cover
        print("  .. pyyaml not available; skipping the workflow scan")
        return
    # `gh issue` and `gh_retry.sh issue` are disjoint strings, so counting one never counts
    # the other -- which is what lets "bare" mean bare.
    BARE = re.compile(r"(?<![\w./-])gh issue ")
    bare, wrapped, no_checkout = [], 0, []
    for path in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        name = os.path.basename(path)
        for jn, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            co = next((i for i, s in enumerate(steps)
                       if "actions/checkout" in str(s.get("uses", ""))), None)
            for i, st in enumerate(steps):
                run = st.get("run") or ""
                touches = False
                for line in run.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "gh_retry.sh issue" in stripped:
                        wrapped += 1
                        touches = True
                    elif BARE.search(stripped):
                        bare.append("%s / %s: %s" % (name, st.get("name") or jn, stripped[:70]))
                        touches = True
                # The wrapper is a file in the repo; a step that never checked out cannot run it.
                if touches and (co is None or i < co):
                    no_checkout.append("%s / %s" % (name, st.get("name") or jn))

    check("the scan actually found the alert call sites", wrapped >= 40, "%d wrapped" % wrapped)
    check("no workflow still calls `gh issue` bare", not bare, " | ".join(bare[:6]))
    check("every wrapped step runs after its job's checkout", not no_checkout, str(no_checkout[:5]))


def main():
    print("gh_retry.sh:")
    for t in (test_success_passes_through, test_classification, test_recovery_mid_ladder,
              test_stdout_is_never_contaminated, test_exhaustion_is_loud_and_preserves_the_exit_code,
              test_permanent_failure_exit_code_is_gh_s_own, test_the_backoff_actually_sleeps,
              test_workflows_route_alerts_through_the_wrapper):
        t()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
