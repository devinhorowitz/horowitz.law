#!/usr/bin/env python3
"""Hermetic unit tests for digest.py's pure selection/rendering helpers (no network).

Covers the logic that decides what goes in the weekly email and how it is labeled:
the date-window selection, per-area membership, the corrections selection, the subject
line and its pluralization, HTML escaping, and a build_html/build_text smoke test.
No Resend call is made -- _req/send_broadcast/main are not exercised. A regression in
any of these silently mails the wrong set, a wrong count, or unescaped content.

Run directly: `python scripts/test_digest.py`.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest    # noqa: E402  (sys.path shim must run first)
import render    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


AREA = next(iter(render.AREA_LABELS))       # a practice-area key the renderer knows


def card(cid, name, date, **extra):
    e = {"cluster_id": cid, "name": name, "court": "ctapp", "date": date,
         "disposition": "affirmed", "why": "it matters", "synopsis": "s", "areas": [AREA]}
    e.update(extra)
    return e


def test_labels_and_esc():
    check("label_for singular", digest.label_for(1) == "1 new opinion")
    check("label_for plural", digest.label_for(3) == "3 new opinions")
    check("label_corrections singular", digest.label_corrections(1) == "1 earlier decision flagged")
    check("label_corrections plural", digest.label_corrections(2) == "2 earlier decisions flagged")
    check("esc escapes angle brackets and quotes", digest.esc('<b>"x"</b>') == "&lt;b&gt;&quot;x&quot;&lt;/b&gt;")
    check("esc handles None", digest.esc(None) == "")


def test_select():
    entries = [card(1, "Fresh v. X", days_ago(1)),
               card(2, "Old v. Y", days_ago(60)),
               card(3, "Edge v. Z", days_ago(7))]
    new, since = digest.select(entries, 7)
    names = [e["name"] for e in new]
    check("select keeps entries inside the window", "Fresh v. X" in names)
    check("select drops entries older than the window", "Old v. Y" not in names)
    check("select is sorted newest-first", names[0] == "Fresh v. X")
    check("select returns the since cutoff", since == days_ago(7))
    # first_seen takes precedence over date when present.
    e = card(9, "Backdated v. Q", days_ago(90), first_seen=days_ago(2))
    got, _ = digest.select([e], 7)
    check("select honors first_seen over date", len(got) == 1)


def test_select_corrections():
    entries = [card(1, "Flagged v. X", days_ago(30), treatment="negative", treatment_date=days_ago(2)),
               card(2, "Clean v. Y", days_ago(30)),
               card(3, "OldFlag v. Z", days_ago(30), treatment="superseded", treatment_date=days_ago(60))]
    cor = digest.select_corrections(entries, 7)
    names = [e["name"] for e in cor]
    check("corrections include a recently-flagged card", "Flagged v. X" in names)
    check("corrections exclude untreated cards", "Clean v. Y" not in names)
    check("corrections exclude flags outside the window", "OldFlag v. Z" not in names)


def test_in_area():
    e = card(1, "Area v. X", days_ago(1))
    check("in_area true for a card's own area", digest.in_area(e, AREA) is True)
    check("in_area false for an area the card lacks", digest.in_area(e, "definitely-not-an-area") is False)


def test_subject_line():
    one, two = [card(1, "A v. B", days_ago(1))], [card(2, "C v. D", days_ago(1))]
    check("subject: new only, singular", "1 new opinion" in digest.subject_line(one, []))
    check("subject: new only, plural", "2 new opinions" in digest.subject_line(one + two, []))
    check("subject: corrections only", "1 earlier decision flagged" in digest.subject_line([], one))
    both = digest.subject_line(one, two)
    check("subject: new and corrections combined", "1 new opinion" in both and "1 flagged" in both)


def test_build_smoke():
    new = [card(1, "Smoke v. Test", days_ago(1))]
    html = digest.build_html(new, [])
    check("build_html returns a full document", html.startswith("<!doctype html>") and html.endswith("</html>"))
    check("build_html includes the case name", "Smoke v. Test" in html)
    check("build_html includes an unsubscribe link", "unsubscribe" in html.lower())
    text = digest.build_text(new, [])
    check("build_text includes the case name", "Smoke v. Test" in text)
    check("build_text names the source link", digest.SITE in text)
    # An empty week still renders without error.
    check("build_html handles an empty week", "No new decisions this week." in digest.build_html([], []))


def main():
    print("digest selection + rendering:")
    test_labels_and_esc()
    test_select()
    test_select_corrections()
    test_in_area()
    test_subject_line()
    test_build_smoke()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
