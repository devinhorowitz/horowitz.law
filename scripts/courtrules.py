#!/usr/bin/env python3
"""Court-rules watch: amendments to the Federal Rules (FRCP / FRE / FRAP / FRBP).

The legislative watch reads statutes; the regulatory watch reads agency rules; this reads the
**Federal Rules** -- Civil Procedure, Evidence, Appellate Procedure, Bankruptcy. They are neither
statutes nor agency regulations: they change through the Rules Enabling Act (28 U.S.C. 2072). The
Judicial Conference's advisory committees propose amendments, the Supreme Court adopts them by
about May 1, and -- absent contrary action by Congress -- they take effect the following
**December 1**. So the cadence is annual and the key event is a set of amendments becoming
effective each December, published by the Administrative Office of the U.S. Courts at uscourts.gov.

This is deliberately the lightest-touch of the three watches, for two honest reasons: the source is
a web page, not an API, and the volume is a handful of rules a year. So the design trades cleverness
for resilience:

  * It reads the page as TEXT and hands that text to the model to EXTRACT the amendments, rather
    than scraping a CSS structure that breaks the first time the site is redesigned.
  * It content-HASHES each page; an unchanged page skips the model call entirely, so a run costs one
    cheap fetch until the rules actually move.
  * It FAILS OPEN: an unreachable page or a model error yields no card and a logged note, never a
    crash and never a false card. And, like the other watches, there is NO auto-publish -- every
    card is held for a human.

Court-rule cards render in a "Court rules" section of the /legislation page (see render.py). There
is deliberately no separate RSS feed: at a few items a year, the page section is the right surface.

Run locally (needs ANTHROPIC_API_KEY; the sandbox's egress may block uscourts.gov -> fail-open):
    ANTHROPIC_API_KEY=... python scripts/courtrules.py            # prints extracted amendments
    python scripts/courtrules.py --json                           # as JSON
    python scripts/courtrules.py --apply                          # persist + write the PR body

Environment:
  ANTHROPIC_API_KEY        required for the extraction call
  COURTRULES_URLS          comma list of "label|url" (or bare url) sources to read
                           (default: the uscourts.gov pending-amendments page)
  COURTRULES_MODEL         extraction model (default claude-opus-5)
  COURTRULES_BATCH         batch the Opus page-extraction pass via the 50%-priced Message Batches API
                           (default on; set 0 for the synchronous rollback). COURTRULES_BATCH_SEC
                           bounds the in-run wait (default 1800).
  COURTRULES_DEBUG         if 1, log each step
"""
import os
import sys
import re
import json
import time
import html as _html
import hashlib
import datetime
import urllib.request
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

JSON_PATH  = os.path.join(REPO, "courtrules.json")
STATE_PATH = os.path.join(REPO, "courtrules_state.json")
LOG_PATH   = os.path.join(REPO, "courtrules_log.jsonl")

UA        = "horowitz.law Court Rules Watch (contact: via horowitz.law)"
TIMEOUT   = 45
MAX_BYTES = 15 * 1024 * 1024
MAX_TEXT  = 60000   # characters of page text handed to the model

_DEFAULT_SOURCE = ("Pending amendments",
                   "https://www.uscourts.gov/forms-rules/pending-rules-and-forms-amendments")


def _sources():
    raw = os.environ.get("COURTRULES_URLS", "")
    if not raw.strip():
        return [_DEFAULT_SOURCE]
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "|" in item:
            label, url = item.split("|", 1)
            out.append((label.strip(), url.strip()))
        else:
            out.append((item, item))
    return out or [_DEFAULT_SOURCE]


