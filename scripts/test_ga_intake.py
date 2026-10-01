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
  * a backlog try is spent only when the cluster was actually evaluated and failed, never on a
    run that did not reach it (OPINIONS_MAX cut, REST-budget deferral); a cluster the mark passes
    is recorded in ga_abandoned, said loudly, and re-admitted later;
  * a feed that ignores the cluster-id range stops the walk; the court feed backs up the walk;
  * a vetoed GA card below the mark is looked up by id for its redraft, and an id is declared not GA
    only after empty answers on two separate runs;
  * infrastructure failures (timeouts, rate budgets, transport, API 429/5xx) never spend a try;
  * an attempted abandoned retry is re-stamped, so stuck entries rotate out of the retry slots;
  * a partial search-feed page carrying out-of-range clusters is a malfunction;
  * a mark held GA_MARK_STALL_RUNS runs with backlog left is said loudly, naming the blocker;
  * an unavailable release index is said loudly, once per run;
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


class RangeBlindSearch(FakeSearch):
    """A malfunctioning search feed: for a closed cluster-id range no wider than `blind_width` it
    ignores the range and returns a full page of arbitrary clusters (as if no filter were given)."""

    def __init__(self, universe, blind_width=0, cap=20):
        super().__init__(universe, cap=cap)
        self.blind_width = blind_width

    def __call__(self, q, deadline=None):
        m = re.match(r"cluster_id:\[(\d+) TO (\d+)\]$", q)
        if m and int(m.group(2)) - int(m.group(1)) <= self.blind_width:
            self.queries.append(q)
            lo = int(m.group(1))
            hits = sorted(c for c in self.u if c > lo + self.blind_width or c < lo)
            hits.sort(key=lambda c: (c * 7919) % 104729)
            raw = atom([(c, self.u[c][0], self.u[c][1], "") for c in hits[:self.cap]], enclosure=False)
            return update._parse_feed(raw, "ga")
        return super().__call__(q, deadline)


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
    # A feed that ignores the range filter on a single id: a full page with nothing in range is a
    # malfunction, never "listed": the walk stops, `covered` stays put, and the log says so loudly.
    fs6 = RangeBlindSearch(universe, blind_width=0)
    with contextlib.redirect_stdout(io.StringIO()) as out6, patched((update.time, "sleep", lambda *a, **k: None)):
        found6, cov6, complete6, _ = update.ga_enumerate(1000, 1001, search=fs6)
    check("single-id full page with no in-range item -> stop, covered not advanced, loud",
          not complete6 and cov6 == 1000 and found6 == {} and "! ga discovery" in out6.getvalue()
          and "ignored the range filter" in out6.getvalue(), (cov6, complete6, out6.getvalue()))
    # The same blindness on narrow ranges deep in a split walk: everything listed is real, covered is
    # still a prefix, and nothing is taken from the bad page.
    fs7 = RangeBlindSearch(universe, blind_width=30)
    with contextlib.redirect_stdout(io.StringIO()) as out7, patched((update.time, "sleep", lambda *a, **k: None)):
        found7, cov7, complete7, _ = update.ga_enumerate(999, 7000, search=fs7)
    check("range-blind feed mid-walk -> incomplete, covered is a true prefix, loud",
          not complete7 and all(c in found7 for c in universe if c <= cov7)
          and all(c in universe for c in found7) and "! ga discovery" in out7.getvalue(),
          (cov7, complete7, out7.getvalue()))
    # A single-id query the feed answers correctly (one entry) is fine.
    fs8 = FakeSearch(universe)
    found8, cov8, complete8, _ = update.ga_enumerate(1000, 1001, search=fs8)
    check("a true single-id answer is listed", 1001 in found8 and complete8, (sorted(found8)[:3], complete8))


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
    # 101, 102 settled; 103 admitted but never reached (cut, budget, deferral); 104/105 waiting.
    new, tries, gave = update._ga_next_mark(100, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                            tries={}, max_tries=3)
    check("mark stops below the first outstanding cluster", new == 102 and gave == [], (new, tries))
    check("a cluster the run never reached spends no try", tries == {}, tries)
    for _ in range(10):      # a quota-starved stretch: nothing is ever charged, nothing is ever passed
        new, tries, gave = update._ga_next_mark(102, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                                tries=tries, max_tries=3)
    check("ten starved runs: the mark still holds below it, no tries spent", new == 102 and tries == {} and gave == [],
          (new, tries, gave))
    new, tries, gave = update._ga_next_mark(102, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                            tries={103: 1}, max_tries=3, failed={103})
    check("an evaluated failure spends one try", new == 102 and tries == {103: 2} and gave == [], (new, tries, gave))
    new, tries, gave = update._ga_next_mark(102, found, 105, True, resolved={101, 102}, waiting={104, 105},
                                            tries={103: 2}, max_tries=3, failed={103})
    check("a cluster that failed max_tries evaluations is given up; waiting ones still hold the mark",
          gave == [103] and new == 103 and tries == {}, (new, tries, gave))
    new, tries, gave = update._ga_next_mark(102, {101: {}, 102: {}}, 102, False, resolved={101, 102},
                                            waiting=set(), tries={103: 2}, max_tries=3)
    check("tries of a cluster an incomplete walk did not list are carried", tries == {103: 2} and gave == [],
          (new, tries, gave))
    new, tries, gave = update._ga_next_mark(100, found, 105, True, resolved=set(found), waiting=set(),
                                            tries={}, max_tries=3)
    check("everything settled: mark rises to the highest listed cluster", new == 105)
    new, _, _ = update._ga_next_mark(100, {101: {}, 150: {}}, 120, False, resolved={101, 150}, waiting=set(),
                                     tries={}, max_tries=3)
    check("incomplete walk: mark never passes what was covered", new == 120, new)
    new, _, _ = update._ga_next_mark(300, {}, 250, False, resolved=set(), waiting=set(), tries={}, max_tries=3)
    check("mark never moves backwards", new == 300)


