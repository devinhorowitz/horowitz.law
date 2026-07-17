#!/usr/bin/env python3
"""Render opinions.json into opinions.html, archive.html, and opinions.xml, plus the
derived pages and feeds (sitemap.xml, o/ permalinks, areas/ slices, stats.html, changes.*,
digests.html).

opinions.json is the single source of truth: a list of entry objects. Run from
anywhere with `python scripts/render.py`. Pure standard library, no dependencies.

The public feed (opinions.html and opinions.xml) shows a rolling window of the
most recent WINDOW_YEARS years by decision date, recomputed every run, so it
never grows without bound and nothing drops on a calendar boundary. The full
record lives in archive.html, grouped by decision year; nothing is removed
there. Cards are written between the start/end markers in each file; nothing else
between the markers is touched, though the footer year, asset ?v= tokens, and
data-cfg identity hooks are re-stamped in place on the shared pages (see below).
"""
import os, re, json, hashlib, html, datetime
from xml.sax.saxutils import escape as xml_escape
import safeio          # crash-safe atomic writes
import jurisdictions   # per-jurisdiction court labels and citation suffixes
import siteconfig      # site-wide config: domain, identity, window, area taxonomy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB  = os.path.join(REPO, "public")   # deployed web root (Cloudflare pages_build_output_dir)
# Inputs (opinions.json, README.md, resume.md) are pipeline data and live at the
# repo root; every generated or served file is read/written under WEB.
JSON_PATH    = os.path.join(REPO, "opinions.json")
HTML_PATH    = os.path.join(WEB, "opinions.html")
ARCHIVE_PATH = os.path.join(WEB, "archive.html")
XML_PATH     = os.path.join(WEB, "opinions.xml")

# Rolling public-feed window and canonical origin, both sourced from siteconfig
# so a change is one edit there. The RSS description and the feed intros derive
# their "N years" wording from WINDOW_YEARS via siteconfig.years_word(), so the
# prose stays in sync with the number automatically.
WINDOW_YEARS = siteconfig.WINDOW_YEARS
SITE         = siteconfig.SITE_URL
ARCHIVE_URL  = siteconfig.ARCHIVE_URL

SITEMAP_PATH = os.path.join(WEB, "sitemap.xml")
CHANGES_PATH = os.path.join(WEB, "changes.html")
STATS_PATH = os.path.join(WEB, "stats.html")
CHANGES_XML_PATH = os.path.join(WEB, "changes.xml")
PERMA_DIR = os.path.join(WEB, "o")
DIGESTS_PATH = os.path.join(WEB, "digests.html")
SUBSCRIBE_PATH = os.path.join(WEB, "subscribe.html")
AREAS_DIR    = os.path.join(WEB, "areas")
COLOPHON_PATH = os.path.join(WEB, "colophon.html")
README_PATH  = os.path.join(REPO, "README.md")
RESUME_PATH  = os.path.join(WEB, "resume.html")
RESUME_MD_PATH = os.path.join(REPO, "resume.md")
SECURITY_TXT_PATH = os.path.join(WEB, ".well-known", "security.txt")
VCARD_PATH = os.path.join(WEB, "devin-horowitz.vcf")
NOTFOUND_PATH = os.path.join(WEB, "404.html")
# Legislative Watch (a sibling feed to opinions; see scripts/legislation.py and docs/LEGISLATION.md).
# legislation.json is pipeline data at the repo root, like opinions.json; the page and feed are
# generated under WEB and owned by render (in OUTPUT_PATHS below).
LEG_JSON_PATH = os.path.join(REPO, "legislation.json")
LEG_HTML_PATH = os.path.join(WEB, "legislation.html")
LEG_XML_PATH  = os.path.join(WEB, "legislation.xml")
# Federal regulatory watch (FMCSA rulemaking; see scripts/regulations.py). Regulation cards render
# in their own section of the SAME /legislation page (statutes + regulations are the two halves of
# "law that moved" for this practice); regulations.json is the source, regulations.xml the feed.
REG_JSON_PATH = os.path.join(REPO, "regulations.json")
REG_XML_PATH  = os.path.join(WEB, "regulations.xml")
# Pages outside the marker-injection set whose footer year would otherwise rot on
# Jan 1 (the injected pages are stamped in _inject). render() re-stamps these in
# place, writing only when the year actually changed, so it is a no-op all year;
# render-sync's add-paths carries the rollover PR.
STATIC_PAGES = [os.path.join(WEB, p) for p in
                ("index.html", "resume.html", "colophon.html", "subscribe.html", "404.html")]


def _rel(p):
    return os.path.relpath(p, REPO)


# The complete set of paths render() creates or maintains, as repo-relative paths -- the ONE source
# of truth for "what render owns". Everything that has to agree on this set derives from it or is
# checked against it, so the three-way duplication that caused real bugs (a page render re-stamps
# but a workflow forgot to stage -> push aborts on a dirty tree) cannot recur:
#   * scripts/publish.py stages exactly opinions.json + OUTPUT_PATHS on the funnel's write-to-main.
#   * render-sync.yml's add-paths must equal OUTPUT_PATHS -- asserted in scripts/check_site.py, so a
#     new output added here without updating that workflow fails CI instead of stranding a file.
# o/ and areas/ are whole directories render fully manages (create/update/delete); the STATIC_PAGES
# are re-stamped in place (footer year, asset tokens); the rest are single generated files.
OUTPUT_PATHS = sorted({_rel(p) for p in (
    STATIC_PAGES + [HTML_PATH, ARCHIVE_PATH, XML_PATH, SITEMAP_PATH, CHANGES_PATH, STATS_PATH,
                    CHANGES_XML_PATH, DIGESTS_PATH, SECURITY_TXT_PATH, VCARD_PATH,
                    LEG_HTML_PATH, LEG_XML_PATH, REG_XML_PATH,
                    PERMA_DIR, AREAS_DIR])})

_YEAR_RE = re.compile(r'(&copy;|\u00a9)\s*\d{4}')

# Asset ?v= tokens are content hashes: the first 10 hex chars of sha256, the
# same scheme scripts/check_site.py enforces in CI. Restamping on every page
# write keeps a rendered page from carrying tokens older than the assets it
# loads -- the shell outside the markers is never otherwise touched, so an
# asset edit made alongside a hand upload would leave the generated pages a
# year stale under immutable caching until someone remembered --fix. The two
# implementations are deliberately independent (this one stamps, check_site
# verifies), so a bug here cannot blind the check there.
# The set is read from the repo root, so a newly added stylesheet or script is
# cache-busted without editing this file; sw.js is excluded deliberately (it is
# served no-cache and never ?v=-stamped). Sorted for a stable order -- stamping
# is per-asset, so order does not affect output.
_ASSETS = tuple(sorted(f for f in os.listdir(WEB)
                       if f.endswith((".css", ".js")) and f != "sw.js"))

def _asset_token(name):
    with open(os.path.join(WEB, name), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:10]

def _stamp_tokens(doc):
    for a in _ASSETS:
        path = os.path.join(WEB, a)
        if not os.path.exists(path):
            continue        # never let a missing optional asset break a render
        tok = _asset_token(a)
        doc = re.sub(r'((?:href|src)="/%s)(\?v=[^"]*)?(")' % re.escape(a),
                     lambda m, t=tok: m.group(1) + "?v=" + t + m.group(3), doc)
    return doc

def _asof():
    """The reference 'today' for render's time-dependent output: the WINDOW_YEARS feed cutoff, the
    footer copyright year, and the security.txt Expires renewal. Real today by default;
    OPINIONS_RENDER_ASOF=YYYY-MM-DD overrides it. That override lets the CI render-idempotency check
    render AS OF the last funnel render (public/status.json `scanned_at`) and compare byte-for-byte
    to the committed pages -- so the check no longer goes spuriously red when the live clock has
    drifted past a card's 2-year edge (or the Jan-1 footer year) before the funnel/render-sync has
    reconciled. Inert in normal operation: the funnel and render-sync leave it unset (real today)."""
    v = os.environ.get("OPINIONS_RENDER_ASOF", "").strip()
    if v:
        try:
            return datetime.date.fromisoformat(v[:10])
        except ValueError:
            pass
    return datetime.date.today()


def _stamp_year(doc):
    """Rewrite any footer copyright year to the current year. A no-op when the
    year already matches, so it adds no spurious diff."""
    year = str(_asof().year)
    return _YEAR_RE.sub(lambda m: m.group(1) + " " + year, doc)

