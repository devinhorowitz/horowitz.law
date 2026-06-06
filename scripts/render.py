#!/usr/bin/env python3
"""Render opinions.json into the #opinions cards in opinions.html and into opinions.xml.

opinions.json is the single source of truth: a list of entry objects. Run from
anywhere with `python scripts/render.py`. Pure standard library, no dependencies.
The cards are written between the opinions:start / opinions:end markers in
opinions.html; nothing else in that file is touched.
"""
import os, re, json, html, datetime
from xml.sax.saxutils import escape as xml_escape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(REPO, "opinions.json")
HTML_PATH = os.path.join(REPO, "opinions.html")
XML_PATH  = os.path.join(REPO, "opinions.xml")

AREA_LABELS = {
    "coverage": "coverage", "badfaith": "bad faith", "auto": "auto",
    "premises": "premises", "negsec": "negligent security", "expert": "expert",
    "procedure": "procedure", "damages": "damages",
}
COURT_LABELS = {"ctapp": "Court of Appeals of Georgia", "scotga": "Supreme Court of Georgia",
                "ca11": "U.S. Court of Appeals for the Eleventh Circuit",
                "scotus": "Supreme Court of the United States"}
TITLE_SUFFIX = {"ctapp": " (Ga. Ct. App.)", "scotga": " (Ga.)",
                "ca11": " (11th Cir.)", "scotus": " (U.S.)"}
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def _date_label(iso):
    d = datetime.date.fromisoformat(iso)
    return f"{_MO[d.month - 1]} {d.day}, {d.year}"

def _no_label(dockets):
    return ("Nos. " + " & ".join(dockets)) if len(dockets) > 1 else ("No. " + dockets[0])

def _rfc822(iso):
    d = datetime.date.fromisoformat(iso)
    off = "-0500" if d.month in (1, 2, 12) else "-0400"
    return f"{_WD[d.weekday()]}, {d.day:02d} {_MO[d.month - 1]} {d.year} 12:00:00 {off}"

def _esc(t):  # HTML text content (leave quotes alone)
    return html.escape(t, quote=False)

def _sorted(entries):
    return sorted(entries, key=lambda e: (e["date"], int(e.get("cluster_id", 0))), reverse=True)

def card_html(e):
    div_part = f", {_esc(e['division'])}" if e.get("division") else ""
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in e["areas"])
    meta = (f'<span class="court">{_esc(COURT_LABELS[e["court"]])}</span>{div_part} \u00b7 decided '
            f'{_esc(_date_label(e["date"]))} \u00b7 {_esc(_no_label(e["dockets"]))} \u00b7 {_esc(e["disposition"])}')
    return (
        f'      <article class="opinion" data-court="{e["court"]}" data-areas="{",".join(e["areas"])}" data-date="{e["date"]}">\n'
        f'        <div class="op-head"><span class="op-name">{_esc(e["name"])}</span></div>\n'
        f'        <div class="op-meta">{meta}</div>\n'
        f'        <div class="op-tags">{tags}</div>\n'
        f'        <p class="op-synopsis">{_esc(e["synopsis"])}</p>\n'
        f'        <p class="op-why"><strong>Why it matters:</strong> {_esc(e["why"])}</p>\n'
        f'        <div class="op-foot">\n'
        f'          <span class="op-source"><a href="{e["url"]}" target="_blank" rel="noopener noreferrer">Read the opinion on CourtListener \u2192</a></span>\n'
        f'          <span class="op-disclaimer">AI-drafted summary \u00b7 verify against the opinion</span>\n'
        f'        </div>\n'
        f'      </article>'
    )

def rss_item(e):
    cats = [COURT_LABELS[e["court"]]] + [AREA_LABELS[c] for c in e["areas"]]
    desc = f'{e["synopsis"]} Why it matters: {e["why"]} AI-drafted summary. Verify against the opinion. {e["url"]}'
    lines = ["    <item>",
             f"      <title>{xml_escape(e['name'] + TITLE_SUFFIX[e['court']])}</title>",
             f"      <link>{e['url']}</link>",
             f'      <guid isPermaLink="true">{e["url"]}</guid>',
             f"      <pubDate>{_rfc822(e['date'])}</pubDate>"]
    lines += [f"      <category>{xml_escape(c)}</category>" for c in cats]
    lines += [f"      <description><![CDATA[{desc}]]></description>", "    </item>"]
    return "\n".join(lines)

def render(entries=None):
    if entries is None:
        entries = json.load(open(JSON_PATH, encoding="utf-8"))
    entries = _sorted(entries)

    cards = "\n\n".join(card_html(e) for e in entries)
    doc = open(HTML_PATH, encoding="utf-8").read()
    doc = re.sub(r'(<!-- opinions:start.*?-->).*?(<!-- opinions:end -->)',
                 lambda m: m.group(1) + "\n" + cards + "\n      <!-- opinions:end -->",
                 doc, count=1, flags=re.S)
    open(HTML_PATH, "w", encoding="utf-8").write(doc)

    build = _rfc822(entries[0]["date"]) if entries else _rfc822(datetime.date.today().isoformat())
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Georgia Appellate Watch</title>',
           '    <link>https://horowitz.law/opinions</link>',
           '    <atom:link href="https://horowitz.law/opinions.xml" rel="self" type="application/rss+xml" />',
           '    <description>AI-assisted synopses of new Georgia appellate, Eleventh Circuit, and U.S. Supreme Court opinions, filtered for civil litigation and insurance-defense practice. Each synopsis is AI-drafted; the linked opinion is the authority. A curated core, not a complete docket.</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{build}</lastBuildDate>',
           '    <generator>horowitz.law Georgia Appellate Watch (prototype)</generator>']
    out += [rss_item(e) for e in entries]
    out += ['  </channel>', '</rss>', '']
    open(XML_PATH, "w", encoding="utf-8").write("\n".join(out))
    return len(entries)

if __name__ == "__main__":
    print("rendered", render(), "entries -> opinions.html, opinions.xml")
