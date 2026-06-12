#!/usr/bin/env python3
"""Resolve the Supreme Court of Georgia's own opinion-PDF URL for a card, so a
scotga card's rendered title can link to the official source (gasupreme.us) with
CourtListener kept as the full record below.

Identity stays on CourtListener's cluster_id (permalinks, treatment, golden, and
dedup all key on it); this only adds an official_url enrichment, so the court's
own site supplements rather than replaces CourtListener. The Court of Appeals is
deliberately not covered: gaappeals.gov's docket endpoint
(/wp-content/themes/benjamin/docket/results_all.php) sits behind an AWS WAF
JavaScript challenge that a server-side fetch cannot pass, so only the Supreme
Court is reachable from the pipeline.

The court publishes a year-index page at
  https://www.gasupreme.us/<year>-opinions/
listing each release date as

  <p><strong>June 2, 2026</strong></p>
  <ul><li><a href="<pdf>">S26A0017. ALMOND v. THE STATE</a></li> ...

where the PDF filename is the lowercased docket (s26a0017.pdf). We fetch the page
for the card's decision year and match the card's docket to a PDF basename. A
consolidated case (cross-appeals decided in one opinion) lists several dockets
pointing at one PDF; the card's lead docket resolves it.

Everything fails open: a network error, a missing year page (the pre-2017 years
404), or an unmatched docket returns None, and the caller leaves official_url
absent so the card renders exactly as before (CourtListener-only)."""
import os
import re
import sys
import json
import urllib.request
import urllib.error

UA = "horowitz.law Georgia Appellate Watch (contact: via horowitz.law)"
HOST = "https://www.gasupreme.us"
YEAR_URL = HOST + "/%s-opinions/"
TIMEOUT = 30

# A Supreme Court of Georgia docket: S + 2-digit year + a type letter
# (A appeals, G certiorari grant, Y bar/discipline, C/D/E ...) + a 3-4 digit seq.
_DOCKET_RE = re.compile(r"^[Ss]\d{2}[A-Za-z]\d{3,4}$")
# A PDF enclosure under the WordPress media tree, href either relative or absolute.
_PDF_HREF_RE = re.compile(
    r'href="([^"]*?/wp-content/uploads/\d{4}/\d{2}/([^"/]+?\.pdf))"', re.I)

_year_cache = {}  # year(str) -> {docket_lower: absolute_url}; cached per process


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _year_map(year, *, html=None):
    """docket (lowercased, no extension) -> absolute PDF url for one
    /<year>-opinions/ page. Cached per process. Returns {} on any failure."""
    if year in _year_cache:
        return _year_cache[year]
    m = {}
    try:
        doc = html if html is not None else _fetch(YEAR_URL % year)
        for href, base in _PDF_HREF_RE.findall(doc):
            url = href if href.lower().startswith("http") else HOST + href
            m[base.lower()[:-4]] = url  # key by docket = basename without .pdf
    except Exception:
        m = {}
    _year_cache[year] = m
    return m


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