SOURCES = _sources()
MODEL = os.environ.get("COURTRULES_MODEL", "claude-opus-5")
# The pending-amendments page can list many rules at once (a full FRCP/FRE/FRAP/FRBP cycle), so the
# extraction JSON needs generous room; 1500 truncated mid-list. Configurable for a heavy year.
EXTRACT_MAX_TOKENS = int(os.environ.get("COURTRULES_MAX_TOKENS", "8000"))
DEBUG = os.environ.get("COURTRULES_DEBUG", "") == "1"
# Batch the (Opus) page-extraction pass through the 50%-priced Message Batches API, like the other
# watches (COURTRULES_BATCH, default on). Volume is tiny -- most runs make zero calls (the page is
# content-hashed) and the amendment cycle is a handful of pages a year -- but the 50% discount is
# unconditional and latency does not matter here, so there is no reason to pay full price. Set
# COURTRULES_BATCH=0 for the synchronous path. BATCH_SEC bounds the in-run wait before deferring.
COURTRULES_BATCH = os.environ.get("COURTRULES_BATCH", "on").strip().lower() in ("1", "true", "yes", "on")
BATCH_SEC = int(os.environ.get("COURTRULES_BATCH_SEC", "1800"))

# Rule sets a civil litigator cares about; the extractor is told to ignore criminal-only rules.
_RULE_SETS = {"FRCP", "FRE", "FRAP", "FRBP"}

# Weak content markers that the real pending-amendments page always carries (the acronyms or the
# spelled-out rule names, plus the word "rule"). Used to tell "page fetched, genuinely no amendments"
# (markers present, an empty extraction is trustworthy and recorded seen) from "page fetched but
# contentless" -- a JS-only shell, a redesign, or a moved page (markers absent). The latter must NOT
# be recorded seen off an empty extraction, or the stable shell hash sticks and the page is never
# re-examined even when the real amendments (which land every December) are live.
_PAGE_MARKERS = tuple(s.lower() for s in _RULE_SETS) + (
    "federal rules of civil procedure", "rules of evidence", "appellate procedure",
    "bankruptcy procedure", "rules enabling act", "pending amendment",
)


def has_rules_markers(text):
    """True if `text` looks like the Federal Rules amendments content (mentions a rule set and the
    word 'rule'), vs. a contentless shell/redesign. Deliberately lenient -- structure-agnostic, like
    strip_html -- to avoid flagging a genuine page whose wording shifts."""
    low = (text or "").lower()
    return ("rule" in low) and any(m in low for m in _PAGE_MARKERS)


def _dbg(msg):
    if DEBUG:
        print("  . " + msg)


# --------------------------------------------------------------------------- #
# Network + text.                                                              #
# --------------------------------------------------------------------------- #
def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8", "replace")


_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n\s*\n+")


def strip_html(doc):
    """Reduce an HTML page to readable text: drop script/style, turn block tags into newlines,
    strip the rest, unescape entities, and collapse whitespace. Structure-agnostic on purpose --
    it survives a site redesign that would break any CSS-selector scrape."""
    if not doc:
        return ""
    doc = _SCRIPT_RE.sub(" ", doc)
    doc = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|br)\s*>", "\n", doc)
    doc = re.sub(r"(?i)<br\s*/?>", "\n", doc)
    doc = _TAG_RE.sub(" ", doc)
    doc = _html.unescape(doc)
    doc = _WS_RE.sub(" ", doc)
    doc = _NL_RE.sub("\n\n", doc)
    return doc.strip()


