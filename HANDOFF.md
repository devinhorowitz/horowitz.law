# HANDOFF.md

Updated 2026-06-12. This file replaced BACKLOG.md as the single
session-to-session reference: outstanding work, carried backlog, operating
doctrine, and external loose ends. Shipped history lives in ROADMAP.md.

To brief a new assistant session: "Fetch and read
https://raw.githubusercontent.com/devinhorowitz/horowitz.law/main/HANDOFF.md
then <task>." The sandbox network allowlist already includes
raw.githubusercontent.com.

## State at handoff (verified 2026-06-12, main 20db712)

37 cards including the first Supreme Court entry (Montgomery v. Caribe
Transport II, cluster 10858760). Aspen (9391147) carries treatment
"negative" dated 2026-06-12 with a human Shepard's note; its treated_by
records Montgomery and Hodge. The PWA is fully live (manifest, sw.js,
registration, app meta on every page) and the "[ install the app ]" row is
serving on /opinions. CI is green and the tree is render-idempotent.

Assistant session ritual: re-sync the working copy from a fresh codeload
tarball of main, replicate CI locally (py_compile, the 16 imports, node
--check on app.js, opinions.js, subscribe.js, and sw.js, workflow YAML
parses, check_site.py, render idempotency via a temp commit and a
double-render diff) before any delivery, and verify against the live site
with curls afterward.

## Operating doctrine (hard-won, do not relearn)

- Sources only in uploads; derived pages belong to the render-sync bot. A
  json, shell, or script change goes up branch-and-PR, that PR's CI is
  EXPECTED to be red on render idempotency, merge it, run Actions ->
  render-sync, merge the bot PR, green.
- Upload technique: drag folders themselves plus loose files in one drag;
  per-file picking strands files. Never upload UPLOAD-NOTES or any
  instruction sheet; they are read-only bundle docs.
- Treatment doctrine: the machine may only set "caution". "negative" and
  "superseded" are human-only on a Shepard's read. editor_note is
  human-only everywhere; assistant drafts are ratified by upload.
- Golden set: append-mostly; anchors must be unambiguous keepers;
  fix-on-arrival applies only to never-validated entries.
- CourtListener from the sandbox: anonymous REST returns 401 and opinion
  HTML returns a 202 bot challenge. Quota-free paths are the court Atom
  feeds and storage.courtlistener.com PDFs. The funnel's feed window is
  anchored to the last state date minus two days, so late-ingested
  clusters (SCOTUS especially) can pass unseen; backfill seed
  (OPINIONS_SEED, for example "10858760:scotus", dry_run first) is the
  remedy.
- Outputs mount flakes on directory operations: stage everything in
  /home/claude, copy once, and use a fresh directory name on any retry.
- ** It runs at temperature 1 (see below), so area tags vary run to run. The summarize guard absorbs this with retries (`OPINIONS_GOLDEN_RETRIES`, default 3). If a genuinely flaky area still misses every attempt now and then, raise that value rather than editing the golden expectations to match a single noisy run. - **Temperature gotcha: do not set a global temperature 0.** `claude-opus-4-8`, the summarizer, rejects any temperature other than 1 with an HTTP 400, which the error handler can mislabel as a retired-model problem. A global temperature 0 once broke every summarize call, the funnel included. There is intentionally no global temperature override. If you want determinism on a tier, set it for that model only and confirm the model accepts the value first.

## Georgia courts' own publishing: probe done, official links shipped

Probe outcome (allowlist carries gaappeals.gov, www.gaappeals.gov,
gasupreme.us, www.gasupreme.us):

- Supreme Court (www.gasupreme.us): clean, no WAF, HTTP 200. Year-index page
  per year at /<year>-opinions/, structured `<h3>Month</h3>`,
  `<p><strong>Date</strong></p>`, `<ul><li><a href="PDF">DOCKET. NAME</a></li>`.
  PDF URL is /wp-content/uploads/<year>/<month>/<docket-lowercased>.pdf, a
  WordPress media path stable across the site's redesigns. Filename is the
  lowercased docket, verified across a release. pdf_text plus _pdf_ok pass on a
  sample (s26a0017.pdf, about 20,800 chars). Apex gasupreme.us 503s, use www.
  Year pages exist back to 2017; /2015- and /2016-opinions/ 404 and the older
  PDFs are not on the current uploads tree, so pre-2017 cards get no official
  link.