def test_lookup_ids():
    universe = {500: ("Vetoed v. State", "2026-06-16", "S26A0300"), 501: ("Other v. State", "2026-06-16", "")}
    fs = FakeSearch(universe)
    with patched((update.time, "sleep", lambda *a, **k: None)):
        got, absent, q = update.ga_lookup_ids([500, 999], search=fs)
    check("lookup by id: a GA cluster is returned, a non-GA id is absent",
          set(got) == {500} and absent == {999} and q == 2 and fs.queries == ["cluster_id:[500 TO 500]",
                                                                             "cluster_id:[999 TO 999]"],
          (got, absent, fs.queries))
    blind = RangeBlindSearch({c: ("C%d" % c, "2026-06-16", "") for c in range(600, 640)}, blind_width=0)
    with contextlib.redirect_stdout(io.StringIO()) as out, patched((update.time, "sleep", lambda *a, **k: None)):
        got2, absent2, q2 = update.ga_lookup_ids([500, 501], search=blind)
    check("a range-blind answer is a malfunction: nothing taken, nothing marked absent, loud, stop",
          got2 == {} and absent2 == set() and q2 == 1 and "ignored the range filter" in out.getvalue(),
          (got2, absent2, q2, out.getvalue()))
    with patched((update.time, "sleep", lambda *a, **k: None)):
        _, _, q3 = update.ga_lookup_ids([500, 501, 502], search=FakeSearch(universe), max_queries=2)
    check("lookups respect the query cap", q3 == 2)


def test_abandoned_helpers():
    ab = [{"cluster_id": 10, "last_try": "2026-09-01", "reason": "x"},
          {"cluster_id": 11, "last_try": "2026-09-30", "reason": "x"},
          {"cluster_id": 12, "last_try": "2026-08-01", "reason": "x"},
          {"cluster_id": 13, "last_try": "2026-07-01", "reason": "x"}]
    due = update._ga_abandoned_due(ab, known={13}, today="2026-10-01", retry_days=7, per_run=2)
    check("abandoned retry: due by last try, oldest first, known ones skipped, capped", due == [12, 10], due)
    out, newly = update._ga_abandon_update(ab, gave_up=[20, 11], retried={12}, resolved={10},
                                           reasons={12: "no text again", 20: "no opinion text available"},
                                           names={20: "Lost v. Found"}, today="2026-10-01", tries=3)
    by = {a["cluster_id"]: a for a in out}
    check("resolved entries leave the list; a retried one is re-stamped with its new reason",
          10 not in by and by[12]["last_try"] == "2026-10-01" and by[12]["reason"] == "no text again"
          and by[12]["retries"] == 1 and by[11]["last_try"] == "2026-09-30", out)
    check("a newly passed cluster is recorded with its reason; an already-recorded one is not duplicated",
          [a["cluster_id"] for a in newly] == [20] and by[20]["reason"] == "no opinion text available"
          and by[20]["name"] == "Lost v. Found" and by[20]["tries"] == 3
          and sum(1 for a in out if a["cluster_id"] == 11) == 1, newly)


