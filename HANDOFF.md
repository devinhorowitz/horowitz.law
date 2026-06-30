# HANDOFF.md

Updated 2026-06-28. The session-to-session working reference: where the site stands, what is
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

## Where the site stands (verified 2026-06-28)

80 cards. The feed polls all eight registry courts; five have carded so far: Court of Appeals of
Georgia, Supreme Court of Georgia, Eleventh Circuit, SCOTUS, and the Florida District Court of Appeal
(one card). The Alabama supplementary feed is wired but has not carded yet. CI is green and the tree
is render-idempotent. The documentation set above is complete and cross-linked.

The two per-card model guards are hardened against a false flag: `crosscheck` (fidelity) must quote
the verbatim card span it faults, `completeness_check` (omission) the verbatim opinion span it says
was omitted, and each re-asks on a flag so a single noisy roll does not stand
(`OPINIONS_CROSSCHECK_TRIES`, `OPINIONS_COMPLETENESS_TRIES`, both default 3). `scripts/test_update.py`
is the repo's first unit test and pins both; the CI smoke job runs it. This followed the 2026-06-28
maintenance false positive (issue #94, a cross-check flag whose premise the model invented, now
closed). Maintenance now re-validates its rotating slice with both guards on one fetched text, and
the funnel carries a docket-aware duplicate guard so a CourtListener twin or corrected republish
does not double-card; `scripts/test_maintain.py` joins `test_update.py` in the CI smoke job.

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

1. Docket-aware duplicate guard (done). The funnel deduped on `cluster_id` alone, so it carded
   both halves when CourtListener issued twin clusters for one consolidated appeal, or republished
   a corrected opinion under a new cluster. The candidate loop now also skips a new cluster that is
   the same case as one already carded or added this run, keyed on the same court and either a
   shared docket (unique within a court, so it catches a revision refiled on a later date) or, the
   same day, two or more distinctive party tokens; the skip is marked seen and surfaced in the PR
   for the editor to reconcile. A repeat appearance at a higher court is untouched, since the court
   differs. `scripts/test_update.py` pins it (six dedup cases).
2. Completeness check on the back catalog (done). `maintain.py` re-validates a rotating slice; it
   now runs the hardened `completeness_check` alongside `crosscheck` on the same fetched opinion
   text (so no extra CourtListener calls), and surfaces both, each flag labeled by the guard that
   raised it (`fidelity:` or `completeness:`). The slice runs if either guard is enabled, and a
   completeness flag exits the job nonzero the same way a fidelity flag does. `scripts/test_maintain.py`
   (new, run in the CI smoke job) pins the wiring (six cases).
3. Failure-alert parity (resolved). Every scheduled workflow now surfaces a failure: render-sync,
   digest, treatment, lighthouse, and the funnel and maintenance all open or update a tracking issue
   on failure, and `links.yml` reports broken links through its own `create-issue-from-file` step
   (with `fail: false` by design, so transient external blips do not red-X the run).
4. Periodic log digest. `opinions_pipeline_log.jsonl` accumulates one record per run but nothing
   summarizes it. A small reader plus a weekly job could post rolling stats to the run summary or
   a digest issue.
5. Late-ingestion recall. CourtListener can publish a cluster weeks after its dateFiled and the
   since-window then skips it. The feeds are now wired; the remaining piece is a detector for feed
   items whose dateFiled sits well behind the window, routing them to the seed.
6. backfill.py assembly (done). It carded through a frozen pre-Phase-4 summarizer, so a seeded
   card could arrive without `first_impression`, `tort_reform`, and `law_applied`. `backfill.py`
   now imports `update` and routes both the single-case path and the window path through
   `update.summarize` and `update.assemble_entry`, so a seeded card carries the same taxonomy as a
   daily-feed card. CI's import smoke exercises `backfill` alongside the rest.
7. treated_by hyperlinks (done). The treatment block rendered `treated_by` names as plain text;
   `render._cited_by_html` now links any citer that is itself carded to its permalink (`/o/<id>`)
   and leaves the rest plain, so a link never 404s. It runs in the card banner (recent feed,
   archive, and permalink) and the `/changes` ledger; the changes RSS stays plain text by design.
   The one currently flagged card, Aspen American v. Landstar Ranger, links Montgomery and Hodge.

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
