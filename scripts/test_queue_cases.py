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


def main():
    print("queue_cases.parse_line:")
    test_blank_and_comment()
    test_url_forms()
    test_bare_and_pair()
    test_force_and_inline_comment()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 18)
    return 0


if __name__ == "__main__":
    sys.exit(main())
