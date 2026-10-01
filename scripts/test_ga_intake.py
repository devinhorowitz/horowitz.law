#!/usr/bin/env python3
"""Hermetic tests for the Supreme Court of Georgia intake (update.py GA helpers, official_ga.py).

Since the 2026-06-30 release CourtListener has stamped every new Supreme Court of Georgia opinion
date_filed 2026-06-16 (juriscraper reads the release date from a <p>; gasupreme.us moved it into an
<h3>), so the funnel's since floor and the 20-entry court feed hid them. These pin the fix:

  * official_ga parses release dates from both page layouts, and the PDF's caption block;
  * the cluster-id enumeration over the search feed splits full pages and stops cleanly;
  * the since-floor exemption covers only never-seen backlog, capped per run, oldest first;
  * a re-scrape of an already-seen docket is skipped; a same-docket later decision is not;
  * a GA card carries the official date (CourtListener's kept as cl_date_filed), and an undated
    one on the stuck date is held, not auto-published;
  * the high-water mark persists, carries the waiting backlog, and never passes an open cluster;
  * a non-GA court is unchanged.

No network: every feed, page and PDF is stubbed, and every path main() writes is a temp dir.
Run directly: `python scripts/test_ga_intake.py`.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update       # noqa: E402
import official_ga  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# A trimmed gasupreme.us /2026-opinions/ page with both layouts and the edge cases the live page has:
# h3 dates (from 06-30), p dates (to 06-16), non-release <p><strong> lines, a multi-docket entry, a
# PDF named for something other than the docket, an upload folder later than the release, and a
# release line carrying a summaries link.
PAGE = """
<h3>September</h3>
<h3><strong>September 22, 2026</strong></h3>
<ul>
<li><a href="/wp-content/uploads/2026/09/s25g1446.pdf" target="_blank" rel="noopener">S25G1446, S25G1447. CRAVENS v. SLAUGHTER et al. (two cases)</a></li>
<li><a href="/wp-content/uploads/2026/09/s26a0604.pdf">S26A0604. WARE v. THE STATE</a></li>
</ul>
<p><strong>10:00 AM</strong></p>
<p><strong>Wednesday, November 04, 2026</strong></p>
<ul><li><a href="/wp-content/uploads/2026/09/argument.pdf">Oral argument calendar</a></li></ul>
<h3>August</h3>
<h3><strong>August 13, 2026</strong></h3>
<ul>
<li><a href="/wp-content/uploads/2026/08/s16y0723_Reinstatement.pdf" target="_blank" rel="noopener">S16Y0723, S16Y0724, S16Y0725. IN THE MATTER OF SHANINA NASHAE LANK</a><span style="color: #dd0000;"> <span style="color: red;">8-13-2026 Reinstatement issued.</span></span></li>
</ul>
<h3><strong>August 11, 2026</strong></h3>
<ul>
<li><a href="/wp-content/uploads/2026/08/s26g0149.pdf" target="_blank" rel="noopener">S26G0149. MCLAMB v. MAYOR AND ALDERMEN OF THE CITY OF SAVANNAH</a></li>
<li><a href="/wp-content/uploads/2026/08/s26g0155.pdf" target="_blank" rel="noopener">S26G0155. MUHAMMAD v. CLAYTON COUNTY</a></li>
</ul>
<h3>June</h3>
<p><strong>June 16, 2026</strong></p>
<ul>
<li><a href="/wp-content/uploads/2026/06/s26a0001.pdf" target="_blank" rel="noopener">S26A0001. REASE v. THE STATE</a></li>
</ul>
<p><strong>June 2, 2026</strong></p>
<ul><li><a href="/wp-content/uploads/2026/07/s26a0062.pdf">S26A0062. CLARK v. THE STATE</a><span style="color: red;">7-1-2026 Substitute opinion issued.</span></li></ul>
<h3>January</h3>
<p><strong>January 5, 2026&#8212;<a href="/wp-content/uploads/2026/01/Jan5Ops.pdf">SUMMARIES of NOTEWORTHY OPINIONS</a></strong></p>
<ul><li><a href="https://www.gasupreme.us/wp-content/uploads/2026/01/s25a1000.pdf">S25A1000. EARLY v. THE STATE</a></li></ul>
"""

PAD = " The court considered the record and the briefs of the parties in full." * 20


def opinion(docket, decided, argued=None):
    line = ("Argued: %s — Decided: %s" % (argued, decided)) if argued else "Decided: %s" % decided
    return ("In the\nSupreme Court of Georgia\nNo. %s\nA Party\nv.\nAnother Party\nOn Writ of Certiorari "
            "from the Court of Appeals\nNo. A25A0936\n%s\nPETERSON, Chief Justice.%s" % (docket, line, PAD))


def atom(items, enclosure=True):
    """A CourtListener-shaped Atom feed: items are (cid, name, published, pdf)."""
    out = ['<?xml version="1.0"?>', '<feed xmlns="http://www.w3.org/2005/Atom">']
    for cid, name, pub, pdf in items:
        out.append("<entry><title>%s</title>" % name)
        out.append('<link rel="alternate" href="https://www.courtlistener.com/opinion/%d/x/"/>' % cid)
        if enclosure and pdf:
            out.append('<link rel="enclosure" type="application/pdf" href="%s"/>' % pdf)
        out.append("<published>%sT00:00:00-07:00</published>" % pub)
        out.append("<summary>NOTICE: This opinion is subject to modification resulting from motions "
                   "for reconsideration under Supreme Court Rule 27.</summary></entry>")
    out.append("</feed>")
    return "\n".join(out).encode()


class FakeSearch:
    """The /feed/search/ endpoint over a GA universe {cid: (name, dateFiled, docket)}: at most `cap`
    entries per query, in an order unrelated to cluster id, no paging, no enclosures."""

    def __init__(self, universe, cap=20, fail_on=None):
        self.u, self.cap, self.queries, self.fail_on = universe, cap, [], fail_on

    def __call__(self, q, deadline=None):
        self.queries.append(q)
        if self.fail_on is not None and len(self.queries) == self.fail_on:
            raise OSError("feed unreachable")
        m = re.match(r"cluster_id:\[(\d+) TO (\d+|\*)\]$", q)
        if m:
            lo, hi = int(m.group(1)), (None if m.group(2) == "*" else int(m.group(2)))
            hits = [c for c in self.u if c >= lo and (hi is None or c <= hi)]
        else:
            d = re.match(r'docketNumber:"([^"]+)"$', q).group(1)
            hits = [c for c, v in self.u.items() if v[2] == d]
        hits.sort(key=lambda c: (c * 7919) % 104729)        # arbitrary, not by id
        raw = atom([(c, self.u[c][0], self.u[c][1], "") for c in hits[:self.cap]], enclosure=False)
        return update._parse_feed(raw, "ga")


@contextlib.contextmanager
def patched(*triples):
    saved = []
    try:
        for obj, name, val in triples:
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        yield
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)


def clear_ga_caches():
    official_ga._page_cache.clear()
    official_ga._index_cache.clear()
    official_ga._year_cache.clear()


# ---- official_ga --------------------------------------------------------------------------------

def test_release_page():
    rel = official_ga.parse_releases(PAGE)
    by = {e["dockets"][0]: e for e in rel}
    check("every opinion row parsed, non-opinion rows skipped", sorted(by) ==
          sorted(["S25G1446", "S26A0604", "S16Y0723", "S26G0149", "S26G0155", "S26A0001", "S26A0062", "S25A1000"]),
          sorted(by))
    check("h3 release date (post-06-30 layout)", by["S26G0149"]["date"] == "2026-08-11", by["S26G0149"])
    check("p release date (pre-06-30 layout)", by["S26A0001"]["date"] == "2026-06-16", by["S26A0001"])
    check("oral-argument <p><strong> lines do not open a release", by["S26A0604"]["date"] == "2026-09-22")
    check("release line with a summaries link still dates its list", by["S25A1000"]["date"] == "2026-01-05")
    check("date comes from the heading, not the PDF's upload folder", by["S26A0062"]["date"] == "2026-06-02")
    check("multi-docket entry keeps every docket", by["S25G1446"]["dockets"] == ["S25G1446", "S25G1447"])
    check("absolute url, title and note", by["S16Y0723"]["url"] ==
          "https://www.gasupreme.us/wp-content/uploads/2026/08/s16y0723_Reinstatement.pdf"
          and by["S16Y0723"]["title"] == "IN THE MATTER OF SHANINA NASHAE LANK"
          and "Reinstatement issued" in by["S16Y0723"]["note"], by["S16Y0723"])
    idx = official_ga.release_index("2026", html=PAGE)
    check("index keys every docket of a consolidated entry", idx["S16Y0725"] is idx["S16Y0723"]
          and idx["S25G1447"]["date"] == "2026-09-22")
    # The official-link enrichment keeps working on the same page (it never read the headings).
    clear_ga_caches()
    check("official_url_for still resolves on the h3 layout",
          official_ga.official_url_for({"court": "scotga", "date": "2026-08-11", "dockets": ["S26G0149"]},
                                       html=PAGE) == "https://www.gasupreme.us/wp-content/uploads/2026/08/s26g0149.pdf")
    clear_ga_caches()
    # One fetch serves both the URL map and the release index; a transport error is not cached.
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("reset")
        return PAGE
    with patched((official_ga, "_fetch", fetch)):
        check("transport error fails open to {}", official_ga.release_index("2026") == {})
        idx2 = official_ga.release_index("2026")
        official_ga._year_map("2026")
    check("retry after a transport error, then one shared fetch", len(calls) == 2 and "S26G0149" in idx2, calls)
    clear_ga_caches()


def test_pdf_caption():
    t = opinion("S26G0149", "August 11, 2026", argued="June 17, 2026")
    check("caption docket (the lower-court docket is ignored)", official_ga.dockets_in_text(t) == ["S26G0149"],
          official_ga.dockets_in_text(t))
    check("Decided: after Argued: on the same line", official_ga.decided_date(t) == "2026-08-11")
    t2 = "In the Supreme Court of Georgia\nNos. S25G1446, S25G1447\nDecided: September 22, 2026\n" + PAD
    check("Nos. with two dockets", official_ga.dockets_in_text(t2) == ["S25G1446", "S25G1447"])
    check("per curiam Decided: line", official_ga.decided_date(t2) == "2026-09-22")
    check("no caption -> nothing", official_ga.dockets_in_text(PAD) == [] and official_ga.decided_date(PAD) == "")
    late = PAD * 10 + "Decided: January 1, 2020"
    check("a date deep in the body is not the decision date", official_ga.decided_date(late) == "")


# ---- discovery -----------------------------------------------------------------------------------

def test_search_feed_parse():
    items = update._parse_feed(atom([(10975752, "McLamb v. Mayor", "2026-06-16", "")], enclosure=False), "ga")
    check("search-feed entry parses with no enclosure", len(items) == 1 and items[0]["cluster_id"] == 10975752
          and items[0]["pdf_url"] == "" and items[0]["dateFiled"] == "2026-06-16" and items[0]["docketNumber"] == "",
          items)
    seen = []

    def fg(url, deadline=None):
        seen.append(url)
        return atom([], enclosure=False)
    with patched((update, "feed_get", fg)):
        update.ga_search_feed("cluster_id:[1 TO *]")
    check("search feed is the free /feed/ path, court=ga, no order_by",
          seen and seen[0].startswith("https://www.courtlistener.com/feed/search/?") and "court=ga" in seen[0]
          and "order_by" not in seen[0] and "/api/rest/" not in seen[0], seen)


def test_enumerate():
    universe = {c: ("Case %d v. State" % c, "2026-06-16", "") for c in
                list(range(1000, 1060)) + [5000, 5001, 7000]}
    fs = FakeSearch(universe)
    with patched((update.time, "sleep", lambda *a, **k: None)):
        found, cov, complete, q = update.ga_enumerate(999, 5001, search=fs)
    check("full pages split until every cluster is listed", set(found) == set(universe) and complete,
          (len(found), complete))
    check("every query counted", q == len(fs.queries) and q > 3, q)
    # The open tail is bounded by the highest id a full page returned (ceiling below everything).
    fs2 = FakeSearch(universe)
    with patched((update.time, "sleep", lambda *a, **k: None)):
        found2, _, complete2, _ = update.ga_enumerate(999, 0, search=fs2)
    check("saturated open tail is bounded and split", set(found2) == set(universe) and complete2)
    # Query cap: stops early, and `covered` is a true lower bound (everything at or below it found).
    fs3 = FakeSearch(universe)
    with patched((update.time, "sleep", lambda *a, **k: None)):
        found3, cov3, complete3, q3 = update.ga_enumerate(999, 7000, search=fs3, max_queries=4)
    check("query cap -> incomplete", not complete3 and q3 == 4)
    check("covered is a prefix: every cluster <= covered was found",
          all(c in found3 for c in universe if c <= cov3) and cov3 >= 999, (cov3, sorted(found3)[:3]))
    # A feed error mid-walk stops cleanly with what was covered.
    fs4 = FakeSearch(universe, fail_on=2)
    with contextlib.redirect_stdout(io.StringIO()), patched((update.time, "sleep", lambda *a, **k: None)):
        found4, cov4, complete4, _ = update.ga_enumerate(999, 7000, search=fs4)
    check("feed error -> incomplete, covered not advanced past a gap",
          not complete4 and all(c in found4 for c in universe if c <= cov4))
    # Nothing new above the mark: a single open query.
    fs5 = FakeSearch(universe)
    found5, cov5, complete5, q5 = update.ga_enumerate(7000, 7000, search=fs5)
    check("quiet day: one query, complete, nothing found", found5 == {} and complete5 and q5 == 1)


def test_seed_mark():
    tmp = tempfile.mkdtemp(prefix="ga-seed-")
    try:
        rp = os.path.join(tmp, "rej.jsonl")
        with open(rp, "w") as f:
            f.write(json.dumps({"court": "scotga", "cluster_id": 10875591}) + "\n")
            f.write(json.dumps({"court": "gactapp", "cluster_id": 10999999}) + "\n")
            f.write("not json\n")
        cards = [{"court": "scotga", "cluster_id": 10875605}, {"court": "ca11", "cluster_id": 10990000}]
        check("seed = highest scotga cluster from cards and rejections",
              update._ga_seed_mark(cards, rp) == 10875605)
        check("seed falls back to siteconfig when nothing is known",
              update._ga_seed_mark([], os.path.join(tmp, "missing")) == update.siteconfig.GA_HIGH_WATER_SEED)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_backlog_and_floor():
    def it(cid, court, filed):
        return {"cluster_id": cid, "caseName": "Case %d" % cid, "court_id": court, "dateFiled": filed}
    found = {c: it(c, "ga", "2026-06-16") for c in (101, 102, 103, 104, 105)}
    found[106] = it(106, "ga", "2026-09-28")           # in-window: an ordinary candidate, not backlog
    found[90] = it(90, "ga", "2026-06-16")             # at/below the mark: not backlog
    admitted, remain = update._ga_backlog(found, 100, "2026-09-27", known={102}, per_run=2)
    check("backlog: never-seen only, oldest first, capped", admitted == [101, 103] and remain == [104, 105],
          (admitted, remain))
    results = list(found.values()) + [it(500, "gactapp", "2026-06-01"), it(501, "gactapp", "2026-09-28"),
                                      it(502, "ala", "2026-09-01")]
    cand, fd = update._select_candidates(results, "2026-09-27", "2026-10-01", have=set(), seen={102},
                                         pending_review=set(), redraft_pending=set(),
                                         exempt={101, 102, 103}, held_back={104, 105})
    ids = sorted(update.cluster_id_of(r) for r in cand)
    check("admitted backlog passes the floor; a seen cluster does not, even if exempt",
          ids == [101, 103, 106, 501], ids)
    check("waiting backlog is not counted as floor-dropped; other courts unchanged",
          fd == {"ala": 1, "ga": 1, "gactapp": 1}, fd)


def test_next_mark():
    found = {c: {} for c in (101, 102, 103, 104, 105)}
    # 101, 102 settled; 103 admitted but deferred; 104/105 waiting.
    new, tries, gave = update._ga_next_mark(100, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                            tries={}, max_tries=3)
    check("mark stops below the first outstanding cluster", new == 102 and tries == {103: 1} and gave == [], (new, tries))
    new, tries, gave = update._ga_next_mark(102, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                            tries={103: 2}, max_tries=3)
    check("a stuck admitted cluster is given up after max tries; waiting ones still hold the mark",
          gave == [103] and new == 103 and tries == {}, (new, tries, gave))
    new, tries, gave = update._ga_next_mark(100, found, 105, True, resolved=set(found), waiting=set(),
                                            tries={}, max_tries=3)
    check("everything settled: mark rises to the highest listed cluster", new == 105)
    new, _, _ = update._ga_next_mark(100, {101: {}, 150: {}}, 120, False, resolved={101, 150}, waiting=set(),
                                     tries={}, max_tries=3)
    check("incomplete walk: mark never passes what was covered", new == 120, new)
    new, _, _ = update._ga_next_mark(300, {}, 250, False, resolved=set(), waiting=set(), tries={}, max_tries=3)
    check("mark never moves backwards", new == 300)


# ---- dating and dedupe ------------------------------------------------------------------------------

def test_resolve():
    idx = official_ga.release_index("2026", html=PAGE)
    universe = {10875591: ("Rease v. State", "2026-06-16", "S26A0001"),
                10975744: ("Rease v. State", "2026-06-16", "S26A0001"),
                10975752: ("McLamb v. Mayor and Aldermen of the City of Savannah", "2026-06-16", "S26G0149"),
                10975754: ("Muhammad v. Clayton County", "2026-06-16", "S26G0155"),
                5749566: ("In the Matter of Lank", "2016-05-02", "S16Y0723"),
                10975766: ("In the Matter of Shanina Nashae Lank", "2026-06-16", "S16Y0723")}
    fs = FakeSearch(universe)
    pdfs = {"https://storage.courtlistener.com/pdf/2026/06/16/mclamb.pdf":
            opinion("S26G0149", "August 11, 2026", argued="June 17, 2026")}

    def ctx(known=()):
        return {"idx": idx, "known": set(known), "today": "2026-10-01", "deadline": None,
                "stats": {"dated": 0, "guessed": 0, "dups": 0, "unresolved": 0}}

    def item(cid, pdf=""):
        return {"cluster_id": cid, "caseName": universe[cid][0], "court_id": "ga", "dateFiled": "2026-06-16",
                "docketNumber": "", "snippet": "NOTICE: ...", "pdf_url": pdf, "absolute_url": "/opinion/%d/x/" % cid}

    with patched((update, "ga_search_feed", fs), (update, "pdf_text", lambda u, deadline=None: pdfs.get(u, ""))):
        r = item(10975752, "https://storage.courtlistener.com/pdf/2026/06/16/mclamb.pdf")
        st = update._ga_resolve(r, ctx())
        check("court-feed item: docket from the PDF caption, date from the h3 release",
              st == "resolved" and r["dateFiled"] == "2026-08-11" and r["cl_dateFiled"] == "2026-06-16"
              and r["docketNumber"] == "S26G0149" and r["_ga_date_source"] == "gasupreme.us", (st, r))
        check("the screen sees the opinion's opening, not the NOTICE boilerplate",
              r["snippet"].startswith("In the Supreme Court of Georgia") and r.get("_text"))

        r = item(10975754)
        c = ctx()
        st = update._ga_resolve(r, c)
        check("search-feed item: caption guess confirmed by a docket query; official PDF set",
              st == "resolved" and r["dateFiled"] == "2026-08-11" and r["docketNumber"] == "S26G0155"
              and r["pdf_url"] == "https://www.gasupreme.us/wp-content/uploads/2026/08/s26g0155.pdf"
              and c["stats"]["guessed"] == 1, (st, r))

        r = item(10975744)
        st = update._ga_resolve(r, ctx(known={10875591}))
        check("re-scrape of a seen docket on the same date is a duplicate",
              st == "dup" and r["_ga_twin"] == 10875591, (st, r.get("_ga_twin")))
        r = item(10975744)
        check("...but not when the earlier cluster was never seen (it is a new case to us)",
              update._ga_resolve(r, ctx(known=set())) == "resolved" and r["dateFiled"] == "2026-06-16")
        r = item(10975766)
        st = update._ga_resolve(r, ctx(known={5749566}))
        check("same docket, different decision (a 2026 reinstatement of a 2016 discipline) is not a duplicate",
              st == "resolved" and r["dateFiled"] == "2026-08-13", (st, r))

    # Fallback to the PDF's Decided: line when the page does not list the docket, and the unverified hold.
    with patched((update, "ga_search_feed", FakeSearch({})),
                 (update, "pdf_text", lambda u, deadline=None: opinion("S26A0999", "September 9, 2026"))):
        r = {"cluster_id": 77, "caseName": "Neely v. Parsell", "court_id": "ga", "dateFiled": "2026-06-16",
             "docketNumber": "", "pdf_url": "https://storage.courtlistener.com/pdf/neely.pdf"}
        st = update._ga_resolve(r, ctx())
        check("Decided: fallback dates a docket the release page lacks",
              st == "resolved" and r["dateFiled"] == "2026-09-09" and r["_ga_date_source"] == "Decided: line", r)
    with patched((update, "ga_search_feed", FakeSearch({})), (update, "pdf_text", lambda u, deadline=None: "")):
        r = {"cluster_id": 78, "caseName": "Nobody v. Noone", "court_id": "ga", "dateFiled": "2026-06-16",
             "docketNumber": "", "pdf_url": ""}
        check("no text yet -> pending (pass 1 retries once it has text)", update._ga_resolve(r, ctx()) == "pending")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            st = update._ga_resolve(r, ctx(), text="Opinion text without a caption." + PAD)
        check("still undated on the stuck date -> unresolved, flagged unverified, logged",
              st == "unresolved" and r["_ga_unverified"] and r["dateFiled"] == "2026-06-16"
              and "no official release date" in out.getvalue(), out.getvalue())
        r2 = {"cluster_id": 79, "caseName": "X v. Y", "court_id": "ga", "dateFiled": "2026-09-30",
              "docketNumber": "", "pdf_url": ""}
        with contextlib.redirect_stdout(io.StringIO()):
            update._ga_resolve(r2, ctx(), text="Opinion text." + PAD)
        check("undated on a real CourtListener date is kept and not held", r2["_ga_unverified"] is False)
    with patched((update, "ga_search_feed", FakeSearch({})),
                 (update, "pdf_text", lambda u, deadline=None: opinion("S26A0998", "December 1, 2026"))):
        r = {"cluster_id": 80, "caseName": "Future v. Date", "court_id": "ga", "dateFiled": "2026-06-16",
             "docketNumber": "", "pdf_url": "https://x/f.pdf"}
        with contextlib.redirect_stdout(io.StringIO()):
            update._ga_resolve(r, ctx())
        check("a date after today is never taken", r["dateFiled"] == "2026-06-16")


# ---- main(), end to end ------------------------------------------------------------------------------

MCLAMB_PDF = "https://storage.courtlistener.com/pdf/2026/06/16/mclamb.pdf"
UNIVERSE = {
    10875591: ("Rease v. State", "2026-06-16", "S26A0001"),        # the original scrape; seen
    10975740: ("Sanders v. State", "2026-06-16", "S26A0700"),      # already seen
    10975744: ("Rease v. State", "2026-06-16", "S26A0001"),        # the re-scrape
    10975746: ("NEELY v. PARSELL", "2026-06-16", "S26A0999"),      # not on the fixture page: Decided: fallback
    10975752: ("McLamb v. Mayor and Aldermen of the City of Savannah", "2026-06-16", "S26G0149"),
    10975754: ("Muhammad v. Clayton County", "2026-06-16", "S26G0155"),
    10978632: ("Ware v. State", "2026-06-16", "S26A0604"),
}
TEXTS = {
    MCLAMB_PDF: opinion("S26G0149", "August 11, 2026", argued="June 17, 2026"),
    "https://www.gasupreme.us/wp-content/uploads/2026/08/s26g0155.pdf": opinion("S26G0155", "August 11, 2026"),
    "https://www.gasupreme.us/wp-content/uploads/2026/09/s26a0604.pdf": opinion("S26A0604", "September 22, 2026"),
    "https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD,
}
# Text the REST fallback returns (stubbed; a cluster with no PDF the page could not match).
REST_TEXTS = {10975746: opinion("S26A0999", "September 9, 2026")}


def court_feed(court, deadline=None):
    if court == "ga":    # the 20-entry feed: tied dates, carries only some of the backlog
        return update._parse_feed(atom([(10975752, UNIVERSE[10975752][0], "2026-06-16", MCLAMB_PDF),
                                        (10875591, "Rease v. State", "2026-06-16", "https://x/rease.pdf")]), "ga")
    return update._parse_feed(atom([(20000001, "Acme v. Roe", "2026-09-28", "https://storage.courtlistener.com/pdf/coa.pdf"),
                                    (20000002, "Old v. Stale", "2026-06-01", "https://x/old.pdf")]), "gactapp")


def run_main(tmp, calls):
    """One real main() over stubbed I/O, with every written path in `tmp`. Returns stdout."""
    def summarize(court_id, name, docket, date_filed, text, note, cl_status=""):
        calls["summarize"].append((name, date_filed, docket))
        return {"relevant": True, "significance": "high", "areas": [next(iter(update.VALID_AREAS))],
                "synopsis": "A holding.", "why": "It matters.", "disposition": "affirmed",
                "confidence": "high", "additional_holdings": []}

    def no_network(*a, **k):
        raise AssertionError("unexpected network call")

    def opinion_text_full(r, deadline=None):
        calls["rest"].append(update.cluster_id_of(r))
        return REST_TEXTS.get(update.cluster_id_of(r), "")

    paths = [(update, n, os.path.join(tmp, n.lower())) for n in
             ("JSON_PATH", "STATE_PATH", "LOG_PATH", "FABLE_LOG_PATH", "REJECT_PATH", "SA_MANIFEST_PATH",
              "SA_STATE_PATH", "PR_PATH", "AUTO_PR_PATH", "REVIEW_PR_PATH")]
    with patched(*paths,
                 (update, "KEY", "test-key"), (update, "CL_TOKEN", "t"), (update, "DRY_RUN", False),
                 (update, "COURTS", ["ga", "gactapp"]), (update, "MAX_RUN", 80),
                 (update, "FUNNEL_BATCH", False), (update, "TRIAGE_BATCH", False), (update, "GUARD_BATCH", False),
                 (update, "SMELL_MODEL", ""), (update, "FABLE_MODE", "off"),
                 (update.siteconfig, "GA_BACKLOG_PER_RUN", 3),
                 (update.time, "sleep", lambda *a, **k: None),
                 (update, "_today_eastern", lambda: "2026-10-01"),
                 (update, "anthropic_status", lambda: ("operational", "ok")),
                 (update, "anthropic_json", no_network), (update, "feed_get", no_network),
                 (update, "feed_court", court_feed), (update, "ga_search_feed", FakeSearch(UNIVERSE)),
                 (update, "pdf_text", lambda u, deadline=None: TEXTS.get(u, "")),
                 (update, "opinion_text_full", opinion_text_full),
                 (update.cl_rate, "remaining", lambda: 100),
                 (update, "screen", lambda *a, **k: {"pass": True}),
                 (update, "pretriage", lambda *a, **k: {"pass": True}),
                 (update, "triage", lambda *a, **k: {"relevant": True, "significance": "high", "note": "", "treats": []}),
                 (update, "summarize", summarize),
                 (update, "enriched", lambda *a, **k: {}),
                 (update, "cluster_precedential_status", lambda *a, **k: "published"),
                 (update, "official_download_url", lambda *a, **k: ""),
                 (update, "crosscheck", lambda *a, **k: {}),
                 (update, "completeness_check", lambda *a, **k: {}),
                 (update, "fable_review_pass", lambda *a, **k: (set(), {})),
                 (update.render, "render", lambda entries: calls["render"].append(len(entries))),
                 (update.review_store, "load_pending", lambda *a, **k: set()),
                 (update.review_store, "load_redraft_ids", lambda *a, **k: set()),
                 (update.review_store, "stage_card", lambda e, reasons: calls["held"].append((e["cluster_id"], reasons))),
                 (update.review_store, "stage_treatment", no_network),
                 (update.review_store, "save_pending", lambda *a, **k: None),
                 (official_ga, "_fetch", lambda url: PAGE if url.endswith("/2026-opinions/") else no_network())):
        clear_ga_caches()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            update.main()
        clear_ga_caches()
        return buf.getvalue()


def test_main_end_to_end():
    tmp = tempfile.mkdtemp(prefix="ga-intake-")
    saved_env = {k: os.environ.pop(k, None) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
    try:
        existing = {"cluster_id": 10875605, "name": "Earlier v. Card", "court": "scotga", "date": "2026-06-16",
                    "dockets": ["S26A0100"], "areas": [next(iter(update.VALID_AREAS))], "first_seen": "2026-06-17"}
        with open(os.path.join(tmp, "json_path"), "w") as f:
            json.dump([existing], f)
        with open(os.path.join(tmp, "state_path"), "w") as f:
            json.dump({"last_filed": "2026-09-29", "seen_clusters": [10875591, 10975740]}, f)
        calls = {"summarize": [], "rest": [], "render": [], "held": []}

        out = run_main(tmp, calls)
        cards = {c["cluster_id"]: c for c in json.load(open(os.path.join(tmp, "json_path")))}
        state = json.load(open(os.path.join(tmp, "state_path")))
        check("mark seeded from the newest scotga card", "high-water mark initialized at cluster 10875605" in out, out)
        check("backlog log line", ". ga backlog: 3 admitted, 2 remain" in out, out)
        check("McLamb (in the court feed, bogus date) carded under its official date",
              cards.get(10975752, {}).get("date") == "2026-08-11"
              and cards[10975752].get("cl_date_filed") == "2026-06-16"
              and cards[10975752].get("dockets") == ["S26G0149"]
              and cards[10975752].get("official_url", "").endswith("/2026/08/s26g0149.pdf"), cards.get(10975752))
        check("Neely (search feed only, not on the page) dated by its Decided: line",
              cards.get(10975746, {}).get("date") == "2026-09-09" and cards[10975746].get("dockets") == ["S26A0999"],
              cards.get(10975746))
        check("the summarizer is told the official date",
              ("McLamb v. Mayor and Aldermen of the City of Savannah", "2026-08-11", "S26G0149") in calls["summarize"],
              calls["summarize"])
        check("Rease re-scrape skipped, logged and marked seen",
              "~ ga re-scrape duplicate" in out and 10975744 not in cards and 10975744 in state["seen_clusters"], out)
        check("waiting backlog (Muhammad, Ware) not drafted this run",
              10975754 not in cards and 10978632 not in cards)
        check("mark held just below the first waiting cluster", state.get("ga_high_water") == 10975753,
              state.get("ga_high_water"))
        check("non-GA court unchanged: in-window card keeps its date, stale item floor-counted",
              cards.get(20000001, {}).get("date") == "2026-09-28" and "cl_date_filed" not in cards[20000001]
              and "since floor dropped 1 never-seen item(s): gactapp=1" in out, out)
        check("nothing held, nothing re-dated pushes last_filed past the newest real date",
              calls["held"] == [] and state["last_filed"] == "2026-09-28", (calls["held"], state["last_filed"]))
        rec = json.loads(open(os.path.join(tmp, "log_path")).read().splitlines()[-1])
        check("run log carries the GA intake record", rec.get("ga", {}).get("admitted") == 3
              and rec["ga"]["remain"] == 2 and rec["ga"]["dups"] == 1 and rec["ga"]["mark"] == 10975753, rec.get("ga"))

        # Second run: the carried backlog drains, through the official PDFs (no REST text).
        calls2 = {"summarize": [], "rest": [], "render": [], "held": []}
        out2 = run_main(tmp, calls2)
        cards = {c["cluster_id"]: c for c in json.load(open(os.path.join(tmp, "json_path")))}
        state = json.load(open(os.path.join(tmp, "state_path")))
        check("second run admits the carried backlog", ". ga backlog: 2 admitted, 0 remain" in out2, out2)
        check("Muhammad and Ware carded under their official dates",
              cards.get(10975754, {}).get("date") == "2026-08-11" and cards.get(10978632, {}).get("date") == "2026-09-22",
              (cards.get(10975754), cards.get(10978632)))
        check("no REST text fetch for clusters the official page matched", calls2["rest"] == [], calls2["rest"])
        check("mark advances past the drained backlog", state.get("ga_high_water") == 10978632,
              state.get("ga_high_water"))
        check("previously carded clusters are not redrafted", not any(n.startswith("McLamb") for n, _, _ in calls2["summarize"]))

        # Third run: nothing new; the no-op run still persists state and the mark stays put.
        calls3 = {"summarize": [], "rest": [], "render": [], "held": []}
        out3 = run_main(tmp, calls3)
        state = json.load(open(os.path.join(tmp, "state_path")))
        check("quiet run: nothing drafted, mark unchanged",
              calls3["summarize"] == [] and state.get("ga_high_water") == 10978632
              and ". ga backlog: 0 admitted, 0 remain" in out3, out3)
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def test_unverified_card_is_held():
    """A GA card whose date could not be verified (stuck CourtListener date, no page entry, no Decided:
    line) is held for review rather than auto-published under a wrong date."""
    tmp = tempfile.mkdtemp(prefix="ga-unverified-")
    saved_env = {k: os.environ.pop(k, None) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
    global UNIVERSE, TEXTS, REST_TEXTS
    old = UNIVERSE, TEXTS, REST_TEXTS
    try:
        UNIVERSE = {10990001: ("Undated v. Nobody", "2026-06-16", "S26A0555")}
        TEXTS = {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
        REST_TEXTS = {10990001: "Opinion with no caption block." + PAD}
        with open(os.path.join(tmp, "json_path"), "w") as f:
            json.dump([], f)
        with open(os.path.join(tmp, "state_path"), "w") as f:
            json.dump({"last_filed": "2026-09-29", "seen_clusters": [], "ga_high_water": 10990000}, f)
        calls = {"summarize": [], "rest": [], "render": [], "held": []}
        out = run_main(tmp, calls)
        held = dict(calls["held"])
        check("undated GA card on the stuck date is held, with the reason",
              10990001 in held and any("release date unverified" in x for x in held[10990001]), (calls["held"], out))
        state = json.load(open(os.path.join(tmp, "state_path")))
        check("the held card's stuck date never reaches last_filed (only the auto lane sets it)",
              state["last_filed"] == "2026-09-28", state["last_filed"])
        check("the held cluster stays unseen (a veto redrafts it); the pending ledger, not the mark, holds it",
              10990001 not in state["seen_clusters"] and state.get("ga_high_water") == 10990001, state)
    finally:
        UNIVERSE, TEXTS, REST_TEXTS = old
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("official_ga release page:")
    test_release_page()
    print("opinion caption block:")
    test_pdf_caption()
    print("search feed:")
    test_search_feed_parse()
    print("enumeration:")
    test_enumerate()
    print("seed mark:")
    test_seed_mark()
    print("backlog and the since floor:")
    test_backlog_and_floor()
    print("high-water mark:")
    test_next_mark()
    print("dating and dedupe:")
    test_resolve()
    print("main() end to end:")
    test_main_end_to_end()
    test_unverified_card_is_held()
    if FAILS:
        print("\n%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL GA INTAKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
