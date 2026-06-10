#!/usr/bin/env python3
"""Render opinions.json into opinions.html, archive.html, and opinions.xml.

opinions.json is the single source of truth: a list of entry objects. Run from
anywhere with `python scripts/render.py`. Pure standard library, no dependencies.

The public feed (opinions.html and opinions.xml) shows a rolling window of the
most recent WINDOW_YEARS years by decision date, recomputed every run, so it
never grows without bound and nothing drops on a calendar boundary. The full
record lives in archive.html, grouped by decision year; nothing is removed
there. Cards are written between the start/end markers in each file; nothing
else in those files is touched.
"""
import os, re, json, hashlib, html, datetime
from xml.sax.saxutils import escape as xml_escape
import safeio          # crash-safe atomic writes
import jurisdictions   # per-jurisdiction court labels and citation suffixes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH    = os.path.join(REPO, "opinions.json")
HTML_PATH    = os.path.join(REPO, "opinions.html")
ARCHIVE_PATH = os.path.join(REPO, "archive.html")
XML_PATH     = os.path.join(REPO, "opinions.xml")

# Rolling public-feed window, by decision date. If you change this, update the
# "two years" wording in the RSS <description> below and in the opinions.html
# scope line so the prose stays in sync with the number.
WINDOW_YEARS = 2
ARCHIVE_URL  = "https://horowitz.law/archive"

SITEMAP_PATH = os.path.join(REPO, "sitemap.xml")
# Pages outside the marker-injection set whose footer year would otherwise rot on
# Jan 1 (the injected pages are stamped in _inject). render() re-stamps these in
# place, writing only when the year actually changed, so it is a no-op all year;
# render-sync's add-paths carries the rollover PR.
STATIC_PAGES = [os.path.join(REPO, p) for p in
                ("index.html", "resume.html", "colophon.html", "subscribe.html", "404.html")]

_YEAR_RE = re.compile(r'(&copy;|\u00a9)\s*\d{4}')

# Asset ?v= tokens are content hashes: the first 10 hex chars of sha256, the
# same scheme scripts/check_site.py enforces in CI. Restamping on every page
# write keeps a rendered page from carrying tokens older than the assets it
# loads -- the shell outside the markers is never otherwise touched, so an
# asset edit made alongside a hand upload would leave the generated pages a
# year stale under immutable caching until someone remembered --fix. The two
# implementations are deliberately independent (this one stamps, check_site
# verifies), so a bug here cannot blind the check there.
_ASSETS = ("base.css", "app.js", "opinions.js", "subscribe.js")

def _asset_token(name):
    with open(os.path.join(REPO, name), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:10]

def _stamp_tokens(doc):
    for a in _ASSETS:
        path = os.path.join(REPO, a)
        if not os.path.exists(path):
            continue        # never let a missing optional asset break a render
        tok = _asset_token(a)
        doc = re.sub(r'((?:href|src)="/%s)(\?v=[^"]*)?(")' % re.escape(a),
                     lambda m, t=tok: m.group(1) + "?v=" + t + m.group(3), doc)
    return doc

def _stamp_year(doc):
    """Rewrite any footer copyright year to the current year. A no-op when the
    year already matches, so it adds no spurious diff."""
    year = str(datetime.date.today().year)
    return _YEAR_RE.sub(lambda m: m.group(1) + " " + year, doc)

AREA_LABELS = {
    "coverage": "coverage", "badfaith": "bad faith", "auto": "auto",
    "premises": "premises", "negsec": "negligent security", "expert": "expert",
    "procedure": "procedure", "damages": "damages",
}
COURT_LABELS = jurisdictions.COURT_LABELS   # internal key -> human label
COURT_SYSTEM = jurisdictions.COURT_SYSTEM   # internal key -> "state" | "federal"
TITLE_SUFFIX = jurisdictions.TITLE_SUFFIX   # internal key -> short citation suffix
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def _date_label(iso):
    d = datetime.date.fromisoformat(iso)
    return f"{_MO[d.month - 1]} {d.day}, {d.year}"