AREA_LABELS = siteconfig.AREA_LABELS
COURT_LABELS = jurisdictions.COURT_LABELS   # internal key -> human label
COURT_SYSTEM = jurisdictions.COURT_SYSTEM   # internal key -> "state" | "federal"
TITLE_SUFFIX = jurisdictions.TITLE_SUFFIX   # internal key -> short citation suffix
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def _date_label(iso):
    d = datetime.date.fromisoformat(iso[:10])   # [:10] to match _valid_date's guard
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
    d = datetime.date.fromisoformat(e["date"][:10])   # [:10] to match _valid_date's guard
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
    d = datetime.date.fromisoformat(iso[:10])   # [:10] to match _valid_date's guard
    return f"{_WD[d.weekday()]}, {d.day:02d} {_MO[d.month - 1]} {d.year} 12:00:00 {_eastern_offset(d)}"

def _esc(t):  # HTML text content (leave quotes alone)
    return html.escape(t, quote=False)

def _attr(t):  # HTML attribute value (escape quotes too)
    return html.escape(t or "", quote=True)

# C0 control characters XML 1.0 forbids ANYWHERE -- even inside CDATA, even as a numeric entity --
# except tab, newline, and carriage return. A single stray one (bad OCR / a corrupt PDF text layer,
# echoed by the model into a synopsis or a treatment note) makes opinions.xml / changes.xml
# unparseable and breaks the feed for every reader, so it is stripped before it reaches the feeds.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def _xml_safe(t):
    return _XML_ILLEGAL.sub("", t or "")

def _stamp_identity(doc):
    """Fill data-cfg hooks from siteconfig.IDENTITY so the name, role, and contact
    details come from one config file instead of being hand-edited across pages.
    The hooks are plain data-* attributes, so they persist and a render fills them
    again next time: change a value in siteconfig (a promotion, say) and it
    propagates on the next render. Idempotent -- refilling with the same value is
    a no-op, which is why a static page stays byte-stable until the config moves.
      data-cfg-text="KEY"        rewrites the element's inner text
      data-cfg-lead="KEY"        rewrites leading text only, leaving trailing child markup
      data-cfg-content="KEY"     rewrites a meta tag's content="" value
      data-cfg-attr="a=KEY,..."  rewrites named attribute(s) from IDENTITY
      data-cfg-jsonld            rewrites distinctive JSON-LD fields in this <script>
    """
    I = siteconfig.IDENTITY

    def _text(m):
        val = I.get(m.group("key"))
        return (m.group("open") + _esc(val) + m.group("close")) if val is not None else m.group(0)
    doc = re.sub(r'(?P<open><(?P<tag>\w+)\b[^>]*\bdata-cfg-text="(?P<key>\w+)"[^>]*>)'
                 r'(?P<inner>.*?)(?P<close></(?P=tag)>)', _text, doc, flags=re.S)

    def _content(m):
        tag = m.group(0)
        key = re.search(r'\bdata-cfg-content="(\w+)"', tag).group(1)
        val = I.get(key)
        if val is None:
            return tag
        return re.sub(r'(\bcontent=")[^"]*(")',
                      lambda mm: mm.group(1) + _attr(val) + mm.group(2), tag, count=1)
    doc = re.sub(r'<meta\b[^>]*\bdata-cfg-content="\w+"[^>]*>', _content, doc)

    def _lead(m):
        # leading text only (up to the first child tag), leaving trailing markup. The hero
        # <h1> uses it: the name must stay a leading text node so the cursor <span> and the
        # app.js typing animation survive; data-cfg-text would swallow the whole inner.
        val = I.get(m.group("key"))
        return (m.group("open") + _esc(val)) if val is not None else m.group(0)
    doc = re.sub(r'(?P<open><(?P<tag>\w+)\b[^>]*\bdata-cfg-lead="(?P<key>\w+)"[^>]*>)[^<]*',
                 _lead, doc)

    def _attrs(m):
        # set named attribute(s) from IDENTITY: data-cfg-attr="alt=name,href=href_tel". For
        # the portrait alt, the mailto/tel hrefs, and the QR aria-label, where the value is
        # an attribute, not text or a meta content.
        tag = m.group(0)
        spec = re.search(r'\bdata-cfg-attr="([^"]*)"', tag).group(1)
        for pair in spec.split(","):
            an, _, ck = pair.strip().partition("=")
            an, ck = an.strip(), ck.strip()
            val = I.get(ck)
            if not an or val is None:
                continue
            if re.search(r'\b%s="' % re.escape(an), tag):
                tag = re.sub(r'(\b%s=")[^"]*(")' % re.escape(an),
                             lambda mm, v=val: mm.group(1) + _attr(v) + mm.group(2), tag, count=1)
            else:
                tag = re.sub(r'(<\w+\b)',
                             lambda mm, a=an, v=val: mm.group(1) + ' %s="%s"' % (a, _attr(v)),
                             tag, count=1)
        return tag
    doc = re.sub(r'<\w+\b[^>]*\bdata-cfg-attr="[^"]*"[^>]*?/?>', _attrs, doc)

    def _jsonld(m):
        block = m.group(0)
        # (json key, IDENTITY key, count): name/givenName/familyName/jobTitle/email hit the
        # first (Person) match; telephone is rewritten at every occurrence (the Person and the
        # worksFor block carry the same number), count=0 meaning all.
        for jkey, ckey, n in (("name", "name", 1), ("givenName", "name_first", 1),
                              ("familyName", "name_last", 1), ("jobTitle", "role", 1),
                              ("email", "email", 1), ("telephone", "phone_e164", 0)):
            val = I.get(ckey)
            if val is None:
                continue
            block = re.sub(r'("%s":\s*")[^"]*(")' % jkey,
                           lambda mm, v=val: mm.group(1) + v.replace('"', '\\"') + mm.group(2),
                           block, count=n)
        firm = I.get("firm")
        if firm is not None:
            block = re.sub(r'("worksFor"\s*:\s*\{[^{}]*?"name"\s*:\s*")[^"]*(")',
                           lambda mm, v=firm: mm.group(1) + v.replace('"', '\\"') + mm.group(2),
                           block, count=1)
        return block
    doc = re.sub(r'<script\b[^>]*\bdata-cfg-jsonld\b[^>]*>.*?</script>', _jsonld, doc, flags=re.S)
    return doc

def _safe_url(u):  # only http(s) belongs in an href; neutralize javascript:, data:, //host
    u = (u or "").strip()
    lo = u.lower()
    return u if lo.startswith("http://") or lo.startswith("https://") else ""

def _valid_date(iso):
    # str() first so a non-string date (a bad hand-edit -- int, dict, list) is coerced rather than
    # crashing the slice; a validation predicate returns False on any error, never raises.
    try:
        datetime.date.fromisoformat(str(iso or "")[:10]); return True
    except Exception:
        return False

def _sorted(entries):
    return sorted(entries, key=lambda e: (e["date"], int(e.get("cluster_id", 0))), reverse=True)

def _cutoff_iso(today=None):
    today = today or _asof()
    try:
        c = today.replace(year=today.year - WINDOW_YEARS)
    except ValueError:            # Feb 29 in a non-leap target year -> Feb 28
        c = today.replace(year=today.year - WINDOW_YEARS, day=28)
    return c.isoformat()

_TREAT_LABEL = {"caution": "Possible negative treatment", "negative": "Negative treatment",
                "superseded": "Superseded"}
_TREAT_TAIL  = {"caution": " Verify on Shepard\u2019s before relying.",
                "negative": " Verify on Shepard\u2019s.", "superseded": " Retained for the record."}

# cluster_ids that have a permalink page this render, so a treated_by citer that
# is itself carded can link to it. Populated at the top of render() from the same
# entries the permalinks are written from; empty otherwise, so a direct call (a
# test, say) degrades to plain-text names exactly as before.
_CARDED_IDS = set()

def _cited_by_html(e):
    """The 'Cited by: ...' clause for a treated card: the later case(s) found to
    treat it adversely. Each citer that is itself carded links to its permalink
    (/o/<id>); the rest stay plain text, so a link never 404s. Up to three, to
    match the note's own brevity. Empty when there are no named citers."""
    by = [b for b in (e.get("treated_by") or []) if b.get("name")]
    if not by:
        return ""
    parts = []
    for b in by[:3]:
        nm = _esc(b["name"])
        cid = b.get("cluster_id")
        parts.append(f'<a href="/o/{cid}">{nm}</a>' if cid in _CARDED_IDS else nm)
    return " Cited by: " + "; ".join(parts) + "."

