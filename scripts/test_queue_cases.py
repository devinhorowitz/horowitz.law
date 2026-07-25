#!/usr/bin/env python3
"""Hermetic unit tests for queue_cases.parse_line (no network, no API key).

parse_line classifies each queue.txt line into blank / comment / entry and, for an
entry, resolves the cluster id from a CourtListener URL, a bare cluster id, or a
cluster:court pair -- honoring the trailing `!` force marker and inline `#` comments,
and rejecting look-alike hosts. This is the parse that decides which cases the manual
queue funnel will pull, so a regression here silently mis-queues or drops a request.

Run directly: `python scripts/test_queue_cases.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_cases    # noqa: E402  (sys.path shim must run first)

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def entry(raw):
    kind, payload = queue_cases.parse_line(raw)
    assert kind == "entry", "expected entry, got %s for %r" % (kind, raw)
    return payload


def test_blank_and_comment():
    check("empty line -> blank", queue_cases.parse_line("") == ("blank", ""))
    check("whitespace-only -> blank", queue_cases.parse_line("   \n")[0] == "blank")
    check("full-line comment -> comment", queue_cases.parse_line("# a note")[0] == "comment")
    check("comment preserves the raw text", queue_cases.parse_line("# a note\n")[1] == "# a note")
    check("line that is only an inline comment -> comment",
          queue_cases.parse_line("   # trailing only")[0] == "comment")


def test_url_forms():
    e = entry("https://www.courtlistener.com/opinion/12345/smith-v-jones/")
    check("full CourtListener URL -> cid", e["cid"] == 12345)
    e = entry("www.courtlistener.com/opinion/67890/foo/")
    check("scheme-less CourtListener paste -> cid", e["cid"] == 67890)
    # A look-alike host must NOT be treated as CourtListener.
    e = entry("courtlistener.com.evil.tld/opinion/999/")
    check("look-alike host is rejected (cid None)", e["cid"] is None)


def test_bare_and_pair():
    check("bare cluster id -> cid", entry("12345")["cid"] == 12345)
    e = entry("12345:ctapp")
    check("cluster:court pair -> cid", e["cid"] == 12345)
    check("cluster:court pair -> lowercased court", e["court"] == "ctapp")
    check("cluster:court pair uppercase court is normalized", entry("77:CTAPP")["court"] == "ctapp")
    check("non-token entry -> cid None", entry("not-a-cluster")["cid"] is None)


def test_force_and_inline_comment():
    e = entry("12345!")
    check("trailing ! sets force", e["force"] is True and e["cid"] == 12345)
    e = entry("12345")
    check("no ! -> force False", e["force"] is False)
    e = entry("12345  # already carded, re-pull")
    check("inline comment is stripped before parsing", e["cid"] == 12345)
    e = entry("12345 !  # forced re-pull")
    check("force survives an inline comment", e["force"] is True and e["cid"] == 12345)
    check("entry keeps the original raw line", entry("12345 # note")["raw"] == "12345 # note")


def test_rewrite_queue():
    # A representative queue: header comment, blank, and four entries fated to each
    # outcome -- carded (remove), parked (unresolved), kept (deferred), and untouched
    # (no recorded outcome, e.g. resolve-only). The park and keep paths crashed in
    # production (2026-07-25, issue #186) because the entry payload is the parse dict,
    # not a string; this pins the payload["raw"] handling for every branch.
    raw_lines = [
        "# curated queue",
        "",
        "11111  # carded last run",
        "22222:ca11 !  # forced entry that failed to resolve",
        "33333  # deferred until text is up",
        "44444",
    ]
    parsed = [queue_cases.parse_line(l) for l in raw_lines]
    outcomes = {
        2: ("remove", None),
        3: ("park", "could not resolve cluster 22222: timeout"),
        4: ("keep", None),
        # index 5 intentionally absent: default outcome must keep the line verbatim
    }
    text = queue_cases.rewrite_queue(parsed, outcomes)
    lines = text.splitlines()
    check("comment survives verbatim", lines[0] == "# curated queue")
    check("blank line survives", lines[1] == "")
    check("removed (carded) line is gone", all("11111" not in l for l in lines))
    check("parked line becomes an annotated comment",
          lines[2] == "# 22222:ca11 !  # forced entry that failed to resolve   "
                      "-- could not resolve cluster 22222: timeout",
          detail=repr(lines[2]))
    check("kept (deferred) line survives verbatim", lines[3] == "33333  # deferred until text is up")
    check("line with no recorded outcome survives verbatim", lines[4] == "44444")
    check("text ends with exactly one newline", text.endswith("\n") and not text.endswith("\n\n"))
    check("all lines removed -> empty text",
          queue_cases.rewrite_queue([queue_cases.parse_line("11111")], {0: ("remove", None)}) == "")


def main():
    print("queue_cases.parse_line:")
    test_blank_and_comment()
    test_url_forms()
    test_bare_and_pair()
    test_force_and_inline_comment()
    print("queue_cases.rewrite_queue:")
    test_rewrite_queue()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 26)
    return 0


if __name__ == "__main__":
    sys.exit(main())