def page_hash(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def fetch_text(url, fetch=None):
    """Fetch a URL and return its readable text, or '' on any error (fail-open)."""
    fetch = fetch or _http_get
    try:
        return strip_html(fetch(url))
    except Exception as e:   # fail-open: any fetch error yields empty text (page treated unreachable)
        _dbg("fetch %s failed: %s" % (url, e))
        return ""


# --------------------------------------------------------------------------- #
# Extraction (the funnel is one model call per CHANGED page).                  #
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM = (
    "You extract amendments to the Federal Rules from the text of a U.S. Courts (uscourts.gov) web "
    "page, for a civil-litigation audience. Report ONLY rules a civil litigator relies on: the "
    "Federal Rules of Civil Procedure (FRCP), Evidence (FRE), Appellate Procedure (FRAP), and "
    "Bankruptcy (FRBP). IGNORE criminal-only rules (FRCrP) unless the same amendment also changes a "
    "civil rule. For each amendment the page describes, give: rule_set (one of FRCP, FRE, FRAP, "
    "FRBP), rule (e.g. 'Rule 26' or 'Rule 702'), a one-sentence plain-English summary of what "
    "changes, status ('pending' or 'effective'), effective_date (YYYY-MM-DD if the page states one, "
    "else empty), and impact (one sentence on why a litigator should care). Ground every field in "
    "the page text; do not invent a rule, date, or change that is not there. If the page names no "
    "such amendment, return an empty list. Reply with ONLY a JSON object: {\"amendments\": "
    "[{\"rule_set\":..., \"rule\":..., \"summary\":..., \"status\":..., \"effective_date\":..., "
    "\"impact\":...}]}."
)


def _extract_body(text, model=None):
    """Messages body for one page's amendment extraction. Shared by the synchronous extract() and the
    batch path, so both build byte-identical requests."""
    return {"model": model or MODEL, "max_tokens": EXTRACT_MAX_TOKENS, "system": EXTRACT_SYSTEM,
            "messages": [{"role": "user", "content": "PAGE TEXT:\n" + (text or "")[:MAX_TEXT]}]}


def _extract_parse(v):
    """Parse an extraction response into a list of amendment dicts; [] when none are named. Shared by
    the sync and batch paths."""
    ams = v.get("amendments")
    if not isinstance(ams, list):
        return []
    return [a for a in ams if isinstance(a, dict)]


def extract(text, ai, model=None, label="courtrules"):
    """Extract amendments from one page's text. Returns a list of dicts, or None on a model/parse
    error (so the caller can leave the page un-hashed and retry next run). [] means 'read fine, no
    relevant amendments named'."""
    if not (text or "").strip():
        return []
    try:
        v = ai(_extract_body(text, model), label)
    except Exception as e:
        _dbg("extract failed: %s" % e)
        return None
    return _extract_parse(v)


def _draft_extractions(pending, deadline=None):
    """Extract the amendments for the CHANGED pages in `pending` (each {url, text, ...}) as ONE
    50%-priced Message Batches job (COURTRULES_BATCH). Returns {url: [amendments] | None}, the SAME
    per-page space extract() produces, so run()'s downstream (hash + card) logic does not branch.
    A whole-batch timeout/transport failure -> every page None (retry next run, un-hashed); a
    per-line error or unparseable body -> that one page None."""
    import batch
    import update
    reqs, meta = [], {}    # custom_id -> url
    for i, p in enumerate(pending):
        cid = "cr-%d" % i   # url is not a valid custom_id (^[A-Za-z0-9_-]{1,64}$); index and map back
        reqs.append(batch.from_body(cid, _extract_body(p["text"])))
        meta[cid] = p["url"]
    try:
        results = batch.run(reqs, deadline=deadline, label="courtrules-extract")
    except (batch.BatchTimeout, batch.BatchError) as e:
        print("  ! courtrules extract batch deferred (%s); %d page(s) retry next run"
              % (e, len(pending)), flush=True)
        return {p["url"]: None for p in pending}
    out = {}
    for cid, url in meta.items():
        res = results.get(cid)
        if not res or not res.get("ok"):
            out[url] = None
            continue
        try:
            out[url] = _extract_parse(update.parse_json(res["text"]))
        except Exception:
            out[url] = None
    return out


def _card_id(rule_set, rule):
    # Identity is (rule set, rule number) -- NOT the effective date. A rule's amendment progresses
    # pending -> effective (and its date may be firmed up along the way); keying on the date too
    # would mint a second card for the same amendment instead of updating the first. One card per
    # rule, updated in place as its status/date settle.
    key = "%s|%s" % ((rule_set or "").upper(), (rule or "").strip())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def build_card(amendment, url, today=None):
    """Assemble a court-rule card from one extracted amendment. Keyed on a stable synthetic id
    (rule_set|rule), since a rule amendment has no natural document number. Returns None if the
    amendment names no recognizable rule set (guards against a stray extraction)."""
    rule_set = (amendment.get("rule_set") or "").strip().upper()
    if rule_set not in _RULE_SETS:
        return None
    today = (today or datetime.date.today()).isoformat()
    eff = str(amendment.get("effective_date") or "").strip()
    if not (len(eff) == 10 and eff[4] == "-" and eff[7] == "-"):
        eff = ""
    status = (amendment.get("status") or "").strip().lower()
    status = status if status in ("pending", "effective") else "pending"
    rule = str(amendment.get("rule") or "").strip()
    return {
        "id": _card_id(rule_set, rule),
        "rule_set": rule_set,
        "rule": rule,
        "status": status,
        "effective_date": eff,
        "summary": str(amendment.get("summary") or "").strip(),
        "impact": str(amendment.get("impact") or "").strip(),
        "url": str(url or "").strip(),
        "first_seen": today,
    }


# --------------------------------------------------------------------------- #
# Orchestration.                                                              #
# --------------------------------------------------------------------------- #
def _default_ai(body, label="call"):
    import update
    return update.anthropic_json(body, label)


def run(fetch=None, ai=None, today=None, sources=None, batch_enabled=False):
    """Read each source page; on a CHANGED page, extract amendments and card the new ones. Returns
    (cards, notes, seen_updates) where seen_updates = {"pages": {url: hash}, "cards": {id: date}}.
    A page is hashed as seen only after a SUCCESSFUL extraction, so a transient fetch/model error
    retries next run. `batch_enabled` runs the Opus extraction over all changed pages as ONE batch
    job (COURTRULES_BATCH); it defaults False so a direct/test caller with an injected `ai` gets the
    synchronous path unchanged, and main() passes the flag. Writes nothing itself. Fail-open."""
    ai = ai or _default_ai
    sources = sources or SOURCES
    notes = []
    seen = _load_seen()
    seen_pages = seen.get("pages") or {}
    seen_cards = seen.get("cards") or {}
    new_pages, new_cards, cards = {}, {}, []
    today_iso = (today or datetime.date.today()).isoformat()

    # Phase 1: fetch + hash + marker-check each source; collect the CHANGED, content-valid pages.
    pending = []   # {label, url, text, h}
    for label, url in sources:
        text = fetch_text(url, fetch)
        if not text:
            notes.append("COURTRULES: %s unreachable; will retry." % label)
            continue
        h = page_hash(text)
        if seen_pages.get(url) == h:
            notes.append("COURTRULES: %s unchanged." % label)
            new_pages[url] = h
            continue
        if not has_rules_markers(text):
            # Fetched, but it does not look like the amendments content (a JS-only shell, a redesign,
            # or a moved page). Recording an empty extraction here as seen would stick the shell hash
            # and the page would never be re-examined. Treat it like a transient failure: do NOT hash
            # it, and surface it so a silent stall becomes a visible, recurring note.
            notes.append("COURTRULES: %s fetched but shows no Federal Rules markers "
                         "(shell/redesign/moved?); not recording, will retry." % label)
            continue
        pending.append({"label": label, "url": url, "text": text, "h": h})

    # Phase 2: extract the changed pages -- one batch job, or synchronously per page. Same {url: ams}
    # space either way (ams is a list, or None on a transient error that must retry un-hashed).
    if batch_enabled and pending:
        notes.append("COURTRULES: batching %d page extraction(s) (COURTRULES_BATCH)." % len(pending))
        extractions = _draft_extractions(pending, deadline=time.time() + BATCH_SEC)
    else:
        extractions = {p["url"]: extract(p["text"], ai) for p in pending}

    # Phase 3: card the amendments from each successfully-extracted page.
    for p in pending:
        label, url, h = p["label"], p["url"], p["h"]
        ams = extractions.get(url)
        if ams is None:
            notes.append("COURTRULES: %s extraction failed; will retry." % label)
            continue                       # do NOT record the hash: retry next run
        new_pages[url] = h                 # page read + extracted: record it
        added_here = 0
        for a in ams:
            card = build_card(a, url, today=today)
            if not card:
                continue
            cid = card["id"]
            if cid in seen_cards or cid in new_cards:
                continue
            cards.append(card)
            new_cards[cid] = today_iso
            added_here += 1
        notes.append("COURTRULES: %s changed; %d amendment(s), %d new." % (label, len(ams), added_here))
    notes.append("COURTRULES: drafted %d card(s)." % len(cards))
    return cards, notes, {"pages": new_pages, "cards": new_cards}


# --------------------------------------------------------------------------- #
# State + persistence helpers.                                               #
# --------------------------------------------------------------------------- #
def _load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"pages": {}, "cards": {}}
        return {"pages": data.get("pages") or {}, "cards": data.get("cards") or {}}
    except FileNotFoundError:
        return {"pages": {}, "cards": {}}
    except Exception:
        return {"pages": {}, "cards": {}}