def _no_label(dockets):
    return ("Nos. " + " & ".join(dockets)) if len(dockets) > 1 else ("No. " + dockets[0])

def _eastern_offset(d):
    """US Eastern UTC offset for a date: EDT (-0400) from the 2nd Sunday of March to
    the 1st Sunday of November, EST (-0500) otherwise. (Date-level; the 2 a.m.
    transition does not matter for a noon pubDate.)"""
    mar1 = datetime.date(d.year, 3, 1)
    dst_start = mar1 + datetime.timedelta(days=(6 - mar1.weekday()) % 7 + 7)  # 2nd Sunday of March
    nov1 = datetime.date(d.year, 11, 1)
    dst_end = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7)        # 1st Sunday of November
    return "-0400" if dst_start <= d < dst_end else "-0500"

def _rfc822(iso):
    d = datetime.date.fromisoformat(iso)
    return f"{_WD[d.weekday()]}, {d.day:02d} {_MO[d.month - 1]} {d.year} 12:00:00 {_eastern_offset(d)}"

def _esc(t):  # HTML text content (leave quotes alone)
    return html.escape(t, quote=False)

def _attr(t):  # HTML attribute value (escape quotes too)
    return html.escape(t or "", quote=True)

def _valid_date(iso):
    try:
        datetime.date.fromisoformat((iso or "")[:10]); return True
    except (ValueError, TypeError):
        return False

def _sorted(entries):
    return sorted(entries, key=lambda e: (e["date"], int(e.get("cluster_id", 0))), reverse=True)

def _cutoff_iso(today=None):
    today = today or datetime.date.today()
    try:
        c = today.replace(year=today.year - WINDOW_YEARS)
    except ValueError:            # Feb 29 in a non-leap target year -> Feb 28
        c = today.replace(year=today.year - WINDOW_YEARS, day=28)
    return c.isoformat()

_TREAT_LABEL = {"caution": "Possible negative treatment", "negative": "Negative treatment",
                "superseded": "Superseded"}
_TREAT_TAIL  = {"caution": " Verify on Shepard\u2019s before relying.",
                "negative": " Verify on Shepard\u2019s.", "superseded": " Retained for the record."}

def _treatment_banner(e):
    """A caution banner for a card treated adversely. Empty for untreated ('ok')
    cards, so their markup is unchanged. Renders wherever the card renders, the
    recent feed and the archive both."""
    t = e.get("treatment") or "ok"
    if t == "ok":
        return ""
    note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
    by = [b.get("name") for b in (e.get("treated_by") or []) if b.get("name")]
    cited = (" Cited by: " + "; ".join(_esc(n) for n in by[:3]) + ".") if by else ""
    body = (_esc(note) + cited).strip()
    return (f'        <div class="op-treatment op-treatment-{t}" role="note">'
            f'<span class="op-treat-label">{_TREAT_LABEL.get(t, "Flagged")}</span> '
            f'{body}{_TREAT_TAIL.get(t, "")}</div>\n')

def all_areas(e):
    """Union of a card's practice areas across its primary and any additional
    holdings, order-preserving (primary areas first). Drives the card's tag row,
    its data-areas attribute, and its RSS categories, so a card with two holdings
    in different areas is found under either area's filter."""
    out = list(e.get("areas") or [])
    for h in (e.get("additional_holdings") or []):
        for a in (h.get("areas") or []):
            if a not in out:
                out.append(a)
    return out


def _area_chips():
    """The practice-area filter chips for opinions.html, generated from AREA_LABELS
    so the taxonomy lives in one place. Injected between the areachips markers."""
    out = ['      <button class="chip" type="button" data-area-filter="all" aria-pressed="true">all</button>']
    for code, label in AREA_LABELS.items():
        out.append('      <button class="chip" type="button" data-area-filter="%s" aria-pressed="false">%s</button>'
                   % (code, _esc(label)))
    return "\n".join(out)