def test_release_index_warning():
    def fails(url):
        raise OSError("connection reset")
    clear_ga_caches()
    with patched((official_ga, "_fetch", fails)):
        errs = []
        check("release_index still fails open, and reports why when asked",
              official_ga.release_index("2026", errors=errs) == {} and errs and "connection reset" in errs[0], errs)
        update._GA_INDEX_WARNED.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            idx = update._ga_release_index({"2026"})
            update._ga_release_index({"2026"})
    lines = [ln for ln in out.getvalue().splitlines() if "release index unavailable" in ln]
    check("unavailable index -> one loud line per run, naming the reason and the fallback",
          idx == {} and len(lines) == 1 and lines[0].strip().startswith("! ga: release index unavailable (")
          and "connection reset" in lines[0] and "dating falls back to PDF Decided: lines" in lines[0], out.getvalue())
    clear_ga_caches()

    def next_year_missing(url):
        if url.endswith("/2026-opinions/"):
            return PAGE
        raise update.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    with patched((official_ga, "_fetch", next_year_missing)):
        update._GA_INDEX_WARNED.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            idx2 = update._ga_release_index({"2026", "2027"})
    check("a year page not yet opened is quiet when another year has releases",
          "S26G0149" in idx2 and "release index unavailable" not in out2.getvalue(), out2.getvalue())
    clear_ga_caches()
    with patched((official_ga, "_fetch", lambda url: "<html>redesigned</html>")):
        update._GA_INDEX_WARNED.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out3:
            update._ga_release_index({"2026"})
    check("a page that parses to nothing (a redesign) is loud too",
          "! ga: release index unavailable (no releases parsed" in out3.getvalue(), out3.getvalue())
    clear_ga_caches()
    update._GA_INDEX_WARNED.clear()


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


GA_COURT_FEED = [(10975752, "McLamb v. Mayor and Aldermen of the City of Savannah", "2026-06-16", MCLAMB_PDF),
                 (10875591, "Rease v. State", "2026-06-16", "https://x/rease.pdf")]


def court_feed(court, deadline=None):
    if court == "ga":    # the 20-entry feed: tied dates, carries only some of the backlog
        return update._parse_feed(atom(GA_COURT_FEED), "ga")
    return update._parse_feed(atom([(20000001, "Acme v. Roe", "2026-09-28", "https://storage.courtlistener.com/pdf/coa.pdf"),
                                    (20000002, "Old v. Stale", "2026-06-01", "https://x/old.pdf")]), "gactapp")


def run_main(tmp, calls, extra=()):
    """One real main() over stubbed I/O, with every written path in `tmp`. Returns stdout. `extra`
    is more (obj, name, value) patches, applied last (so they override the defaults here)."""
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
                 (official_ga, "_fetch", lambda url: PAGE if url.endswith("/2026-opinions/") else no_network()),
                 *extra):
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


