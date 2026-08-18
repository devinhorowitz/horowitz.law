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

HERE = os.path.dirname(os.path.abspath(__file__))

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


def test_every_email_setting_has_a_home_in_the_repo():
    """No email setting may exist only as a GitHub repo Variable.

    This is the check that would have caught the actual bug. The Legislative & Regulatory Watch
    email landed 2026-07-18 reading its Resend audience from repo Variables; the migration that
    made the repo master of its own configuration landed a week later and moved the opinions
    values only. Nothing noticed the three it missed. When the Variables were later purged, the
    audience became empty, the send returned early, and the run stayed green -- three weeks of
    digests lost silently.

    The rule: a module-level RESEND_*/DIGEST_*/LEGISLATION_* read from os.environ must fall back
    to siteconfig, not to a literal. A literal default is fine for a preview path or a debug
    flag; it is not fine for a value that decides who gets email.
    """
    print("every email setting falls back to siteconfig")
    import re
    import siteconfig
    src = open(os.path.join(HERE, "digest.py"), encoding="utf-8").read()
    # The WHOLE file, not just the module header. RESEND_AREA_TOPICS is read inside a helper
    # function and is every bit as much configuration as the ids above it -- scanning only the
    # header let a mutation revert it to a UI-only literal without failing anything.
    pat = re.compile(r'os\.environ\.get\("((?:RESEND|DIGEST|LEGISLATION)_[A-Z_]+)"\)')
    checked, offenders = 0, []
    EXEMPT = {"DIGEST_LEG_PREVIEW", "DIGEST_PREVIEW",     # local file paths, not audience config
              "DIGEST_DRY_RUN", "DIGEST_DRAFT",           # per-run switches
              "DIGEST_ALLOW_NO_POSTAL",                   # deliberate one-off escape hatch
              "RESEND_API_KEY"}                           # a REAL secret: the one thing that
                                                          # must live outside the repo

    def fallback_window(pos):
        """Source between this env read and the NEXT one, capped.

        The boundary is what makes the check mean something. A fixed line window ran into the
        following setting's line and borrowed its `siteconfig.`, so a reverted line passed on
        its neighbour's compliance -- two mutations survived that way. Stopping at the next
        env read means each setting can only be vouched for by its own fallback. The cap keeps
        the last setting in a file from swallowing everything after it."""
        nxt = src.find('os.environ.get("', pos)
        stop = len(src) if nxt < 0 else nxt
        return src[pos:min(stop, pos + 240)]

    for m in pat.finditer(src):
        name = m.group(1)
        if name in EXEMPT:
            continue
        checked += 1
        stmt = fallback_window(m.end())
        if "siteconfig." not in stmt:
            offenders.append("%s -> %s" % (name, stmt.strip()[:60]))
    assert checked >= 8, "the scan found only %d settings; the pattern has drifted" % checked
    assert not offenders, "email settings with no siteconfig fallback: %s" % offenders
    print("  ok  %d settings, all falling back to siteconfig" % checked)

    # And the names it falls back to must actually exist, or the fallback is an AttributeError
    # waiting for the one run where the env is unset.
    for attr in ("RESEND_SEGMENT_ID", "RESEND_TOPIC_ID", "RESEND_LEGISLATION_SEGMENT_ID",
                 "RESEND_LEGISLATION_TOPIC_ID", "RESEND_AREA_TOPICS", "DIGEST_FROM",
                 "DIGEST_POSTAL", "DIGEST_DISCLAIMER", "LEGISLATION_DIGEST"):
        assert hasattr(siteconfig, attr), "siteconfig has no %s" % attr
    print("  ok  every fallback name exists in siteconfig")


