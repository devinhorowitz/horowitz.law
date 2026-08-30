#!/usr/bin/env python3
"""mcp_feed.py -- build public/api/feed.json, the machine-readable change feed the MCP server reads.

The site already publishes for humans (opinions.xml, changes.xml, the email digest). This is the
same content addressed to a MODEL: a routine polls it, sees what moved since it last looked, and
digests the delta -- rather than a person reading a digest email and hand-feeding law into a chat.

Two ideas carry the whole design.

CHANGED, one timestamp per card. A canary has to answer "what moved since <date>", and two very
different things count as movement: a NEW case was carded, and an OLD case was flagged as treated.
The second matters more -- a colleague who relied on a case last month needs to hear that it has
been questioned -- and it is invisible to any feed keyed on publication date, because the card's
date never changes. So each entry carries `changed` = max(first_seen, treatment_date) and a `change`
kind naming which happened. A cursor compares one field and catches both.

HEALTH IS NOT CONTENT. This file is written when content is rendered; public/status.json is written
on every scan whether or not anything was found. Keeping them separate is what lets a caller tell
"Georgia was quiet" from "the funnel stalled" -- the two look identical in any feed that reports
only content. See functions/mcp/index.js, which refuses to report a clean bill of health without
reading both. That distinction is not hypothetical here: this repo has already seen an 82-hour
content gap sitting behind a perfectly healthy-looking scan.

Written by render.py alongside the other generated outputs, so it ships on the same deploy.
"""
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Feed schema version. Bump when an existing field changes meaning or disappears, never for an
# addition -- a consumer pinned to 1 must keep working when a field is added beside the ones it
# reads. The MCP server reports this so a routine can refuse a feed it does not understand.
SCHEMA = 1

# Card fields copied verbatim into the feed. Deliberately not "everything": `why` and `synopsis`
# are what a model needs to decide whether a change touches a skill it maintains, and the source
# urls are what it needs to verify before acting. Internal bookkeeping stays out.
CARD_FIELDS = ("cluster_id", "name", "court", "division", "date", "dockets", "disposition",
               "areas", "precedential", "url", "official_url", "synopsis", "why", "law_applied",
               "first_impression", "jurisdiction", "tort_reform", "additional_holdings",
               "editor_note", "first_seen")
TREATMENT_FIELDS = ("treatment", "treatment_date", "treatment_note", "treated_by")


def change_event(card):
    """The (timestamp, kind) a cursor compares against.

    `first_seen` is when the card entered the feed; `treatment_date` is when a published card was
    flagged as disturbed. Whichever is later is when this card last MOVED, which is the only
    question a delta query asks. Falls back to `date` because a hand-added card may predate the
    first_seen convention, and a card with no usable date sorts as unknown rather than as new --
    silently dating it today would make it appear in every caller's next poll, forever."""
    first = (card.get("first_seen") or card.get("date") or "").strip()
    treated = (card.get("treatment_date") or "").strip()
    if treated and treated > first:
        return treated, "treatment"
    return first, "new"


def card_entry(card):
    """One feed entry: the copied fields, plus `changed`/`change`."""
    out = {k: card[k] for k in CARD_FIELDS if k in card and card[k] not in (None, "", [])}
    for k in TREATMENT_FIELDS:
        if card.get(k) not in (None, "", []):
            out[k] = card[k]
    changed, kind = change_event(card)
    out["changed"] = changed
    out["change"] = kind
    return out


def watch_entry(item, kind):
    """A legislation or court-rule item, reduced to the same delta shape as a card."""
    out = dict(item)
    out.pop("change_hash", None)          # internal dedupe key, meaningless to a consumer
    changed = (item.get("first_seen") or item.get("status_date")
               or item.get("effective_date") or "").strip()
    out["changed"] = changed
    out["change"] = kind
    return out


def area_counts(entries):
    """Cards per practice area. Published so a caller can see the DENOMINATOR before relying on a
    thin one: `negsec` and `badfaith` hold 4 cards each, and a tool that answers a negligent-security
    question from 4 cards without saying so is misleading in the same way a confident drop reason
    was. The MCP returns this with every search."""
    counts = {}
    for e in entries:
        for a in e.get("areas") or []:
            counts[a] = counts.get(a, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def build(cards, legislation=(), courtrules=(), generated=""):
    """The whole feed document. Pure: no I/O, so the tests can drive it directly."""
    entries = [card_entry(c) for c in cards]
    entries.sort(key=lambda e: (e.get("changed") or "", str(e.get("cluster_id") or "")), reverse=True)
    watches = ([watch_entry(i, "legislation") for i in legislation or ()]
               + [watch_entry(i, "courtrule") for i in courtrules or ()])
    watches.sort(key=lambda e: (e.get("changed") or ""), reverse=True)
    return {
        "schema": SCHEMA,
        "generated": generated,
        "counts": {
            "cards": len(entries),
            "watches": len(watches),
            "by_area": area_counts(entries),
            "treated": sum(1 for e in entries if e.get("treatment")),
        },
        "cards": entries,
        "watches": watches,
    }


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def build_from_repo(generated=""):
    """Read the repo's own JSON and build the feed. Every input is optional -- a missing watch file
    yields an empty section rather than failing the render, because a feed that fails to write is a
    silent outage for every routine polling it."""
    cards = _load(os.path.join(REPO, "opinions.json"), [])
    if isinstance(cards, dict):
        cards = cards.get("items") or cards.get("cards") or []
    return build(cards,
                 _load(os.path.join(REPO, "legislation.json"), []),
                 _load(os.path.join(REPO, "courtrules.json"), []),
                 generated=generated)