@contextlib.contextmanager
def scenario(prefix, universe, texts, rest_texts, state, cards=(), feed=None):
    """A temp dir seeded with opinions.json and opinions_state.json, and the module fixtures swapped."""
    global UNIVERSE, TEXTS, REST_TEXTS, GA_COURT_FEED
    tmp = tempfile.mkdtemp(prefix=prefix)
    saved_env = {k: os.environ.pop(k, None) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
    old = UNIVERSE, TEXTS, REST_TEXTS, GA_COURT_FEED
    try:
        UNIVERSE, TEXTS, REST_TEXTS = universe, texts, rest_texts
        if feed is not None:
            GA_COURT_FEED = feed
        with open(os.path.join(tmp, "json_path"), "w") as f:
            json.dump(list(cards), f)
        with open(os.path.join(tmp, "state_path"), "w") as f:
            json.dump(state, f)
        yield tmp
    finally:
        UNIVERSE, TEXTS, REST_TEXTS, GA_COURT_FEED = old
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _state(tmp):
    return json.load(open(os.path.join(tmp, "state_path")))


def _cards(tmp):
    return {c["cluster_id"]: c for c in json.load(open(os.path.join(tmp, "json_path")))}


def _calls():
    return {"summarize": [], "rest": [], "render": [], "held": []}


def test_starved_backlog_is_never_charged_then_abandoned_loudly():
    """A backlog cluster the run never reaches (cut by OPINIONS_MAX, deferred on the REST budget)
    holds the mark for free, however many runs that lasts; one that is evaluated and fails spends a
    try, and after GA_BACKLOG_MAX_TRIES failures it is abandoned -- recorded, said loudly, and
    re-admitted later, so a fix for its text source still cards it."""
    textless = 10990001
    uni = {textless: ("Textless v. Nobody", "2026-06-16", "S26A0777")}
    texts = {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-starved-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [],
                                                  "ga_high_water": 10990000}, feed=[]) as tmp:
        # Run 1: cut by OPINIONS_MAX (the in-window Court of Appeals card sorts first).
        c1 = _calls()
        run_main(tmp, c1, extra=[(update, "MAX_RUN", 1)])
        st = _state(tmp)
        check("OPINIONS_MAX cut: no try spent, mark held below the cluster",
              st.get("ga_high_water") == 10990000 and not st.get("ga_backlog_tries") and c1["rest"] == [],
              (st.get("ga_high_water"), st.get("ga_backlog_tries"), c1["rest"]))
        # Runs 2-5: the CourtListener REST budget is spent, so its text fetch is deferred every time.
        for _ in range(4):
            run_main(tmp, _calls(), extra=[(update.cl_rate, "remaining", lambda: 0)])
        st = _state(tmp)
        check("four REST-starved runs: still no try spent, mark still held, nothing abandoned",
              st.get("ga_high_water") == 10990000 and not st.get("ga_backlog_tries") and not st.get("ga_abandoned"),
              st)
        # Runs 6-8: REST is available but returns no text -- a real, failed evaluation each time.
        outs = []
        for _ in range(3):
            outs.append(run_main(tmp, _calls()))
        st = _state(tmp)
        ab = {a["cluster_id"]: a for a in st.get("ga_abandoned") or []}
        check("after three failed evaluations the mark passes it, and it is recorded in ga_abandoned",
              st.get("ga_high_water") == textless and textless in ab and "no opinion text" in ab[textless]["reason"]
              and ab[textless]["tries"] == 3 and ab[textless]["name"].startswith("Textless"), st)
        check("abandonment is loud and names the queue.txt recovery",
              "! ga backlog: ABANDONED cluster %d" % textless in outs[-1] and "queue.txt" in outs[-1]
              and "! ga backlog: 1 abandoned cluster(s) outstanding" in outs[-1], outs[-1])
        check("...and only on the run that passed it", all("ABANDONED" not in o for o in outs[:-1]))
        # Run 9: the retry is not due yet (last try today); no fetch, entry kept.
        c9 = _calls()
        run_main(tmp, c9)
        check("not due yet: no retry, still recorded", c9["rest"] == [] and _state(tmp).get("ga_abandoned"),
              (c9["rest"], _state(tmp).get("ga_abandoned")))
        # Run 10: a week later and the text source fixed: the abandoned cluster is looked up by id,
        # re-admitted past the floor, carded, and leaves the list.
        st = _state(tmp)
        st["ga_abandoned"][0]["last_try"] = "2026-09-20"
        with open(os.path.join(tmp, "state_path"), "w") as f:
            json.dump(st, f)
        global REST_TEXTS
        REST_TEXTS = {textless: opinion("S26A0777", "September 15, 2026")}
        c10 = _calls()
        out10 = run_main(tmp, c10)
        st = _state(tmp)
        check("a due abandoned cluster is re-admitted and carded under its Decided: date",
              _cards(tmp).get(textless, {}).get("date") == "2026-09-15" and "1 abandoned retry" in out10,
              (out10, _cards(tmp).get(textless)))
        check("...and leaves ga_abandoned", not st.get("ga_abandoned"), st.get("ga_abandoned"))


