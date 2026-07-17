#!/usr/bin/env python3
"""Federal regulatory watch: FMCSA (and kin) rulemaking, filtered for a civil practice.

The legislative watch reads statutes; this reads *agency regulations*. Statutes come from a
legislature; regulations come from an agency and live in the Federal Register, not in any bill
tracker -- which is why they are a separate source from legislation.py. For a Georgia
trucking / personal-injury / insurance-defense practice the agency that matters most is the
**FMCSA** (Federal Motor Carrier Safety Administration), whose rules live in 49 CFR 350-399:
hours of service, driver qualification and CDL, vehicle inspection and maintenance, drug and
alcohol testing, carrier/broker liability, and financial-responsibility (insurance) minimums.
A new safety standard can anchor a negligence-per-se theory; a financial-responsibility change
moves the available coverage. Those are the rules worth a card.

The source is the **Federal Register API** (federalregister.gov/developers/documentation/api/v1):
public, keyless, JSON. We query the documents endpoint filtered to the agency and to FINAL RULES
(the regulatory analog of a statute that became law), read the abstract + CFR references the
endpoint already returns (so a run is a single paginated fetch, no per-document call), and run the
same cheapest-first funnel as the other watches. A Federal Register document is IMMUTABLE once
published -- its document_number never changes content -- so 'seen' is just a set of processed
document_numbers, no change_hash needed.

Design mirrors legislation.py: standard library only, the network and model calls are injectable
seams (`fetch=`, `ai=`) so the whole funnel is unit-testable with no network, and everything FAILS
OPEN -- a Federal Register outage or a model error yields an empty run and a logged note, never a
crash and never a false card. Like legislation, there is NO auto-publish: every card is held for a
human. Cards render on the /legislation page under a "Federal regulations" section (see render.py).

Congress's FMCSA-*authorizing statutes* ride the legislation watch (state US); this watches the
regulations the FMCSA actually issues. The two are complementary halves of "federal trucking law
that moved."

Run locally (dry; the Federal Register API needs no key, but this sandbox's egress may block it):
    ANTHROPIC_API_KEY=... python scripts/regulations.py            # prints drafted cards
    python scripts/regulations.py --json                           # cards as JSON
    python scripts/regulations.py --apply                          # persist + write the PR body

Environment:
  ANTHROPIC_API_KEY        required for the relevance screen and the writer
  REGULATION_AGENCIES      comma list of Federal Register agency slugs
                           (default "federal-motor-carrier-safety-administration")
  REGULATION_TYPES         comma list of FR document types: RULE (final), PRORULE (proposed)
                           (default "RULE")
  REGULATION_LOOKBACK_DAYS how many days back to scan on each run (default 45)
  REGULATION_SCREEN_MODEL  relevance screen model (default claude-haiku-4-5)
  REGULATION_MODEL         card writer model (default claude-opus-4-8)
  REGULATION_MAX           cap on CARDS drafted per run (default 40); a run stops once this many
                           rules have been carded (FMCSA's low volume needs no separate screen cap)
  REGULATION_DEBUG         if 1, log each step
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import siteconfig  # shared practice-area taxonomy  # noqa: E402

JSON_PATH  = os.path.join(REPO, "regulations.json")
STATE_PATH = os.path.join(REPO, "regulations_state.json")
LOG_PATH   = os.path.join(REPO, "regulations_log.jsonl")

FR_API     = "https://www.federalregister.gov/api/v1/documents.json"
UA         = "horowitz.law Federal Regulatory Watch (contact: via horowitz.law)"
TIMEOUT    = 45
MAX_BYTES  = 25 * 1024 * 1024

AGENCIES = [s.strip() for s in os.environ.get(
    "REGULATION_AGENCIES", "federal-motor-carrier-safety-administration").split(",") if s.strip()]
TYPES = [s.strip().upper() for s in os.environ.get("REGULATION_TYPES", "RULE").split(",") if s.strip()]
LOOKBACK_DAYS = int(os.environ.get("REGULATION_LOOKBACK_DAYS", "45"))
SCREEN_MODEL  = os.environ.get("REGULATION_SCREEN_MODEL", "claude-haiku-4-5")
WRITE_MODEL   = os.environ.get("REGULATION_MODEL", "claude-opus-4-8")
MAX_RUN       = int(os.environ.get("REGULATION_MAX", "40"))
DEBUG         = os.environ.get("REGULATION_DEBUG", "") == "1"
PAGES_MAX     = int(os.environ.get("REGULATION_PAGES_MAX", "10"))  # safety cap on pagination

# The Federal Register returns type "Rule" for a final rule and "Proposed Rule" for an NPRM; the
# query FILTER uses RULE / PRORULE. Normalize the returned value to a display label.
_TYPE_LABEL = {"Rule": "Final Rule", "Proposed Rule": "Proposed Rule",
               "Notice": "Notice", "Presidential Document": "Presidential Document"}
# Short agency labels for the card badge; an unknown slug falls back to the raw agency name.
_AGENCY_LABEL = {
    "federal-motor-carrier-safety-administration": "FMCSA",
    "national-highway-traffic-safety-administration": "NHTSA",
    "pipeline-and-hazardous-materials-safety-administration": "PHMSA",
}
# The endpoint fields we need; requesting only these keeps the payload small.
_FIELDS = ["document_number", "title", "type", "abstract", "action", "publication_date",
           "effective_on", "html_url", "pdf_url", "cfr_references", "agencies", "agency_names",
           "regulation_id_numbers"]

AREA_CODES_STR = ", ".join(siteconfig.AREA_CODES)


def _dbg(msg):
    if DEBUG:
        print("  . " + msg)


# --------------------------------------------------------------------------- #
# Network seam: the Federal Register documents endpoint.                       #
# --------------------------------------------------------------------------- #
def _http_get(url):
    """Default fetch seam: GET a URL, return decoded text. Byte-capped. Tests inject their own."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8", "replace")


