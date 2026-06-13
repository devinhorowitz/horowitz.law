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
CHANGES_PATH = os.path.join(REPO, "changes.html")
STATS_PATH = os.path.join(REPO, "stats.html")
CHANGES_XML_PATH = os.path.join(REPO, "changes.xml")
PERMA_DIR = os.path.join(REPO, "o")
DIGESTS_PATH = os.path.join(REPO, "digests.html")
SUBSCRIBE_PATH = os.path.join(REPO, "subscribe.html")
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

# Bluebook month forms for the slip-opinion citation (May, June, and July are
# never abbreviated; Sept. takes four letters).
_BB_MO = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July",
          "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]

def _slip_cite(e):
    """The copy-button citation: case name, docket number(s), and a single
    court-plus-date parenthetical -- the Bluebook slip-opinion form. No reporter
    cite is ever included (the funnel deliberately strips them), so this is the
    working form until one exists; the button's title reminds the user to
    confirm on Shepard's before filing, per the house rule. The court inside
    the parenthetical reuses the registry's TITLE_SUFFIX so a second
    jurisdiction needs no edit here."""
    d = datetime.date.fromisoformat(e["date"])
    court = TITLE_SUFFIX.get(e["court"], "").strip().strip("()")
    when = "%s %d, %d" % (_BB_MO[d.month - 1], d.day, d.year)
    return "%s, %s (%s %s)" % (e["name"], _no_label(e["dockets"]), court, when)

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
        # Overlay jurisdictions are honest at the point of choice: the option
        # itself says the view is federal decisions only.
        suffix = " \u00b7 federal" if jurisdictions.jurisdiction_mode(key) == "overlay" else ""
        out.append('          <option value="%s"%s>%s%s</option>' % (key, sel, _esc(label.lower()), suffix))
    return "\n".join(out)


def card_html(e, permalink_link=True):
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
    # jurisdiction may be a single key or, for a federal card relevant to more
    # than one covered state, a list of keys (the multistate plan: an Eleventh
    # Circuit card stamped ["ga","fl"] appears under both states' filters and
    # digests). Either shape renders as a comma list in the attribute; the page
    # filter tests membership, so single values behave exactly as before.
    # Federal courts: bindingness is derived from the court by judicial
    # hierarchy (see jurisdictions.court_binds) -- an Eleventh Circuit card is
    # authority in ga, fl, and al alike; a SCOTUS card in every registered
    # jurisdiction, forever, including ones registered later. State courts:
    # the card's own stamp (which the pipeline may make a list once a second
    # state is covered in full).
    binds = jurisdictions.court_binds(e["court"])
    if binds:
        # Erie refinement (Phase 4): when the pipeline has recorded which body of
        # substantive law a federal holding turns on, narrow the overlay to the
        # jurisdictions the card is actually salient to -- the primary (whose
        # screen kept it) plus the state whose law it applies. "federal" or an
        # absent field keeps full court-level bindingness.
        la = (e.get("law_applied") or "").strip().lower()
        if la and la != "federal" and la in jurisdictions.JURISDICTIONS:
            juris = ",".join(dict.fromkeys([jurisdictions.JURISDICTION, la]))
        else:
            juris = ",".join(binds)
    else:
        juris = e.get("jurisdiction") or jurisdictions.JURISDICTION
        if isinstance(juris, (list, tuple)):
            juris = ",".join(juris)
    areas_all = all_areas(e)
    div_part = f", {_esc(e['division'])}" if e.get("division") else ""
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in areas_all)
    # Phase 4 badges, rendered in the same chip row but visually distinct. Both
    # are searchable for free: the q filter reads card textContent.
    if e.get("first_impression"):
        tags += '<span class="tag tag-badge">first impression</span>'
    if e.get("tort_reform"):
        tags += '<span class="tag tag-badge">tort reform</span>'
    # editor's note: the human-analysis layer, never model-written, rendered
    # visually distinct from the AI synopsis (Phase 4).
    note = (e.get("editor_note") or "").strip()
    note_html = (f'        <p class="op-editornote"><span class="op-editorlabel">editor\'s note</span> {_esc(note)}</p>\n'
                 if note else "")
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
    # Phase 5 official-link enrichment. When a card carries official_url (today,
    # a Supreme Court of Georgia opinion resolved from gasupreme.us), the case
    # name links to the court's own opinion PDF -- the authoritative source -- and
    # the CourtListener link in the foot is relabeled to the full record it is
    # (citations, treatment, and docket data the court's own listing omits). A
    # card without official_url is unchanged: plain name, CourtListener as the
    # read-the-opinion link. Identity still keys on the CourtListener cluster_id.
    official = (e.get("official_url") or "").strip()
    if official:
        name_html = (f'<a href="{_attr(official)}" target="_blank" rel="noopener noreferrer" '
                     f'title="Official opinion \u00b7 {_attr(COURT_LABELS[e["court"]])}">{_esc(e["name"])}</a>')
        source_label = "Full record on CourtListener \u2192"
    else:
        name_html = _esc(e["name"])
        source_label = "Read the opinion on CourtListener \u2192"
    return (
        f'      <article id="op-{e["cluster_id"]}" class="opinion" data-court="{e["court"]}" data-system="{system}" data-jurisdiction="{juris}" data-areas="{",".join(areas_all)}" data-date="{e["date"]}"{attr}{prec_attr}>\n'
        f'{banner}'
        f'        <div class="op-head"><span class="op-name">{name_html}</span></div>\n'
        f'        <div class="op-meta">{meta}</div>\n'
        f'        <div class="op-tags">{tags}</div>\n'
        f'{body}{note_html}'
        f'        <div class="op-foot">\n'
        f'          <span class="op-source"><a href="{_attr(e["url"])}" target="_blank" rel="noopener noreferrer">{source_label}</a></span>\n'
        f'          <button type="button" class="op-copycite" data-cite="{_attr(_slip_cite(e))}" title="Copy slip-opinion citation \u00b7 confirm on Shepard\u2019s before filing">[ copy cite ]</button>\n'
        + (f'          <a class="op-permalink" href="/o/{e["cluster_id"]}" title="Permanent page for this decision">[ permalink ]</a>\n' if permalink_link else '') +
        f'          <span class="op-disclaimer">AI-drafted summary \u00b7 verify against the opinion</span>\n'
        f'        </div>\n'
        f'      </article>'
    )



