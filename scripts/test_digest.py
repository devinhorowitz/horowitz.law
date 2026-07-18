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


def law_card(number, status, first_seen, **extra):
    d = {"number": number, "title": "A bill", "status": status, "status_date": first_seen,
         "synopsis": "Does a thing.", "areas": ["damages"], "url": "https://legiscan.com/x",
         "state": "GA", "first_seen": first_seen}
    d.update(extra)
    return d


def rule_card(agency, first_seen, **extra):
    d = {"agency": agency, "title": "A rule", "type": "Final Rule", "cfr": "49 CFR 395",
         "synopsis": "Adjusts a standard.", "areas": ["auto"], "url": "https://federalregister.gov/x",
         "document_number": "2026-1", "first_seen": first_seen}
    d.update(extra)
    return d


def test_legislation_digest():
    # select_leg: window membership, newest-first, drops the stale one.
    cards = [law_card("HB 1", "enacted", days_ago(1)), law_card("HB 2", "vetoed", days_ago(20))]
    new, since = digest.select_leg(cards, 7)
    nums = [c["number"] for c in new]
    check("select_leg keeps a card first-seen in the window", "HB 1" in nums)
    check("select_leg drops a card older than the window", "HB 2" not in nums)
    check("select_leg returns the since cutoff", since == days_ago(7))

    # subject line pluralization across both streams.
    check("leg subject: singular", "1 update" in digest.leg_subject_line([law_card("HB 9", "enacted", days_ago(1))], []))
    check("leg subject: plural counts law+rule",
          "2 updates" in digest.leg_subject_line([law_card("HB 9", "enacted", days_ago(1))],
                                                 [rule_card("FMCSA", days_ago(1))]))

    leg = [law_card("HB 100", "enacted", days_ago(1), effective_date="2026-07-01")]
    reg = [rule_card("FMCSA", days_ago(1))]
    html = digest.build_leg_html(leg, reg)
    check("build_leg_html is a full document", html.startswith("<!doctype html>") and html.endswith("</html>"))
    check("build_leg_html carries the law and the rule", "HB 100" in html and "FMCSA" in html)
    check("build_leg_html labels an enacted law", "Enacted" in html)
    check("build_leg_html links to the legislation site", digest.LEG_SITE in html)
    check("build_leg_html has an unsubscribe link + tag", "unsubscribe" in html.lower() and digest.UNSUB_TAG in html)
    text = digest.build_leg_text(leg, reg)
    check("build_leg_text carries both streams", "HB 100" in text and "FMCSA" in text)
    check("build_leg_text names the source links", leg[0]["url"] in text and reg[0]["url"] in text)

    # A stream with only one side renders that side and omits the empty one.
    only_leg = digest.build_leg_html(leg, [])
    check("build_leg_html omits an empty regulations section", "Federal regulations" not in only_leg
          and "Georgia legislation" in only_leg)


def main():
    print("digest selection + rendering:")
    test_labels_and_esc()
    test_select()
    test_select_corrections()
    test_in_area()
    test_subject_line()
    test_build_smoke()
    test_legislation_digest()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 38)
    return 0


if __name__ == "__main__":
    sys.exit(main())