def test_redraft_lookup():
    """A vetoed GA card below the mark is redrafted even though /feed/court/ga/ no longer carries it:
    its id is looked up on the search feed. A non-GA redraft id is remembered so it is not re-queried."""
    muhammad = 10975754
    uni = {muhammad: ("Muhammad v. Clayton County", "2026-06-16", "S26G0155")}
    texts = {"https://www.gasupreme.us/wp-content/uploads/2026/08/s26g0155.pdf": opinion("S26G0155", "August 11, 2026"),
             "https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-redraft-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [],
                                                  "ga_high_water": 10990000}, feed=[]) as tmp:
        fs = FakeSearch(uni)
        redraft = (update.review_store, "load_redraft_ids", lambda *a, **k: {muhammad, 20000099})
        out = run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", fs)])
        check("vetoed GA cluster below the mark is looked up by id and redrafted under its official date",
              _cards(tmp).get(muhammad, {}).get("date") == "2026-08-11"
              and "cluster_id:[%d TO %d]" % (muhammad, muhammad) in fs.queries, (out, fs.queries))
        st = _state(tmp)
        check("one empty answer is only noted, not a verdict: the id is not yet declared not GA",
              not st.get("ga_redraft_not_ga") and st.get("ga_redraft_absent_once") == {"20000099": "2026-10-01"}
              and ". ga redraft: cluster 20000099" in out and "first time" in out,
              (st.get("ga_redraft_not_ga"), st.get("ga_redraft_absent_once"), out))
        # A second run the same day is not a separate run: still only noted.
        fs_same = FakeSearch(uni)
        run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", fs_same)])
        st = _state(tmp)
        check("a second empty answer the same day does not count",
              not st.get("ga_redraft_not_ga") and "cluster_id:[20000099 TO 20000099]" in fs_same.queries,
              (st.get("ga_redraft_not_ga"), fs_same.queries))
        # A later run, still empty: now it is remembered as not GA, with a loud line.
        fs2 = FakeSearch(uni)
        out2 = run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", fs2),
                                              (update, "_today_eastern", lambda: "2026-10-02")])
        st = _state(tmp)
        check("a second empty answer on a later run: remembered as not GA, loudly",
              st.get("ga_redraft_not_ga") == [20000099] and not st.get("ga_redraft_absent_once")
              and "! ga redraft: cluster 20000099 is not on the Supreme Court of Georgia search feed" in out2,
              (st.get("ga_redraft_not_ga"), st.get("ga_redraft_absent_once"), out2))
        fs3 = FakeSearch(uni)
        run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", fs3)])
        check("...and not queried again", not any("20000099" in q for q in fs3.queries), fs3.queries)
    # A first empty answer followed by a real one clears the note: nothing is ever declared not GA.
    with scenario("ga-redraft-hiccup-", {}, {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD},
                  {}, {"last_filed": "2026-09-29", "seen_clusters": [], "ga_high_water": 10990000}, feed=[]) as tmp:
        flaky = 10975760
        redraft = (update.review_store, "load_redraft_ids", lambda *a, **k: {flaky})
        run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", FakeSearch({}))])
        check("hiccup: first empty answer noted", _state(tmp).get("ga_redraft_absent_once") == {str(flaky): "2026-10-01"},
              _state(tmp))
        back = FakeSearch({flaky: ("Flaky v. State", "2026-06-16", "S26A0888")})
        run_main(tmp, _calls(), extra=[redraft, (update, "ga_search_feed", back),
                                       (update, "_today_eastern", lambda: "2026-10-02")])
        st = _state(tmp)
        check("...the feed answers it on the next run: the note clears and it is never declared not GA",
              not st.get("ga_redraft_absent_once") and not st.get("ga_redraft_not_ga"), st)


def test_court_feed_backs_up_enumeration():
    """A never-seen GA cluster the court feed carries above the mark is admitted even when the
    cluster-id enumeration did not list it."""
    uni = {}       # the search feed lists nothing (it missed the cluster)
    texts = {MCLAMB_PDF: opinion("S26G0149", "August 11, 2026"),
             "https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-witness-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [10875591],
                                                  "ga_high_water": 10975000}) as tmp:
        out = run_main(tmp, _calls())
        st = _state(tmp)
        check("court-feed GA cluster missed by the enumeration is added, loudly, and carded",
              "! ga discovery: /feed/court/ga/ carries never-seen cluster 10975752" in out
              and _cards(tmp).get(10975752, {}).get("date") == "2026-08-11" and st.get("ga_high_water") == 10975752,
              (out, st.get("ga_high_water")))


def test_main_release_index_unavailable():
    """gasupreme.us down: one loud line, and the PDF's Decided: line still dates the card."""
    def down(url):
        raise OSError("gasupreme.us unreachable")
    uni = {10975752: UNIVERSE[10975752]}
    texts = {MCLAMB_PDF: opinion("S26G0149", "August 11, 2026"),
             "https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-index-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [10875591],
                                                "ga_high_water": 10975000}) as tmp:
        out = run_main(tmp, _calls(), extra=[(official_ga, "_fetch", down)])
        lines = [ln for ln in out.splitlines() if "release index unavailable" in ln]
        check("main(): unavailable release index is said once, with the reason",
              len(lines) == 1 and "gasupreme.us unreachable" in lines[0]
              and "dating falls back to PDF Decided: lines" in lines[0], out)
        check("...and the Decided: line still dates the card",
              _cards(tmp).get(10975752, {}).get("date") == "2026-08-11", _cards(tmp).get(10975752))