def _jurisdiction_options():
    """The jurisdiction <select> options for opinions.html, generated from the
    registry so adding a state is one registry entry. The active jurisdiction is
    selected. Injected between the jurisdictions markers (nested in the select)."""
    out = []
    for key, label in jurisdictions.ALL_JURISDICTIONS:
        sel = " selected" if key == jurisdictions.JURISDICTION else ""
        out.append('          <option value="%s"%s>%s</option>' % (key, sel, _esc(label.lower())))
    return "\n".join(out)


def card_html(e):
    treat = e.get("treatment") or "ok"
    attr = f' data-treatment="{treat}"' if treat != "ok" else ""
    prec = (e.get("precedential") or "").strip().lower()
    prec_note = {"unpublished": "unpublished, not binding precedent",
                 "physical precedent": "physical precedent only, not binding"}.get(prec, "")
    prec_meta = f' \u00b7 <span class="op-noprec">{_esc(prec_note)}</span>' if prec_note else ""
    prec_attr = f' data-precedential="{prec}"' if prec_note else ""
    banner = _treatment_banner(e)
    # Federal-vs-state is the primary axis the page filters on; jurisdiction (the
    # state) is secondary. system is a stable property of the court. jurisdiction
    # falls back to the active jurisdiction for cards that predate per-card
    # stamping; once a second state is added, the pipeline stamps it on the card
    # (a federal opinion can be relevant to more than one state, so it cannot be
    # derived from the court alone).
    system = COURT_SYSTEM.get(e["court"], "")
    juris = e.get("jurisdiction") or jurisdictions.JURISDICTION
    areas_all = all_areas(e)
    div_part = f", {_esc(e['division'])}" if e.get("division") else ""
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in areas_all)
    meta = (f'<span class="court">{_esc(COURT_LABELS[e["court"]])}</span>{div_part} \u00b7 decided '
            f'{_esc(_date_label(e["date"]))} \u00b7 {_esc(_no_label(e["dockets"]))} \u00b7 {_esc(e["disposition"])}'
            + prec_meta)
    # One synopsis and reason for the common single-holding opinion (markup
    # unchanged). When a decision has additional distinct holdings, each renders as
    # its own equally weighted block labeled with its areas, so a salient secondary
    # holding is never crowded out and stays reachable under its own area filter.
    additional = e.get("additional_holdings") or []
    if additional:
        def _holding(h_areas, syn, why):
            ht = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in h_areas)
            return (f'          <div class="op-holding">\n'
                    f'            <div class="op-holding-areas">{ht}</div>\n'
                    f'            <p class="op-synopsis">{_esc(syn)}</p>\n'
                    f'            <p class="op-why"><strong>Why it matters:</strong> {_esc(why)}</p>\n'
                    f'          </div>\n')
        blocks = _holding(e["areas"], e["synopsis"], e["why"])
        for h in additional:
            blocks += _holding(h.get("areas") or [], h.get("synopsis") or "", h.get("why") or "")
        body = f'        <div class="op-holdings">\n{blocks}        </div>\n'
    else:
        body = (f'        <p class="op-synopsis">{_esc(e["synopsis"])}</p>\n'
                f'        <p class="op-why"><strong>Why it matters:</strong> {_esc(e["why"])}</p>\n')
    return (
        f'      <article id="op-{e["cluster_id"]}" class="opinion" data-court="{e["court"]}" data-system="{system}" data-jurisdiction="{juris}" data-areas="{",".join(areas_all)}" data-date="{e["date"]}"{attr}{prec_attr}>\n'
        f'{banner}'
        f'        <div class="op-head"><span class="op-name">{_esc(e["name"])}</span></div>\n'
        f'        <div class="op-meta">{meta}</div>\n'
        f'        <div class="op-tags">{tags}</div>\n'
        f'{body}'
        f'        <div class="op-foot">\n'
        f'          <span class="op-source"><a href="{_attr(e["url"])}" target="_blank" rel="noopener noreferrer">Read the opinion on CourtListener \u2192</a></span>\n'
        f'          <span class="op-disclaimer">AI-drafted summary \u00b7 verify against the opinion</span>\n'
        f'        </div>\n'
        f'      </article>'
    )