# ============================== changes ledger ==============================

def _flagged(entries):
    """Cards currently carrying an adverse-treatment flag, newest flag first.
    Current state, not an event history: a cleared flag drops off, matching the
    doctrine that a cleared card stands again."""
    f = [e for e in entries if (e.get("treatment") or "ok") != "ok"]
    return sorted(f, key=lambda e: (e.get("treatment_date") or e["date"], int(e.get("cluster_id", 0))), reverse=True)

def changes_block(entries):
    """The /changes ledger rows, injected between the changes markers."""
    flagged = _flagged(entries)
    if not flagged:
        return ('      <p class="change-empty">No active flags. Every published card '
                'currently stands untreated \u2014 the sweep ran and found nothing to raise.</p>')
    rows = []
    for e in flagged:
        t = e["treatment"]
        note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
        by = [b.get("name") for b in (e.get("treated_by") or []) if b.get("name")]
        cited = (" Cited by: " + "; ".join(_esc(n) for n in by[:3]) + ".") if by else ""
        rows.append(
            f'      <article class="change" id="ch-{e["cluster_id"]}">\n'
            f'        <div class="change-date">{_esc(e.get("treatment_date") or e["date"])}</div>\n'
            f'        <div class="change-main">\n'
            f'          <span class="change-label change-{t}">{_TREAT_LABEL.get(t, "Flagged")}</span>\n'
            f'          <div class="change-case"><a href="/o/{e["cluster_id"]}">{_esc(e["name"])}</a></div>\n'
            f'          <div class="change-meta">{_esc(COURT_LABELS[e["court"]])} \u00b7 decided {_esc(_date_label(e["date"]))} \u00b7 {_esc(_no_label(e["dockets"]))}</div>\n'
            f'          <p class="change-note">{_esc(note)}{cited}{_TREAT_TAIL.get(t, "")}</p>\n'
            f'        </div>\n'
            f'      </article>')
    return "\n\n".join(rows)