def test_enumerate_partial_stray_page():
    """A PARTIAL page carrying a cluster outside the queried range is a feed malfunction too (as in
    ga_lookup_ids): nothing from it is listed, the walk stops loudly, and `covered` does not advance."""
    universe = {c: ("Case %d v. State" % c, "2026-06-16", "") for c in (1000, 1001, 1002, 1003, 3000)}
    blind = RangeBlindSearch(universe, blind_width=0)          # a single-id query gets 4 strays: a partial page
    with contextlib.redirect_stdout(io.StringIO()) as out, patched((update.time, "sleep", lambda *a, **k: None)):
        found, cov, complete, _ = update.ga_enumerate(1000, 1001, search=blind)
    check("partial page of strays for a single id -> stop, covered not advanced, loud",
          found == {} and cov == 1000 and not complete and "out-of-range cluster(s)" in out.getvalue()
          and out.getvalue().startswith("  ! ga discovery"), (found, cov, complete, out.getvalue()))

    def mixed(q, deadline=None):       # the in-range cluster plus one from far outside, well under the cap
        lo = int(re.match(r"cluster_id:\[(\d+) TO", q).group(1))
        return update._parse_feed(atom([(lo, "In v. Range", "2026-06-16", ""),
                                        (lo - 500, "Out v. Range", "2026-06-16", "")], enclosure=False), "ga")
    with contextlib.redirect_stdout(io.StringIO()) as out2, patched((update.time, "sleep", lambda *a, **k: None)):
        found2, cov2, complete2, q2 = update.ga_enumerate(5000, 5010, search=mixed)
    check("partial page mixing in-range and out-of-range clusters -> nothing taken, covered not advanced",
          found2 == {} and cov2 == 5000 and not complete2 and q2 == 1 and "4501" in out2.getvalue(),
          (found2, cov2, complete2, out2.getvalue()))


def test_infra_error_classification():
    import http.client
    import urllib.error
    infra = [TimeoutError("courtlistener deadline exceeded"), update.cl_rate.RateBudgetExceeded("budget"),
             urllib.error.URLError("reset"), urllib.error.HTTPError("u", 503, "busy", {}, None),
             update.TransientAPIError("summarize m -> HTTP 529: overloaded"), ConnectionResetError("reset"),
             http.client.IncompleteRead(b"x"), RuntimeError("triage m -> HTTP 500: internal"),
             RuntimeError("pretriage m -> network error: timed out"), OSError(113, "No route to host")]
    genuine = [RuntimeError("summarize m returned unparseable JSON: x"), RuntimeError("triage m hit max_tokens (900)"),
               ValueError("verdict must be one of"), KeyError("areas"), TypeError("NoneType"),
               urllib.error.HTTPError("u", 404, "not found", {}, None), urllib.error.HTTPError("u", 410, "gone", {}, None)]
    check("infrastructure failures are recognized", all(update._ga_infra_error(e) for e in infra),
          [repr(e) for e in infra if not update._ga_infra_error(e)])
    check("genuine evaluation failures are not", not any(update._ga_infra_error(e) for e in genuine),
          [repr(e) for e in genuine if update._ga_infra_error(e)])

    # anthropic_json: an overload that outlasts the retries, and a dead network, raise TransientAPIError;
    # an unusable answer raises a plain RuntimeError.
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def overloaded(req, timeout=None):
        raise urllib.error.HTTPError("u", 529, "overloaded", {}, io.BytesIO(b'{"type":"overloaded_error"}'))

    def offline(req, timeout=None):
        raise urllib.error.URLError("unreachable")

    def garbage(req, timeout=None):
        return Resp(json.dumps({"content": [{"type": "text", "text": "no json here"}],
                                "stop_reason": "end_turn"}).encode())
    body = {"model": "m", "max_tokens": 10, "messages": []}
    got = []
    for fn in (overloaded, offline, garbage):
        with contextlib.redirect_stdout(io.StringIO()), \
                patched((update.urllib.request, "urlopen", fn), (update.time, "sleep", lambda *a, **k: None)):
            try:
                update.anthropic_json(dict(body), label="t")
                got.append(None)
            except Exception as e:
                got.append(e)
    check("anthropic_json: exhausted 529 and network errors raise TransientAPIError; bad JSON does not",
          isinstance(got[0], update.TransientAPIError) and isinstance(got[1], update.TransientAPIError)
          and isinstance(got[2], RuntimeError) and not isinstance(got[2], update.TransientAPIError),
          [repr(e) for e in got])


