#!/usr/bin/env python3
"""Hermetic tests for scripts/link_suspects.py -- the two-strike link-rot rule (no network).

The whole point of this module is to NOT file an issue on a first failure, so the tests
that matter are the ones proving silence is temporary: a suspect that recovers is dropped,
a suspect younger than 24h is left alone, and a suspect that fails twice 24h apart is
reported. The clock and the URL checker are both injected, so a 24-hour rule is tested in
milliseconds and no request is made.

Run directly: `python scripts/test_link_suspects.py`.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import link_suspects as ls  # noqa: E402  (sys.path shim must run first)

FAILS = []
CHECKS = [0]
DAY = 24 * 3600


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def fail(url, status="404", source="public/index.html"):
    return {"url": url, "status": status, "source": source}


# --- parsing lychee's report ---------------------------------------------
def test_parse_lychee():
    report = {"total": 100, "successful": 98,
              "fail_map": {"public/legislation.html": [
                  {"url": "https://a.test/1", "status": "Timeout"},
                  {"url": "https://b.test/2", "status": {"code": 404, "text": "Not Found"}}]}}
    got = ls.parse_lychee(report)
    check("failures are extracted from fail_map", [f["url"] for f in got] ==
          ["https://a.test/1", "https://b.test/2"], str(got))
    check("a dict status is flattened to text", got[1]["status"] in ("404", "Not Found"), str(got[1]))
    check("the source file is kept", got[0]["source"] == "public/legislation.html")

    # Lychee has used more than one key for this over versions; a rename must not make the
    # parser silently find nothing, which would mean never filing an issue again.
    check("error_map is understood too",
          [f["url"] for f in ls.parse_lychee({"error_map": {"x": [{"url": "https://c.test"}]}})]
          == ["https://c.test"])
    check("a URL failing on two pages is one suspect",
          len(ls.parse_lychee({"fail_map": {"a": [{"url": "https://d.test"}],
                                            "b": [{"url": "https://d.test"}]}})) == 1)
    check("a clean report yields nothing", ls.parse_lychee({"total": 5, "successful": 5}) == [])
    for junk in (None, [], "nope", {"fail_map": "not a dict"}, {"fail_map": {"a": [None, 7]}}):
        check("malformed report %r does not raise" % (junk,), ls.parse_lychee(junk) == [])


# --- record: a failure starts a clock, it does not file anything ---------
def test_record():
    t0 = 1000.0
    state, added, still, rec = ls.record({"suspects": {}}, [fail("https://a.test")], t0)
    check("a first failure becomes a suspect", added == ["https://a.test"] and not rec)
    check("and its clock starts now", state["suspects"]["https://a.test"]["first_failed"] == t0)

    # Second crawl, still failing: the ORIGINAL timestamp must survive, or the 24h can never
    # elapse and a genuinely dead link would be re-suspected forever without ever confirming.
    state2, added2, still2, rec2 = ls.record(state, [fail("https://a.test", "500")], t0 + DAY)
    check("a repeat failure does not reset the clock",
          state2["suspects"]["https://a.test"]["first_failed"] == t0,
          str(state2["suspects"]["https://a.test"]))
    check("but the latest status is updated",
          state2["suspects"]["https://a.test"]["last_status"] == "500")
    check("a repeat is reported as repeat, not new", added2 == [] and still2 == ["https://a.test"])

    # The blip case this whole module exists for.
    state3, _, _, rec3 = ls.record(state2, [], t0 + 2 * DAY)
    check("a suspect the next crawl finds healthy is dropped", rec3 == ["https://a.test"])
    check("and leaves no state behind", state3["suspects"] == {})

    multi = ls.record({"suspects": {}}, [fail("https://a.test"), fail("https://b.test")], t0)[0]
    state4, _, _, rec4 = ls.record(multi, [fail("https://b.test")], t0 + DAY)
    check("recovery is per-URL, not all-or-nothing",
          rec4 == ["https://a.test"] and list(state4["suspects"]) == ["https://b.test"])


# --- the 24-hour wait -----------------------------------------------------
def test_due_window():
    t0 = 1000.0
    state = ls.record({"suspects": {}}, [fail("https://a.test")], t0)[0]
    check("nothing is due immediately", ls.due(state, t0) == [])
    check("nothing is due at 23h59m", ls.due(state, t0 + DAY - 60) == [])
    check("it is due at exactly 24h", ls.due(state, t0 + DAY) == ["https://a.test"])
    check("and still due later", ls.due(state, t0 + 3 * DAY) == ["https://a.test"])


def test_confirm():
    t0 = 1000.0
    state = ls.record({"suspects": {}}, [fail("https://dead.test"), fail("https://blip.test")], t0)[0]

    # Too early: nothing is settled either way, or the 24h rule is decoration. Note the
    # re-check report is IGNORED for a suspect that is not due yet -- even a failing one.
    s_early, conf_early, rec_early = ls.confirm(state, t0 + 3600, ["https://dead.test"])
    check("nothing is settled before 24h", conf_early == [] and rec_early == [])
    check("and no suspect is lost by looking early", len(s_early["suspects"]) == 2)

    s, conf, rec = ls.confirm(state, t0 + DAY, ["https://dead.test"])
    check("a URL still failing after 24h is confirmed", conf == ["https://dead.test"])
    check("a URL that recovered is dropped, not reported", rec == ["https://blip.test"])
    check("the recovered one leaves the state", "https://blip.test" not in s["suspects"])
    check("the confirmed one stays, so recovery can close the issue later",
          "https://dead.test" in s["suspects"])
    check("and is stamped confirmed", s["suspects"]["https://dead.test"].get("confirmed_at") == t0 + DAY)

    # A confirmed link that later comes back must clear itself.
    s2, conf2, rec2 = ls.confirm(s, t0 + 2 * DAY, [])
    check("a confirmed link that recovers is dropped", rec2 == ["https://dead.test"] and conf2 == [])
    check("leaving nothing to report", s2["suspects"] == {})


# --- the issue body -------------------------------------------------------
def test_safe_url():
    """Only http(s) is re-checkable. A mailto:, tel:, or file: entry that became a suspect
    could never be confirmed or cleared, so it would sit in the state forever."""
    for good in ("https://a.test/x", "http://a.test", "https://a.test:8443/p?q=1#f"):
        check("http(s) URL is fetchable: %s" % good, ls.safe_url(good)[0])
    for bad in ("file:///etc/passwd", "file://localhost/etc/passwd", "mailto:a@b.test",
                "tel:+15551234", "ftp://a.test/x", "javascript:alert(1)", "data:text/html,x",
                "", "not a url", "https://", "//a.test/x"):
        ok, why = ls.safe_url(bad)
        check("refused: %r" % bad, not ok and why.startswith(("refused", "unparseable")), why)
    ok, why = ls.safe_url("https://user:pw@a.test/x")
    check("a URL with embedded credentials is refused (the status lands in a public issue)",
          not ok and "credential" in why, why)

    # Such a URL must never become a suspect: nothing downstream could confirm or clear it.
    got = ls.parse_lychee({"fail_map": {"p.html": [
        {"url": "file:///etc/passwd"}, {"url": "mailto:a@b.test"}, {"url": "https://real.test"}]}})
    check("only http(s) failures become suspects",
          [f["url"] for f in got] == ["https://real.test"], str(got))


def test_issue_body():
    t0 = 1000.0
    state = ls.record({"suspects": {}}, [fail("https://dead.test", "404", "public/o/1.html")], t0)[0]
    state, conf, _ = ls.confirm(state, t0 + 2 * DAY, ["https://dead.test"])
    body = ls.issue_body(state, conf, t0 + 2 * DAY)
    check("the body names the URL", "https://dead.test" in body)
    check("it says how long it has been failing", "48h ago" in body or "2d ago" in body, body)
    check("it says where the link lives", "public/o/1.html" in body)
    check("it states the two-strike rule so the reader knows what it is not",
          "24 hours" in body and "recovered" in body)


# --- the CLI handoff to lychee -------------------------------------------
def test_cli_due_and_confirm():
    """`due` hands lychee a URL list; `confirm` reads lychee's verdict back. The failure
    that matters is an unreadable verdict: treating that as "everything passed" would drop
    real rot on a hiccup, so it must confirm nothing and leave every clock running."""
    import shutil
    import subprocess
    root = os.path.dirname(HERE)
    tmp = tempfile.mkdtemp(prefix=".linktest-", dir=root)   # inside the repo: paths are confined
    try:
        st = os.path.join(tmp, "s.json")
        lst = os.path.join(tmp, "due.txt")
        old = ls.record({"suspects": {}}, [fail("https://dead.test")], 1000.0)[0]
        old["suspects"]["https://dead.test"]["first_failed"] = 0.0    # long overdue
        ls.save_state(old, st)

        def run(*args):
            return subprocess.run([sys.executable, os.path.join(HERE, "link_suspects.py")] + list(args),
                                  capture_output=True, text=True)

        r = run("due", "--state", st, "--out", lst)
        check("due exits 0", r.returncode == 0, r.stderr[-200:])
        check("due writes the URL list lychee will read",
              open(lst).read().strip() == "https://dead.test", open(lst).read())
        check("and reports the count for the workflow", "due_count=1" in r.stdout, r.stdout)

        r = run("confirm", "--state", st, "--report", os.path.join(tmp, "nope.json"))
        check("an unreadable re-check report confirms nothing", "confirmed=0" in r.stdout, r.stdout)
        check("and does not silently drop the suspect",
              "https://dead.test" in ls.load_state(st)["suspects"])

        rep = os.path.join(tmp, "re.json")
        json.dump({"fail_map": {"due.txt": [{"url": "https://dead.test", "status": "404"}]}},
                  open(rep, "w"))
        body = os.path.join(tmp, "b.md")
        r = run("confirm", "--state", st, "--report", rep, "--body", body)
        check("a failing re-check confirms the suspect", "confirmed=1" in r.stdout, r.stdout)
        check("and writes the issue body", os.path.exists(body) and "https://dead.test" in open(body).read())

        json.dump({"total": 1, "successful": 1}, open(rep, "w"))
        r = run("confirm", "--state", st, "--report", rep)
        check("a clean re-check clears it instead", "confirmed=0" in r.stdout, r.stdout)
        check("and removes it from the state", ls.load_state(st)["suspects"] == {})

        # argv is this program's only untrusted-shaped input, and every value is a path.
        r = run("due", "--state", st, "--out", "../../../tmp/escape.txt")
        check("a path escaping the checkout is refused, not followed",
              r.returncode != 0 and "outside the repository" in (r.stdout + r.stderr),
              (r.stdout + r.stderr)[-160:])
        r = run("due", "--state", st, "--out", "/etc/escape.txt")
        check("an absolute path outside the checkout is refused too",
              r.returncode != 0 and "outside the repository" in (r.stdout + r.stderr),
              (r.stdout + r.stderr)[-160:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- state file on disk ---------------------------------------------------
def test_state_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "s.json")
        check("a missing state file reads as empty", ls.load_state(p) == {"suspects": {}})
        ls.save_state({"suspects": {"https://a.test": {"first_failed": 1.0}}}, p)
        check("state round-trips", ls.load_state(p)["suspects"]["https://a.test"]["first_failed"] == 1.0)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        check("a corrupt state file reads as empty instead of crashing the check",
              ls.load_state(p) == {"suspects": {}})
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"suspects": "wrong type"}, f)
        check("a wrong-shaped state file reads as empty too", ls.load_state(p) == {"suspects": {}})


# --- the committed state file --------------------------------------------
def test_committed_state_is_valid():
    d = ls.load_state()
    check("the committed suspect file parses", isinstance(d.get("suspects"), dict))
    bad = [u for u, s in d["suspects"].items()
           if not isinstance(s, dict) or not isinstance(s.get("first_failed"), (int, float))]
    check("every committed suspect carries a numeric clock", not bad, str(bad))
    n = len(d["suspects"])
    if n:
        print("  note  %d suspect(s) currently tracked: %s"
              % (n, ", ".join(sorted(d["suspects"])[:5])))


def main():
    print("link_suspects:")
    test_parse_lychee()
    test_record()
    test_due_window()
    test_confirm()
    test_safe_url()
    test_issue_body()
    test_cli_due_and_confirm()
    test_state_roundtrip()
    test_committed_state_is_valid()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