def changes_rss(entries):
    """changes.xml: one item per active flag, so a correction reaches a feed
    reader the day it is raised. Mirrors the main feed's conventions (noon
    Eastern pubDate, CDATA description, split-escape for a literal ]]>)."""
    flagged = _flagged(entries)
    newest = (flagged[0].get("treatment_date") if flagged else None) or (entries[0]["date"] if entries else datetime.date.today().isoformat())
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Treatment Corrections</title>',
           '    <link>https://horowitz.law/changes</link>',
           '    <atom:link href="https://horowitz.law/changes.xml" rel="self" type="application/rss+xml" />',
           '    <description>Adverse-treatment flags on published Georgia Appellate Watch cases. '
           'The sweep may only raise a flag to caution; negative and superseded are human determinations '
           'made on Shepard\u2019s. The flagged card and the linked opinion are the authority.</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{_rfc822(newest)}</lastBuildDate>',
           '    <generator>horowitz.law Georgia Appellate Watch (prototype)</generator>']
    for e in flagged:
        t = e["treatment"]
        note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
        by = [b.get("name") for b in (e.get("treated_by") or []) if b.get("name")]
        cited = (" Cited by: " + "; ".join(by[:3]) + ".") if by else ""
        desc = f'{note}{cited}{_TREAT_TAIL.get(t, "")} Card: https://horowitz.law/o/{e["cluster_id"]}'
        desc = desc.replace("]]>", "]]]]><![CDATA[>")
        out += ["    <item>",
                f"      <title>{xml_escape(_TREAT_LABEL.get(t, 'Flagged') + ': ' + e['name'])}</title>",
                f"      <link>https://horowitz.law/o/{e['cluster_id']}</link>",
                f'      <guid isPermaLink="false">change-{e["cluster_id"]}-{e.get("treatment_date") or e["date"]}</guid>',
                f"      <pubDate>{_rfc822(e.get('treatment_date') or e['date'])}</pubDate>",
                f"      <category>{xml_escape(t)}</category>",
                f"      <description><![CDATA[{desc}]]></description>",
                "    </item>"]
    out += ['  </channel>', '</rss>', '']
    safeio.atomic_write_text(CHANGES_XML_PATH, "\n".join(out))

# ================================ stats =====================================

def _bar_rows(pairs):
    """pairs: [(label, n)] -> bar rows scaled to the max."""
    mx = max((n for _, n in pairs), default=1) or 1
    rows = []
    for label, n in pairs:
        pct = max(round(n * 100 / mx), 1) if n else 0
        rows.append(f'      <div class="stat-row"><span class="stat-label">{_esc(str(label))}</span>'
                    f'<span class="stat-track"><span class="stat-bar" style="width:{pct}%"></span></span>'
                    f'<span class="stat-n">{n}</span></div>')
    return "\n".join(rows)

def _disposition_bucket(d):
    s = (d or "").lower()
    if "in part" in s: return "mixed"
    if "affirm" in s: return "affirmed"
    if "revers" in s: return "reversed"
    if "vacat" in s: return "vacated"
    if "dismiss" in s: return "dismissed"
    return "other"

def stats_block(entries):
    """The /stats sections, injected between the stats markers. Deterministic
    from opinions.json, so re-renders are byte-stable and CI stays green."""
    flagged = _flagged(entries)
    years = {}
    for e in entries: years[e["date"][:4]] = years.get(e["date"][:4], 0) + 1
    courts = [(COURT_LABELS[k], sum(1 for e in entries if e["court"] == k))
              for k in COURT_LABELS if any(e["court"] == k for e in entries)]
    areas = {}
    for e in entries:
        for a in all_areas(e): areas[a] = areas.get(a, 0) + 1
    area_rows = sorted(((AREA_LABELS[a], n) for a, n in areas.items()), key=lambda x: (-x[1], x[0]))
    disp = {}
    for e in entries:
        b = _disposition_bucket(e.get("disposition")); disp[b] = disp.get(b, 0) + 1
    disp_rows = sorted(disp.items(), key=lambda x: (-x[1], x[0]))
    prec = {"published": 0, "unpublished": 0, "physical precedent": 0}
    for e in entries:
        p = (e.get("precedential") or "").strip().lower()
        prec[p if p in prec else "published"] += 1
    span = f'{entries[-1]["date"]} \u2192 {entries[0]["date"]}' if entries else "\u2014"

    def sec(title, body):
        return (f'    <section>\n      <h2 class="section-header">{title}</h2>\n{body}\n    </section>')

    head = (f'      <p class="stat-line"><strong>{len(entries)}</strong> decisions covered \u00b7 '
            f'<strong>{span}</strong> \u00b7 <strong>{len(flagged)}</strong> under an active flag</p>')
    parts = [sec("coverage", head),
             sec("by year", _bar_rows(sorted(years.items()))),
             sec("by court", _bar_rows(courts)),
             sec("by practice area", _bar_rows(area_rows)),
             sec("by disposition", _bar_rows(disp_rows)),
             sec("precedential status", _bar_rows([(k, v) for k, v in prec.items() if v]))]
    return "\n\n".join(parts)

