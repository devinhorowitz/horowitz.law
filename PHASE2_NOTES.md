# Phase 2: new surfaces from existing data · 2026-06-11

Three surfaces, all deterministic projections of opinions.json, zero marginal
API cost. 48 files — but 32 of them are the generated permalink pages, and the
pipeline regenerates those forever after this commit.

## 1. /changes — the treatment ledger (+ /changes.xml)

The citator finally has a public face. Every card under an active flag renders
as a ledger row — date, label, case link, court, the note, the Shepard's
caveat — and the same ledger ships as an RSS feed, so a correction reaches a
subscriber's reader the day the sweep raises it. Doctrine preserved in the
page copy: the machine only raises to caution; negative and superseded are
human calls; a cleared flag drops off and the card stands again. Live today
with one entry: the Aspen/Landstar caution.

## 2. /o/<id> — a permanent page per decision (32 today, grows daily)

Every covered decision now has a standalone page: the same opinion card the
feed renders, its own OG tags, canonical URL, and Article JSON-LD whose
dateModified is the treatment date when flagged — the citator data flows into
the structured data. Three anti-drift splices: the CSP-pinned inline script,
the chrome styles, and the card CSS are all read verbatim from committed pages
at render time, so a generated page can never carry a stale copy of any of
them. Every card in the feed and archive gains a `[ permalink ]` link beside
`[ copy cite ]`. Strays are deleted on render; the sitemap gains all 32 URLs
between new marker comments.

## 3. /stats — coverage statistics

CSS bar charts (no JS, CSP-clean) rendered straight from the data: by year,
court, practice area, disposition bucket, and precedential status, headed by
the honesty line — "32 decisions covered · 2015-06-29 → 2026-06-10 · 1 under
an active flag" — and framed explicitly as coverage of this feed, not a
census of the courts.

## Wiring

- check_site now guards changes.html, stats.html, every o/*.html (CSP + token
  checks), and changes.xml well-formedness. **Negative-tested:** corrupted a
  permalink's token → check failed with the exact message → one render
  self-healed it → green.
- ci.yml: trigger paths + the idempotency diff now cover the new artifacts.
  Idempotency replicated CLEAN on the full new list.
- Colophon's under-the-hood section is now the feeds directory it promised:
  opinions.xml, changes.xml, /changes, /stats. The 404's `ls /` learned the
  two new paths. Both feed intros link the ledger.
- base.css token moved (op-permalink styles + print rule) — every page
  restamped automatically, which is why index/resume/subscribe ride along.

## Upload set (same paths)

scripts/render.py · scripts/check_site.py · .github/workflows/ci.yml ·
changes.html · stats.html · changes.xml · sitemap.xml · base.css ·
colophon.html · 404.html · opinions.html · archive.html · index.html ·
resume.html · subscribe.html · the entire o/ folder (32 files)

## After deploy

1. /changes — one Aspen row; /changes.xml validates in a reader.
2. /o/9391147 — the flagged permalink: banner, ledger link, JSON-LD.
3. Any card → [ permalink ] → standalone page; [ copy cite ] still works.
4. /stats — bars render; numbers match the archive.
5. CI green: invariants + idempotency both replicated locally first.
