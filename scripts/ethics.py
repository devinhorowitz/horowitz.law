#!/usr/bin/env python3
"""Ethics watch: State Bar of Georgia Formal Advisory Opinions.

The fourth watch, and the one that covers a gap the other three cannot reach. The legislative
watch reads statutes, the regulatory watch agency rules, the court-rules watch the Federal Rules.
None of them sees a **Formal Advisory Opinion** -- the State Bar's Formal Advisory Opinion Board
issues these on the Georgia Rules of Professional Conduct, they are filed with the Supreme Court
of Georgia, and once the Court acts they bind every Georgia lawyer.

Why they need their own watch rather than a wider net on the opinions funnel:

  * They are NOT on CourtListener. Searched 2025-2026 for "Formal Advisory Opinion" across the
    Georgia court and then across every court: no Georgia FAO is indexed. The opinions funnel
    reads CourtListener, so it cannot see these no matter how its gates are tuned.
  * Even if it could, all three of its gates exclude "attorney discipline or bar admission"
    (update.py screen/pretriage/triage). That rule is right for a disbarment -- discipline of one
    named lawyer is not civil-litigation intelligence -- but an advisory opinion about a duty
    every litigator owes is a different animal, and gets caught by the same words.

FAO 24-1 is the case in point: hiring a third-party vendor to obtain medical records is, for
Rule 5.3 purposes, delegating to a nonlawyer assistant, so the lawyer must supervise it. Filed
with the Court 2025-09-05, acted on around 2026-08-12, and invisible to this repo the whole time.

Design follows courtrules.py, the closest sibling: page text (not a CSS scrape), a content hash so
an unchanged page costs one fetch, model extraction of the whole page, fail-open at every step,
and NO auto-publish -- every card is held for a human. Two deliberate differences:

  * IDENTITY IS THE OPINION NUMBER, canonicalized. Court rules taught this the expensive way: the
    id was a hash of the extractor's raw designation, the extractor relabelled the same rule
    between runs, and FRE 707 reached three cards on the public page. An FAO is cited half a dozen
    ways ("FAO 24-1", "Formal Advisory Opinion No. 24-1", "24-1"), so the number is parsed out and
    everything else discarded before hashing.
  * A CARD UPDATES. A court rule's status barely moves; an FAO's whole life is proposed -> filed
    with the Court -> approved, and the status IS the news. So a re-extraction of a known opinion
    is not skipped, it is merged, and the run reports a change only when a field actually differs.

Run locally (needs ANTHROPIC_API_KEY; sandbox egress may block gabar.org -> fail-open):
    ANTHROPIC_API_KEY=... python scripts/ethics.py            # prints extracted opinions
    python scripts/ethics.py --json                           # as JSON
    python scripts/ethics.py --apply                          # persist + write the PR body

Environment:
  ANTHROPIC_API_KEY   required for the extraction call
  ETHICS_URLS         comma list of "label|url" (or bare url) sources
  ETHICS_MODEL        extraction model (default claude-opus-5)
  ETHICS_BATCH        batch the extraction via the 50%-priced Batches API (default on);
                      ETHICS_BATCH_SEC bounds the in-run wait (default 1800)
  ETHICS_DEBUG        if 1, log each step
"""
import os
import sys
import re
import json
import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import courtrules   # noqa: E402  -- shared fetch/strip/hash primitives; see _http_get below

JSON_PATH  = os.path.join(REPO, "ethics.json")
STATE_PATH = os.path.join(REPO, "ethics_state.json")
LOG_PATH   = os.path.join(REPO, "ethics_log.jsonl")

MAX_TEXT = 60000

# Only the pending-opinions page has been OBSERVED to exist (it appeared in search results while
# tracing FAO 24-1). The index page is the obvious parent of that path but was never fetched from
# here, because this sandbox blocks egress to gabar.org. Both are configurable and each page fails
# open independently, so a wrong URL costs a logged note and nothing else -- but the first real run
# is where the index URL gets confirmed. Do not read its presence here as verification.
_DEFAULT_SOURCES = [
    ("Pending with the Supreme Court",
     "https://www.gabar.org/general-counsel/advisory-opinions/"
     "proposed-opinions-pending-with-the-supreme-court-of-georgia"),
    ("Formal advisory opinions", "https://www.gabar.org/general-counsel/advisory-opinions/"),
]


def _sources():
    raw = os.environ.get("ETHICS_URLS", "")
    if not raw.strip():
        return list(_DEFAULT_SOURCES)
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        label, url = item.split("|", 1) if "|" in item else (item, item)
        out.append((label.strip(), url.strip()))
    return out or list(_DEFAULT_SOURCES)