# ============================ digest archive ================================

def _week_monday(iso):
    d = datetime.date.fromisoformat(iso[:10])
    return (d - datetime.timedelta(days=d.weekday())).isoformat()

def digests_block(entries):
    """The /digests page: every week's digest contents, reconstructed from the
    record. A card belongs to the week of its first_seen (when the pipeline
    added it), matching how digest.py selects; a flag belongs to the week of its
    treatment_date. Pure projection of opinions.json -- deterministic, so the
    page reaches back before the email existed and never needs a sent-mail log."""
    weeks = {}
    for e in entries:
        seen = (e.get("first_seen") or e.get("date") or "")[:10]
        if not _valid_date(seen):
            continue
        weeks.setdefault(_week_monday(seen), {"new": [], "flags": []})["new"].append(e)
    for e in _flagged(entries):
        td = (e.get("treatment_date") or "")[:10]
        if _valid_date(td):
            weeks.setdefault(_week_monday(td), {"new": [], "flags": []})["flags"].append(e)
    out = []
    for wk in sorted(weeks, reverse=True):
        w = weeks[wk]
        w["new"].sort(key=lambda e: (e.get("first_seen") or e["date"], int(e.get("cluster_id", 0))), reverse=True)
        d = datetime.date.fromisoformat(wk)
        label = f"week of {_MO[d.month - 1]} {d.day}, {d.year}"
        n_new, n_fl = len(w["new"]), len(w["flags"])
        counts = []
        if n_new: counts.append(f'{n_new} decision{"s" if n_new != 1 else ""} added')
        if n_fl:  counts.append(f'{n_fl} flag{"s" if n_fl != 1 else ""} raised')
        items = []
        for e in w["new"]:
            tags = " ".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in all_areas(e))
            items.append(
                f'        <div class="week-item"><a href="/o/{e["cluster_id"]}">{_esc(e["name"])}</a>'
                f' <span class="week-meta">\u00b7 {_esc(COURT_LABELS[e["court"]])} \u00b7 decided {_esc(_date_label(e["date"]))}</span>'
                f'<div class="op-tags">{tags}</div></div>')
        for e in w["flags"]:
            items.append(
                f'        <div class="week-flag">{_TREAT_LABEL.get(e["treatment"], "Flagged")}: '
                f'<a href="/o/{e["cluster_id"]}">{_esc(e["name"])}</a> \u2014 see <a href="/changes">the ledger</a>.</div>')
        out.append(
            f'      <section class="week" id="w{wk}">\n'
            f'        <div class="week-head"><span class="week-date">{label}</span>'
            f'<span class="week-count">{_esc(" \u00b7 ".join(counts))}</span></div>\n'
            + "\n".join(items) + "\n"
            f'      </section>')
    return "\n\n".join(out)

# ========================= subscribe area choices ===========================