- Court of Appeals (gaappeals.gov): blocked. The live docket data endpoint is
  /wp-content/themes/benjamin/docket/results_all.php (the recorded path's extra
  docketdate/ segment was stale), behind a persistent AWS WAF JS challenge
  (window.gokuProps), HTTP 405 on GET and POST, all date formats and retries.
  The human /docket-search/ form page loads, but the data endpoint a headless
  funnel needs does not. Not usable, and the date-format question (6-12 vs 12-6)
  stays undetermined as a result. CourtListener remains the source for the
  Court of Appeals.

Shipped this session: official_url enrichment, Supreme Court of Georgia only. A
scotga card's case name links to the court's own opinion PDF, and the
CourtListener link below is relabeled "Full record on CourtListener" (CL carries
citations, treatment, and docket data the court's own listing omits, so it
stays). A card without official_url is unchanged: plain name, CourtListener as
the read link. Identity still keys on the CourtListener cluster_id.

- Mechanism: scripts/official_ga.py. official_url_for(card) fetches the
  /<year>-opinions/ page for the card's decision year and matches a docket to a
  PDF basename, fail-open (any miss or error returns None). Reused two ways: a
  backfill CLI (`python scripts/official_ga.py [--apply]`) that filled the
  existing cards, and a forward hook in update.py that populates new scotga cards
  at assembly time (also fail-open; if the year page lags CL, a later backfill
  fills it).
- Coverage now: 7 of 10 scotga cards carry official_url (SMG, Walmart, Miller,
  Adventure Motorsports, Quynn, Cooper Tire, Martin). Phillips, Toyo, and Scapa
  are pre-2017 with no PDF on the current site, so they keep CourtListener-only
  rendering.
- Ground-truth join re-anchored from the Waddle Trucking card (ctapp, behind the
  WAF) to the current civil Supreme Court opinion S26A0807, Sockwell Corners v.
  Newton County, which official_ga resolves cleanly.