SOURCES = _sources()
MODEL = os.environ.get("ETHICS_MODEL", "claude-opus-5")
EXTRACT_MAX_TOKENS = int(os.environ.get("ETHICS_MAX_TOKENS", "8000"))
DEBUG = os.environ.get("ETHICS_DEBUG", "") == "1"
ETHICS_BATCH = os.environ.get("ETHICS_BATCH", "on").strip().lower() in ("1", "true", "yes", "on")
BATCH_SEC = int(os.environ.get("ETHICS_BATCH_SEC", "1800"))

STATUSES = ("proposed", "pending", "approved", "withdrawn")

# Weak markers the real advisory-opinion pages carry. Same job as courtrules' markers: tell "read
# the page, it names no opinions" (trustworthy empty -> record the hash) from "fetched a JS shell
# or a redesign" (do NOT record, or the shell hash sticks and the page is never re-read).
_PAGE_MARKERS = ("formal advisory opinion", "advisory opinion", "state bar of georgia",
                 "rules of professional conduct", "formal advisory opinion board")


def has_ethics_markers(text):
    low = (text or "").lower()
    return any(m in low for m in _PAGE_MARKERS)


def _dbg(msg):
    if DEBUG:
        print("  . " + msg)


# --------------------------------------------------------------------------- #
# Identity: the opinion NUMBER, canonicalized.                                 #
# --------------------------------------------------------------------------- #
# An FAO is cited many ways for the same document: "Formal Advisory Opinion No. 24-1", "FAO 24-1",
# "Advisory Opinion 24-1", "No. 24-1", "24-1". Court rules learned what happens when the raw string
# is hashed: the extractor relabels between runs and the same authority is carded again under a new
# id (FRE 707 reached three cards). So the number is PARSED OUT and everything else discarded.
_NUM_RE = re.compile(r"(\d{2})\s*[-‐-―]\s*(\d{1,3})")


def canonical_number(raw):
    """'Formal Advisory Opinion No. 24-1' / 'FAO 24-1' / '24 - 1' -> '24-1'. Returns '' when the
    text names no opinion number, which the caller treats as an unusable extraction."""
    m = _NUM_RE.search(str(raw or ""))
    return "%s-%s" % (m.group(1), int(m.group(2))) if m else ""


def card_id(number):
    """A stable id for an opinion number. The number is already unique and short, so it IS the id
    -- no hash, which keeps the anchor readable (#eth-24-1) and makes a collision impossible to
    introduce by relabelling."""
    return canonical_number(number)


# --------------------------------------------------------------------------- #
# Extraction.                                                                  #
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM = (
    "You extract State Bar of Georgia FORMAL ADVISORY OPINIONS from the text of a gabar.org web "
    "page, for a civil-litigation and insurance-defense audience.\n\n"
    "Report an opinion only when the page names it by NUMBER (for example 24-1, 05-13). For each "
    "one give: number (just the number, e.g. '24-1'); status, one of 'proposed' (the Board has "
    "published it for comment), 'pending' (filed with the Supreme Court of Georgia and awaiting "
    "action), 'approved' (the Court has approved it or declined review, so it binds), or "
    "'withdrawn'; subject, a short noun phrase naming what it is about; summary, one or two "
    "sentences on what it actually requires of a lawyer; rules, the Georgia Rules of Professional "
    "Conduct it turns on (e.g. ['5.3','1.1']) or an empty list; and impact, one sentence on what a "
    "litigator must DO differently.\n\n"
    "SCOPE. Keep an opinion that bears on litigation practice -- supervising nonlawyer assistants "
    "or vendors, discovery and records handling, communicating with represented or unrepresented "
    "people, experts and consultants, conflicts in insurance-defense representation, candor to a "
    "tribunal, fees in litigated matters, confidentiality of client information. Skip one that is "
    "purely about law-firm administration with no litigation bearing (trust-account mechanics, "
    "advertising, bar admission, office sharing) -- omit it rather than reporting it with a note.\n\n"
    "Ground every field in the page text. Do not invent a number, status, rule, or date, and do "
    "not infer a status the page does not state -- when the page names an opinion without saying "
    "where it stands, use 'proposed'. If the page names no opinion, return an empty list.\n\n"
    "Reply with ONLY a JSON object: {\"opinions\": [{\"number\":..., \"status\":..., "
    "\"subject\":..., \"summary\":..., \"rules\":[...], \"impact\":...}]}."
)


def _extract_body(text, model=None):
    return {"model": model or MODEL, "max_tokens": EXTRACT_MAX_TOKENS, "system": EXTRACT_SYSTEM,
            "messages": [{"role": "user", "content": "PAGE TEXT:\n" + (text or "")[:MAX_TEXT]}]}


def _extract_parse(v):
    ops = v.get("opinions")
    if not isinstance(ops, list):
        return []
    return [o for o in ops if isinstance(o, dict)]