def jurisdiction_choices():
    """The which-states checkboxes on /subscribe -- DORMANT until the registry
    holds a second jurisdiction, then they appear on the next render with every
    covered state checked. Gated on the registry on purpose: the same Phase 5
    commit that adds Florida to jurisdictions.py is the one that must also teach
    the subscribe wire and the digest about states (see ROADMAP.md Phase 5), so
    the form can never offer a choice the backend does not yet honor."""
    full = [(k, l) for k, l in jurisdictions.ALL_JURISDICTIONS
            if jurisdictions.jurisdiction_mode(k) == "full"]
    if len(full) < 2:
        return ("      <!-- subscriptions are per fully-covered state; federal-overlay\n"
                "           jurisdictions join here automatically when their state courts do -->")
    out = ['      <fieldset class="areas" id="jurisChoices">',
           '        <legend>which states</legend>']
    for key, label in full:
        out.append(f'        <label class="area"><input type="checkbox" name="juris" value="{key}" checked> {_esc(label.lower())}</label>')
    out.append('      </fieldset>')
    return "\n".join(out)


def area_choices():
    """The per-area checkboxes on /subscribe, generated from AREA_LABELS so the
    form can never drift from the taxonomy. 'all' is the full weekly digest and
    the default; each area maps to a Resend Topic at confirm time (see
    functions/api/subscribe/confirm.js and RESEND_AREA_TOPICS)."""
    out = ['      <label class="area"><input type="checkbox" name="area" value="all" checked> everything <span class="area-hint">\u2014 the full weekly digest</span></label>']
    for code, label in AREA_LABELS.items():
        out.append(f'      <label class="area"><input type="checkbox" name="area" value="{code}"> {_esc(label)}</label>')
    return "\n".join(out)

# ============================== permalinks ==================================

# Chrome styles for the standalone permalink pages, read once per render from
# the committed changes.html shell so every generated page matches the house
# chrome without a second hand-maintained copy. Fails loud if the block moves.
_PERMA_CORE_CACHE = None
def _perma_core():
    global _PERMA_CORE_CACHE
    if _PERMA_CORE_CACHE is None:
        doc = open(CHANGES_PATH, encoding="utf-8").read()
        m = re.search(r"(  :root \{.*?@media \(max-width: 600px\) \{\n.*?\n  \})", doc, re.S)
        if not m:
            raise RuntimeError("render: chrome style block not found in changes.html")
        _PERMA_CORE_CACHE = m.group(1)
    return _PERMA_CORE_CACHE

def _inline_script():
    """The CSP hash-pinned pre-paint script, read verbatim from the live feed
    page so a generated permalink can never carry a stale copy. Fails loud if
    the script shape ever changes (check_site is the second net)."""
    doc = open(HTML_PATH, encoding="utf-8").read()
    m = re.search(r"<script>\(function\(\)\{.*?\}\)\(\);</script>", doc, re.S)
    if not m:
        raise RuntimeError("render: pre-paint inline script not found in %s" % HTML_PATH)
    return m.group(0)

def _card_styles():
    """The opinion-card CSS, read verbatim from the live feed page's stylesheet
    so a permalink card renders pixel-identical and cannot drift."""
    doc = open(HTML_PATH, encoding="utf-8").read()
    m = re.search(r"\n(  \.opinion \{.*?)\n\n  \.empty", doc, re.S)
    if not m:
        raise RuntimeError("render: card style block not found in %s" % HTML_PATH)
    return m.group(1)