def _treatment_banner(e):
    """A caution banner for a card treated adversely. Empty for untreated ('ok')
    cards, so their markup is unchanged. Renders wherever the card renders, the
    recent feed and the archive both."""
    t = e.get("treatment") or "ok"
    if t == "ok":
        return ""
    note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
    cited = _cited_by_html(e)
    body = (_esc(note) + cited).strip()
    return (f'        <div class="op-treatment op-treatment-{t}" role="note">'
            f'<span class="op-treat-label">{_TREAT_LABEL.get(t, "Flagged")}</span> '
            f'{body}{_TREAT_TAIL.get(t, "")}</div>\n')

def all_areas(e):
    """Union of a card's practice areas across its primary and any additional
    holdings, order-preserving (primary areas first). Drives the card's tag row,
    its data-areas attribute, and its RSS categories, so a card with two holdings
    in different areas is found under either area's filter."""
    # Defensive against a malformed hand-edit: a non-list "areas" or "additional_holdings" (or a
    # non-dict holding) must not crash the taxonomy guard that calls this before _valid_shape runs.
    src = e.get("areas")
    out = list(src) if isinstance(src, list) else []
    ah = e.get("additional_holdings")
    for h in (ah if isinstance(ah, list) else []):
        if not isinstance(h, dict):
            continue
        ha = h.get("areas")
        for a in (ha if isinstance(ha, list) else []):
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


def _nav_block():
    """The /404 listing's top-level page links, between the pages markers. Driven
    by siteconfig.PAGES (the same registry the sitemap uses); an empty label
    drops a page from the listing, so home is sitemap-only."""
    return "\n".join('        <a href="%s">%s</a>' % (path, _esc(label))
                     for path, label, *_ in siteconfig.PAGES if label)


