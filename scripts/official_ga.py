#!/usr/bin/env python3
"""Resolve the Supreme Court of Georgia's own opinion-PDF URL for a card, so a
scotga card's rendered title can link to the official source (gasupreme.us) with
CourtListener kept as the full record below, and the official RELEASE DATE of a
Supreme Court of Georgia opinion for the funnel.

Identity stays on CourtListener's cluster_id (permalinks, treatment, golden, and
dedup all key on it); this only adds an official_url enrichment and a date, so the
court's own site supplements rather than replaces CourtListener. The Court of
Appeals is deliberately not covered: gaappeals.gov's docket endpoint
(/wp-content/themes/benjamin/docket/results_all.php) sits behind an AWS WAF
JavaScript challenge that a server-side fetch cannot pass, so only the Supreme
Court is reachable from the pipeline.

The court publishes a year-index page at
  https://www.gasupreme.us/<year>-opinions/
listing each release date as

  <p><strong>June 2, 2026</strong></p>
  <ul><li><a href="<pdf>">S26A0017. ALMOND v. THE STATE</a></li> ...

or, from the 2026-06-30 release on, with the date inside a heading:

  <h3><strong>September 22, 2026</strong></h3>
  <ul><li><a href="<pdf>">S25G1446, S25G1447. CRAVENS v. SLAUGHTER et al. (two cases)</a></li> ...

where the PDF filename is usually the lowercased lead docket (s26a0017.pdf). We
fetch the page for the card's decision year and match the card's docket to a PDF
basename. A consolidated case (cross-appeals decided in one opinion) lists several
dockets pointing at one PDF; the card's lead docket resolves it.

The same page is the funnel's source of truth for a release DATE (release_index).
CourtListener has stamped every release since 2026-06-30 date_filed 2026-06-16,
because juriscraper still reads the date from a <p> while the court moved it into
the <h3>. The opinion PDF's own "Decided:" line (decided_date) is the fallback when
the page does not list a docket.

Everything fails open: a network error, a missing year page (the pre-2017 years
404), or an unmatched docket returns None / {} / "", and the caller carries on
exactly as before (CourtListener-only)."""
import os
import re
import sys
import json
import datetime
import html as _html
import urllib.request
import urllib.error

try:
    import siteconfig as _cfg
except Exception:   # imported from another directory: fall back to the built-in host
    _cfg = None

UA = "horowitz.law Georgia Appellate Watch (contact: via horowitz.law)"
HOST = (getattr(_cfg, "GASUPREME_HOST", "") or "https://www.gasupreme.us").rstrip("/")
YEAR_URL = HOST + "/%s-opinions/"
TIMEOUT = 30

# A Supreme Court of Georgia docket: S + 2-digit year + a type letter
# (A appeals, G certiorari grant, Y bar/discipline, C/D/E ...) + a 3-4 digit seq.
_DOCKET_RE = re.compile(r"^[Ss]\d{2}[A-Za-z]\d{3,4}$")
# A PDF enclosure under the WordPress media tree, href either relative or absolute.
_PDF_HREF_RE = re.compile(
    r'href="([^"]*?/wp-content/uploads/\d{4}/\d{2}/([^"/]+?\.pdf))"', re.I)

_year_cache = {}   # year(str) -> {docket_lower: absolute_url}; cached per process
_page_cache = {}   # year(str) -> the page html, or "" for a definitive miss (an HTTP error)
_index_cache = {}  # year(str) -> release_index result


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _page(year):
    """The /<year>-opinions/ html, fetched once per process and shared by the URL map and the
    release index. An HTTPError (the pre-2017 years 404) is definitive and cached as ""; a
    transport error (timeout, DNS, reset) raises and is NOT cached, so a later call retries."""
    if year in _page_cache:
        return _page_cache[year]
    try:
        doc = _fetch(YEAR_URL % year)
    except urllib.error.HTTPError:
        doc = ""
    _page_cache[year] = doc
    return doc


def _year_map(year, *, html=None):
    """docket (lowercased, no extension) -> absolute PDF url for one
    /<year>-opinions/ page. Cached per process. Returns {} on any failure."""
    if year in _year_cache:
        return _year_cache[year]
    m = {}
    try:
        doc = html if html is not None else _page(year)
        for href, base in _PDF_HREF_RE.findall(doc):
            url = href if href.lower().startswith("http") else HOST + href
            m[base.lower()[:-4]] = url  # key by docket = basename without .pdf
    except Exception:
        # A transport error (timeout, DNS, reset) is transient: do NOT cache it, so a
        # later same-year card retries the fetch instead of inheriting a poisoned {}.
        return {}
    _year_cache[year] = m
    return m


# ---- Release dates ----------------------------------------------------------------------
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
# A release-date line: the date in <strong> as the FIRST thing inside a <p> (through 2026-06-16)
# or a heading (<h3>, from 2026-06-30). Strict on purpose: the page also carries <p><strong>
# lines such as "10:00 AM" and "Wednesday, November 04, 2026" (oral-argument notices), which
# must not open a release. A trailing "—<a>SUMMARIES...</a>" after the date is allowed.
_RELEASE_RE = re.compile(
    r"<(?:p|h[1-6])(?:\s[^>]*)?>\s*<strong>\s*((?:%s)\s+\d{1,2},\s*\d{4})\b" % _MONTHS, re.I)
_LI_RE = re.compile(r"<li\b[^>]*>\s*<a\s[^>]*?href=\"([^\"]+?\.pdf)\"[^>]*>(.*?)</a>(.*?)</li>",
                    re.I | re.S)
_LI_DOCKETS_RE = re.compile(r"^((?:S\d{2}[A-Z]\d{4}\s*(?:,|&|and)?\s*)+)\.?\s*(.*)$", re.S)
_ANY_DOCKET_RE = re.compile(r"\bS\d{2}[A-Z]\d{4}\b")