def _query_url(agencies, types, since, page=1, per_page=100):
    """Build one Federal Register documents.json query URL. Repeated bracketed params
    (conditions[agencies][], conditions[type][], fields[]) are encoded as a tuple list."""
    pairs = []
    for a in agencies:
        pairs.append(("conditions[agencies][]", a))
    for t in types:
        pairs.append(("conditions[type][]", t))
    if since:
        pairs.append(("conditions[publication_date][gte]", since))
    for f in _FIELDS:
        pairs.append(("fields[]", f))
    pairs += [("order", "newest"), ("per_page", str(per_page)), ("page", str(page))]
    return FR_API + "?" + urllib.parse.urlencode(pairs)


def fetch_documents(agencies=None, types=None, since=None, fetch=None, pages_max=None):
    """Return the Federal Register result documents for the agencies/types since a date, following
    pagination up to pages_max. Fail-open: returns whatever was gathered before an error (or [] on
    the first-page error), so a mid-pagination failure degrades rather than crashing the run."""
    agencies = agencies or AGENCIES
    types = types or TYPES
    fetch = fetch or _http_get
    pages_max = PAGES_MAX if pages_max is None else pages_max
    out = []
    for page in range(1, pages_max + 1):
        url = _query_url(agencies, types, since, page=page)
        try:
            data = json.loads(fetch(url))
        except Exception as e:
            _dbg("fetch page %d failed: %s" % (page, e))
            break
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            break
        out.extend(r for r in results if isinstance(r, dict) and r.get("document_number"))
        # Stop when the API says there is no next page, or we have caught the last page.
        try:
            total_pages = int(data.get("total_pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages or not data.get("next_page_url"):
            break
    return out


def new_documents(docs, seen):
    """Documents not yet processed. `seen` maps document_number -> publication_date (any truthy
    membership counts). A Federal Register document is immutable, so once seen it never returns."""
    return [d for d in docs if str(d.get("document_number")) not in seen]


# --------------------------------------------------------------------------- #
# Formatting helpers.                                                          #
# --------------------------------------------------------------------------- #
def cfr_label(doc):
    """A compact '49 CFR 395, 396' from the cfr_references array ([{title, part}, ...]).
    Deduped, title-then-part ordered. Returns '' if none."""
    refs = doc.get("cfr_references") or []
    seen, parts = set(), []
    for r in refs:
        if not isinstance(r, dict):
            continue
        title, part = r.get("title"), r.get("part")
        key = (title, part)
        if title is None or key in seen:
            continue
        seen.add(key)
        parts.append((title, part))
    parts.sort(key=lambda tp: (tp[0] or 0, tp[1] or 0))
    # Group by CFR title: "49 CFR 387, 395".
    by_title = {}
    for title, part in parts:
        by_title.setdefault(title, [])
        if part is not None:
            by_title[title].append(str(part))
    chunks = []
    for title in sorted(by_title):
        ps = ", ".join(by_title[title])
        chunks.append(("%s CFR %s" % (title, ps)) if ps else ("%s CFR" % title))
    return "; ".join(chunks)


def agency_label(doc):
    """Short badge label for the issuing agency (FMCSA, NHTSA, ...), from the agency slug when
    known, else the first agency's name, else ''."""
    for a in (doc.get("agencies") or []):
        if isinstance(a, dict):
            slug = a.get("slug") or ""
            if slug in _AGENCY_LABEL:
                return _AGENCY_LABEL[slug]
    names = doc.get("agency_names") or []
    if names:
        return str(names[0])
    for a in (doc.get("agencies") or []):
        if isinstance(a, dict) and a.get("name"):
            return str(a["name"])
    return ""


def _type_label(doc):
    return _TYPE_LABEL.get((doc.get("type") or "").strip(), (doc.get("type") or "").strip() or "Rule")


def _first_rin(doc):
    rins = doc.get("regulation_id_numbers") or []
    for r in rins:
        if isinstance(r, str) and r.strip():
            return r.strip()
    return ""


# --------------------------------------------------------------------------- #
# The funnel: relevance screen (cheap) then the card writer (expensive).       #
# --------------------------------------------------------------------------- #
SCREEN_SYSTEM = (
    "You are a triage filter for a Georgia civil-litigation and insurance-defense practice with a "
    "focus on trucking and motor-carrier cases. You are shown a FEDERAL AGENCY RULE (usually an "
    "FMCSA final rule) from the Federal Register. Decide whether it changes something such a "
    "practice would actually rely on: motor-carrier safety standards (hours of service, driver "
    "qualification / CDL, vehicle inspection and maintenance, drug and alcohol testing), carrier or "
    "broker liability, financial-responsibility / insurance minimums (49 CFR 387), hazardous-"
    "materials transport, or crash-data / recordkeeping standards that bear on negligence or "
    "spoliation. Be moderately strict: DROP purely administrative or technical rules -- registration "
    "and filing fees, civil-penalty inflation adjustments, ELD device technical specifications, "
    "information-collection notices, technical corrections, and agency IT/registry mechanics -- "
    "UNLESS they change a substantive duty or standard. Reply with ONLY a JSON object: "
    "{\"relevant\": true|false, \"areas\": [codes], \"reason\": \"<=15 words\"}. Valid area codes: "
    + AREA_CODES_STR + "."
)

WRITE_SYSTEM = (
    "You write for a Georgia civil-litigation and insurance-defense (trucking) audience of "
    "practicing lawyers. Given a federal agency rule from the Federal Register, write a tight, "
    "neutral, plain-English card: what duty or standard in the Code of Federal Regulations it "
    "changes, its effective date, and why a trucking or injury litigator should care -- for example, "
    "a new safety standard can anchor a negligence-per-se theory, or a financial-responsibility "
    "change moves the coverage available. If the document is a PROPOSED rule, say plainly that it is "
    "not yet binding. No hype, no editorializing. Ground every statement in the provided title, "
    "action, CFR references, and abstract -- do not invent CFR sections, effective dates, or dollar "
    "figures that are not present. Reply with ONLY a JSON object: {\"keep\": true|false, "
    "\"areas\": [codes], \"synopsis\": \"2-4 sentences\", \"impact\": \"one sentence, <=30 words\", "
    "\"effective_date\": \"YYYY-MM-DD or empty if not stated\"}. Set keep=false if, on a full read, "
    "the rule does not in fact affect the practice. Valid area codes: " + AREA_CODES_STR + "."
)


def _doc_brief(doc):
    """The compact text block both model tiers read: identity + CFR + dates + abstract."""
    parts = [
        "Agency: %s" % (agency_label(doc) or "?"),
        "Document type: %s" % _type_label(doc),
        "Title: %s" % (doc.get("title") or ""),
        "Action: %s" % (doc.get("action") or ""),
        "CFR: %s" % (cfr_label(doc) or "(none listed)"),
        "Published: %s" % (doc.get("publication_date") or ""),
        "Effective: %s" % (doc.get("effective_on") or "(not stated)"),
        "Abstract: %s" % (doc.get("abstract") or ""),
    ]
    return "\n".join(p for p in parts if p)


def screen_doc(doc, ai, model=None):
    """Cheap relevance gate. Returns (keep, areas, reason). Fail-open on a model/parse error:
    keep the rule (route it to the writer) rather than silently dropping a real regulation."""
    model = model or SCREEN_MODEL
    try:
        v = ai({"model": model, "max_tokens": 256, "system": SCREEN_SYSTEM,
                "messages": [{"role": "user", "content": _doc_brief(doc)}]}, "reg-screen")
    except Exception as e:
        _dbg("screen failed, keeping: %s" % e)
        return True, [], "screen-error-kept"
    keep = v.get("relevant") is True
    areas = [a for a in (v.get("areas") or []) if a in siteconfig.AREA_CODES]
    return keep, areas, str(v.get("reason") or "")[:120]


# Sentinel: a TRANSIENT writer failure (retry next run, do not record seen) vs a DEFINITIVE decline
# (the model read it and said no -- record seen so we never re-screen it). Same discipline as
# legislation.py: a bare None conflates the two.
WRITER_ERROR = object()


def write_card(doc, ai, model=None):
    """The card writer. Returns a verdict dict on a keep, None on a DEFINITIVE decline, or the
    WRITER_ERROR sentinel on a transient model/parse error. Never a partial card."""
    model = model or WRITE_MODEL
    try:
        v = ai({"model": model, "max_tokens": 900, "system": WRITE_SYSTEM,
                "messages": [{"role": "user", "content": _doc_brief(doc)}]}, "reg-write")
    except Exception as e:
        _dbg("write failed: %s" % e)
        return WRITER_ERROR
    if v.get("keep") is not True:
        return None
    if not str(v.get("synopsis") or "").strip():
        return None
    return v


def build_card(doc, verdict, today=None):
    """Assemble a regulation card from a Federal Register document and the writer verdict.
    Keyed on the immutable document_number."""
    today = (today or datetime.date.today()).isoformat()
    areas = [a for a in (verdict.get("areas") or []) if a in siteconfig.AREA_CODES]
    eff = str(verdict.get("effective_date") or "").strip()
    if not (len(eff) == 10 and eff[4] == "-" and eff[7] == "-"):
        # Fall back to the endpoint's own effective_on when the writer did not surface one.
        eff = str(doc.get("effective_on") or "").strip()
        if not (len(eff) == 10 and eff[4] == "-" and eff[7] == "-"):
            eff = ""
    return {
        "document_number": str(doc.get("document_number")),
        "agency": agency_label(doc),
        "type": _type_label(doc),
        "title": (doc.get("title") or "").strip(),
        "cfr": cfr_label(doc),
        "rin": _first_rin(doc),
        "action": (doc.get("action") or "").strip(),
        "publication_date": (doc.get("publication_date") or "").strip(),
        "effective_date": eff,
        "areas": areas,
        "synopsis": str(verdict.get("synopsis") or "").strip(),
        "impact": str(verdict.get("impact") or "").strip(),
        "url": (doc.get("html_url") or "").strip(),
        "first_seen": today,
    }


# --------------------------------------------------------------------------- #
# Orchestration.                                                              #
# --------------------------------------------------------------------------- #
def _since(today=None, lookback=None):
    today = today or datetime.date.today()
    lookback = LOOKBACK_DAYS if lookback is None else lookback
    return (today - datetime.timedelta(days=lookback)).isoformat()


def _default_ai(body, label="call"):
    """Default model seam: delegate to update.anthropic_json (same JSON contract). Imported lazily
    so this module imports cleanly with no ANTHROPIC_API_KEY and tests never pull update.py in."""
    import update
    return update.anthropic_json(body, label)


def run(fetch=None, ai=None, today=None, max_run=None, lookback=None):
    """Full funnel: fetch recent agency rules, screen, write. Returns (cards, notes, seen_updates).
    `seen_updates` maps document_number -> publication_date for every rule that reached a DEFINITIVE
    outcome (carded, or read and declined). A transient error leaves it absent so it retries next
    run. Writes nothing itself. Fail-open throughout. No API key needed for the Federal Register."""
    ai = ai or _default_ai
    max_run = MAX_RUN if max_run is None else max_run
    notes = []
    seen_updates = {}
    cards = []
    seen = _load_seen()
    docs = fetch_documents(since=_since(today, lookback), fetch=fetch)
    fresh = new_documents(docs, seen)
    notes.append("REGULATION: %d document(s) in window, %d new." % (len(docs), len(fresh)))
    for d in fresh:
        if len(cards) >= max_run:
            notes.append("REGULATION: hit REGULATION_MAX=%d; remaining rules retry next run." % max_run)
            break
        dn, pub = str(d.get("document_number")), (d.get("publication_date") or "")
        keep, areas, reason = screen_doc(d, ai)
        if not keep:
            _dbg("screen dropped %s: %s" % (dn, reason))
            seen_updates[dn] = pub
            continue
        verdict = write_card(d, ai)
        if verdict is WRITER_ERROR:
            continue
        if verdict is None:
            seen_updates[dn] = pub
            continue
        if not verdict.get("areas") and areas:
            verdict["areas"] = areas
        cards.append(build_card(d, verdict, today=today))
        seen_updates[dn] = pub
    notes.append("REGULATION: drafted %d card(s)." % len(cards))
    return cards, notes, seen_updates


# --------------------------------------------------------------------------- #
# State + persistence helpers.                                               #
# --------------------------------------------------------------------------- #
def _load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        seen = data.get("seen") if isinstance(data, dict) else None
        return seen if isinstance(seen, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


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
        json.dumps({"seen": seen, "updated": today, "count": len(seen)},
                   ensure_ascii=False, indent=2) + "\n")


def append_log(rec, cap=2000):
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
    """Merge new cards into the existing list, keyed on document_number, preserving first_seen.
    Returns (merged, added, updated), newest publication_date first."""
    by_id = {}
    for c in existing:
        if isinstance(c, dict) and c.get("document_number"):
            by_id[str(c["document_number"])] = c
    added = updated = 0
    for c in new_cards:
        dn = str(c["document_number"])
        if dn in by_id:
            c = dict(c, first_seen=by_id[dn].get("first_seen") or c.get("first_seen"))
            updated += 1
        else:
            added += 1
        by_id[dn] = c
    merged = sorted(by_id.values(),
                    key=lambda c: (c.get("publication_date") or "", str(c.get("document_number") or "")),
                    reverse=True)
    return merged, added, updated


def _pr_body(added, updated, cards):
    lines = ["## Federal Regulatory Watch: new agency rules", "",
             "The watch found federal agency rules (FMCSA and kin) relevant to a Georgia trucking / "
             "civil-litigation practice. **Every card is held for your review** — nothing publishes "
             "on the machine alone. Read each against the Federal Register document, edit "
             "`regulations.json` on this branch if needed, and merge to publish.", "",
             "| Agency | Type | CFR | Rule |", "|---|---|---|---|"]
    for c in cards:
        lines.append("| %s | %s | %s | [%s](%s) |" % (
            c.get("agency") or "?", c.get("type") or "?", c.get("cfr") or "—",
            (c.get("title") or "")[:80], c.get("url") or ""))
    lines += ["", "%d new, %d updated." % (added, updated), "",
              "_AI-drafted summaries; the Federal Register document is the authority._"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    apply = "--apply" in argv
    cards, notes, seen_updates = run()
    for n in notes:
        print(n)

    if apply:
        existing = load_cards()
        merged, added, updated = merge_cards(existing, cards)
        seen = _load_seen()
        seen.update(seen_updates)
        content_changed = bool(added or updated)
        if content_changed:
            save_cards(merged)
        save_seen(seen)
        append_log({"cards": len(cards), "added": added, "updated": updated,
                    "seen_total": len(seen), "notes": notes})
        if content_changed:
            import safeio
            safeio.atomic_write_text(os.path.join(REPO, "scripts", "pr_body_regulations.md"),
                                     _pr_body(added, updated, cards))
        print("REGULATION_CONTENT_CHANGED=%s" % ("1" if content_changed else "0"))
        print("REGULATION: %d added, %d updated; %d docs tracked." % (added, updated, len(seen)))

    if as_json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))
    elif not apply:
        for c in cards:
            print("  %-8s %-12s %-16s %s" % (c["agency"], c["type"], c["cfr"] or "-",
                                             (c["title"] or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
