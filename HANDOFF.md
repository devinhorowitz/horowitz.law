# HANDOFF.md

Updated 2026-06-21. The session-to-session working reference: where the site stands, what is
open, and the external loose ends. The durable how-to lives in the docs below; shipped history
lives in ROADMAP.md.

To brief a new session: "Fetch and read the codeload tarball of main,
<https://codeload.github.com/devinhorowitz/horowitz.law/tar.gz/refs/heads/main>, read AGENTS.md,
then <task>." Use codeload, not raw.githubusercontent, which lags by minutes.

## The docs

Read these before changing anything:

- AGENTS.md: how to operate the repo, verifying state, deploying, validating, the session
  protocol, and the landmines.
- MAINTENANCE.md: what to edit and what is generated, the workflow catalog, and the common tasks.
- PIPELINE.md: how the opinions pipeline works and how to tune it, and the opinions.json card
  schema.
- README.md: what the site is.
- ROADMAP.md: what has shipped and what is planned.

This file carries the current working state; those carry the durable how-to.

## Where the site stands (verified 2026-06-21)

70 cards across the four monitored courts (Court of Appeals of Georgia, Supreme Court of Georgia,
Eleventh Circuit, SCOTUS). The Florida and Alabama supplementary feeds are wired but not yet
carding. CI is green and the tree is render-idempotent. The documentation set above is complete
and cross-linked.

Official court links ship for every Eleventh Circuit and SCOTUS card and for the post-2017
Supreme Court of Georgia cards: the case name links to the court's own PDF, with CourtListener
kept below as the full record. The Court of Appeals stays CourtListener-only behind its AWS WAF.
The PWA is live: manifest, service worker, and the install affordance on /opinions.

## Operating doctrine

The general doctrine now lives in AGENTS.md (state verification, the render-sync deploy flow, the
validation bar, the temperature gotcha, the CourtListener constraints) and MAINTENANCE.md (the
source-to-generated map, the CSP-hash invariant, the Cloudflare settings). A few rules specific to
the editorial content, not repeated there:

- Treatment is human-gated. The machine may only set "caution"; "negative" and "superseded"
  require a human Shepard's read. editor_note is human-only everywhere. Assistant drafts are
  ratified by upload, never self-applied.
- Golden set: append-mostly; anchors must be unambiguous keepers; fix-on-arrival applies only to
  never-validated entries.
- The funnel's feed window anchors to the last state date minus two days, so a late-ingested
  cluster (SCOTUS slip opinions especially) can pass unseen. The remedy is a backfill seed
  (`OPINIONS_SEED`, for example `10858760:scotus`, dry_run first).

## Open work

Pipeline and correctness:

1. Docket-aware duplicate guard. The funnel dedups on `cluster_id` alone, so it cards both halves
   when CourtListener issues twin clusters for one consolidated appeal (the 10873764/10873765 pair
   is the fixture) or republishes a revised opinion under a new cluster (a SCOTUS revision was
   caught and removed by hand this session). Guard at the queue or update stage: same court, same
   date, identical docket list or party-token set means one case; keep the consolidated or
   authoritative cluster.
2. Completeness check on the back catalog. `completeness_check` runs only on new cards in the
   funnel; the older cards predate it. `maintain.py` already re-validates a rotating slice with
   `crosscheck`; add `completeness_check` to that slice and surface its flags the same way.
3. Failure-alert parity. The find-or-create issue-on-failure step exists on the funnel and
   maintenance workflows but not on every scheduled one (render-sync among them). Audit
   `.github/workflows` for the missing `if: failure()` step, or factor it into a reusable step.
4. Periodic log digest. `opinions_pipeline_log.jsonl` accumulates one record per run but nothing
   summarizes it. A small reader plus a weekly job could post rolling stats to the run summary or
   a digest issue.
5. Late-ingestion recall. CourtListener can publish a cluster weeks after its dateFiled and the
   since-window then skips it. The feeds are now wired; the remaining piece is a detector for feed
   items whose dateFiled sits well behind the window, routing them to the seed.
6. backfill.py assembly. Its summarizer prompt and assembly are frozen pre-Phase 4, so seeded
   cards can arrive without `first_impression`, `tort_reform`, and `law_applied`. Sync it with
   update.py, or have backfill import update's summarize and assembly directly.
7. treated_by hyperlinks. The permalink treatment block renders `treated_by` names as plain text;
   when the citing cluster is itself carded, render the name as a link to its permalink. About
   five lines in render.py.

Feature candidates:

- Professional-image set: a professional-responsibility footer line on /opinions, /archive, and
  the permalinks (index and subscribe already carry it); a curator byline on the Watch intro
  linking the resume; a first-impressions quick-filter chip.
- Annotated landmarks: editor notes on the flagship cards (Phillips, Quynn, Martin, and Toyo are
  carded and noteless; Montgomery is the prime candidate).
- Florida in full: the composite `RESEND_TOPIC_MAP` keys, the HMAC v2 confirm token with a grace
  window, per-state digests by membership, and the overlay-to-full mode flip that springs the
  dormant subscribe checkboxes.
- Web Push for the installed app: a VAPID keypair, a subscribe endpoint (Pages Function plus KV),
  and a sender step on the alert workflow.

## External, non-repo

- Cloudflare: a zone-wide `Access-Control-Allow-Origin: *` header is injected and the font
  Cache-Control is duplicated. The origin (_headers and the middleware) is exonerated, and the
  pages.dev differential localized it to the horowitz.law zone layer. Hunt order: Rules >
  Overview; Zero Trust > Access > Applications (the CORS panel); Rules > Snippets; the zone's
  Workers Routes; legacy Page Rules. Enumerate first, change nothing.
- Ko-fi: the one-line colophon sponsor swap, once the URL exists.
- devin@horowitz.law via Cloudflare Email Routing: still an open question.
- LinkedIn cadence from the Monday digest: a habit, not code; permalinks already unfurl with
  proper cards.