def load_cards():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_cards(cards):
    import safeio
    safeio.atomic_write_text(JSON_PATH, json.dumps(cards, ensure_ascii=False, indent=2) + "\n")


def save_seen(seen, today=None):
    import safeio
    today = (today or datetime.date.today()).isoformat()
    safeio.atomic_write_text(
        STATE_PATH,
        json.dumps({"pages": seen.get("pages") or {}, "cards": seen.get("cards") or {},
                    "updated": today}, ensure_ascii=False, indent=2) + "\n")


def append_log(rec, cap=1000):
    import safeio
    try:
        lines = []
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            pass
        lines.append(json.dumps(rec, ensure_ascii=False))
        safeio.atomic_write_text(LOG_PATH, "\n".join(lines[-cap:]) + "\n")
    except Exception as e:
        _dbg("log append failed: %s" % e)


def merge_cards(existing, new_cards):
    """Merge new cards into the existing list, keyed on id, preserving first_seen. Newest
    effective_date first, then rule set/number."""
    by_id = {}
    for c in existing:
        if isinstance(c, dict) and c.get("id"):
            by_id[c["id"]] = c
    added = updated = 0
    for c in new_cards:
        if c["id"] in by_id:
            c = dict(c, first_seen=by_id[c["id"]].get("first_seen") or c.get("first_seen"))
            updated += 1
        else:
            added += 1
        by_id[c["id"]] = c
    merged = sorted(by_id.values(),
                    key=lambda c: (c.get("effective_date") or "", c.get("rule_set") or "", c.get("rule") or ""),
                    reverse=True)
    return merged, added, updated


