#!/usr/bin/env python3
"""Hermetic unit test for the dependency-bump reviewer (scripts/dep_review.py), no network.

Stubs update.anthropic_json. Pins the safety-critical behavior: it reviews ONLY major bumps (minor/
patch and unparseable titles are skipped), it grounds the prompt in the repo's usage note plus the
changelog, and -- the load-bearing property -- any failure, non-major, or empty verdict leaves NO
comment file so the workflow posts nothing.

Run directly: `python scripts/test_dep_review.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dep_review  # noqa: E402
import update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Stub:
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def __call__(self, body, label="call"):
        self.calls.append(body)
        if isinstance(self.ret, Exception):
            raise self.ret
        return self.ret


def run_main(title, body, ret, tmp):
    """Drive dep_review.main() with a stubbed API and env, returning (rc, comment-or-None)."""
    prev = {k: os.environ.get(k) for k in ("DEP_PR_TITLE", "DEP_PR_BODY", "DEP_REVIEW_OUT")}
    real = update.anthropic_json
    os.environ["DEP_PR_TITLE"] = title
    os.environ["DEP_PR_BODY"] = body
    os.environ["DEP_REVIEW_OUT"] = tmp
    if os.path.exists(tmp):
        os.remove(tmp)
    update.anthropic_json = Stub(ret)
    try:
        rc = dep_review.main()
    finally:
        update.anthropic_json = real
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    comment = None
    if os.path.exists(tmp):
        with open(tmp, encoding="utf-8") as f:
            comment = f.read()
        os.remove(tmp)
    return rc, comment


def main():
    print("dependency-bump review:")

    # --- classify: major vs minor/patch, prefixes, v-prefix, group updates ---
    c = dep_review.classify("Bump pypdf from 6.14.2 to 7.0.0")
    check("major bump is classified major", c and c["is_major"] and c["dep"] == "pypdf" and c["new"] == "7.0.0")
    check("minor bump is not major", dep_review.classify("Bump pypdf from 6.14.2 to 6.15.0")["is_major"] is False)
    check("patch bump is not major", dep_review.classify("Bump pyyaml from 6.0.2 to 6.0.3")["is_major"] is False)
    check("prefixed title parses", (dep_review.classify("deps: bump pypdf from 6.14.2 to 7.0.0") or {}).get("is_major"))
    ca = dep_review.classify("ci: bump actions/setup-node from 6.0.0 to 7.0.0")
    check("action major (v6->v7) parses with owner/name", ca and ca["is_major"] and ca["dep"] == "actions/setup-node")
    check("v-prefixed versions parse", (dep_review.classify("Bump actions/checkout from v7.0.0 to v8.0.0") or {}).get("is_major"))
    check("multi-major jump (6->8) is major", dep_review.classify("Bump x from 6.1.0 to 8.0.0")["is_major"])
    check("group update is unclassifiable (None)", dep_review.classify("Bump the pip group with 2 updates") is None)

    # --- usage_note: known dep specific, action generic, unknown generic ---
    check("pypdf usage note is specific", "extract_text" in dep_review.usage_note("pypdf"))
    check("action usage note is the generic action one", "GitHub Actions step" in dep_review.usage_note("actions/setup-node"))
    check("unknown dep gets a generic note", "No specific usage note" in dep_review.usage_note("leftpad"))

    # --- build_request: grounded and well-formed ---
    req = dep_review.build_request("pypdf", "6.14.2", "7.0.0", "CHANGELOG: removed extract_text", "USAGE NOTE", model="claude-fable-5")
    check("build_request pins the model", req["model"] == "claude-fable-5")
    user = req["messages"][0]["content"]
    check("request carries the usage note", "USAGE NOTE" in user)
    check("request carries the changelog and versions", "CHANGELOG" in user and "6.14.2" in user and "7.0.0" in user)
    check("system tells it to ignore unused-feature breakage", "disregard breaking changes to features it does not use" in req["system"])

    # --- format_comment ---
    md = dep_review.format_comment({
        "verdict": "caution",
        "summary": "7.0 removes the legacy extract_text kwarg this repo does not pass.",
        "concerns": ["extract_text signature changed"],
        "checks": ["confirm reader.pages still iterates"],
        "confidence": "medium",
    }, "pypdf", "6.14.2", "7.0.0")
    check("comment shows the bump and verdict", "pypdf" in md and "6.14.2 → 7.0.0" in md and "caution" in md)
    check("comment renders concerns and checks", "signature changed" in md and "still iterates" in md)
    check("comment carries the advisory/held caveat", "held for your review" in md)
    check("empty verdict renders no comment", dep_review.format_comment({}, "pypdf", "6.0", "7.0") == "")

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dep_review_test_tmp.md")

    verdict = {"verdict": "hold", "summary": "extract_text removed.", "concerns": ["core path breaks"],
               "checks": ["migrate to the new reader API"], "confidence": "high"}

    # --- main(): major bump writes a comment ---
    rc, comment = run_main("Bump pypdf from 6.14.2 to 7.0.0", "changelog: extract_text removed", verdict, tmp)
    check("main() exits 0 and writes a comment for a major bump", rc == 0 and comment is not None and "hold" in (comment or ""))

    # --- main(): minor bump -> no review, no file ---
    rc, comment = run_main("Bump pypdf from 6.14.2 to 6.15.0", "changelog", verdict, tmp)
    check("main() skips a minor bump (no file)", rc == 0 and comment is None)

    # --- main(): unparseable/group title -> no file ---
    rc, comment = run_main("Bump the pip group with 2 updates", "changelog", verdict, tmp)
    check("main() skips an unparseable title (no file)", rc == 0 and comment is None)

    # --- main(): API failure -> best-effort, no file ---
    rc, comment = run_main("Bump pypdf from 6.14.2 to 7.0.0", "changelog", RuntimeError("no key"), tmp)
    check("main() swallows an API failure and writes nothing", rc == 0 and comment is None)

    # --- main(): empty verdict -> no file ---
    rc, comment = run_main("Bump pypdf from 6.14.2 to 7.0.0", "changelog", {}, tmp)
    check("main() writes nothing on an empty verdict", rc == 0 and comment is None)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
