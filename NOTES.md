# Phase 4 — taxonomy and trust · 2026-06-11

The first phase that touches your API bill, and "touches" is generous: the
one-time backfill of all 32 cards runs Haiku over their own short digests —
roughly a cent, total. Ongoing cost is a few extra output tokens per future
card, riding the summarizer call that already happens. Nothing reader-facing
costs anyone anything, ever.

## What shipped

**1. Badges: first impression + tort reform.** The Tier-3 summarizer now
returns `first_impression` (true only when the opinion itself says so) and
`tort_reform` (true only when a holding turns on SB 68, SB 69, SB 426, or
HB 961 — defined by statute in the prompt, not vibes). Both render as
accent-colored chips beside the area tags, on the feed, the archive, and every
permalink, and both are q-searchable for free since the filter reads card text.
Stored only when true, so the JSON stays lean.

**2. `editor_note` — the human layer.** A new card field rendered as a
visually distinct block (accent rule, "editor's note —" label) under the AI
synopsis. It is human-only by construction: the pipeline, the backfill, and
every script refuse to write it. To use it: edit opinions.json in the GitHub
web editor, add `"editor_note": "..."` to a card, commit — the nightly render
bot carries it onto the page, or any open PR's render does.

**3. Erie refinement, live.** The summarizer also returns `law_applied` for
federal cards (federal / ga / fl / al), and render uses it three ways:
`"federal"` or absent → full court-level bindingness (`ga,fl,al`);
a state code → narrowed to the primary jurisdiction plus that state. Demoed:
GoAuto stamped `law_applied: ga` rendered `data-jurisdiction="ga"` while Aspen
at `federal` kept all three. The "· federal" dropdown label keeps carrying the
honesty for untagged cards.

**4. Tag backfill, PR-gated.** `scripts/tagfill.py` + the `tagfill` workflow:
Haiku reads each card's own digest (per the roadmap: synopses, not opinions)
and answers the three questions conservatively — what the digest doesn't show
is false, and it only ever ADDS, never removes a hand-set field. Dry-run
prints the per-card plan and writes nothing; apply re-renders and opens a PR
so you review all 32 decisions as one diff before anything goes live.

**5. Golden-set hardening (BACKLOG #1 and #2).** The set grew 9 → 14:
- **Three negative controls**, picked from the live court feeds and cached via
  the quota-free PDF path: *John Clark v. State* (criminal), *In re Estate of
  Kevin L. George* (probate), and *Parmer v. Niven* — a divorce-contempt
  appeal dismissed for lack of jurisdiction, out of scope twice over. A check
  run that KEEPS any of these is now a loud regression toward an unfiltered
  feed.
- **Two thin-area anchors** for the rarest areas: *GoAuto* (badfaith) and
  *Giles v. Greenhouse Apartments* (negsec). Their cached text needs one
  `golden-check` run in **build** mode (anonymous REST is auth-walled now —
  the workflow has your CourtListener token).

This was the stated prerequisite for ever exercising PIPELINE.md's
"dropping the gate" path; the gate itself stays exactly where it is.

**6. Golden-set self-nomination (your idea, the safe half).** When a future
run cards an opinion in an area the golden set covers thinly, the PR body now
carries a paste-ready golden entry: nomination is automatic, adoption stays
your paste + merge. Three gates keep it honest: it never proposes a card the
run itself flagged or the cross-check disputed, never proposes a duplicate,
and the set never adopts or swaps anything on its own — entries replay frozen
text, so old anchors stay valid forever and removal is how benchmarks drift.
Quiet today (every area now has two anchors); it will light up exactly when
needed — a thin new area, or the Florida era.

## Your three buttons, in order

1. Upload this set, CI goes green.
2. Actions → **golden-check** → run with mode **build** (fills the two
   anchors), then once with **check** (watch three DROPs and nine keeps).
3. Actions → **tagfill** → run once with apply **off** (read the per-card
   plan in the log), then with apply **on**, and review the PR it opens.

## Upload set (same paths)

scripts/update.py · scripts/render.py · scripts/tagfill.py ·
scripts/golden_set.json · .github/workflows/ci.yml ·
.github/workflows/tagfill.yml · ROADMAP.md · opinions.html · archive.html

(The 32 `o/` permalinks pick up the new stylesheet on the nightly render-sync
PR — no need to upload them; badges only appear there after the tagfill PR
merges anyway.)