def permalink_html(e):
    name = _esc(e["name"])
    desc = (e["synopsis"][:157] + "\u2026") if len(e["synopsis"]) > 158 else e["synopsis"]
    modified = e.get("treatment_date") or (e.get("first_seen") or "")[:10] or e["date"]
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "Article",
        "headline": e["name"],
        "datePublished": e["date"], "dateModified": modified,
        "url": f'https://horowitz.law/o/{e["cluster_id"]}',
        "mainEntityOfPage": f'https://horowitz.law/o/{e["cluster_id"]}',
        "isAccessibleForFree": True,
        "author": {"@type": "Organization", "name": "Georgia Appellate Watch \u00b7 horowitz.law"},
        "publisher": {"@type": "Person", "name": "Devin R. Horowitz", "url": "https://horowitz.law/"},
        "description": desc + " AI-drafted synopsis; the linked opinion is the authority."}, ensure_ascii=False)
    flagged_line = ""
    if (e.get("treatment") or "ok") != "ok":
        flagged_line = ('    <p class="perma-note">This card carries an adverse-treatment flag \u2014 '
                        'see <a href="/changes">the changes ledger</a>.</p>\n\n')
    return (
"<!DOCTYPE html>\n"
'<html lang="en">\n'
"<head>\n"
'<meta charset="UTF-8">\n'
'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
f"<title>{name} \u00b7 horowitz.law</title>\n"
f'<meta name="description" content="{_attr(desc)}">\n'
'<meta name="theme-color" content="#0d0e10" media="(prefers-color-scheme: dark)">\n'
'<meta name="theme-color" content="#f5ede0" media="(prefers-color-scheme: light)">\n'
"\n"
f'<link rel="canonical" href="https://horowitz.law/o/{e["cluster_id"]}">\n'
"\n"
'<meta property="og:type" content="article">\n'
f'<meta property="og:url" content="https://horowitz.law/o/{e["cluster_id"]}">\n'
'<meta property="og:locale" content="en_US">\n'
f'<meta property="og:title" content="{_attr(e["name"])}">\n'
f'<meta property="og:description" content="{_attr(desc)}">\n'
'<meta property="og:image" content="https://horowitz.law/og-card.jpg">\n'
'<meta property="og:site_name" content="horowitz.law">\n'
f'<meta property="article:published_time" content="{e["date"]}">\n'
"\n"
'<meta name="twitter:card" content="summary">\n'
f'<meta name="twitter:title" content="{_attr(e["name"])}">\n'
f'<meta name="twitter:description" content="{_attr(desc)}">\n'
'<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
'<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">\n'
'<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">\n'
'<link rel="manifest" href="/manifest.webmanifest">\n'
'<meta name="apple-mobile-web-app-capable" content="yes">\n'
'<meta name="apple-mobile-web-app-title" content="GA Watch">\n'
'<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
"\n"
'<link rel="preload" href="/fonts/jetbrains-mono-400.subset.woff2" as="font" type="font/woff2" crossorigin>\n'
'<link rel="preload" href="/fonts/jetbrains-mono-600.subset.woff2" as="font" type="font/woff2" crossorigin>\n'
'<link rel="preload" href="/fonts/jetbrains-mono-700.subset.woff2" as="font" type="font/woff2" crossorigin>\n'
"\n"
f'<script type="application/ld+json">{ld}</script>\n'
"\n"
"<!-- pre-paint theme + .js marker (CSP hash-pinned; spliced verbatim from the feed page at render time) -->\n"
f"{_inline_script()}\n"
'<link rel="stylesheet" href="/base.css">\n'
"\n"
"<style>\n"
f"{_perma_core()}\n"
"\n"
f"{_card_styles()}\n"
"\n"
"  .perma-note { color: var(--fg-muted); font-size: 13px; margin: 18px 0 0; }\n"
"  .perma-note a, .perma-nav a { color: var(--fg); text-decoration: none; border-bottom: 1px dotted var(--fg-muted); transition: color 0.2s, border-color 0.2s; }\n"
"  .perma-note a:hover, .perma-nav a:hover { color: var(--accent); border-bottom-color: var(--accent); }\n"
"  .perma-nav { margin-top: 28px; color: var(--fg-muted); font-size: 13px; }\n"
"  .perma-nav span { display: block; margin: 6px 0; }\n"
"  .perma-nav span::before { content: '$ '; color: var(--accent); font-weight: 600; }\n"
"  h1.page-title { font-size: clamp(22px, 3.6vw, 30px); }\n"
"</style>\n"
"</head>\n"
"<body>\n"
'<a class="skip-link" href="#main-content">Skip to main content</a>\n'
'<div class="container">\n'
"\n"
'  <div class="topbar">\n'
f'    <span class="topbar-prompt"><a href="/">~ horowitz.law</a><a href="/opinions">/opinions</a> \u00b7 o/{e["cluster_id"]}</span>\n'
'    <button type="button" class="theme-toggle" id="themeToggle" aria-pressed="false" aria-label="Toggle light or dark theme">[ light ]</button>\n'
"  </div>\n"
"\n"
'  <main id="main-content">\n'
"\n"
f'    <h1 class="page-title">{name}</h1>\n'
f'    <div class="subtitle">// {_esc(COURT_LABELS[e["court"]])} \u00b7 decided {_esc(_date_label(e["date"]))}</div>\n'
"\n"
f"{card_html(e, permalink_link=False)}\n"
"\n"
f'{flagged_line}    <div class="perma-nav">\n'
'      <span><a href="/opinions">cd /opinions</a>  <em># the rolling feed</em></span>\n'
'      <span><a href="/archive">cd /archive</a>  <em># everything, by year</em></span>\n'
'      <span><a href="/changes">cat /changes</a>  <em># the treatment ledger</em></span>\n'
"    </div>\n"
"\n"
"  </main>\n"
"\n"
"  <footer>\n"
"    <span>\u00a9 2026 \u00b7 Hand-coded by Devin R. Horowitz</span>\n"
'    <span><a href="/opinions">\u2190 back to the watch</a></span>\n'
"  </footer>\n"
"\n"
"</div>\n"
"\n"
'<script src="/app.js" defer></script>\n'
"\n"
"</body>\n"
"</html>\n")