def _jurisdiction_options():
    """The jurisdiction <select> options for opinions.html, generated from the
    registry so adding a state is one registry entry. The active jurisdiction is
    selected. Injected between the jurisdictions markers (nested in the select)."""
    out = []
    for key, label in jurisdictions.ALL_JURISDICTIONS:
        sel = " selected" if key == jurisdictions.JURISDICTION else ""
        # Options are honest at the point of choice. A jurisdiction may name its
        # coverage explicitly (Florida: "also pulled", a supplementary state whose
        # in-area decisions surface but is not a curated focus); a supplementary
        # state with no explicit note still shows "also pulled", an overlay says the
        # view is federal-only, and a fully covered state carries no qualifier.
        note = jurisdictions.jurisdiction_filter_note(key)
        mode = jurisdictions.jurisdiction_mode(key)
        if note:
            suffix = " \u00b7 " + _esc(note)
        elif mode == "supplementary":
            suffix = " \u00b7 also pulled"
        elif mode == "overlay":
            suffix = " \u00b7 federal"
        else:
            suffix = ""
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
        def _holding(h_areas, syn, why, show_areas=True):
            ht = "".join(f'<span class="tag">{_esc(AREA_LABELS[c])}</span>' for c in h_areas)
            areas_row = f'            <div class="op-holding-areas">{ht}</div>\n' if show_areas else ""
            return (f'          <div class="op-holding">\n'
                    f'{areas_row}'
                    f'            <p class="op-synopsis">{_esc(syn)}</p>\n'
                    f'            <p class="op-why"><strong>Why it matters:</strong> {_esc(why)}</p>\n'
                    f'          </div>\n')
        # The first holding is the card's primary; its areas already appear in the
        # card tag row just above, so labeling it again stacks a duplicate chip line
        # directly above it. Additional holdings keep their own area labels, so each
        # stays distinguishable and reachable under its own area filter.
        blocks = _holding(e["areas"], e["synopsis"], e["why"], show_areas=False)
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
        name_html = (f'<a href="{_attr(_safe_url(official))}" target="_blank" rel="noopener noreferrer" '
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
        f'          <span class="op-source"><a href="{_attr(_safe_url(e["url"]))}" target="_blank" rel="noopener noreferrer">{source_label}</a></span>\n'
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
        cited = _cited_by_html(e)
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
           f'    <link>{SITE}/changes</link>',
           f'    <atom:link href="{SITE}/changes.xml" rel="self" type="application/rss+xml" />',
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
        desc = f'{note}{cited}{_TREAT_TAIL.get(t, "")} Card: {SITE}/o/{e["cluster_id"]}'
        desc = _xml_safe(desc).replace("]]>", "]]]]><![CDATA[>")
        out += ["    <item>",
                f"      <title>{xml_escape(_TREAT_LABEL.get(t, 'Flagged') + ': ' + _xml_safe(e['name']))}</title>",
                f"      <link>{SITE}/o/{e['cluster_id']}</link>",
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
    """The which-states checkboxes on /subscribe -- DORMANT until a second
    fully-covered jurisdiction joins the registry (only Georgia is 'full' today;
    Florida and Alabama are supplementary overlay), then they appear on the next
    render with every covered state checked. Gated on the registry on purpose: the
    commit that promotes a state to full coverage is the one that must also teach
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

_INLINE_SCRIPT_CACHE = None
def _inline_script():
    """The CSP hash-pinned pre-paint script, read verbatim from the live feed
    page so a generated permalink can never carry a stale copy. Fails loud if
    the script shape ever changes (check_site is the second net). Memoized per
    render like _perma_core: permalink_html calls it once per entry."""
    global _INLINE_SCRIPT_CACHE
    if _INLINE_SCRIPT_CACHE is None:
        doc = open(HTML_PATH, encoding="utf-8").read()
        m = re.search(r"<script>\(function\(\)\{.*?\}\)\(\);</script>", doc, re.S)
        if not m:
            raise RuntimeError("render: pre-paint inline script not found in %s" % HTML_PATH)
        _INLINE_SCRIPT_CACHE = m.group(0)
    return _INLINE_SCRIPT_CACHE

_CARD_STYLES_CACHE = None
def _card_styles():
    """The opinion-card CSS, read verbatim from the live feed page's stylesheet
    so a permalink card renders pixel-identical and cannot drift. Memoized per
    render like _perma_core: permalink_html calls it once per entry."""
    global _CARD_STYLES_CACHE
    if _CARD_STYLES_CACHE is None:
        doc = open(HTML_PATH, encoding="utf-8").read()
        m = re.search(r"\n(  \.opinion \{.*?)\n\n  \.empty", doc, re.S)
        if not m:
            raise RuntimeError("render: card style block not found in %s" % HTML_PATH)
        _CARD_STYLES_CACHE = m.group(1)
    return _CARD_STYLES_CACHE

def permalink_html(e):
    name = _esc(e["name"])
    desc = (e["synopsis"][:157] + "\u2026") if len(e["synopsis"]) > 158 else e["synopsis"]
    # The social/SEO description leads with the AI caveat as a prefix, so the
    # signal survives the platform's own display truncation and the unfurl that
    # travels (a shared LinkedIn card, a search snippet) leads with the same
    # disclosure the card and the JSON-LD already carry. JSON-LD keeps its own
    # suffix form below, so it is left on the bare desc to avoid a double caveat.
    social_desc = "AI-drafted summary. " + desc
    modified = e.get("treatment_date") or (e.get("first_seen") or "")[:10] or e["date"]
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "Article",
        "headline": e["name"],
        "datePublished": e["date"], "dateModified": modified,
        "url": f'{SITE}/o/{e["cluster_id"]}',
        "mainEntityOfPage": f'{SITE}/o/{e["cluster_id"]}',
        "isAccessibleForFree": True,
        "author": {"@type": "Organization", "name": "Georgia Appellate Watch \u00b7 horowitz.law"},
        "publisher": {"@type": "Person", "name": siteconfig.PUBLISHER_NAME, "url": siteconfig.AUTHOR_URL},
        "description": desc + " AI-drafted synopsis; the linked opinion is the authority."}, ensure_ascii=False)
    # JSON inside an HTML <script>: escape "<" (and ">") so a "</script>" in a case name or synopsis
    # cannot break out of the element, and escape U+2028/U+2029 -- valid in a JSON string but PHYSICAL
    # line terminators to a JS parser, so a raw one (common in text pasted from a PDF opinion) splits
    # the string literal and throws SyntaxError, silently dropping the whole rich snippet. json.dumps
    # here uses ensure_ascii=False, so it does not escape them for us. All four stay valid JSON.
    ld = (ld.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
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
f'<meta name="description" content="{_attr(social_desc)}">\n'
'<meta name="theme-color" content="#0d0e10" media="(prefers-color-scheme: dark)">\n'
'<meta name="theme-color" content="#f5ede0" media="(prefers-color-scheme: light)">\n'
"\n"
f'<link rel="canonical" href="{SITE}/o/{e["cluster_id"]}">\n'
"\n"
'<meta property="og:type" content="article">\n'
f'<meta property="og:url" content="{SITE}/o/{e["cluster_id"]}">\n'
'<meta property="og:locale" content="en_US">\n'
f'<meta property="og:title" content="{_attr(e["name"])}">\n'
f'<meta property="og:description" content="{_attr(social_desc)}">\n'
f'<meta property="og:image" content="{SITE}/og-card.jpg">\n'
'<meta property="og:site_name" content="horowitz.law">\n'
f'<meta property="article:published_time" content="{e["date"]}">\n'
"\n"
'<meta name="twitter:card" content="summary">\n'
f'<meta name="twitter:title" content="{_attr(e["name"])}">\n'
f'<meta name="twitter:description" content="{_attr(social_desc)}">\n'
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
f"    <span>\u00a9 2026 \u00b7 Hand-coded by {siteconfig.NAME}</span>\n"
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
    # Strip XML-illegal control chars, THEN split-escape a literal "]]>" (which would otherwise end the
    # CDATA early). CDATA is verbatim, so a control byte in the card text would still poison the feed.
    desc = _xml_safe(desc).replace("]]>", "]]]]><![CDATA[>")
    lines = ["    <item>",
             f"      <title>{xml_escape(_xml_safe(e['name']) + TITLE_SUFFIX[e['court']])}</title>",
             f"      <link>{xml_escape(e['url'])}</link>",
             f'      <guid isPermaLink="true">{xml_escape(e["url"])}</guid>',
             f"      <pubDate>{_rfc822(e['date'])}</pubDate>"]
    lines += [f"      <category>{xml_escape(c)}</category>" for c in cats]
    lines += [f"      <description><![CDATA[{desc}]]></description>", "    </item>"]
    return "\n".join(lines)


# ============================== legislative watch ==============================
# A sibling feed to opinions: Georgia bills that became law (or were vetoed), read for a
# civil-litigation practice (scripts/legislation.py; docs/LEGISLATION.md). Same house style,
# but human-confirmed -- no auto lane -- so this renderer only ever projects legislation.json.

_LEG_STATUS_LABEL = {"enacted": "Enacted", "vetoed": "Vetoed"}
# Jurisdiction label on a legislation card: Georgia is the core, U.S. Congress the federal overlay
# (the FAAAA / motor-carrier and federal-jurisdiction statutes that reach a Georgia practice), the
# same core-plus-federal shape the opinion feed carries. An unknown code renders as itself.
_LEG_JURIS = {"GA": "Ga.", "US": "U.S."}
_LEG_EMPTY = ('      <div class="leg-empty">No enacted legislation is carded yet. This watch fills as '
              'the Georgia General Assembly acts — each entry is a bill that became law (signed, or '
              'allowed to become law without signature) or was vetoed, read for civil-litigation impact '
              'and confirmed by hand before it appears here.</div>')


def load_legislation():
    """The legislation source of truth (a list of card objects), or [] if absent/unreadable."""
    try:
        with open(LEG_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _leg_sorted(cards):
    return sorted(cards, key=lambda c: (str(c.get("status_date") or ""), int(c.get("bill_id") or 0)),
                  reverse=True)


def legislation_card_html(c):
    status = (c.get("status") or "").strip().lower()
    status_class = status if status in _LEG_STATUS_LABEL else "other"
    slabel = _LEG_STATUS_LABEL.get(status, (status.title() or "—"))
    areas = [a for a in (c.get("areas") or []) if a in AREA_LABELS]
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[a])}</span>' for a in areas)
    date = c.get("status_date") or ""
    date_lbl = _date_label(date) if _valid_date(date) else date
    impact = (c.get("impact") or "").strip()
    impact_html = (f'        <p class="leg-why"><strong>Why it matters:</strong> {_esc(impact)}</p>\n'
                   if impact else "")
    eff = (c.get("effective_date") or "").strip()
    eff_html = (f'          <span class="leg-eff">Effective {_esc(_date_label(eff) if _valid_date(eff) else eff)}</span>\n'
                if eff else "")
    url = _safe_url(c.get("url") or "")
    state_link = _safe_url(c.get("state_link") or "")
    src = (f'<a href="{_attr(url)}" target="_blank" rel="noopener noreferrer">LegiScan record →</a>'
           if url else "")
    official = (f' · <a href="{_attr(state_link)}" target="_blank" rel="noopener noreferrer">state page</a>'
                if state_link else "")
    st = (c.get("state") or "").strip().upper()
    juris = _LEG_JURIS.get(st, st)
    juris_html = f'<span class="leg-juris">{_esc(juris)}</span>' if juris else ""
    return (
        f'      <article id="leg-{int(c["bill_id"])}" class="leg" data-status="{status_class}" '
        f'data-juris="{_attr(st.lower())}" data-areas="{",".join(areas)}" data-date="{_attr(date)}">\n'
        f'        <div class="leg-head"><span class="leg-status leg-{status_class}">{_esc(slabel)}</span>'
        f'{juris_html}<span class="leg-number">{_esc(c.get("number") or "")}</span>'
        f'<span class="leg-date">{_esc(date_lbl)}</span></div>\n'
        f'        <div class="leg-title">{_esc((c.get("title") or "").strip())}</div>\n'
        + (f'        <div class="leg-tags">{tags}</div>\n' if tags else "")
        + f'        <p class="leg-synopsis">{_esc((c.get("synopsis") or "").strip())}</p>\n'
        + impact_html
        + '        <div class="leg-foot">\n'
        + (f'          <span class="leg-source">{src}{official}</span>\n' if (src or official) else "")
        + eff_html
        + '          <span class="leg-disclaimer">AI-drafted summary · verify against the enrolled bill</span>\n'
        + '        </div>\n'
        + '      </article>'
    )


def legislation_rss_item(c):
    status = (c.get("status") or "").strip().lower()
    slabel = _LEG_STATUS_LABEL.get(status, status.title() or "Update")
    st = (c.get("state") or "").strip().upper()
    cats = [slabel] + ([_LEG_JURIS.get(st, st)] if st else []) \
        + [AREA_LABELS[a] for a in (c.get("areas") or []) if a in AREA_LABELS]
    desc = str(c.get("synopsis") or "")
    impact = (c.get("impact") or "").strip()
    if impact:
        desc += " Why it matters: " + impact
    eff = (c.get("effective_date") or "").strip()
    if eff:
        desc += " Effective %s." % eff
    desc += " AI-drafted summary. Verify against the enrolled bill."
    url = c.get("url") or ""
    if url:
        desc += " " + url
    desc = _xml_safe(desc).replace("]]>", "]]]]><![CDATA[>")
    title = ("%s — %s" % (c.get("number") or "", (c.get("title") or "").strip())).strip(" —")
    guid = url or ("urn:legiscan:%s" % c.get("bill_id"))
    date = c.get("status_date") or ""
    pub = _rfc822(date) if _valid_date(date) else _rfc822(datetime.date.today().isoformat())
    lines = ["    <item>",
             f"      <title>{xml_escape(_xml_safe(title))} ({xml_escape(slabel)})</title>",
             f"      <link>{xml_escape(url)}</link>",
             f'      <guid isPermaLink="{"true" if url else "false"}">{xml_escape(guid)}</guid>',
             f"      <pubDate>{pub}</pubDate>"]
    lines += [f"      <category>{xml_escape(cat)}</category>" for cat in cats]
    lines += [f"      <description><![CDATA[{desc}]]></description>", "    </item>"]
    return "\n".join(lines)


def render_legislation():
    """Project legislation.json onto the /legislation page (between the legislation markers) and
    legislation.xml. Deterministic from legislation.json so the CI idempotency gate covers it.
    No-ops the page injection if legislation.html is absent; always (re)writes the feed."""
    cards = _leg_sorted([c for c in load_legislation()
                         if isinstance(c, dict) and c.get("bill_id") is not None])
    if os.path.exists(LEG_HTML_PATH):
        body = "\n\n".join(legislation_card_html(c) for c in cards) if cards else _LEG_EMPTY
        _inject(LEG_HTML_PATH, "legislation", body)  # the page is stamped once, after both sections

    build = (_rfc822(cards[0]["status_date"])
             if (cards and _valid_date(cards[0].get("status_date") or ""))
             else _rfc822(datetime.date.today().isoformat()))
    desc = ("Georgia legislation that became law (signed, or allowed to become law without signature) "
            "or was vetoed, read for a civil-litigation and insurance-defense practice. Each summary is "
            "AI-drafted and human-confirmed; the enrolled bill is the authority.")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Georgia Legislative Watch</title>',
           f'    <link>{SITE}/legislation</link>',
           f'    <atom:link href="{SITE}/legislation.xml" rel="self" type="application/rss+xml" />',
           f'    <description>{xml_escape(desc)}</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{build}</lastBuildDate>',
           '    <generator>horowitz.law Georgia Legislative Watch (prototype)</generator>']
    out += [legislation_rss_item(c) for c in cards]
    out += ['  </channel>', '</rss>', '']
    safeio.atomic_write_text(LEG_XML_PATH, "\n".join(out))


# ---- Federal regulations (a second section on the same /legislation page) ----
_REG_EMPTY = ('      <div class="leg-empty">No federal regulations are carded yet. This section fills '
              'as the FMCSA (and kindred agencies) issue rules in the Federal Register that reach a '
              'Georgia trucking practice — read for civil-litigation impact and confirmed by hand.</div>')


def load_regulations():
    try:
        with open(REG_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _reg_sorted(cards):
    return sorted(cards, key=lambda c: (str(c.get("publication_date") or ""),
                                        str(c.get("document_number") or "")), reverse=True)


def regulation_card_html(c):
    typ = (c.get("type") or "Rule").strip()
    type_class = "final" if typ.lower().startswith("final") else ("proposed" if "propos" in typ.lower() else "other")
    areas = [a for a in (c.get("areas") or []) if a in AREA_LABELS]
    tags = "".join(f'<span class="tag">{_esc(AREA_LABELS[a])}</span>' for a in areas)
    date = c.get("publication_date") or ""
    date_lbl = _date_label(date) if _valid_date(date) else date
    impact = (c.get("impact") or "").strip()
    impact_html = (f'        <p class="leg-why"><strong>Why it matters:</strong> {_esc(impact)}</p>\n'
                   if impact else "")
    eff = (c.get("effective_date") or "").strip()
    eff_html = (f'          <span class="leg-eff">Effective {_esc(_date_label(eff) if _valid_date(eff) else eff)}</span>\n'
                if eff else "")
    url = _safe_url(c.get("url") or "")
    src = (f'<a href="{_attr(url)}" target="_blank" rel="noopener noreferrer">Federal Register →</a>'
           if url else "")
    agency = (c.get("agency") or "").strip()
    agency_html = f'<span class="leg-juris">{_esc(agency)}</span>' if agency else ""
    cfr = (c.get("cfr") or "").strip()
    cfr_html = f'<span class="leg-number">{_esc(cfr)}</span>' if cfr else ""
    return (
        f'      <article id="reg-{_attr(str(c.get("document_number") or ""))}" class="leg" '
        f'data-type="{type_class}" data-areas="{",".join(areas)}" data-date="{_attr(date)}">\n'
        f'        <div class="leg-head"><span class="leg-type leg-type-{type_class}">{_esc(typ)}</span>'
        f'{agency_html}{cfr_html}<span class="leg-date">{_esc(date_lbl)}</span></div>\n'
        f'        <div class="leg-title">{_esc((c.get("title") or "").strip())}</div>\n'
        + (f'        <div class="leg-tags">{tags}</div>\n' if tags else "")
        + f'        <p class="leg-synopsis">{_esc((c.get("synopsis") or "").strip())}</p>\n'
        + impact_html
        + '        <div class="leg-foot">\n'
        + (f'          <span class="leg-source">{src}</span>\n' if src else "")
        + eff_html
        + '          <span class="leg-disclaimer">AI-drafted summary · verify against the rule</span>\n'
        + '        </div>\n'
        + '      </article>'
    )


def regulation_rss_item(c):
    typ = (c.get("type") or "Rule").strip()
    cats = [typ] + ([c.get("agency")] if c.get("agency") else []) \
        + [AREA_LABELS[a] for a in (c.get("areas") or []) if a in AREA_LABELS]
    desc = str(c.get("synopsis") or "")
    impact = (c.get("impact") or "").strip()
    if impact:
        desc += " Why it matters: " + impact
    if (c.get("cfr") or "").strip():
        desc += " (" + c["cfr"].strip() + ")"
    eff = (c.get("effective_date") or "").strip()
    if eff:
        desc += " Effective %s." % eff
    desc += " AI-drafted summary. Verify against the Federal Register document."
    url = c.get("url") or ""
    if url:
        desc += " " + url
    desc = _xml_safe(desc).replace("]]>", "]]]]><![CDATA[>")
    title = ("%s — %s" % (c.get("agency") or "", (c.get("title") or "").strip())).strip(" —")
    guid = url or ("urn:federalregister:%s" % c.get("document_number"))
    date = c.get("publication_date") or ""
    pub = _rfc822(date) if _valid_date(date) else _rfc822(datetime.date.today().isoformat())
    lines = ["    <item>",
             f"      <title>{xml_escape(_xml_safe(title))} ({xml_escape(typ)})</title>",
             f"      <link>{xml_escape(url)}</link>",
             f'      <guid isPermaLink="{"true" if url else "false"}">{xml_escape(guid)}</guid>',
             f"      <pubDate>{pub}</pubDate>"]
    lines += [f"      <category>{xml_escape(cat)}</category>" for cat in cats]
    lines += [f"      <description><![CDATA[{desc}]]></description>", "    </item>"]
    return "\n".join(lines)


def render_regulations():
    """Project regulations.json onto the /legislation page (between the regulations markers) and
    regulations.xml. Deterministic from regulations.json; the page itself is stamped once by
    render_legislation_page()."""
    cards = _reg_sorted([c for c in load_regulations()
                         if isinstance(c, dict) and c.get("document_number")])
    if os.path.exists(LEG_HTML_PATH):
        body = "\n\n".join(regulation_card_html(c) for c in cards) if cards else _REG_EMPTY
        _inject(LEG_HTML_PATH, "regulations", body)

    build = (_rfc822(cards[0]["publication_date"])
             if (cards and _valid_date(cards[0].get("publication_date") or ""))
             else _rfc822(datetime.date.today().isoformat()))
    desc = ("Federal agency rulemaking (FMCSA and kindred agencies) that reaches a Georgia trucking / "
            "civil-litigation practice: safety standards, carrier and broker liability, and financial-"
            "responsibility minimums in 49 CFR. From the Federal Register. Each summary is AI-drafted "
            "and human-confirmed; the Federal Register document is the authority.")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Federal Regulatory Watch</title>',
           f'    <link>{SITE}/legislation</link>',
           f'    <atom:link href="{SITE}/regulations.xml" rel="self" type="application/rss+xml" />',
           f'    <description>{xml_escape(desc)}</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{build}</lastBuildDate>',
           '    <generator>horowitz.law Federal Regulatory Watch (prototype)</generator>']
    out += [regulation_rss_item(c) for c in cards]
    out += ['  </channel>', '</rss>', '']
    safeio.atomic_write_text(REG_XML_PATH, "\n".join(out))


def render_legislation_page():
    """Render both sections of the /legislation page (statutes + federal regulations) and their two
    feeds, then stamp the page once (tokens + footer year + identity hooks). Deterministic; the CI
    idempotency gate covers legislation.html, legislation.xml, and regulations.xml."""
    render_legislation()
    render_regulations()
    if os.path.exists(LEG_HTML_PATH):
        doc = open(LEG_HTML_PATH, encoding="utf-8").read()
        stamped = _stamp_tokens(_stamp_year(_stamp_identity(doc)))
        if stamped != doc:
            safeio.atomic_write_text(LEG_HTML_PATH, stamped)


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

def _stamp_security_txt(window_days=30, renew_days=365):
    """Keep .well-known/security.txt's Expires from lapsing.

    RFC 9116 requires an Expires field, and a lapsed one reads as a neglected
    policy. When the value is missing, unparseable, within `window_days` of now,
    or already past, rewrite it to today + `renew_days` (UTC). Written only when
    the value changes, so this is inert until the renewal window and the bump
    then rides render-sync, exactly like the footer year. For the bump to land,
    .well-known/security.txt must stay in render-sync.yml add-paths.
    """
    if not os.path.exists(SECURITY_TXT_PATH):
        return
    doc = open(SECURITY_TXT_PATH, encoding="utf-8").read()
    m = re.search(r"(?mi)^(Expires:[ \t]*)(\S.*?)[ \t]*$", doc)
    today = _asof()   # honor OPINIONS_RENDER_ASOF so the idempotency check is deterministic
    fresh = False
    if m:
        dm = re.match(r"(\d{4})-(\d{2})-(\d{2})", m.group(2))
        if dm:
            try:
                cur = datetime.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                fresh = (cur - today).days > window_days
            except ValueError:
                fresh = False
    if fresh:
        return
    new_val = (today + datetime.timedelta(days=renew_days)).strftime("%Y-%m-%dT00:00:00.000Z")
    if m:
        new_doc = doc[:m.start(2)] + new_val + doc[m.end(2):]
    else:
        new_doc = re.sub(r"(?mi)^(Contact:.*\n)", r"\1Expires: " + new_val + "\n", doc, count=1)
        if new_doc == doc:
            new_doc = "Expires: " + new_val + "\n" + doc
    if new_doc != doc:
        safeio.atomic_write_text(SECURITY_TXT_PATH, new_doc)


def _vcard_escape(value):
    """vCard 3.0 text-value escaping (RFC 2426): backslash first, then ; and ,
    and a literal newline. FIRM's commas become \\, which is what the file holds."""
    return (value.replace("\\", "\\\\").replace(";", "\\;")
                 .replace(",", "\\,").replace("\n", "\\n"))


def _stamp_vcard():
    """Keep the downloadable vCard's TITLE and ORG in step with siteconfig.

    devin-horowitz.vcf duplicates identity that lives in siteconfig (ROLE,
    FIRM); left static it drifts on a promotion or a firm move (the title sat
    at the old one once already). Rewrite only those two lines from siteconfig,
    leaving everything else, including the embedded PHOTO, untouched. newline=""
    preserves the file's CRLF on read; the text write preserves it on Linux.
    Deterministic from siteconfig, not the clock, so unlike security.txt it can
    sit in ci.yml's render-idempotency diff as a drift tripwire. Written only
    when a value changes, so it is inert unless siteconfig and the card disagree,
    and the fix then rides render-sync, like the footer year. For the bump to
    land, devin-horowitz.vcf must stay in render-sync.yml add-paths.
    """
    if not os.path.exists(VCARD_PATH):
        return
    doc = open(VCARD_PATH, encoding="utf-8", newline="").read()
    new_doc = re.sub(r"(?m)^(TITLE:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.ROLE + m.group(3), doc, count=1)
    new_doc = re.sub(r"(?m)^(ORG:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + _vcard_escape(siteconfig.FIRM) + m.group(3),
                     new_doc, count=1)
    # Name (N is structured last;first;middle.;;, FN is the composed display name), both emails
    # (the PREF line is personal, the plain WORK line is the firm), and the phone. Raw values,
    # not _vcard_escape: the N semicolons are structural and the TEL comma is the DTMF extension
    # separator, both of which escaping would corrupt.
    new_doc = re.sub(r"(?m)^(N:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + "%s;%s;%s.;;" % (
                         siteconfig.NAME_LAST, siteconfig.NAME_FIRST, siteconfig.NAME_MIDDLE) + m.group(3),
                     new_doc, count=1)
    new_doc = re.sub(r"(?m)^(FN:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.NAME + m.group(3), new_doc, count=1)
    new_doc = re.sub(r"(?m)^(EMAIL;TYPE=WORK,PREF:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.EMAIL + m.group(3), new_doc, count=1)
    new_doc = re.sub(r"(?m)^(EMAIL;TYPE=WORK:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.EMAIL_FIRM + m.group(3), new_doc, count=1)
    new_doc = re.sub(r"(?m)^(TEL;TYPE=WORK,VOICE:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.PHONE_TEL + m.group(3), new_doc, count=1)
    new_doc = re.sub(r"(?m)^(URL;TYPE=LinkedIn:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.LINKEDIN_URL + m.group(3), new_doc, count=1)
    new_doc = re.sub(r"(?m)^(URL;TYPE=Firm:)([^\r\n]*)(\r?)$",
                     lambda m: m.group(1) + siteconfig.FIRM_PROFILE_URL + m.group(3), new_doc, count=1)
    if new_doc != doc:
        safeio.atomic_write_text(VCARD_PATH, new_doc)


def _slice_entry(e):
    """The per-area slice shape: the fields a draft-time consumer needs, derived
    wholly from opinions.json with no timestamps, so the slices stay deterministic
    and the render-idempotency guard holds. Treatment fields ride only when the
    card carries a flag, so a stale or cautioned opinion is visible at draft time."""
    rec = {
        "cluster_id": e["cluster_id"],
        "name": e["name"],
        "court": COURT_LABELS[e["court"]],
        "date": e["date"],
        "disposition": e.get("disposition", ""),
        "areas": all_areas(e),
        "precedential": e.get("precedential", ""),
        "url": e["url"],
        "synopsis": e.get("synopsis", ""),
        "why": e.get("why", ""),
    }
    if e.get("dockets"):
        rec["dockets"] = e["dockets"]
    t = e.get("treatment")
    if t and t != "ok":
        rec["treatment"] = t
        # Prefer the human treatment_note over the machine treatment_auto_note, exactly as the HTML
        # card and permalink do -- otherwise an editor's manual correction ("good law for duty, only
        # overruled on causation") is silently replaced by the generic auto note in the /areas/*.json
        # feed that draft-time consumers read.
        note = e.get("treatment_note") or e.get("treatment_auto_note")
        if note:
            rec["treatment_note"] = note
        if e.get("treatment_date"):
            rec["treatment_date"] = e["treatment_date"]
    return rec


def _write_area_slices(entries):
    """Per-area extracts of opinions.json: one /areas/<area>.json per practice
    area, plus /areas/index.json. The drip-in source -- a drafting skill (or a
    per-area reader) pulls just its area's opinions and their treatment state.
    `entries` arrives sorted desc, so each slice is newest-first and fully
    determined by opinions.json; it rides the same idempotency guard as the pages."""
    os.makedirs(AREAS_DIR, exist_ok=True)
    index = []
    for code, label in AREA_LABELS.items():
        sel = [e for e in entries if code in all_areas(e)]
        doc = {
            "area": code,
            "label": label,
            "count": len(sel),
            "opinions": [_slice_entry(e) for e in sel],
        }
        safeio.atomic_write_text(
            os.path.join(AREAS_DIR, code + ".json"),
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        )
        index.append({"area": code, "label": label, "count": len(sel),
                      "url": "%s/areas/%s.json" % (SITE, code)})
    safeio.atomic_write_text(
        os.path.join(AREAS_DIR, "index.json"),
        json.dumps({"areas": index}, ensure_ascii=False, indent=2) + "\n",
    )


# --- README as the canonical source for the colophon's shared prose -------------------
# The README is hand-edited lyrical markdown. render derives the colophon's shared
# sections from it (converting the small markdown subset the README uses to HTML) so the
# two never diverge; the colophon then carries everything the README has plus its
# web-only extras -- the terminal lines, the support ask, and the page chrome.

README_TO_COLOPHON = {
    "stack":           "col-stack",
    "hosting":         "col-hosting",
    "under the hood":  "col-underhood",
    "what isn't here": "col-whatisnt",
    "source":          "col-source",
}


def _md_inline(s):
    """The inline markdown the README uses -> HTML. HTML-special characters in the prose
    are escaped first so the text stays literal, then links, code, strong, and em are
    converted. Deliberately small: the README's shared sections use only this subset."""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    return s


def _md_paragraphs(body, indent="      "):
    """A markdown section body -> the colophon's one-line <p> blocks: blank-line-separated
    paragraphs, each unwrapped (the README hard-wraps its prose) into a single <p>."""
    out = []
    for para in re.split(r"\n[ \t]*\n", body.strip()):
        text = " ".join(ln.strip() for ln in para.splitlines() if ln.strip())
        if text:
            out.append(indent + "<p>" + _md_inline(text) + "</p>")
    return "\n".join(out)


def _readme_sections():
    """Parse README.md into {header: body}, keyed by each '## header' line."""
    out, name, buf = {}, None, []
    for line in open(README_PATH, encoding="utf-8").read().splitlines():
        m = re.match(r"##[ \t]+(.+?)[ \t]*$", line)
        if m:
            if name is not None:
                out[name] = "\n".join(buf)
            name, buf = m.group(1).strip(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf)
    return out


def _inject_readme_into_colophon():
    """Inject each shared README section into its colophon marker, so the colophon's prose
    is a derived view of the README. Fails loud if the README is missing a shared section
    or the colophon is missing a marker -- the marker pair is the contract."""
    sections = _readme_sections()
    for header, marker in README_TO_COLOPHON.items():
        if header not in sections:
            raise RuntimeError("render: README is missing the '%s' section" % header)
        _inject(COLOPHON_PATH, marker, _md_paragraphs(sections[header]))


# --- resume.md -> resume.html -------------------------------------------------
# resume.md is the hand-editable source for the CV's main sections; the page keeps
# its header, footer, and chrome, and each section body is derived here. Same marker
# contract as the colophon. The entry sections (education, work experience) carry
# structure -- employer, optional link and practice, role or degree, dates, bullets --
# so the converter is a small CV templater, not just a prose injector.
RESUME_SECTIONS = {
    "summary":                "resume-summary",
    "education":              "resume-education",
    "bar & court admissions": "resume-bar",
    "work experience":        "resume-work",
}
RESUME_ROLE_SECTIONS = {"work experience"}   # jobs (entry-role); else schooling (degree span)
_MONTHS = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
           "jul": "07", "aug": "08", "sep": "09", "sept": "09", "oct": "10", "nov": "11",
           "dec": "12"}


def _md_link(s, link_class=""):
    """Inline markdown -> HTML for the resume: [text](url) links (http links get
    target/rel; link_class is added when given), **strong**, and *em*. Text is
    HTML-escaped with quotes and apostrophes preserved (so "DA's Office" stays literal);
    link URLs are left intact."""
    cls = (' class="%s"' % link_class) if link_class else ""
    out, last = [], 0
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", s):
        out.append(_esc(s[last:m.start()]))
        url = m.group(2)
        rel = ' target="_blank" rel="noopener noreferrer"' if url.startswith("http") else ""
        out.append('<a href="%s"%s%s>%s</a>' % (url, rel, cls, _esc(m.group(1))))
        last = m.end()
    out.append(_esc(s[last:]))
    r = "".join(out)
    r = re.sub(r"\*\*([^*]+)\*\*", lambda m: "<strong>%s</strong>" % m.group(1), r)
    r = re.sub(r"\*([^*]+)\*", lambda m: "<em>%s</em>" % m.group(1), r)
    return r


def _resume_dates(s):
    """'Mon YYYY - Mon YYYY' (en dash or hyphen accepted) -> <time> spans, emitting an
    en dash. A token that is not 'Mon YYYY' (e.g. 'Present') passes through verbatim."""
    out = []
    for tok in re.split(r"\s+[-\u2013]\s+", s.strip()):
        m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", tok)
        key = m.group(1).lower() if m else ""
        if m and key in _MONTHS:
            out.append('<time datetime="%s-%s">%s</time>' % (m.group(2), _MONTHS[key], tok))
        else:
            out.append(tok)
    return " \u2013 ".join(out)


def _resume_prose(body):
    """A prose section (summary, bar & court admissions) -> the admissions-body div,
    blank-line-separated paragraphs joined by <br><br>."""
    paras = []
    for p in re.split(r"\n[ \t]*\n", body.strip()):
        if p.strip():
            paras.append(_md_link(" ".join(ln.strip() for ln in p.splitlines() if ln.strip())))
    inner = "\n        <br><br>\n".join("        " + p for p in paras)
    return '      <div class="admissions-body">\n' + inner + "\n      </div>"


def _resume_entry(block, is_role, cfg_role=False):
    """One '### ...' entry block -> the entry div. is_role picks the job subhead
    (entry-role) over the schooling subhead (a plain degree span); cfg_role adds the
    data-cfg-text="role" identity hook to the current job. The title line is
    '[employer](url) (practice) <middot> location' (link and practice optional); the
    next line is 'role-or-degree <middot> dates'; the rest are '- ' bullets."""
    lines = block.split("\n")
    head, _, location = lines[0].lstrip()[3:].strip().rpartition(" \u00b7 ")
    lm = re.match(r"\[([^\]]+)\]\(([^)]+)\)\s*(.*)$", head)
    if lm:
        rel = ' target="_blank" rel="noopener noreferrer"' if lm.group(2).startswith("http") else ""
        emp = '<a href="%s"%s class="text-link">%s</a>' % (lm.group(2), rel, _esc(lm.group(1)))
        tail = lm.group(3).strip()
    else:
        pm = re.search(r"\s*\(([^)]+)\)\s*$", head)
        tail = "(%s)" % pm.group(1) if pm else ""
        emp = _md_link(head[:pm.start()].strip() if pm else head)
    practice_html = ""
    if tail.startswith("(") and tail.endswith(")"):
        practice_html = ' <span class="entry-practice">(%s)</span>' % _md_link(tail[1:-1])

    role_text, _, dates_text = lines[1].strip().rpartition(" \u00b7 ")
    if is_role:
        cfg = ' data-cfg-text="role"' if cfg_role else ""
        role_html = '<span class="entry-role"%s>%s</span>' % (cfg, _md_link(role_text))
    else:
        role_html = "<span>%s</span>" % _md_link(role_text)

    bullets = [ln.strip()[2:].strip() for ln in lines[2:] if ln.strip().startswith("- ")]
    L = ['      <div class="entry">',
         '        <div class="entry-head">',
         '          <span class="entry-title">%s%s</span>' % (emp, practice_html),
         '          <span class="entry-location">%s</span>' % _md_link(location),
         "        </div>",
         '        <div class="entry-subhead">',
         "          " + role_html,
         '          <span class="entry-dates">%s</span>' % _resume_dates(dates_text),
         "        </div>"]
    if bullets:
        L.append('        <ul class="entry-bullets">')
        L += ["          <li>%s</li>" % _md_link(b) for b in bullets]
        L.append("        </ul>")
    L.append("      </div>")
    return "\n".join(L)


def _resume_sections():
    """Parse resume.md into {header: body}, keyed by each '## header' line (lowercased).
    '### ' entry lines are body, not section headers."""
    out, name, buf = {}, None, []
    for line in open(RESUME_MD_PATH, encoding="utf-8").read().splitlines():
        m = re.match(r"##[ \t]+(.+?)[ \t]*$", line)
        if m:
            if name is not None:
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1).lower(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf).strip()
    return out


def _inject_resume():
    """Derive the resume's main sections from resume.md into resume.html. Fails loud if a
    section is missing from the source or its marker is missing from the page."""
    sections = _resume_sections()
    for header, marker in RESUME_SECTIONS.items():
        if header not in sections:
            raise RuntimeError("render: resume.md is missing the '%s' section" % header)
        body = sections[header]
        if "###" in body:
            is_role = header in RESUME_ROLE_SECTIONS
            blocks = [b for b in re.split(r"\n(?=###[ \t])", body.strip()) if b.lstrip().startswith("###")]
            html = "\n\n".join(_resume_entry(b, is_role, cfg_role=(is_role and i == 0))
                               for i, b in enumerate(blocks))
        else:
            html = _resume_prose(body)
        _inject(RESUME_PATH, marker, html)


def render(entries=None):
    if entries is None:
        entries = json.load(open(JSON_PATH, encoding="utf-8"))
    # A non-dict entry (a hand-edit that leaves a stray string/number in the list) would crash every
    # e.get(...) below; drop it first so the shape guards can assume a dict.
    entries = [e for e in entries if isinstance(e, dict)]
    for e in entries:
        if not _valid_date(e.get("date")):
            print("render: skipping a card with an unparseable date %r (%s)"
                  % (e.get("date"), str(e.get("name") or "?")[:50]))
    entries = [e for e in entries if _valid_date(e.get("date"))]

    # Taxonomy guard, symmetric with the date filter above: a card whose court or any
    # practice-area code is outside the registry has no label/suffix and would KeyError
    # mid-render, zeroing every page. Drop it with a warning instead so one malformed
    # card can't take the whole site down. All current cards pass, so this is inert today.
    # Type-checked because these guards run on UNVALIDATED entries: a non-string (and possibly
    # unhashable) court/area from a bad hand-edit must be dropped, not crash the `in` test.
    def _known_taxonomy(e):
        court = e.get("court")
        if not isinstance(court, str) or court not in COURT_LABELS:
            print("render: skipping a card with an unknown court %r (%s)"
                  % (court, str(e.get("name") or "?")[:50]))
            return False
        unknown = [a for a in all_areas(e) if not (isinstance(a, str) and a in AREA_LABELS)]
        if unknown:
            print("render: skipping a card with unknown area code(s) %r (%s)"
                  % (unknown, str(e.get("name") or "?")[:50]))
            return False
        return True
    entries = [e for e in entries if _known_taxonomy(e)]

    # Shape guard, symmetric with the date and taxonomy filters above: card_html / _slip_cite /
    # _no_label hard-index required fields (dockets[0], name, synopsis, why, url, disposition,
    # cluster_id), so a hand-edit to opinions.json that empties "dockets" or drops one of those
    # keys would raise mid-render and zero every page. Drop such a card with a warning instead --
    # the machine path always emits them, so this only guards human edits, exactly like the two
    # filters above. All current cards pass, so it is inert today.
    _REQUIRED_STR = ("name", "synopsis", "why", "url", "disposition")
    def _valid_shape(e):
        if e.get("cluster_id") is None:
            print("render: skipping a card with no cluster_id (%s)" % str(e.get("name") or "?")[:50])
            return False
        if not (isinstance(e.get("dockets"), list) and e["dockets"]):
            print("render: skipping a card with empty/missing dockets (%s)" % str(e.get("name") or "?")[:50])
            return False
        # Present and a string (empty is fine -- the crash is a missing key or a non-string, not
        # an empty value; over-dropping a legitimately blank field would change the render).
        missing = [k for k in _REQUIRED_STR if not isinstance(e.get(k), str)]
        if missing:
            print("render: skipping a card missing required string field(s) %r (%s)"
                  % (missing, str(e.get("name") or "?")[:50]))
            return False
        return True
    entries = [e for e in entries if _valid_shape(e)]

    entries = _sorted(entries)

    # Every entry gets a permalink, so a treated_by citer that is itself carded
    # can link to its /o/<id>. Set from the post-filter entry set the permalinks
    # are written from, so a link target always exists.
    global _CARDED_IDS
    _CARDED_IDS = {e["cluster_id"] for e in entries}

    # Per-area slices for drip-in (and per-area readers): /areas/<area>.json,
    # deterministic from opinions.json so the idempotency guard covers them.
    _write_area_slices(entries)

    # The README is the canonical lyrical source; the colophon's shared prose is derived
    # from it, and the colophon adds its web-only extras around each injected block. This
    # keeps the README and the colophon from ever diverging on the shared sections.
    _inject_readme_into_colophon()

    # resume.html's main sections are derived from resume.md the same way. This runs
    # before the STATIC_PAGES stamp loop below so _stamp_identity fills the injected
    # role hook on the current job from siteconfig.
    _inject_resume()

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
    desc = (f"AI-assisted synopses of new {siteconfig.COVERAGE} opinions, filtered for civil litigation "
            "and insurance practice. Each synopsis is AI-drafted; the linked opinion is the authority. "
            "A curated core, not a complete docket. This feed covers "
            f"the most recent {siteconfig.years_word(WINDOW_YEARS)}; older opinions are archived by year at {ARCHIVE_URL}.")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
           '  <channel>',
           '    <title>horowitz.law: Georgia Appellate Watch</title>',
           f'    <link>{SITE}/opinions</link>',
           f'    <atom:link href="{SITE}/opinions.xml" rel="self" type="application/rss+xml" />',
           f'    <description>{desc}</description>',
           '    <language>en-us</language>',
           f'    <lastBuildDate>{build}</lastBuildDate>',
           '    <generator>horowitz.law Georgia Appellate Watch (prototype)</generator>']
    out += [rss_item(e) for e in recent]
    out += ['  </channel>', '</rss>', '']
    safeio.atomic_write_text(XML_PATH, "\n".join(out))

    # The Legislative & Regulatory Watch page + feeds: sibling projections from legislation.json
    # (statutes) and regulations.json (FMCSA rulemaking), two sections of the same /legislation
    # page. Independent of the opinion entries above; the page is stamped once inside.
    render_legislation_page()

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

    # The /404 listing's top-level links, from siteconfig.PAGES, so a new page
    # surfaces there without editing 404.html. Before the STATIC_PAGES stamp loop
    # so the injected links are token- and identity-stamped with the page.
    if os.path.exists(NOTFOUND_PATH):
        _inject(NOTFOUND_PATH, "pages", _nav_block())

    # Footer year on the non-generated pages: stamped in place (no markers involved),
    # written only when the year actually changed, so this is inert all year and the
    # Jan 1 rollover rides the next render-sync or content PR.
    for p in STATIC_PAGES:
        if os.path.exists(p):
            doc = open(p, encoding="utf-8").read()
            stamped = _stamp_tokens(_stamp_year(_stamp_identity(doc)))
            if stamped != doc:
                safeio.atomic_write_text(p, stamped)

    # The generated pages (opinions/archive/stats/changes/digests) are injected rather than
    # listed in STATIC_PAGES, but their committed shell still carries the footer name (and,
    # on opinions/archive, identity meta), so run the identity stamp on them too. This
    # rewrites only the data-cfg hooks, never the injected body between the markers.
    for p in (os.path.join(WEB, f) for f in
              ("opinions.html", "archive.html", "stats.html", "changes.html", "digests.html")):
        if os.path.exists(p):
            doc = open(p, encoding="utf-8").read()
            stamped = _stamp_identity(doc)
            if stamped != doc:
                safeio.atomic_write_text(p, stamped)

    _stamp_security_txt()
    _stamp_vcard()
    _update_sitemap(recent, entries)
    return len(recent), len(entries)


def _update_sitemap(recent, entries):
    """Regenerate the sitemap's static URL block from siteconfig.PAGES and its
    permalink block from opinions.json, so adding a page to PAGES surfaces it
    here (and in the /404 listing) with no hand edit. The list, changefreq, and
    priority come from PAGES; lastmod is the hand-set date there, or the newest
    relevant date from the data for the pages that track the feed. All values are
    deterministic, so an unchanged re-render is a no-op and CI stays green.
    Skipped gracefully if the file is absent."""
    if not os.path.exists(SITEMAP_PATH):
        return
    doc = open(SITEMAP_PATH, encoding="utf-8").read()

    # Data-driven lastmods for the pages whose content tracks the feed. A page
    # left empty in PAGES but absent here falls back to the newest decision date,
    # so a new feed-backed page is coarse but never wrong.
    flagged = _flagged(entries)
    newest = entries[0]["date"] if entries else datetime.date.today().isoformat()
    newest_seen = max(((e.get("first_seen") or e.get("date") or "")[:10] for e in entries), default=newest)
    dyn = {
        "/opinions": recent[0]["date"] if recent else newest,
        "/archive":  newest,
        "/stats":    newest,
        "/digests":  newest_seen,
        "/changes":  (flagged[0].get("treatment_date") or flagged[0]["date"]) if flagged else newest,
    }
    rows = ["  <url>\n    <loc>%s%s</loc>\n    <lastmod>%s</lastmod>\n"
            "    <changefreq>%s</changefreq>\n    <priority>%s</priority>\n  </url>"
            % (SITE, path, lm or dyn.get(path) or newest, freq, prio)
            for path, label, freq, prio, lm in siteconfig.PAGES]
    new = re.sub(r"([ \t]*)(<!-- pages:start.*?-->).*?<!-- pages:end -->",
                 lambda m: m.group(1) + m.group(2) + "\n" + "\n".join(rows) + "\n" + m.group(1) + "<!-- pages:end -->",
                 doc, count=1, flags=re.S)
    # Permalink entries live between sitemap markers and regenerate wholesale.
    urls = []
    for e in entries:
        lastmod = e.get("treatment_date") or (e.get("first_seen") or "")[:10] or e["date"]
        urls.append("  <url>\n    <loc>" + SITE + "/o/%s</loc>\n    <lastmod>%s</lastmod>\n"
                    "    <changefreq>yearly</changefreq>\n    <priority>0.3</priority>\n  </url>" % (e["cluster_id"], lastmod))
    pat = re.compile(r"(<!-- permalinks:start.*?-->).*?(<!-- permalinks:end -->)", re.S)
    if pat.search(new):
        new = pat.sub(lambda m: m.group(1) + "\n" + "\n".join(urls) + "\n" + m.group(2), new, count=1)
    if new != doc:
        safeio.atomic_write_text(SITEMAP_PATH, new)

if __name__ == "__main__":
    r, t = render()
    print(f"rendered {r} recent of {t} total -> pages, feeds, stats, ledger, permalinks")