def rss_item(e):
    cats = [COURT_LABELS[e["court"]]] + [AREA_LABELS[c] for c in all_areas(e)]
    prec = (e.get("precedential") or "").strip().lower()
    prec_txt = {"unpublished": " Unpublished; not binding precedent.",
                "physical precedent": " Physical precedent only; not binding."}.get(prec, "")
    extra = "".join(f' Also: {h.get("synopsis", "")} Why it matters: {h.get("why", "")}'
                    for h in (e.get("additional_holdings") or []))
    desc = f'{e["synopsis"]} Why it matters: {e["why"]}{extra}{prec_txt} AI-drafted summary. Verify against the opinion. {e["url"]}'
    # CDATA is verbatim: a literal "]]>" anywhere in the card text would terminate the
    # section early and break the feed. Standard split-escape keeps the XML well-formed.
    desc = desc.replace("]]>", "]]]]><![CDATA[>")
    lines = ["    <item>",
             f"      <title>{xml_escape(e['name'] + TITLE_SUFFIX[e['court']])}</title>",
             f"      <link>{xml_escape(e['url'])}</link>",
             f'      <guid isPermaLink="true">{xml_escape(e["url"])}</guid>',
             f"      <pubDate>{_rfc822(e['date'])}</pubDate>"]
    lines += [f"      <category>{xml_escape(c)}</category>" for c in cats]
    lines += [f"      <description><![CDATA[{desc}]]></description>", "    </item>"]
    return "\n".join(lines)

def archive_html(entries):
    """All entries, grouped by decision year (desc), with a year jump-nav."""
    by_year = {}
    for e in entries:                         # entries arrive already _sorted (desc)
        by_year.setdefault(e["date"][:4], []).append(e)
    years = sorted(by_year, reverse=True)
    nav = ('      <nav class="year-nav" aria-label="Jump to year">\n'
           + "".join(f'        <a href="#y{y}">{y}</a>\n' for y in years)
           + '      </nav>')
    blocks = []
    for y in years:
        cards = "\n\n".join(card_html(e) for e in by_year[y])
        blocks.append(
            f'      <section class="archive-year-block" aria-labelledby="y{y}">\n'
            f'        <h2 class="archive-year" id="y{y}">{y}</h2>\n'
            f'{cards}\n'
            f'      </section>'
        )
    return nav + "\n\n" + "\n\n".join(blocks)

def _inject(path, marker, block):
    # Capture the start marker's leading indent and reuse it for the regenerated
    # end marker, so a marker pair nested at any depth (for example inside a
    # <select>) stays aligned. Behavior-neutral for the existing top-level markers.
    pat = re.compile(r'([ \t]*)(<!-- ' + marker + r':start.*?-->).*?<!-- ' + marker + r':end -->', re.S)
    doc = open(path, encoding="utf-8").read()
    repl = lambda m: m.group(1) + m.group(2) + "\n" + block + "\n" + m.group(1) + "<!-- " + marker + ":end -->"
    doc, n = pat.subn(repl, doc, count=1)
    if n != 1:
        # The marker pair is the contract between the page and the renderer. If it
        # is missing or malformed (a stray manual edit), fail loud rather than
        # silently writing the page back with stale cards.
        raise RuntimeError("render: %s:start/%s:end marker pair not found in %s" % (marker, marker, path))
    # Keep the footer copyright year current so it does not rot to a stale year,
    # and the asset ?v= tokens current so a regenerated page never pins visitors
    # to year-old immutable CSS/JS (see _stamp_tokens above).
    doc = _stamp_tokens(_stamp_year(doc))
    safeio.atomic_write_text(path, doc)