def merge_seen(base, updates):
    """Fold a run's seen_updates into the stored seen map."""
    pages = dict(base.get("pages") or {})
    cards = dict(base.get("cards") or {})
    pages.update(updates.get("pages") or {})
    cards.update(updates.get("cards") or {})
    return {"pages": pages, "cards": cards}


def _pr_body(added, updated, cards):
    lines = ["## Court Rules Watch: Federal Rules amendments", "",
             "The watch found amendments to the Federal Rules (Civil Procedure, Evidence, Appellate, "
             "Bankruptcy) on uscourts.gov. **Every card is held for your review** — read each against "
             "the source, edit `courtrules.json` on this branch if needed, and merge to publish.", "",
             "| Rule set | Rule | Status | Effective |", "|---|---|---|---|"]
    for c in cards:
        lines.append("| %s | %s | %s | %s |" % (
            c.get("rule_set") or "?", c.get("rule") or "?", c.get("status") or "?",
            c.get("effective_date") or "—"))
    lines += ["", "%d new, %d updated." % (added, updated), "",
              "_AI-extracted from uscourts.gov; the official rule text is the authority._"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    apply = "--apply" in argv
    cards, notes, seen_updates = run(batch_enabled=COURTRULES_BATCH)   # COURTRULES_BATCH default on
    for n in notes:
        print(n)

    if apply:
        existing = load_cards()
        merged, added, updated = merge_cards(existing, cards)
        seen = merge_seen(_load_seen(), seen_updates)
        content_changed = bool(added or updated)
        if content_changed:
            save_cards(merged)
        save_seen(seen)
        append_log({"cards": len(cards), "added": added, "updated": updated,
                    "pages": len(seen.get("pages") or {}), "notes": notes})
        if content_changed:
            import safeio
            safeio.atomic_write_text(os.path.join(REPO, "scripts", "pr_body_courtrules.md"),
                                     _pr_body(added, updated, cards))
        print("COURTRULES_CONTENT_CHANGED=%s" % ("1" if content_changed else "0"))
        print("COURTRULES: %d added, %d updated." % (added, updated))

    if as_json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))
    elif not apply:
        for c in cards:
            print("  %-5s %-10s %-9s %s" % (c["rule_set"], c["rule"], c["status"],
                                            (c["summary"] or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