def _write_permalinks(entries):
    """One standalone page per covered decision at /o/<cluster_id>: its own OG
    card, Article JSON-LD, and the same opinion-card markup the feed uses (the
    chrome, card styles, and CSP script are spliced from committed pages, so
    nothing can drift). Strays from removed entries are deleted."""
    os.makedirs(PERMA_DIR, exist_ok=True)
    want = set()
    for e in entries:
        fn = f'{e["cluster_id"]}.html'
        want.add(fn)
        doc = _stamp_tokens(_stamp_year(permalink_html(e)))
        path = os.path.join(PERMA_DIR, fn)
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if cur != doc:
            safeio.atomic_write_text(path, doc)
    for fn in os.listdir(PERMA_DIR):
        if fn.endswith(".html") and fn not in want:
            os.remove(os.path.join(PERMA_DIR, fn))
    return len(want)

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

    # The treatment ledger, its feed, the stats page, and the permalinks: all
    # deterministic projections of the same opinions.json the cards come from.
    if os.path.exists(CHANGES_PATH):
        _inject(CHANGES_PATH, "changes", changes_block(entries))
    changes_rss(entries)
    if os.path.exists(STATS_PATH):
        _inject(STATS_PATH, "stats", stats_block(entries))
    if os.path.exists(DIGESTS_PATH):
        _inject(DIGESTS_PATH, "digests", digests_block(entries))
    if os.path.exists(SUBSCRIBE_PATH):
        sub_doc = open(SUBSCRIBE_PATH, encoding="utf-8").read()
        if "areachoices:start" in sub_doc:
            _inject(SUBSCRIBE_PATH, "areachoices", area_choices())
        if "jurischoices:start" in sub_doc:
            _inject(SUBSCRIBE_PATH, "jurischoices", jurisdiction_choices())
    _write_permalinks(entries)

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
        new = set_lastmod(new, "https://horowitz.law/stats", entries[0]["date"])
        newest_seen = max(((e.get("first_seen") or e.get("date") or "")[:10] for e in entries), default="")
        if newest_seen:
            new = set_lastmod(new, "https://horowitz.law/digests", newest_seen)
    flagged = _flagged(entries)
    if flagged:
        new = set_lastmod(new, "https://horowitz.law/changes", flagged[0].get("treatment_date") or flagged[0]["date"])
    # Permalink entries live between sitemap markers and regenerate wholesale.
    urls = []
    for e in entries:
        lastmod = e.get("treatment_date") or (e.get("first_seen") or "")[:10] or e["date"]
        urls.append("  <url>\n    <loc>https://horowitz.law/o/%s</loc>\n    <lastmod>%s</lastmod>\n"
                    "    <changefreq>yearly</changefreq>\n    <priority>0.3</priority>\n  </url>" % (e["cluster_id"], lastmod))
    pat = re.compile(r"(<!-- permalinks:start.*?-->).*?(<!-- permalinks:end -->)", re.S)
    if pat.search(new):
        new = pat.sub(lambda m: m.group(1) + "\n" + "\n".join(urls) + "\n" + m.group(2), new, count=1)
    if new != doc:
        safeio.atomic_write_text(SITEMAP_PATH, new)

if __name__ == "__main__":
    r, t = render()
    print(f"rendered {r} recent of {t} total -> pages, feeds, stats, ledger, permalinks")