- Data fix in passing: the Martin v. Six Flags card (cluster 5749712) had an
  empty docket; set to S16G0743 and S16G0750 (the consolidated cross-appeals,
  confirmed against the court's 2017 listing), which also let the docket-based
  resolver find its PDF. Worth a sanity-check on review.

Still locked, still pending: the cluster_id stays the identity spine, so the
Georgia sites supplement rather than replace. The early-alert poller (notice
court-posted opinions absent from the CL feed and route them to the seed list
when the cluster appears, closing the late-ingestion hole, backlog item 6) is
viable for the Supreme Court only given the Court of Appeals WAF, and is not yet
built.

## Open projects

1. Professional-image set, one render bundle on request: a
   professional-responsibility footer line (not legal advice, no
   attorney-client relationship) on /opinions, /archive, and the
   permalinks (the audit confirmed index and subscribe have it and the
   legal-content pages do not); a curator byline on the Watch intro
   linking the resume; a "[ first impressions ]" quick-filter chip
   (?q=first impression).
2. Annotated landmarks, the editor's pen: Phillips, Quynn, Martin, and
   Toyo are carded and noteless; Montgomery is the flagship candidate.
3. Phase 5, Florida in full (design locked in earlier sessions): composite
   RESEND_TOPIC_MAP keys ("fl", "fl:premises"), HMAC v2 confirm token
   email.ts.a:<csv>.j:<csv> with a 7-day grace window, per-state digests
   by membership, and the overlay-to-full mode flip springs the dormant
   subscribe state checkboxes.
4. Web Push for the installed app: VAPID keypair, a subscribe endpoint
   (Pages Function plus KV), and a sender step on the alert workflow. iOS
   supports push for installed PWAs.

## Backlog (carried from BACKLOG.md and renumbered)

### 1. Cross-check and completeness run at the default temperature

`crosscheck` and `completeness_check` in `scripts/update.py` use the triage model at the
model default temperature, so a flag on a real carding PR can vary run to run. They are
advisory (they ride the PR for the editor and never drop a card), so this is tolerable.
If they ever feel noisy:

- Apply the same retry idea the summarize guard uses (re-run, treat only a persistent flag
  as real), or
- Lower their temperature, but only per model and only after confirming that model accepts
  the value. See the temperature gotcha below.

### 2. Completeness check on published cards

`completeness_check` runs only on new cards in the funnel. The existing cards predate it and
are never re-checked. `scripts/maintain.py` already re-validates a rotating slice of published
cards with `crosscheck`; add a `completeness_check` call to that slice to cover the back
catalog over time and surface its flags the same way.

### 3. Failure-alert parity across workflows

The find-or-create issue alert on failure exists on the funnel and maintenance workflows.
Several scheduled workflows fail without one. Audit `.github/workflows/` for which lack the
`if: failure()` issue step (`render-sync` is one) and add it, or factor it into a reusable
step, so a broken scheduled job is noticed.

### 4. Periodic log digest

`opinions_pipeline_log.jsonl` accumulates one record per funnel run (cards, drop reasons,
CourtListener calls, cross-check and completeness flag counts) but nothing summarizes it.
A small reader plus a weekly scheduled job could post rolling stats to the run summary or a
digest issue, turning the log into something glanceable.

### 5. Duplicate-cluster guard in the funnel

CourtListener issued twin clusters for one consolidated Eleventh Circuit
appeal (10873764 and 10873765, identical docket lists) and the funnel
carded both; the existing dedup keys on cluster id, the one thing that
differed. Guard at the queue or update stage: same court, same dateFiled,
identical docket list or identical party-token set means one case; keep
the consolidated-titled cluster. The 2026-06-11 pair is the test fixture.
(The apparent Hodge twin that day was a false alarm: "Willy Hodge" was the
treated_by styling on Aspen, not a second card.)

### 6. Late-ingestion recall

CourtListener can publish a cluster weeks after its dateFiled, SCOTUS slip
opinions especially; the since-window then skips them unseen (Montgomery
left zero trace in state or rejections). Remedy exists (backfill seed);
the durable fix is the Georgia-direct early-alert poller above, plus
detection for feed items whose dateFiled sits well behind the window.

### 7. backfill.py predates Phase 4

Its summarizer prompt and assembly are frozen pre-Phase 4, so seeded cards
arrive without first_impression, tort_reform, and law_applied (Montgomery
did). Sync them with update.py, or refactor backfill to import update's
summarize and assembly directly.

### 8. treated_by hyperlinks

The permalink treatment block renders treated_by names as plain text. When
the citing cluster is itself carded (Aspen to /o/10858760), render the
name as a link. About five lines in render.py.

## Housekeeping (one sitting)

- Root litter delete, eight files in the same PR that adds this one:
  COMMIT_NOTES.md, INTEGRATION.md, NOTES.md, PHASE1_NOTES.md,
  PHASE2_NOTES.md, PHASE3_NOTES.md, UPLOAD-NOTES.md, and BACKLOG.md.
- Branch sweep: bot/opinions-backfill, bot/opinions-render,
  bot/opinions-treatment, bot/opinions-update, tagfill, and
  devinhorowitz-patch-1 through -4, plus any newer patch branches.
- Issues #18 and #19: close if still open.

## External, non-repo

- Cloudflare: an Access-Control-Allow-Origin: * header is injected
  zone-wide and the font Cache-Control value is duplicated; the origin
  (_headers and the middleware) is exonerated and the pages.dev
  differential localized it to the horowitz.law zone layer. Hunt order:
  Rules -> Overview; Zero Trust -> Access -> Applications (the CORS
  panel); Rules -> Snippets; the zone's Workers Routes; legacy Page
  Rules. Paste for Cloudflare's AI:

  "For the zone horowitz.law: list every entry in this zone's
  http_response_headers_transform ruleset, every Snippet, every Workers
  route bound to this zone, every legacy Page Rule, and every Zero Trust
  Access application whose domain matches horowitz.law (including its
  CORS settings). For each, show the match expression and the actions or
  headers it sets. I am hunting whatever adds Access-Control-Allow-Origin:
  * to all responses and appends a duplicate Cache-Control on font files.
  Enumerate first, change nothing."

- Ko-fi: one-line colophon sponsor swap when the URL exists.
- devin@horowitz.law via Cloudflare Email Routing, still an open question.
- LinkedIn cadence from the Monday digest: a habit, not code; permalinks
  already unfurl with proper cards.