def _iso(month_day_year):
    """'August 11, 2026' -> '2026-08-11', or "" if it is not a real date."""
    m = re.match(r"\s*([A-Za-z]+)\.?\s+(\d{1,2})\s*,\s*(\d{4})\s*$", month_day_year or "")
    if not m:
        return ""
    try:
        return datetime.datetime.strptime("%s %s %s" % (m.group(1).title(), m.group(2), m.group(3)),
                                          "%B %d %Y").date().isoformat()
    except ValueError:
        return ""


def _plain(fragment):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def parse_releases(doc):
    """Every opinion on a /<year>-opinions/ page as a list of
    {date, dockets, title, url, note}, in page order (newest release first). Each <li> takes the
    date of the nearest release line above it -- never the PDF's upload folder, which can be a
    later month (a substitute opinion re-uploaded in July under a June release). A list item
    without a leading docket (a summaries link) is skipped."""
    doc = doc or ""
    out = []
    marks = [(m.start(), _iso(m.group(1))) for m in _RELEASE_RE.finditer(doc)]
    marks = [(pos, d) for pos, d in marks if d]
    for i, (pos, d) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(doc)
        for href, text, note in _LI_RE.findall(doc[pos:end]):
            m = _LI_DOCKETS_RE.match(_plain(text))
            if not m:
                continue
            dockets = [x.upper() for x in _ANY_DOCKET_RE.findall(m.group(1))]
            if not dockets:
                continue
            url = href if href.lower().startswith("http") else HOST + href
            out.append({"date": d, "dockets": dockets, "title": m.group(2).strip(), "url": url,
                        "note": _plain(note)})
    return out


def release_index(year, *, html=None):
    """{DOCKET (upper-case): release entry} for one year page; every docket of a consolidated
    entry points at the same entry dict. A docket listed twice keeps its newest release (the page
    runs newest first). Cached per process. Fails open to {} (the caller falls back to the PDF's
    "Decided:" line); a transport error is not cached, so a later call retries."""
    year = str(year)
    if html is None and year in _index_cache:
        return _index_cache[year]
    try:
        doc = html if html is not None else _page(year)
        idx = {}
        for ent in parse_releases(doc):
            for d in ent["dockets"]:
                idx.setdefault(d, ent)
    except Exception:
        return {}
    if html is None:
        _index_cache[year] = idx
    return idx


# The caption block of a Supreme Court of Georgia slip opinion:
#   No. S26G0149                      (or "Nos. S25G1446, S25G1447")
#   Argued: June 17, 2026 — Decided: August 11, 2026
# Read only from the top of the text, so a docket or a date quoted in the body cannot win.
_TEXT_DOCKETS_RE = re.compile(r"\bNos?\.\s*((?:S\d{2}[A-Z]\d{4}\s*(?:,|&|and)?\s*)+)")
_DECIDED_RE = re.compile(r"Decided:?\s*((?:%s)\.?\s+\d{1,2}\s*,\s*\d{4})" % _MONTHS, re.I)
_HEAD_CHARS = 3000


def dockets_in_text(text):
    """Supreme Court dockets named in the opinion's caption ("No. S26G0149"), upper-case, in
    order; [] if none. A lower-court docket ("No. A25A0936") is not a Supreme Court docket."""
    out = []
    for grp in _TEXT_DOCKETS_RE.findall((text or "")[:_HEAD_CHARS]):
        for d in _ANY_DOCKET_RE.findall(grp):
            if d not in out:
                out.append(d)
    return out


def decided_date(text):
    """The ISO date of the caption's "Decided:" line, or "" if there is none."""
    m = _DECIDED_RE.search((text or "")[:_HEAD_CHARS])
    return _iso(m.group(1)) if m else ""


def official_url_for(card, *, html=None):
    """Return the official gasupreme.us PDF url for a scotga card, or None.

    Matches each of the card's dockets to a PDF basename on the page for the
    card's decision year. `html` (the year page already fetched) is accepted for
    offline testing. Fails open: any error or miss returns None."""
    try:
        if (card.get("court") or "") != "scotga":
            return None
        year = (card.get("date") or "")[:4]
        if not re.match(r"^\d{4}$", year):
            return None
        m = _year_map(year, html=html)
        if not m:
            return None
        for d in (card.get("dockets") or []):
            key = str(d).strip().lower()
            if key and _DOCKET_RE.match(key) and key in m:
                return m[key]
        return None
    except Exception:
        return None


def _backfill(apply=False):
    """Fill official_url on every scotga card that lacks one and resolves.
    Dry-run by default; pass --apply to write opinions.json (same serializer the
    funnel uses, so the only diff is the added fields)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, "opinions.json")
    cards = json.load(open(path, encoding="utf-8"))
    changed = 0
    print("%-9s %-46s %s" % ("docket", "name", "official_url"))
    print("-" * 96)
    for c in cards:
        if c.get("court") != "scotga" or c.get("official_url"):
            continue
        u = official_url_for(c)
        dk = ", ".join(d for d in (c.get("dockets") or []) if d) or "-"
        print("%-9s %-46s %s" % (dk[:9], (c.get("name") or "")[:46], u or "(no official PDF found)"))
        if u:
            c["official_url"] = u
            changed += 1
    if apply and changed:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import safeio
        safeio.atomic_write_text(path, json.dumps(cards, ensure_ascii=False, indent=2) + "\n")
        print("\nwrote %d official_url(s) to opinions.json" % changed)
    else:
        print("\n%d resolvable; dry-run (pass --apply to write)" % changed)


if __name__ == "__main__":
    _backfill(apply="--apply" in sys.argv)