def test_infra_failures_spend_no_try():
    """A GA cluster stopped by infrastructure -- the Anthropic API overloaded or rate-limited after its
    retries, a transport error, the CourtListener budget, the time budget -- holds the mark and keeps
    every try; only a genuinely unusable answer spends one."""
    import urllib.error
    stuck = 10990001
    uni = {stuck: ("Stuck v. Nobody", "2026-06-16", "S26A0777")}
    texts = {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    rest = {stuck: opinion("S26A0777", "September 15, 2026")}

    def raising(exc):
        def pre(name, docket, text):
            if name.startswith("Stuck"):
                raise exc
            return {"pass": True}
        return (update, "pretriage", pre)

    def rest_raises(exc):
        def f(r, deadline=None):
            if update.cluster_id_of(r) == stuck:
                raise exc
            return ""
        return (update, "opinion_text_full", f)
    with scenario("ga-infra-", uni, texts, rest, {"last_filed": "2026-09-29", "seen_clusters": [],
                                                  "ga_high_water": 10990000}, feed=[]) as tmp:
        infra = [raising(update.TransientAPIError("pretriage m -> HTTP 529: overloaded")),
                 raising(update.TransientAPIError("pretriage m -> HTTP 429: rate limited")),
                 raising(RuntimeError("pretriage m -> HTTP 503: unavailable")),
                 raising(urllib.error.URLError("connection reset")),
                 raising(update.cl_rate.RateBudgetExceeded("courtlistener throttled")),
                 rest_raises(TimeoutError("courtlistener deadline exceeded")),
                 rest_raises(urllib.error.HTTPError("u", 502, "bad gateway", {}, None))]
        outs = [run_main(tmp, _calls(), extra=[p]) for p in infra]
        st = _state(tmp)
        check("seven infrastructure failures in a row: no try spent, mark held, nothing abandoned",
              not st.get("ga_backlog_tries") and st.get("ga_high_water") == 10990000 and not st.get("ga_abandoned"),
              st)
        check("each says it charged no try",
              all(". ga backlog: cluster %d not charged a try" % stuck in o for o in outs),
              [o for o in outs if "not charged" not in o][:1])
        out = run_main(tmp, _calls(), extra=[raising(RuntimeError("pretriage m returned unparseable JSON: x"))])
        st = _state(tmp)
        check("an unusable model answer is a genuine failure: one try spent",
              st.get("ga_backlog_tries") == {str(stuck): 1} and "not charged" not in out, (st, out))


def test_abandoned_retry_rotation():
    """last_try is stamped whenever an abandoned retry is attempted, not only when it fails, so two
    entries that never resolve cannot hold the GA_ABANDONED_RETRY_PER_RUN slots forever."""
    a, b, c = 10970001, 10970002, 10970003
    ab = [{"cluster_id": a, "name": "A", "reason": "x", "tries": 3, "abandoned": "2026-08-01", "last_try": "2026-08-01"},
          {"cluster_id": b, "name": "B", "reason": "x", "tries": 3, "abandoned": "2026-08-01", "last_try": "2026-08-02"},
          {"cluster_id": c, "name": "C", "reason": "x", "tries": 3, "abandoned": "2026-08-01", "last_try": "2026-09-01"}]
    out, _ = update._ga_abandon_update(ab, gave_up=[], retried={a, b}, resolved=set(), reasons={}, names={},
                                       today="2026-10-01", tries=3)
    check("helper: attempted retries are stamped even with no failure",
          [x["last_try"] for x in out] == ["2026-10-01", "2026-10-01", "2026-09-01"], out)
    check("helper: so the third entry is due next", update._ga_abandoned_due(out, set(), "2026-10-01", 7, 2) == [c])
    # main(): A is a GA cluster that is never reached (REST budget spent, no PDF), B is absent from the
    # feed. Neither ever resolves or fails; C must still get its retry on the following run.
    uni = {a: ("Alpha v. Stuck", "2026-06-16", "S26A0901")}
    texts = {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-rotate-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [],
                                                 "ga_high_water": 10990000, "ga_abandoned": ab}, feed=[]) as tmp:
        fs1 = FakeSearch(uni)
        run_main(tmp, _calls(), extra=[(update, "ga_search_feed", fs1), (update.cl_rate, "remaining", lambda: 0)])
        st = _state(tmp)
        by = {x["cluster_id"]: x for x in st.get("ga_abandoned") or []}
        check("run 1: A and B retried (looked up), stamped though neither resolved nor failed; C waits",
              "cluster_id:[%d TO %d]" % (a, a) in fs1.queries and "cluster_id:[%d TO %d]" % (b, b) in fs1.queries
              and not any(str(c) in q for q in fs1.queries)
              and by[a]["last_try"] == "2026-10-01" and by[b]["last_try"] == "2026-10-01"
              and by[c]["last_try"] == "2026-09-01" and not st.get("ga_backlog_tries"), (fs1.queries, by))
        fs2 = FakeSearch(uni)
        run_main(tmp, _calls(), extra=[(update, "ga_search_feed", fs2), (update.cl_rate, "remaining", lambda: 0)])
        st = _state(tmp)
        by = {x["cluster_id"]: x for x in st.get("ga_abandoned") or []}
        check("run 2: the third entry gets its retry; the two stuck ones do not hog the slots",
              "cluster_id:[%d TO %d]" % (c, c) in fs2.queries
              and not any(str(a) in q or str(b) in q for q in fs2.queries)
              and by[c]["last_try"] == "2026-10-01", (fs2.queries, by))


