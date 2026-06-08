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
import os, re, json, html, datetime
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
    div_part = f", {_esc(e['division'])}" if e.get("division") else ""
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in e["areas"])
    meta = (f'<span class="court">{_esc(COURT_LABELS[e["court"]])}</span>{div_part} \u00b7 decided '
            f'{_esc(_date_label(e["date"]))} \u00b7 {_esc(_no_label(e["dockets"]))} \u00b7 {_esc(e["disposition"])}'
            + prec_meta)
    return (
        f'      <article id="op-{e["cluster_id"]}" class="opinion" data-court="{e["court"]}" data-system="{system}" data-jurisdiction="{juris}" data-areas="{",".join(e["areas"])}" data-date="{e["date"]}"{attr}{prec_attr}>\n'
        f'{banner}'
        f'        <div class="op-head"><span class="op-name">{_esc(e["name"])}</span></div>\n'
        f'        <div class="op-meta">{meta}</div>\n'
        f'        <div class="op-tags">{tags}</div>\n'
        f'        <p class="op-synopsis">{_esc(e["synopsis"])}</p>\n'
        f'        <p class="op-why"><strong>Why it matters:</strong> {_esc(e["why"])}</p>\n'
        f'        <div class="op-foot">\n'
        f'          <span class="op-source"><a href="{_attr(e["url"])}" target="_blank" rel="noopener noreferrer">Read the opinion on CourtListener \u2192</a></span>\n'
        f'          <span class="op-disclaimer">AI-drafted summary \u00b7 verify against the opinion</span>\n'
        f'        </div>\n'
        f'      </article>'
    )

def rss_item(e):
    cats = [COURT_LABELS[e["court"]]] + [AREA_LABELS[c] for c in e["areas"]]
    prec = (e.get("precedential") or "").strip().lower()
    prec_txt = {"unpublished": " Unpublished; not binding precedent.",
                "physical precedent": " Physical precedent only; not binding."}.get(prec, "")
    desc = f'{e["synopsis"]} Why it matters: {e["why"]}{prec_txt} AI-drafted summary. Verify against the opinion. {e["url"]}'
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
    pat = re.compile(r'(<!-- ' + marker + r':start.*?-->).*?<!-- ' + marker + r':end -->', re.S)
    doc = open(path, encoding="utf-8").read()
    repl = lambda m: m.group(1) + "\n" + block + "\n      <!-- " + marker + ":end -->"
    doc, n = pat.subn(repl, doc, count=1)
    if n != 1:
        # The marker pair is the contract between the page and the renderer. If it
        # is missing or malformed (a stray manual edit), fail loud rather than
        # silently writing the page back with stale cards.
        raise RuntimeError("render: %s:start/%s:end marker pair not found in %s" % (marker, marker, path))
    # Keep the footer copyright year current so it does not rot to a stale year.
    # A no-op when the year already matches, so it adds no spurious diff.
    year = str(datetime.date.today().year)
    doc = re.sub(r'(&copy;|\u00a9)\s*\d{4}', lambda m: m.group(1) + " " + year, doc)
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

    # Full archive: everything, grouped by year. Skipped gracefully if the file
    # is not present, so a missing archive.html never breaks the feed render.
    if os.path.exists(ARCHIVE_PATH):
        _inject(ARCHIVE_PATH, "archive", archive_html(entries))

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
    return len(recent), len(entries)

if __name__ == "__main__":
    r, t = render()
    print(f"rendered {r} recent of {t} total -> opinions.html, archive.html, opinions.xml")