def render(entries=None):
    if entries is None:
        entries = json.load(open(JSON_PATH, encoding="utf-8"))
    for e in entries:
        if not _valid_date(e.get("date")):
            print("render: skipping a card with an unparseable date %r (%s)"
                  % (e.get("date"), (e.get("name") or "?")[:50]))
    entries = [e for e in entries if _valid_date(e.get("date"))]
    entries = _sorted(entries)

    cutoff = _cutoff_iso()
    recent = [e for e in entries if e["date"] >= cutoff]

    # Public feed: rolling WINDOW_YEARS window.
    _inject(HTML_PATH, "opinions", "\n\n".join(card_html(e) for e in recent))

    # Filter chrome generated from a single source: the practice-area chips from
    # AREA_LABELS, the jurisdiction options from the registry. Keeps the page's
    # filters from drifting out of sync with the taxonomy as either one grows.
    _inject(HTML_PATH, "areachips", _area_chips())
    _inject(HTML_PATH, "jurisdictions", _jurisdiction_options())

    # Full archive: everything, grouped by year. Skipped gracefully if the file
    # is not present, so a missing archive.html never breaks the feed render.
    if os.path.exists(ARCHIVE_PATH):
        _inject(ARCHIVE_PATH, "archive", archive_html(entries))
        _inject(ARCHIVE_PATH, "areachips", _area_chips())
        _inject(ARCHIVE_PATH, "jurisdictions", _jurisdiction_options())

    # RSS: same rolling window as the page.
    build = _rfc822(recent[0]["date"]) if recent else _rfc822(datetime.date.today().isoformat())
    desc = ("AI-assisted synopses of new Georgia appellate, Eleventh Circuit, and U.S. Supreme Court "
            "opinions, filtered for civil litigation and insurance practice. Each synopsis is AI-drafted; "
            "the linked opinion is the authority. A curated core, not a complete docket. This feed covers "
            f"the most recent two years; older opinions are archived by year at {ARCHIVE_URL}.")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Georgia Appellate Watch</title>',
           '    <link>https://horowitz.law/opinions</link>',
           '    <atom:link href="https://horowitz.law/opinions.xml" rel="self" type="application/rss+xml" />',
           f'    <description>{desc}</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{build}</lastBuildDate>',
           '    <generator>horowitz.law Georgia Appellate Watch (prototype)</generator>']
    out += [rss_item(e) for e in recent]
    out += ['  </channel>', '</rss>', '']
    safeio.atomic_write_text(XML_PATH, "\n".join(out))

    # Footer year on the non-generated pages: stamped in place (no markers involved),
    # written only when the year actually changed, so this is inert all year and the
    # Jan 1 rollover rides the next render-sync or content PR.
    for p in STATIC_PAGES:
        if os.path.exists(p):
            doc = open(p, encoding="utf-8").read()
            stamped = _stamp_tokens(_stamp_year(doc))
            if stamped != doc:
                safeio.atomic_write_text(p, stamped)

    _update_sitemap(recent, entries)
    return len(recent), len(entries)


def _update_sitemap(recent, entries):
    """Keep sitemap lastmod current for the two pages this renderer owns. The value
    is the newest decision date, deterministic from the data, so a re-render with
    unchanged cards changes nothing and the CI idempotency check stays green. The
    other URLs' lastmod stay hand-set. Skipped gracefully if the file is absent."""
    if not os.path.exists(SITEMAP_PATH):
        return
    doc = open(SITEMAP_PATH, encoding="utf-8").read()

    def set_lastmod(d, loc, date):
        pat = re.compile(r'(<loc>%s</loc>\s*<lastmod>)[^<]*(</lastmod>)' % re.escape(loc))
        return pat.sub(lambda m: m.group(1) + date + m.group(2), d, count=1)

    new = doc
    if recent:                              # recent arrives sorted desc; [0] is newest
        new = set_lastmod(new, "https://horowitz.law/opinions", recent[0]["date"])
    if entries:                             # entries likewise sorted desc
        new = set_lastmod(new, "https://horowitz.law/archive", entries[0]["date"])
    if new != doc:
        safeio.atomic_write_text(SITEMAP_PATH, new)

if __name__ == "__main__":
    r, t = render()
    print(f"rendered {r} recent of {t} total -> opinions.html, archive.html, opinions.xml")