def test_stall_warning():
    """When the high-water mark holds for GA_MARK_STALL_RUNS consecutive runs while backlog remains,
    the run says so loudly and names the blocking cluster; a moved mark clears the count."""
    global REST_TEXTS
    rec, loud = None, False
    for _ in range(5):
        rec, loud = update._ga_stall_update(rec, 100, 100, 101, 6)
    check("helper: five held runs are counted, not yet loud",
          rec == {"mark": 100, "runs": 5, "blocker": 101} and not loud, rec)
    rec, loud = update._ga_stall_update(rec, 100, 100, 101, 6)
    check("helper: the sixth is loud", loud and rec["runs"] == 6)
    check("helper: a moved mark or no backlog clears it",
          update._ga_stall_update(rec, 100, 101, 102, 6) == (None, False)
          and update._ga_stall_update(rec, 100, 100, None, 6) == (None, False))
    check("siteconfig default is 6", update.siteconfig.GA_MARK_STALL_RUNS == 6)
    stuck = 10990001
    uni = {stuck: ("Stalled v. Nobody", "2026-06-16", "S26A0779")}
    texts = {"https://storage.courtlistener.com/pdf/coa.pdf": "Court of Appeals opinion." + PAD}
    with scenario("ga-stall-", uni, texts, {}, {"last_filed": "2026-09-29", "seen_clusters": [],
                                                "ga_high_water": 10990000}, feed=[]) as tmp:
        outs = [run_main(tmp, _calls(), extra=[(update.cl_rate, "remaining", lambda: 0)]) for _ in range(6)]
        check("five held runs: no stall line yet", all("STALLED" not in o for o in outs[:5]),
              [o for o in outs[:5] if "STALLED" in o][:1])
        line = [ln for ln in outs[5].splitlines() if "STALLED" in ln]
        check("sixth held run: a loud '!' line naming the blocking cluster",
              len(line) == 1 and line[0].startswith("  ! ga high-water mark STALLED at 10990000 for 6")
              and "blocking cluster %d (Stalled v. Nobody)" % stuck in line[0], outs[5])
        check("the count is kept in the state", _state(tmp).get("ga_mark_stall", {}).get("runs") == 6, _state(tmp))
        REST_TEXTS = {stuck: opinion("S26A0779", "September 15, 2026")}
        out = run_main(tmp, _calls())
        st = _state(tmp)
        check("once the blocker resolves the mark moves and the stall record clears",
              st.get("ga_high_water") == stuck and "ga_mark_stall" not in st and "STALLED" not in out, st)


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
    print("lookups by id, abandoned backlog:")
    test_lookup_ids()
    test_abandoned_helpers()
    print("release index warning:")
    test_release_index_warning()
    print("dating and dedupe:")
    test_resolve()
    print("main() end to end:")
    test_main_end_to_end()
    test_unverified_card_is_held()
    test_starved_backlog_is_never_charged_then_abandoned_loudly()
    test_redraft_lookup()
    test_court_feed_backs_up_enumeration()
    test_main_release_index_unavailable()
    print("verifier fixes (infra tries, retry rotation, redraft confirmation, stray pages, stall):")
    test_enumerate_partial_stray_page()
    test_infra_error_classification()
    test_infra_failures_spend_no_try()
    test_abandoned_retry_rotation()
    test_stall_warning()
    if FAILS:
        print("\n%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL GA INTAKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
