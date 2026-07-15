#!/usr/bin/env python3
"""Hermetic unit test for the AI issue-diagnosis helper (scripts/diagnose.py), no network.

Stubs update.anthropic_json, so it makes no Anthropic call. Pins the parts that keep the diagnostic
safe and quiet: it fires only on real problem reports (not routine notifications), it grounds the
prompt in the runbook and issue, it renders a verdict to markdown, and -- the load-bearing property
-- any failure or empty answer leaves NO comment file so the workflow posts nothing.

Run directly: `python scripts/test_diagnose.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diagnose  # noqa: E402
import update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Stub:
    """Stand-in for update.anthropic_json: returns a fixed verdict (or raises), and records calls."""
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def __call__(self, body, label="call"):
        self.calls.append(body)
        if isinstance(self.ret, Exception):
            raise self.ret
        return self.ret


def run_main(title, body, ret, tmp):
    """Drive diagnose.main() with a stubbed API and env, returning (rc, comment-or-None)."""
    prev = {k: os.environ.get(k) for k in ("ISSUE_TITLE", "ISSUE_BODY", "DIAGNOSE_OUT")}
    real = update.anthropic_json
    os.environ["ISSUE_TITLE"] = title
    os.environ["ISSUE_BODY"] = body
    os.environ["DIAGNOSE_OUT"] = tmp
    if os.path.exists(tmp):
        os.remove(tmp)
    update.anthropic_json = Stub(ret)
    try:
        rc = diagnose.main()
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
    print("AI issue diagnosis:")

    # --- _worth_diagnosing: problems fire, notifications do not ---
    check("failure issue is worth diagnosing", diagnose._worth_diagnosing("Digest run failures", "A run failed."))
    check("heartbeat stall is worth diagnosing", diagnose._worth_diagnosing("Funnel heartbeat: possible stall", ""))
    check("drift issue is worth diagnosing", diagnose._worth_diagnosing("Feed-shape drift detected", "entries but zero parsed"))
    check("embedded log (code fence) is worth diagnosing", diagnose._worth_diagnosing("Odd", "output:\n```\nboom\n```"))
    check("'newer model' notification is NOT diagnosed",
          not diagnose._worth_diagnosing("Funnel: a newer Claude model is available", "Consider updating the repo Variable."))
    check("'queued cases' notification is NOT diagnosed",
          not diagnose._worth_diagnosing("Georgia Appellate Watch: queued case(s)", "3 cases queued for review."))

    # --- build_request: grounded and well-formed ---
    req = diagnose.build_request("T", "B", "RUNBOOK TEXT", model="claude-fable-5")
    check("build_request pins the model", req["model"] == "claude-fable-5")
    check("build_request bounds output", isinstance(req["max_tokens"], int) and req["max_tokens"] > 0)
    user = req["messages"][0]["content"]
    check("build_request includes the runbook context", "RUNBOOK TEXT" in user)
    check("build_request includes the issue title and body", "Title: T" in user and "B" in user)
    check("system demands JSON only", "JSON object" in req["system"])

    # --- format_comment: renders fields, empty -> '' ---
    md = diagnose.format_comment({
        "summary": "The funnel could not reach CourtListener.",
        "likely_causes": ["CL outage", "network egress block"],
        "where_to_look": ["scripts/update.py cl_get", "maintain.yml allowed-endpoints"],
        "suggested_next_steps": ["re-run the workflow", "check CL status page"],
        "confidence": "medium",
    })
    check("comment has the diagnosis header", "Automated first-pass diagnosis" in md)
    check("comment renders the summary", "could not reach CourtListener" in md)
    check("comment renders causes and where-to-look", "CL outage" in md and "cl_get" in md)
    check("comment renders confidence", "medium" in md)
    check("comment carries the no-repo-access caveat", "no live repo access" in md)
    check("empty verdict renders no comment", diagnose.format_comment({}) == "")

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnosis_test_tmp.md")

    # --- main(): happy path writes a comment ---
    verdict = {"summary": "Model id was retired.", "likely_causes": ["retired snapshot"],
               "where_to_look": ["repo Variable OPINIONS_SCREEN_MODEL"], "suggested_next_steps": ["update it"],
               "confidence": "high"}
    rc, comment = run_main("Instant-alert run failures", "HTTP 404 model not found", verdict, tmp)
    check("main() exits 0 on success", rc == 0)
    check("main() writes the comment file on a real problem", comment is not None and "Model id was retired." in (comment or ""))

    # --- main(): non-problem issue -> no call, no file ---
    rc, comment = run_main("Funnel: a newer Claude model is available", "Update the Variable.", verdict, tmp)
    check("main() skips a notification (no comment file)", rc == 0 and comment is None)

    # --- main(): API failure -> best-effort, no file, exit 0 ---
    rc, comment = run_main("Digest run failures", "boom", RuntimeError("no api key"), tmp)
    check("main() swallows an API failure and writes nothing", rc == 0 and comment is None)

    # --- main(): model returns an empty/unusable verdict -> no file ---
    rc, comment = run_main("Digest run failures", "boom", {}, tmp)
    check("main() writes nothing when the verdict is empty", rc == 0 and comment is None)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