def extract(text, ai, model=None, label="ethics"):
    """Extract opinions from one page's text. [] means 'read fine, none named'; None means a model
    or parse error, so the caller leaves the page un-hashed and retries next run."""
    if not (text or "").strip():
        return []
    try:
        v = ai(_extract_body(text, model), label)
    except Exception as e:
        _dbg("extract failed: %s" % e)
        return None
    return _extract_parse(v)


def _draft_extractions(pending, deadline=None):
    """Extract the CHANGED pages as ONE 50%-priced batch job. Returns {url: [opinions] | None},
    the same per-page space extract() produces, so run() does not branch."""
    import batch
    import update
    reqs, meta = [], {}
    for i, p in enumerate(pending):
        cid = "eth-%d" % i
        reqs.append(batch.from_body(cid, _extract_body(p["text"])))
        meta[cid] = p["url"]
    try:
        results = batch.run(reqs, deadline=deadline, label="ethics-extract")
    except (batch.BatchTimeout, batch.BatchError) as e:
        print("  ! ethics extract batch deferred (%s); %d page(s) retry next run"
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


def build_card(op, url, today=None):
    """Assemble one card from an extracted opinion. Returns None when the extraction names no
    usable opinion number, which is the one field the card cannot be built without."""
    number = canonical_number(op.get("number"))
    if not number:
        return None
    status = str(op.get("status") or "").strip().lower()
    status = status if status in STATUSES else "proposed"
    rules = op.get("rules")
    rules = [str(r).strip() for r in rules if str(r).strip()] if isinstance(rules, list) else []
    today = (today or datetime.date.today()).isoformat()
    return {
        "id": card_id(number),
        "number": number,
        "status": status,
        "subject": str(op.get("subject") or "").strip(),
        "summary": str(op.get("summary") or "").strip(),
        "rules": rules,
        "impact": str(op.get("impact") or "").strip(),
        "url": str(url or "").strip(),
        "first_seen": today,
    }


# --------------------------------------------------------------------------- #
# Orchestration.                                                               #
# --------------------------------------------------------------------------- #
def _default_ai(body, label="call"):
    import update
    return update.anthropic_json(body, label)


def run(fetch=None, ai=None, today=None, sources=None, batch_enabled=False):
    """Read each source page; on a CHANGED page, extract the opinions it names and card them.
    Returns (cards, notes, seen_updates). A page is hashed as seen only after a SUCCESSFUL
    extraction, so a transient error retries. Writes nothing itself. Fail-open.

    Unlike the court-rules watch, a card for an ALREADY-SEEN opinion is still produced: an FAO's
    status is the news, so the merge decides whether anything changed, not this loop."""
    ai = ai or _default_ai
    sources = sources or SOURCES
    notes = []
    seen = _load_seen()
    seen_pages = seen.get("pages") or {}
    seen_cards = seen.get("cards") or {}
    new_pages, new_cards, cards = {}, {}, []
    today_iso = (today or datetime.date.today()).isoformat()

    pending = []
    for label, url in sources:
        text = courtrules.fetch_text(url, fetch)
        if not text:
            notes.append("ETHICS: %s unreachable; will retry." % label)
            continue
        h = courtrules.page_hash(text)
        if seen_pages.get(url) == h:
            notes.append("ETHICS: %s unchanged." % label)
            new_pages[url] = h
            continue
        if not has_ethics_markers(text):
            notes.append("ETHICS: %s fetched but shows no advisory-opinion markers "
                         "(shell/redesign/moved?); not recording, will retry." % label)
            continue
        pending.append({"label": label, "url": url, "text": text, "h": h})

    if batch_enabled and pending:
        notes.append("ETHICS: batching %d page extraction(s) (ETHICS_BATCH)." % len(pending))
        extractions = _draft_extractions(pending, deadline=__import__("time").time() + BATCH_SEC)
    else:
        extractions = {p["url"]: extract(p["text"], ai) for p in pending}

    for p in pending:
        label, url, h = p["label"], p["url"], p["h"]
        ops = extractions.get(url)
        if ops is None:
            notes.append("ETHICS: %s extraction failed; will retry." % label)
            continue
        new_pages[url] = h
        here = 0
        for o in ops:
            card = build_card(o, url, today=today)
            if not card:
                continue
            cid = card["id"]
            if cid in new_cards:      # the same opinion listed twice on one page
                continue
            # first_seen belongs to the FIRST sighting, not this run's.
            card["first_seen"] = seen_cards.get(cid) or today_iso
            cards.append(card)
            new_cards[cid] = card["first_seen"]
            here += 1
        notes.append("ETHICS: %s changed; %d opinion(s) named." % (label, here))
    notes.append("ETHICS: drafted %d card(s)." % len(cards))
    return cards, notes, {"pages": new_pages, "cards": new_cards}


# --------------------------------------------------------------------------- #
# State + persistence.                                                         #
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
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            lines = []
        lines.append(json.dumps(rec, ensure_ascii=False))
        safeio.atomic_write_text(LOG_PATH, "\n".join(lines[-cap:]) + "\n")
    except Exception as e:
        _dbg("log append failed: %s" % e)


# Fields whose change is real news. `url` and `first_seen` are deliberately NOT here: a page can
# move without the opinion changing, and first_seen never should. Without this, every run that
# re-reads a changed index page would count every listed opinion as "updated" and open a PR that
# changes nothing -- the noise that makes a watch get ignored.
_MEANINGFUL = ("status", "subject", "summary", "rules", "impact")


def merge_cards(existing, new_cards):
    """Merge by opinion number, preserving first_seen. An FAO's status moves, so a known opinion
    UPDATES rather than being skipped -- but only counts as updated when a meaningful field
    actually differs. Returns (merged, added, updated, changes) where changes lists
    (number, field, before, after) for the PR body."""
    by_id = {c["id"]: c for c in existing if isinstance(c, dict) and c.get("id")}
    added = 0
    changes = []
    for c in new_cards:
        old = by_id.get(c["id"])
        if old is None:
            added += 1
            by_id[c["id"]] = c
            continue
        c = dict(c, first_seen=old.get("first_seen") or c.get("first_seen"))
        diff = [(f, old.get(f), c.get(f)) for f in _MEANINGFUL if old.get(f) != c.get(f)]
        if diff:
            changes += [(c["number"], f, b, a) for f, b, a in diff]
            by_id[c["id"]] = c
        # no diff: leave the stored card exactly as it is, so nothing is rewritten
    merged = sorted(by_id.values(), key=lambda c: _sort_key(c), reverse=True)
    return merged, added, len({n for n, _f, _b, _a in changes}), changes


def _sort_key(c):
    """Newest opinion number first: '24-1' sorts above '05-13'. Year then sequence, numerically,
    so 24-10 follows 24-9 rather than preceding it as a string compare would."""
    m = _NUM_RE.search(str(c.get("number") or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def merge_seen(base, updates):
    pages = dict(base.get("pages") or {})
    cards = dict(base.get("cards") or {})
    pages.update(updates.get("pages") or {})
    cards.update(updates.get("cards") or {})
    return {"pages": pages, "cards": cards}


def _pr_body(added, updated, cards, changes):
    lines = ["## Ethics Watch: Georgia Formal Advisory Opinions", "",
             "The watch read the State Bar's advisory-opinion pages. **Every card is held for your "
             "review** — read each against the source, edit `ethics.json` on this branch if needed, "
             "and merge to publish.", "",
             "| Opinion | Status | Rules | Subject |", "|---|---|---|---|"]
    for c in cards:
        lines.append("| %s | %s | %s | %s |" % (
            c.get("number") or "?", c.get("status") or "?",
            ", ".join(c.get("rules") or []) or "—", c.get("subject") or "—"))
    if changes:
        lines += ["", "### What changed", "",
                  "| Opinion | Field | Before | After |", "|---|---|---|---|"]
        for num, field, before, after in changes:
            lines.append("| %s | %s | %s | %s |" % (num, field, _cell(before), _cell(after)))
    lines += ["", "%d new, %d updated." % (added, updated), "",
              "_AI-extracted from gabar.org; the opinion text is the authority._"]
    return "\n".join(lines) + "\n"


def _cell(v):
    s = ", ".join(v) if isinstance(v, list) else str(v or "—")
    s = " ".join(s.split())
    return (s[:80] + "…") if len(s) > 80 else (s or "—")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    apply = "--apply" in argv
    cards, notes, seen_updates = run(batch_enabled=ETHICS_BATCH)
    for n in notes:
        print(n)

    if apply:
        merged, added, updated, changes = merge_cards(load_cards(), cards)
        seen = merge_seen(_load_seen(), seen_updates)
        content_changed = bool(added or updated)
        if content_changed:
            save_cards(merged)
        save_seen(seen)
        append_log({"cards": len(cards), "added": added, "updated": updated,
                    "pages": len(seen.get("pages") or {}), "notes": notes})
        if content_changed:
            import safeio
            safeio.atomic_write_text(os.path.join(REPO, "scripts", "pr_body_ethics.md"),
                                     _pr_body(added, updated, cards, changes))
        print("ETHICS_CONTENT_CHANGED=%s" % ("1" if content_changed else "0"))
        print("ETHICS: %d added, %d updated." % (added, updated))

    if as_json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))
    elif not apply:
        for c in cards:
            print("  %-6s %-9s %s" % (c["number"], c["status"], (c["subject"] or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
