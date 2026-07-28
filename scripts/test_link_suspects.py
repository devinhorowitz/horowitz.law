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

    calls = []

    def checker(url):
        calls.append(url)
        return (False, "HTTP 404") if url == "https://dead.test" else (True, "HTTP 200")

    # Too early: the re-check must not run at all, or the 24h rule is decoration.
    s_early, conf_early, rec_early = ls.confirm(state, t0 + 3600, checker=checker)
    check("nothing is re-checked before 24h", calls == [] and conf_early == [] and rec_early == [])
    check("and no suspect is lost by looking early", len(s_early["suspects"]) == 2)

    s, conf, rec = ls.confirm(state, t0 + DAY, checker=checker)
    check("a URL still failing after 24h is confirmed", conf == ["https://dead.test"])
    check("a URL that recovered is dropped, not reported", rec == ["https://blip.test"])
    check("the recovered one leaves the state", "https://blip.test" not in s["suspects"])
    check("the confirmed one stays, so recovery can close the issue later",
          "https://dead.test" in s["suspects"])
    check("and is stamped confirmed", s["suspects"]["https://dead.test"].get("confirmed_at") == t0 + DAY)

    # A confirmed link that later comes back must clear itself.
    s2, conf2, rec2 = ls.confirm(s, t0 + 2 * DAY, checker=lambda u: (True, "HTTP 200"))
    check("a confirmed link that recovers is dropped", rec2 == ["https://dead.test"] and conf2 == [])
    check("leaving nothing to report", s2["suspects"] == {})


# --- the URL re-check itself ---------------------------------------------
def test_check_url():
    LIVE = "https://u.test/x"      # a real scheme: safe_url now refuses bare placeholders
    def opener(codes):
        seq = list(codes)
        def _o(url, method, timeout):
            v = seq.pop(0) if seq else 200
            if isinstance(v, Exception):
                raise v
            return v
        return _o

    ok, d = ls.check_url(LIVE, opener=opener([200]), sleep=lambda s: None)
    check("200 is alive", ok, d)
    for code in ls.ACCEPT_CODES:
        ok, _ = ls.check_url(LIVE, opener=opener([code]), sleep=lambda s: None)
        check("accepted code %s counts as alive (matches the crawl)" % code, ok)
    ok, d = ls.check_url(LIVE, opener=opener([301]), sleep=lambda s: None)
    check("a redirect is alive", ok, d)
    ok, d = ls.check_url(LIVE, opener=opener([404, 404, 404, 404, 404, 404]), sleep=lambda s: None)
    check("404 is dead", not ok and "404" in d, d)
    ok, d = ls.check_url(LIVE, opener=opener([TimeoutError("slow")] * 8), sleep=lambda s: None)
    check("a timeout is dead, and is named", not ok and "Timeout" in d, d)

    # HEAD-hostile sites are common; refusing HEAD must not read as rot.
    ok, d = ls.check_url(LIVE, opener=opener([405, 200]), sleep=lambda s: None)
    check("a HEAD-refusing site falls back to GET", ok, d)

    # The blip: fails twice, answers on the third attempt. The crawl retries 3x and so must
    # this -- one request per attempt, since a 500 is not a method refusal worth a GET retry.
    ok, d = ls.check_url(LIVE, opener=opener([500, 500, 200]), tries=3, sleep=lambda s: None)
    check("a transient failure is retried, not called dead", ok, d)
    ok, d = ls.check_url(LIVE, opener=opener([500, 500, 500, 200]), tries=3, sleep=lambda s: None)
    check("but the retries stop at the budget rather than trying forever", not ok, d)

    waits = []
    ls.check_url(LIVE, opener=opener([404] * 12), tries=3, wait=5, sleep=waits.append)
    check("retries are bounded by the try count", len(waits) == 2, str(waits))


# --- the issue body -------------------------------------------------------
def test_safe_url():
    """urlopen honours whatever scheme it is handed. The URLs here come from a crawl report
    and a state file, so file:// would turn a link re-check into a local file read."""
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

    # The sink must refuse on its own, not merely because a caller checked first.
    hit = []
    ok, why = ls.check_url("file:///etc/passwd",
                           opener=lambda u, m, t: hit.append(u) or 200, sleep=lambda s: None)
    check("check_url refuses a file: URL without opening it", not ok and hit == [], str(hit))
    try:
        ls._urlopen("file:///etc/passwd", "HEAD", 1)
        raised = False
    except ValueError:
        raised = True
    except Exception:
        raised = False
    check("_urlopen refuses a file: URL even when called directly", raised)

    # And such a URL must never become a suspect in the first place.
    got = ls.parse_lychee({"fail_map": {"p.html": [
        {"url": "file:///etc/passwd"}, {"url": "mailto:a@b.test"}, {"url": "https://real.test"}]}})
    check("only http(s) failures become suspects",
          [f["url"] for f in got] == ["https://real.test"], str(got))


def test_issue_body():
    t0 = 1000.0
    state = ls.record({"suspects": {}}, [fail("https://dead.test", "404", "public/o/1.html")], t0)[0]
    state, conf, _ = ls.confirm(state, t0 + 2 * DAY, checker=lambda u: (False, "HTTP 404"))
    body = ls.issue_body(state, conf, t0 + 2 * DAY)
    check("the body names the URL", "https://dead.test" in body)
    check("it says how long it has been failing", "48h ago" in body or "2d ago" in body, body)
    check("it says where the link lives", "public/o/1.html" in body)
    check("it states the two-strike rule so the reader knows what it is not",
          "24 hours" in body and "recovered" in body)


# --- the re-check must not be stricter than the crawl --------------------
def test_accept_codes_match_the_workflow():
    """A re-check that rejects what the crawl accepts would 'confirm' healthy links. The
    two lists live in different languages, so assert rather than trust."""
    path = os.path.join(os.path.dirname(HERE), ".github", "workflows", "links.yml")
    text = open(path, encoding="utf-8").read()
    marker = "--accept "
    i = text.find(marker)
    check("the workflow still passes --accept to lychee", i >= 0)
    if i < 0:
        return
    listed = text[i + len(marker):].split()[0].strip()
    codes = tuple(int(c) for c in listed.split(",") if c.strip().isdigit())
    check("the re-check accepts exactly what the crawl accepts",
          codes == ls.ACCEPT_CODES, "workflow=%s module=%s" % (str(codes), str(ls.ACCEPT_CODES)))


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
    test_check_url()
    test_safe_url()
    test_issue_body()
    test_accept_codes_match_the_workflow()
    test_state_roundtrip()
    test_committed_state_is_valid()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