def test_the_email_carries_every_watch_section():
    """The /legislation page has four sections; this email had two. Court rules had been rendering
    publicly since 2026-07 and Formal Advisory Opinions since 2026-08, and a subscriber to the
    "Legislative & Regulatory Watch" was told about neither -- 31 court-rule cards published in
    silence. Nothing failed, which is why it went unnoticed for weeks.

    Pinned in both bodies, because HTML and plain text are built separately and a section added to
    one and forgotten in the other is the same silence in half the clients."""
    print("every watch section reaches the inbox")
    leg = [law_card("HB 1", "enacted", days_ago(1))]
    reg = [{"agency": "FMCSA", "title": "A rule", "type": "Final rule", "synopsis": "Body.",
            "first_seen": days_ago(1), "url": "https://reg.example"}]
    crs = [{"rule_set": "FRCP", "rule": "Rule 26", "status": "pending",
            "effective_date": "2027-12-01", "summary": "Discovery changes.",
            "impact": "Affects scheduling.", "first_seen": days_ago(1), "url": "https://cr.example"}]
    eth = [{"number": "24-1", "status": "approved", "subject": "records vendors", "rules": ["5.3"],
            "summary": "Rule 5.3 governs a records vendor.", "impact": "Supervise it.",
            "first_seen": days_ago(1), "url": "https://eth.example"}]
    html_body = digest.build_leg_html(leg, reg, crs, eth)
    text_body = digest.build_leg_text(leg, reg, crs, eth)
    for label in ("Georgia legislation", "Federal regulations", "Court rules", "Ethics opinions"):
        check("html has the %s section" % label, label in html_body)
        check("text has the %s section" % label, (label + ":") in text_body)
    check("the court rule is named in both", "Rule 26" in html_body and "Rule 26" in text_body)
    check("the opinion is named in both",
          "Formal Advisory Opinion 24-1" in html_body and "Formal Advisory Opinion 24-1" in text_body)
    check("the source links survive",
          "cr.example" in html_body and "eth.example" in html_body
          and "cr.example" in text_body and "eth.example" in text_body)
    # An approved FAO binds; the email must not present it as merely proposed.
    check("an approved opinion is labelled as binding", "Approved — binding" in html_body)
    # Empty buckets must not print as "0 ...", and the four surfaces must agree on the phrasing.
    check("counts omit empty buckets", digest._leg_counts(leg, [], [], []) == "1 new law",
          digest._leg_counts(leg, [], [], []))
    check("counts join the rest", digest._leg_counts(leg, reg, crs, eth)
          == "1 new law, 1 new rule, 1 court-rule amendment and 1 ethics opinion",
          digest._leg_counts(leg, reg, crs, eth))
    check("nothing new says so", digest._leg_counts([], [], [], []) == "nothing new")
    check("the subject line counts all four", "4 updates" in digest.leg_subject_line(leg, reg, crs, eth))
    # Backward compatible: the two-argument form still builds, so no caller breaks silently.
    check("the two-argument form still works", "Georgia legislation" in digest.build_leg_html(leg, reg))


def test_an_unset_audience_is_loud_when_the_email_is_meant_to_send():
    """Dormant-but-expected must never look like a clean run again.

    The old behaviour printed one line and returned 0, which is how three weeks of sends were
    lost without anyone noticing. siteconfig.LEGISLATION_DIGEST now separates "deliberately
    off" from "meant to send but unconfigured", and only the first is quiet."""
    print("unset audience is loud")
    import io
    import contextlib
    import siteconfig
    saved = (digest.LEG_SEGMENT_ID, digest.API_KEY, digest.DRY_RUN,
             siteconfig.LEGISLATION_DIGEST, digest._load_leg_cards)
    try:
        # Cards in the window, a key present, not a dry run: everything ready except the audience.
        # Path-aware, so this also proves all FOUR watch files are read. They were not: court rules
        # and ethics opinions rendered on the page and reached no inbox until 2026-08-18.
        def _by_path(path):
            if path.endswith("courtrules.json"):
                return [{"rule_set": "FRCP", "rule": "Rule 26", "status": "pending",
                         "summary": "s", "first_seen": days_ago(1)}]
            if path.endswith("ethics.json"):
                return [{"number": "24-1", "status": "approved", "subject": "vendors",
                         "summary": "s", "first_seen": days_ago(1)}]
            return [law_card("HB 1", "enacted", days_ago(1))]
        digest._load_leg_cards = _by_path
        digest.API_KEY, digest.DRY_RUN, digest.LEG_SEGMENT_ID = "k", False, ""

        siteconfig.LEGISLATION_DIGEST = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            digest.send_legislation_digest()
        out = buf.getvalue()
        check("an expected-but-unconfigured send warns", "::warning::" in out, out[:160])
        check("and says it did not send", "NOT SENT" in out, out[:160])
        check("and says where to fix it", "siteconfig.py" in out, out[:160])
        # Assert the COUNT, not the old wording: the phrase is built by digest._leg_counts, which
        # every surface shares so a fifth section cannot be added to one and missed in another.
        check("and says how much went unsent", "went unsent" in out, out[:200])
        for stream in ("new law", "court-rule amendment", "ethics opinion"):
            check("and counts %ss among them" % stream, stream in out, out[:200])

        siteconfig.LEGISLATION_DIGEST = False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            digest.send_legislation_digest()
        out = buf.getvalue()
        check("a deliberately disabled email stays quiet", "::warning::" not in out, out[:160])
        check("and says it is disabled", "disabled" in out, out[:160])
    finally:
        (digest.LEG_SEGMENT_ID, digest.API_KEY, digest.DRY_RUN,
         siteconfig.LEGISLATION_DIGEST, digest._load_leg_cards) = saved


def main():
    print("digest selection + rendering:")
    test_labels_and_esc()
    test_select()
    test_select_corrections()
    test_in_area()
    test_subject_line()
    test_build_smoke()
    test_legislation_digest()
    test_every_email_setting_has_a_home_in_the_repo()
    test_the_email_carries_every_watch_section()
    test_an_unset_audience_is_loud_when_the_email_is_meant_to_send()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % 38)
    return 0


if __name__ == "__main__":
    sys.exit(main())
